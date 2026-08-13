"""LLM 기반 공시 → 관계 트리플 추출.

실행: uv run python -m kograph.graph.extract [--limit N] [--model MODEL]

- 대상: doc_text가 있고 아직 kg_triple이 없는 공시 중, 관계가 명확한 보고서 유형
- 출력: kg_triple 스테이징 테이블 (Neo4j 적재 전 원본 보존·리니지)
- 기본 모델: claude-haiku-4-5 — 배치 추출 비용 최적화(태스크별 모델 라우팅).
  --model claude-opus-5 등으로 바꿔 품질·비용 비교 가능 (기술노트 002 대상)
"""

import argparse
import json
import logging
import time
from enum import StrEnum

import anthropic
from pydantic import BaseModel, Field

from kograph.config import get_settings
from kograph.db.oracle import connect

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-haiku-4-5"
MAX_DOC_CHARS = 8_000  # 주요사항보고는 대부분 이 안에 핵심이 있음
COMMIT_EVERY = 20

# 관계 추출 가치가 높은 보고서명 키워드
TARGET_REPORT_KEYWORDS = [
    "공급계약", "타법인주식", "출자", "채무보증", "합병", "분할",
    "영업양수", "영업양도", "지분", "주식변동", "유상증자", "신규시설투자",
]


class Predicate(StrEnum):
    SUPPLIES_TO = "SUPPLIES_TO"            # 공급계약: subject가 object에 공급
    OWNS_STAKE = "OWNS_STAKE"              # 지분 보유
    INVESTS_IN = "INVESTS_IN"              # 출자·신규투자
    GUARANTEES_DEBT_OF = "GUARANTEES_DEBT_OF"  # 채무보증
    ACQUIRES = "ACQUIRES"                  # 인수·합병·영업양수
    SUBSIDIARY_OF = "SUBSIDIARY_OF"        # 종속회사 관계
    PARTNERS_WITH = "PARTNERS_WITH"        # 합작·업무제휴
    OFFICER_OF = "OFFICER_OF"              # 임원 겸직


class Triple(BaseModel):
    subject: str = Field(description="관계의 주체 기업/인물 정식 명칭")
    predicate: Predicate
    object: str = Field(description="관계의 대상 기업/인물 정식 명칭")
    amount_krw: int | None = Field(None, description="계약·출자·보증 금액(원), 명시된 경우만")
    ratio_pct: float | None = Field(None, description="지분율·자기자본대비 비율(%), 명시된 경우만")
    start_date: str | None = Field(None, description="계약/효력 시작일 YYYY-MM-DD")
    end_date: str | None = Field(None, description="계약/효력 종료일 YYYY-MM-DD")
    note: str | None = Field(None, description="관계 파악에 중요한 부가 정보 한 줄")


class ExtractionResult(BaseModel):
    triples: list[Triple] = Field(description="공시에서 확인된 기업 간 관계. 없으면 빈 배열")


_SYSTEM = """너는 한국 금융감독원 DART 공시에서 기업 간 관계를 추출하는 분석가다.

규칙:
- 공시 본문에 명시적으로 드러난 관계만 추출한다. 추측하지 않는다.
- subject/object는 본문에 나온 정식 회사명을 쓴다 (약칭·영문 병기 제거).
- 공시 제출사가 관계의 주체인 경우가 대부분이다.
- 금액은 원 단위 정수로. "50,988백만원" → 50988000000.
- 확실하지 않은 필드는 null로 둔다. 관계가 없으면 빈 배열을 반환한다."""


def _user_prompt(corp_name: str, report_nm: str, doc_text: str) -> str:
    return (
        f"공시 제출사: {corp_name}\n"
        f"보고서명: {report_nm}\n"
        f"--- 공시 본문 ---\n{doc_text[:MAX_DOC_CHARS]}"
    )


