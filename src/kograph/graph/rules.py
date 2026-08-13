"""규칙 기반 공시 → 관계 트리플 파서 (LLM 불필요).

실행: uv run python -m kograph.graph.rules [--limit N] [--dry-run]

DART 주요사항보고서는 고정 양식이라 "라벨 줄 다음이 값 줄" 구조가 일정하다.
정규식 파싱이 LLM보다 정확하고(환각 없음), 결정론적이며, 비용이 0이다.
비정형 잔여분만 extract.py의 LLM 경로로 넘긴다.

관계의 주체(subject)는 항상 공시 제출사의 corp 마스터 명칭을 쓴다 —
Neo4j에서 유니버스 Company 노드와 이름으로 조인되어야 하기 때문.
"""

import argparse
import logging
import re
from collections.abc import Callable

from kograph.graph.models import Predicate, Triple
from kograph.graph.store import pending_filings, store_triples

logger = logging.getLogger(__name__)

SOURCE = "rules:v1"

_NUM_PREFIX = re.compile(r"^\d+\s*\.\s*")
_WS = re.compile(r"\s+")
_EMPTY_VALUES = {"", "-", "해당없음", "없음", "N/A"}


def _norm(line: str) -> str:
    """라벨 비교용 정규화: 앞 번호(`3.`)와 하이픈, 중복 공백 제거."""
    s = _NUM_PREFIX.sub("", line.strip())
    return _WS.sub(" ", s.lstrip("-").strip())


def _find_label(lines: list[str], labels: tuple[str, ...], start: int = 0) -> int | None:
    targets = {_norm(x) for x in labels}
    for i in range(start, len(lines)):
        if _norm(lines[i]) in targets:
            return i
    return None


def _field(lines: list[str], *labels: str, after: str | None = None) -> str | None:
    """라벨 줄을 찾아 그다음 비어있지 않은 줄을 값으로 반환.

    labels는 **인자 순서가 곧 우선순위**다. 문서 등장 순서가 아니라 앞에 쓴
    라벨을 먼저 시도한다 — 채무보증 공시처럼 의미가 다른 금액 라벨이
    여러 개 있을 때 원하는 쪽을 확실히 고르기 위함.

    after를 주면 해당 섹션 라벨 이후부터 탐색한다 (시작일/종료일처럼
    같은 라벨이 문서에 여러 번 나오는 경우를 구분하기 위함).
    """
    start = 0
    if after is not None:
        section = _find_label(lines, (after,))
        if section is None:
            return None
        start = section

    for label in labels:
        idx = _find_label(lines, (label,), start)
        if idx is None or idx + 1 >= len(lines):
            continue
        # 값은 반드시 '바로 다음 줄'이다. 본문은 빈 줄이 제거된 상태이므로
        # 앞으로 스캔하면 비공개('-') 항목에서 다음 '라벨'을 값으로 집는다.
        value = lines[idx + 1].strip()
        if value and value not in _EMPTY_VALUES and _norm(value) not in _FORM_LABELS:
            return value
    return None


def _money(raw: str | None) -> int | None:
    """'90,960,000,000' -> 90960000000. 값이 없거나 숫자가 아니면 None."""
    if not raw:
        return None
    digits = re.sub(r"[^\d]", "", raw)
    return int(digits) if digits else None


def _pct(raw: str | None) -> float | None:
    if not raw:
        return None
    m = re.search(r"-?\d+(?:\.\d+)?", raw.replace(",", ""))
    return float(m.group()) if m else None


def _date(raw: str | None) -> str | None:
    """ISO(2026-07-01) 또는 2026.07.01 / 20260701 형태를 YYYY-MM-DD로."""
    if not raw:
        return None
    m = re.search(r"(\d{4})[-.\s/]?(\d{2})[-.\s/]?(\d{2})", raw)
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else None


def _clean_name(raw: str | None) -> str | None:
    if not raw:
        return None
    name = raw.strip()
    return name if name not in _EMPTY_VALUES else None


def _strip_trailing_paren(name: str | None) -> str | None:
    """'주식회사 테스트글로벌 (대한민국)' -> '주식회사 테스트글로벌'.

    발행회사 항목은 '회사명(국적)' 라벨을 쓰는 폼이 있어 값 끝에 국적이 붙는다.
    선행 괄호를 쓰는 상호((주)포스코)는 건드리지 않는다.
    """
    if not name:
        return None
    return re.sub(r"\s*\([^()]*\)\s*$", "", name).strip() or None


