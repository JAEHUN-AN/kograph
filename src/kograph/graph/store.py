"""kg_triple 스테이징 read/write — 규칙 파서와 LLM 추출기가 공유."""

import json
import logging

from kograph.db.oracle import connect
from kograph.graph.models import Triple

logger = logging.getLogger(__name__)

# 관계 추출 가치가 높은 보고서명 키워드
TARGET_REPORT_KEYWORDS = [
    "공급계약", "타법인주식", "출자", "채무보증", "합병", "분할",
    "영업양수", "영업양도", "지분", "주식변동", "유상증자", "신규시설투자",
]


def pending_filings(limit: int | None = None, report_like: str | None = None) -> list[tuple]:
    """추출 대상: 본문 있음 + 트리플 미생성 + 대상 보고서 유형.

    Returns list of (rcept_no, corp_name, report_nm, doc_text).
    """
    kw_cond = " OR ".join(f"report_nm LIKE '%{kw}%'" for kw in TARGET_REPORT_KEYWORDS)
    sql = f"""
        SELECT f.rcept_no, f.corp_name, f.report_nm, f.doc_text
        FROM filing f
        WHERE f.doc_text IS NOT NULL
          AND ({kw_cond})
          AND NOT EXISTS (SELECT 1 FROM kg_triple t WHERE t.rcept_no = f.rcept_no)
    """
    params: dict[str, str] = {}
    if report_like:
        sql += " AND f.report_nm LIKE :report_like"
        params["report_like"] = report_like
    sql += " ORDER BY f.rcept_dt"
    if limit:
        sql += f" FETCH FIRST {int(limit)} ROWS ONLY"

    with connect() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        rows = []
        for rcept_no, corp_name, report_nm, doc_text in cur.fetchall():
            text = doc_text.read() if hasattr(doc_text, "read") else doc_text
            rows.append((rcept_no, (corp_name or "").strip(), (report_nm or "").strip(), text))
        return rows


NAME_MAX_CHARS = 200  # kg_triple.subject_name / object_name (VARCHAR2(200 CHAR))


def _fit(name: str) -> str:
    """컬럼 폭에 맞춰 자른다. 정상 상호는 절대 걸리지 않는 최후 방어선."""
    if len(name) <= NAME_MAX_CHARS:
        return name
    logger.warning("name truncated (%d chars): %s...", len(name), name[:60])
    return name[:NAME_MAX_CHARS]


def store_triples(rcept_no: str, source: str, triples: list[Triple], cur) -> int:
    """트리플을 kg_triple에 INSERT. source는 'rules:v1' 또는 모델 ID."""
    for t in triples:
        props = t.model_dump(exclude={"subject", "predicate", "object"}, exclude_none=True)
        cur.execute(
            """INSERT INTO kg_triple
               (rcept_no, subject_name, predicate, object_name, props_json, model)
               VALUES (:1, :2, :3, :4, :5, :6)""",
            [rcept_no, _fit(t.subject), t.predicate.value, _fit(t.object),
             json.dumps(props, ensure_ascii=False), source],
        )
    return len(triples)