def extract_triples(
    client: anthropic.Anthropic, model: str, corp_name: str, report_nm: str, doc_text: str
) -> list[Triple]:
    """단일 공시에서 관계 트리플 추출. 구조화 출력으로 파싱 실패 없음."""
    response = client.messages.parse(
        model=model,
        max_tokens=2048,
        system=[{"type": "text", "text": _SYSTEM, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": _user_prompt(corp_name, report_nm, doc_text)}],
        output_format=ExtractionResult,
    )
    result = response.parsed_output
    return result.triples if result else []


def pending_filings(limit: int | None) -> list[tuple]:
    """추출 대상: 본문 있음 + 트리플 미생성 + 대상 보고서 유형."""
    kw_cond = " OR ".join(f"report_nm LIKE '%{kw}%'" for kw in TARGET_REPORT_KEYWORDS)
    sql = f"""
        SELECT f.rcept_no, f.corp_name, f.report_nm, f.doc_text
        FROM filing f
        WHERE f.doc_text IS NOT NULL
          AND ({kw_cond})
          AND NOT EXISTS (SELECT 1 FROM kg_triple t WHERE t.rcept_no = f.rcept_no)
        ORDER BY f.rcept_dt
    """
    if limit:
        sql += f" FETCH FIRST {int(limit)} ROWS ONLY"
    with connect() as conn, conn.cursor() as cur:
        cur.execute(sql)
        rows = []
        for rcept_no, corp_name, report_nm, doc_text in cur.fetchall():
            text = doc_text.read() if hasattr(doc_text, "read") else doc_text
            rows.append((rcept_no, corp_name, report_nm, text))
        return rows


def store_triples(rcept_no: str, model: str, triples: list[Triple], cur) -> int:
    for t in triples:
        props = t.model_dump(exclude={"subject", "predicate", "object"}, exclude_none=True)
        cur.execute(
            """INSERT INTO kg_triple
               (rcept_no, subject_name, predicate, object_name, props_json, model)
               VALUES (:1, :2, :3, :4, :5, :6)""",
            [rcept_no, t.subject[:200], t.predicate.value, t.object[:200],
             json.dumps(props, ensure_ascii=False), model],
        )
    return len(triples)


def run(limit: int | None, model: str) -> tuple[int, int]:
    """Returns (처리 공시 수, 추출 트리플 수)."""
    settings = get_settings()
    if not settings.anthropic_api_key or settings.anthropic_api_key.startswith("your-"):
        raise ValueError("ANTHROPIC_API_KEY not configured in .env")

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    todo = pending_filings(limit)
    logger.info("extraction targets: %d filings (model=%s)", len(todo), model)

    done = total_triples = 0
    started = time.monotonic()
    with connect() as conn, conn.cursor() as cur:
        for rcept_no, corp_name, report_nm, doc_text in todo:
            try:
                triples = extract_triples(client, model, corp_name, report_nm, doc_text)
                total_triples += store_triples(rcept_no, model, triples, cur)
                done += 1
            except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as exc:
                conn.commit()
                raise RuntimeError(f"aborting: credential problem — {exc}") from exc
            except anthropic.BadRequestError as exc:
                # 크레딧 부족은 400으로 오고 재시도해도 풀리지 않는다 -> 즉시 중단.
                # 그 외 400(과대 입력 등)은 해당 공시만 건너뛴다.
                if "credit balance" in str(exc).lower():
                    conn.commit()
                    raise RuntimeError(
                        "aborting: Anthropic credit balance exhausted — "
                        "top up at console.anthropic.com > Plans & Billing"
                    ) from exc
                logger.error("skipped %s: %s", rcept_no, exc)
            except anthropic.APIStatusError as exc:
                # 개별 공시 실패는 격리 — 다음 실행에서 재시도됨 (kg_triple 미생성 상태 유지)
                logger.error("extract failed %s: %s", rcept_no, exc)
            if done % COMMIT_EVERY == 0:
                conn.commit()
                logger.info("progress %d/%d, triples=%d, %.0fs",
                            done, len(todo), total_triples, time.monotonic() - started)
        conn.commit()

    logger.info("done: filings=%d triples=%d elapsed=%.0fs",
                done, total_triples, time.monotonic() - started)
    return done, total_triples


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    args = parser.parse_args()
    run(args.limit, args.model)