# '~인' 형태만 인정한다. 맨 '자회사'/'종속회사'는 '회사와 관계' 항목의 값으로도
# 등장하므로, 그것까지 받으면 관계 값을 자회사 상호로 오인한다.
_SECTION_HEADER = re.compile(r"^\d+\s*\.\s+\S")

# 값 자리에 라벨이 오는 경우를 걸러내는 안전망.
# 일부 공시는 라벨을 먼저 모아 나열하고 값을 뒤에 붙이는 표 레이아웃을 쓴다.
# 그 변형까지 파싱하기보다, 라벨을 값으로 채택하지 않는 쪽이 안전하다 —
# 관계 하나를 놓치는 것보다 없는 사실을 그래프에 넣는 쪽이 나쁘다.
_FORM_LABELS = frozenset({
    "성명", "생년월일", "생년월일 또는 사업자등록번호", "성별", "국내외 구분", "국적",
    "최대주주 및 발행회사와의 관계", "겸직내용1", "겸직내용2",
    "변경일", "변경원인", "주식의 종류", "변경전주식수", "증감주식수", "변경후주식수", "비고",
    "최근 매출액(원)", "최근매출액(원)", "주요사업", "회사와의 관계", "회사와 관계",
    "매출액대비(%)", "매출액 대비(%)", "자기자본(원)", "자기자본대비(%)",
    "회사와 최근 3년간 동종계약 이행여부", "대규모법인여부", "대기업해당여부",
})


def _section_bounds(lines: list[str], label: str) -> tuple[int, int]:
    """번호가 붙은 섹션의 [시작, 끝) 인덱스. 없으면 문서 전체."""
    start = _find_label(lines, (label,))
    if start is None:
        return 0, len(lines)
    for i in range(start + 1, len(lines)):
        if _SECTION_HEADER.match(lines[i].strip()):
            return start, i
    return start, len(lines)


_SUBSIDIARY_INTRO = ("자회사인", "종속회사인")


def _subsidiary_name(lines: list[str]) -> str | None:
    """'자회사인 / <상호> / 의 주요경영사항신고' 머리말에서 자회사 상호를 추출.

    모회사가 자회사를 대신해 내는 공시. 헤더 근처에만 나오므로 앞부분만 본다.
    """
    for i, line in enumerate(lines[:12]):
        if _norm(line) not in _SUBSIDIARY_INTRO:
            continue
        for j in range(i + 1, min(i + 3, len(lines))):
            candidate = lines[j].strip()
            if not candidate or candidate in _EMPTY_VALUES:
                continue
            if candidate.startswith("의 "):  # '의 주요경영사항신고' 꼬리말
                break
            return candidate
    return None


# --- 폼별 파서 ---------------------------------------------------------------


# 법인격 뒤에 '(가칭)' 같은 괄호가 붙는 경우까지 조각으로 인정한다
_LEGAL_FRAGMENT = re.compile(
    r"^\s*(Ltd|Inc|lnc|LLC|Corp|Co|GmbH|Pte|Zrt|S\.?A|B\.?V|N\.?V)\b\.?\s*"
    r"(\([^()]*\))?[.,)\s]*$",
    re.IGNORECASE,
)


def _split_companies(raw: str | None) -> list[str]:
    """'A사(중국), B사(중국)' -> ['A사', 'B사'].

    지분 일괄 취득·처분 공시는 발행회사 칸에 여러 상호를 쉼표로 나열한다.
    단순 split은 상호 내부의 쉼표('… New Energy Co., Ltd')까지 쪼개
    'Ltd)' 같은 유령 회사를 만든다. 괄호 밖 쉼표에서만 자르고, 법인격
    조각만 남은 항목은 앞 상호에 되붙인다.
    """
    if not raw:
        return []

    parts: list[str] = []
    buf: list[str] = []
    depth = 0
    for ch in raw:
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth = max(0, depth - 1)
        if ch == "," and depth == 0:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    parts.append("".join(buf))

    merged: list[str] = []
    for part in parts:
        if merged and _LEGAL_FRAGMENT.match(part):
            merged[-1] = f"{merged[-1]}, {part.strip()}"
        else:
            merged.append(part)

    names = []
    for part in merged:
        name = _strip_trailing_paren(_clean_name(part))
        if name:
            names.append(name)
    return names


def _parse_supply_contract(filer: str, lines: list[str], report_nm: str) -> list[Triple]:
    """단일판매ㆍ공급계약체결 -> filer SUPPLIES_TO 계약상대."""
    counterparty = _clean_name(_field(lines, "계약상대", "계약상대방"))
    if not counterparty:
        return []
    return [Triple(
        subject=filer,
        predicate=Predicate.SUPPLIES_TO,
        object=counterparty,
        amount_krw=_money(_field(lines, "계약금액(원)")),
        start_date=_date(_field(lines, "시작일", after="계약기간")),
        end_date=_date(_field(lines, "종료일", after="계약기간")),
        note=_clean_name(_field(lines, "회사와의 관계")),
    )]


def _parse_stake_acquisition(filer: str, lines: list[str], report_nm: str) -> list[Triple]:
    """타법인주식및출자증권취득결정 -> filer INVESTS_IN 발행회사 (+ OWNS_STAKE).

    처분결정은 투자의 반대이므로 INVESTS_IN을 만들지 않는다. 그 경우 헤더에서
    얻은 모자 관계(SUBSIDIARY_OF)만 남는다.
    """
    if "처분" in report_nm:
        return []

    issuers = _split_companies(_field(lines, "회사명", "회사명(국적)", after="발행회사"))
    if not issuers:
        return []
    relation = _clean_name(_field(lines, "회사와 관계", "회사와의 관계"))
    acquired_at = _date(_field(lines, "취득예정일자"))
    stake = _pct(_field(lines, "지분비율(%)", after="취득후 소유주식수 및 지분비율"))

    # 금액·비율은 건별 합계라 대상이 여럿이면 특정 회사에 귀속시킬 수 없다.
    single = len(issuers) == 1
    amount = _money(_field(lines, "취득금액(원)")) if single else None
    equity_ratio = _pct(_field(lines, "자기자본대비(%)")) if single else None

    triples: list[Triple] = []
    for issuer in issuers:
        triples.append(Triple(
            subject=filer,
            predicate=Predicate.INVESTS_IN,
            object=issuer,
            amount_krw=amount,
            ratio_pct=equity_ratio,
            start_date=acquired_at,
            note=relation,
        ))
        # 취득 후 지분율이 명시되면 보유 관계도 별도 엣지로 남긴다
        if stake is not None and single:
            triples.append(Triple(
                subject=filer,
                predicate=Predicate.OWNS_STAKE,
                object=issuer,
                ratio_pct=stake,
                start_date=acquired_at,
                note=relation,
            ))
    return triples


def _parse_debt_guarantee(filer: str, lines: list[str], report_nm: str) -> list[Triple]:
    """타인에대한채무보증결정 -> filer GUARANTEES_DEBT_OF 채무자."""
    debtor = _clean_name(_field(lines, "채무자"))
    if not debtor:
        return []
    return [Triple(
        subject=filer,
        predicate=Predicate.GUARANTEES_DEBT_OF,
        object=debtor,
        amount_krw=_money(_field(lines, "채무보증금액(원)", "채무(차입)금액(원)")),
        ratio_pct=_pct(_field(lines, "자기자본대비(%)")),
        start_date=_date(_field(lines, "시작일", after="채무보증기간")),
        end_date=_date(_field(lines, "종료일", after="채무보증기간")),
        note=_clean_name(_field(lines, "회사와의 관계")),
    )]


def _shareholder_predicate(relation: str | None) -> Predicate:
    """공시의 '관계'가 발행회사 임원임을 말할 때만 OFFICER_OF.

    '계열사임원'은 이 회사의 임원이 아니라 계열사의 임원이므로 제외한다.
    관계가 비어 있으면(공시의 약 30%) 임원이라고 단정할 근거가 없다.
    """
    if not relation:
        return Predicate.RELATED_PARTY_OF
    text = relation.replace(" ", "")
    if "임원" in text and "계열사" not in text:
        return Predicate.OFFICER_OF
    return Predicate.RELATED_PARTY_OF


def _parse_shareholder_change(filer: str, lines: list[str], report_nm: str) -> list[Triple]:
    """최대주주등소유주식변동신고서 -> 신고 대상자와 발행회사의 관계.

    '개인별 세부변동사항' 섹션 안에서만 '성명' 블록을 읽는다. 뒤따르는
    '최대주주등 주식소유현황(총괄현황)'은 라벨-값 쌍이 아니라 표 머리글이
    연속으로 나열된 구조라, 범위를 두지 않으면 머리글을 이름으로 오독한다.

    신고 대상은 임원만이 아니다. 친인척·계열사·재단도 들어오므로 공시의
    '관계'가 임원임을 말할 때만 OFFICER_OF를 준다. 이 구분이 없으면 법인이
    사람의 자리에 들어가 '한미컴퍼니가 한미반도체의 임원'이 된다.
    """
    start, end = _section_bounds(lines, "개인별 세부변동사항")

    triples: list[Triple] = []
    seen: set[str] = set()
    idx = start
    while idx < end:
        pos = _find_label(lines, ("성명",), idx)
        if pos is None or pos >= end:
            break
        idx = pos + 1
        person = _clean_name(lines[pos + 1] if pos + 1 < len(lines) else None)
        if not person or person in seen or _norm(person) in _FORM_LABELS:
            continue
        # 관계 라벨은 각 블록 안에 있으므로 해당 성명 이후에서 찾는다
        relation = None
        rel_pos = _find_label(lines, ("최대주주 및 발행회사와의 관계",), pos)
        if rel_pos is not None and rel_pos + 1 < len(lines):
            relation = _clean_name(lines[rel_pos + 1])
        seen.add(person)
        triples.append(Triple(
            subject=person,
            predicate=_shareholder_predicate(relation),
            object=filer,
            note=relation,
        ))
    return triples


# report_nm 부분일치 -> 파서. 앞쪽 항목이 우선한다.
_PARSERS: tuple[tuple[str, Callable[[str, list[str], str], list[Triple]]], ...] = (
    ("공급계약", _parse_supply_contract),
    ("타법인주식", _parse_stake_acquisition),
    ("채무보증", _parse_debt_guarantee),
    ("최대주주등소유주식변동", _parse_shareholder_change),
)


def parse(corp_name: str, report_nm: str, doc_text: str) -> list[Triple]:
    """공시 하나를 파싱해 트리플 목록 반환. 지원하지 않는 폼이면 빈 목록."""
    filer = (corp_name or "").strip()
    if not filer or not doc_text:
        return []
    lines = doc_text.splitlines()

    triples: list[Triple] = []
    subject = filer

    # 모회사가 자회사를 대신해 내는 공시: 모자 관계를 남기고,
    # 계약/투자의 실제 주체를 자회사로 바로잡는다.
    subsidiary = _subsidiary_name(lines)
    if subsidiary and subsidiary != filer:
        triples.append(Triple(
            subject=subsidiary,
            predicate=Predicate.SUBSIDIARY_OF,
            object=filer,
        ))
        subject = subsidiary

    for keyword, parser in _PARSERS:
        if keyword in report_nm:
            triples.extend(parser(subject, lines, report_nm))
            break
    return triples


def run(limit: int | None = None, dry_run: bool = False) -> tuple[int, int, int]:
    """Returns (처리 공시 수, 트리플 수, 미지원/무수확 공시 수)."""
    from kograph.db.oracle import connect

    todo = pending_filings(limit)
    logger.info("candidates: %d filings", len(todo))

    parsed = total = misses = 0
    with connect() as conn, conn.cursor() as cur:
        for rcept_no, corp_name, report_nm, doc_text in todo:
            triples = parse(corp_name, report_nm, doc_text)
            if not triples:
                misses += 1
                continue
            parsed += 1
            total += len(triples)
            if not dry_run:
                store_triples(rcept_no, SOURCE, triples, cur)
        if not dry_run:
            conn.commit()

    logger.info("done: parsed=%d triples=%d unparsed=%d%s",
                parsed, total, misses, " (dry-run)" if dry_run else "")
    return parsed, total, misses


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--dry-run", action="store_true", help="DB에 쓰지 않고 수확량만 확인")
    args = p.parse_args()
    run(args.limit, args.dry_run)
