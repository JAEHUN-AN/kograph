"""kograph MCP 서버 — 공시 지식그래프를 LLM이 직접 조회하게 노출한다.

실행(stdio): uv run python -m kograph.mcp_server.server

도구 설계 원칙:
1. 설명에 "무엇을 하는지"가 아니라 **"언제 호출해야 하는지"**를 쓴다.
   최신 모델일수록 도구를 보수적으로 고르므로, 호출 조건이 명시된 설명이
   실제 호출률을 좌우한다.
2. 모든 결과에 **근거 공시번호**를 붙인다. 모델이 출처를 인용할 수 있어야
   금융 도메인에서 쓸 수 있다.
3. 결과는 토큰 효율을 위해 압축된 텍스트로 돌려준다. 원본 JSON 덤프는
   컨텍스트만 먹고 가독성이 낮다.
"""

import argparse
import csv
import logging
import os
from datetime import UTC, date, datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from kograph.db.oracle import connect as oracle_connect
from kograph.observability import metrics_payload, set_embed_backend, track_tool
from kograph.rag.retriever import graph_search, known_companies, vector_search

logger = logging.getLogger(__name__)

UNIVERSE_CSV = Path("data/universe/universe_seed.csv")
MAX_LIMIT = 20
MAX_PRICE_ROWS = 250

def _build_marker() -> str:
    """실행 중인 코드가 언제 것인지 식별한다.

    stdio 서버는 클라이언트가 띄운 하위 프로세스라, 소스를 고쳐도 그 프로세스가
    살아 있는 한 옛 코드로 계속 답한다. 정적 버전(0.1.0)은 편집해도 그대로라
    이걸 잡아내지 못하므로 소스 수정 시각을 함께 남긴다.

    로컬과 컨테이너를 견주는 것이 이 값의 용도이므로 시각은 UTC로 고정한다.
    지역 시간으로 찍으면 같은 파일이 로컬 10:37, 컨테이너 01:37로 보여
    다른 코드라고 오해하게 된다.
    """
    try:
        ver = version("kograph")
    except PackageNotFoundError:  # 편집 설치가 아닌 경우
        ver = "unknown"
    stamp = datetime.fromtimestamp(Path(__file__).stat().st_mtime, tz=UTC)
    return f"kograph {ver} (server.py {stamp:%Y-%m-%dT%H:%M:%SZ})"


mcp = MCPServer(
    name="kograph",
    # serverInfo로 실려 stdio 클라이언트도 조회할 수 있다. /healthz는 HTTP
    # 전용이라, 정작 낡음이 잘 생기는 stdio 경로에서는 확인할 방법이 없었다.
    version=_build_marker(),
    instructions=(
        "한국 반도체·2차전지 밸류체인의 공시 지식그래프. 기업 간 공급·지분·"
        "채무보증·모자 관계와 공시 본문, 일별 시세를 조회할 수 있다. "
        "관계를 묻는 질문은 get_company_relations 또는 find_connection을, "
        "계약 조건·투자 목적처럼 서술형 내용은 search_filings를 쓴다."
    ),
)


def _universe_rows() -> list[dict]:
    if not UNIVERSE_CSV.exists():
        return []
    with UNIVERSE_CSV.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _universe_names() -> list[str]:
    return [r["corp_name"] for r in _universe_rows()]


@mcp.tool()
def list_companies() -> str:
    """분석 대상 기업 목록과 섹터를 반환한다.

    다른 도구를 호출하기 전에 **어떤 회사를 조회할 수 있는지 모를 때** 먼저
    호출한다. 사용자가 이 목록 밖의 회사를 물으면 데이터 범위 밖임을 알려야 한다.
    """
    with track_tool("list_companies"):
        rows = _universe_rows()
        if not rows:
            return f"유니버스 파일을 찾을 수 없습니다: {UNIVERSE_CSV}"
        lines = [f"분석 대상 {len(rows)}개 종목:"]
        lines += [f"  {r['stock_code']}  {r['corp_name']} ({r['sector']})" for r in rows]
        return "\n".join(lines)


# 거래·지배 관계를 임원·특수관계인보다 앞에 둔다. 리서치 질문의 답은
# 대부분 앞쪽에 있고, 모델이 결과를 잘라 읽어도 중요한 것이 남는다.
_RELATION_PRIORITY = (
    "SUPPLIES_TO",
    "INVESTS_IN",
    "OWNS_STAKE",
    "GUARANTEES_DEBT_OF",
    "SUBSIDIARY_OF",
    "OFFICER_OF",
    "RELATED_PARTY_OF",
)


def _relation_sort_key(evidence) -> tuple[int, int, str]:
    """(관계 우선순위, 홉 수, 표시문자열). 같은 순위면 짧은 경로가 먼저."""
    rels = getattr(evidence, "rel_types", None) or []
    rank = min(
        (_RELATION_PRIORITY.index(r) for r in rels if r in _RELATION_PRIORITY),
        default=len(_RELATION_PRIORITY),
    )
    return (rank, len(rels), evidence.text)


@mcp.tool()
def get_company_relations(company: str, hops: int = 1) -> str:
    """한 기업의 거래·지배 관계를 그래프에서 조회한다.

    **다음과 같은 질문에 호출한다**: 누가 누구에게 납품하는가, 지분을 누가
    보유하는가, 자회사가 어디인가, 채무보증 대상이 어디인가. 관계가 여러
    공시에 흩어져 있어도 그래프가 모아서 돌려주므로, 이런 질문에는
    search_filings보다 이 도구가 정확하다.

    hops=1은 직접 관계, hops=2는 "공급사의 다른 고객"처럼 한 다리 건넌
    관계까지 포함한다. 먼저 1로 시도하고 부족할 때 2로 넓힌다.
    """
    with track_tool("get_company_relations"):
        hops = max(1, min(int(hops), 2))
        evidences = graph_search(company, hops=hops)
        if not evidences:
            return (
                f"'{company}'의 관계를 찾지 못했습니다. "
                "list_companies로 조회 가능한 회사명을 확인하세요."
            )
        lines = [f"'{company}' 관련 관계 {len(evidences)}건 (hops={hops}):"]
        for ev in sorted(evidences, key=_relation_sort_key):
            cite = f"  [공시 {ev.rcept_no}]" if ev.rcept_no else ""
            lines.append(f"  {ev.text}{cite}")
        return "\n".join(lines)


def _shared_partners(evidences, a_key: str, b_key: str) -> set[str]:
    """두 회사가 각각 연결된 상대의 교집합 (직접 경로가 없을 때의 차선책)."""
    a_side: set[str] = set()
    b_side: set[str] = set()
    for ev in evidences:
        names = [e.replace(" ", "") for e in ev.entities]
        if any(a_key in n for n in names):
            a_side.update(ev.entities)
        if any(b_key in n for n in names):
            b_side.update(ev.entities)
    shared = a_side & b_side
    return {
        s for s in shared
        if a_key not in s.replace(" ", "") and b_key not in s.replace(" ", "")
    }


@mcp.tool()
def find_connection(company_a: str, company_b: str) -> str:
    """두 기업이 어떻게 연결되는지 경로를 찾는다.

    **"A와 B가 관계가 있나", "둘의 공통 거래처는", "A가 B에 간접적으로
    엮여 있나" 같은 질문에 호출한다.** 두 회사를 각각 조회해 사용자가 직접
    대조하게 두지 말고, 이 도구로 연결 경로를 확인한다.
    """
    with track_tool("find_connection"):
        evidences = graph_search(f"{company_a} {company_b}", hops=2)
        a_key, b_key = company_a.replace(" ", ""), company_b.replace(" ", "")
        linked = [
            ev for ev in evidences
            if any(a_key in e.replace(" ", "") for e in ev.entities)
            and any(b_key in e.replace(" ", "") for e in ev.entities)
        ]
        if not linked:
            shared = _shared_partners(evidences, a_key, b_key)
            if shared:
                return (
                    f"'{company_a}'와 '{company_b}'의 직접 경로는 없지만 "
                    f"공통 상대가 있습니다: {', '.join(sorted(shared))}"
                )
            return f"'{company_a}'와 '{company_b}'를 잇는 경로를 찾지 못했습니다."
        lines = [f"'{company_a}' - '{company_b}' 연결 {len(linked)}건:"]
        for ev in linked:
            cite = f"  [공시 {ev.rcept_no}]" if ev.rcept_no else ""
            lines.append(f"  {ev.text}{cite}")
        return "\n".join(lines)


@mcp.tool()
def search_filings(query: str, limit: int = 5, company: str | None = None) -> str:
    """공시 본문을 의미 검색한다.

    **관계가 아니라 서술형 내용을 물을 때 호출한다** — 계약 조건, 투자 목적,
    금액 산정 근거, 공시에 문장으로 적힌 배경 설명 등. 기업 간 관계 자체를
    묻는 질문이면 get_company_relations가 더 정확하다.

    **특정 회사의 공시를 원하면 company를 반드시 지정한다.** 의미 검색은
    소유권을 구분하지 못해서, 회사명을 query에만 넣으면 같은 그룹 계열사의
    공시가 섞여 들어온다.
    """
    with track_tool("search_filings"):
        limit = max(1, min(int(limit), MAX_LIMIT))
        evidences = vector_search(query, k=limit, company=company)
        if not evidences:
            scope = f" ({company} 공시로 한정)" if company else ""
            return (
                f"'{query}'에 해당하는 공시 내용을 찾지 못했습니다{scope}. "
                "list_companies로 회사명을 확인하거나 company 없이 다시 시도하세요."
            )
        scope = f" [{company} 한정]" if company else ""
        lines = [f"'{query}' 관련 공시 {len(evidences)}건{scope}:"]
        for ev in evidences:
            body = " ".join(ev.text.split())[:400]
            lines.append(f"\n[공시 {ev.rcept_no}] (유사도 {ev.score:.3f})\n{body}")
        return "\n".join(lines)


def _format_price_row(trade_date, close, volume, change_rate) -> str:
    row = f"  {trade_date:%Y-%m-%d}  종가 {close:>9,}  거래량 {volume:>12,}"
    return f"{row}  {change_rate:+.2f}%" if change_rate is not None else row


# 시세만 주면 "왜 움직였나"에 답할 수 없고, 모델은 자기 지식으로 지어낸다.
# 같은 기간의 공시를 함께 주면 근거를 짚어 말할 수 있다.
MAX_PERIOD_FILINGS = 30


def _filings_in_period(cur, code: str, begin: date, end: date) -> list[tuple]:
    """기간 내 공시 (rcept_dt는 DATE가 아니라 YYYYMMDD 문자열이다)."""
    cur.execute(
        """SELECT rcept_dt, report_nm, rcept_no
           FROM filing
           WHERE stock_code = :code AND rcept_dt BETWEEN :b AND :e
           ORDER BY rcept_dt""",
        code=code, b=begin.strftime("%Y%m%d"), e=end.strftime("%Y%m%d"),
    )
    return cur.fetchall()


def _format_filing_row(rcept_dt: str, report_nm: str, rcept_no: str) -> str:
    day = f"{rcept_dt[:4]}-{rcept_dt[4:6]}-{rcept_dt[6:]}"
    return f"  {day}  {report_nm}  [공시 {rcept_no}]"


@mcp.tool()
def get_price_series(stock_code: str, start_date: str, end_date: str) -> str:
    """종목의 일별 시세를 조회한다 (날짜는 YYYY-MM-DD).

    **주가 추이·수익률·거래량을 묻는 질문에 호출한다.** 관계 질문에 이어
    "그 계약 이후 주가가 어땠나" 같은 후속 질문이 나오면 여기서 확인한다.
    기간이 길면 앞뒤 일부만 반환하므로, 특정 구간을 보려면 범위를 좁혀 호출한다.

    **같은 기간에 제출된 공시 목록을 함께 돌려준다.** "왜 올랐나/떨어졌나"를
    물으면 그 날짜의 공시를 근거로 답한다. 공시가 없는 날의 등락은 원인을
    추측하지 말고 확인할 수 없다고 말한다.
    """
    with track_tool("get_price_series"):
        code = stock_code.strip().zfill(6)
        try:
            begin, end = date.fromisoformat(start_date), date.fromisoformat(end_date)
        except ValueError:
            return "날짜는 YYYY-MM-DD 형식이어야 합니다 (예: 2026-01-31)."
        if begin > end:
            return "start_date가 end_date보다 늦습니다."

        with oracle_connect() as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT trade_date, close_price, volume, change_rate
                   FROM price_daily
                   WHERE stock_code = :code AND trade_date BETWEEN :b AND :e
                   ORDER BY trade_date""",
                code=code, b=begin, e=end,
            )
            rows = cur.fetchall()
            filings = _filings_in_period(cur, code, begin, end)

        if not rows:
            return f"{code}의 {start_date}~{end_date} 시세가 없습니다."

        first_close, last_close = rows[0][1], rows[-1][1]
        change = (last_close / first_close - 1) * 100 if first_close else 0.0
        out = [
            f"{code} 시세 {len(rows)}거래일 ({rows[0][0]:%Y-%m-%d} ~ {rows[-1][0]:%Y-%m-%d})",
            f"기간 수익률 {change:+.1f}% ({first_close:,} -> {last_close:,})",
        ]
        shown = rows if len(rows) <= MAX_PRICE_ROWS else rows[:10] + rows[-10:]
        if len(rows) > MAX_PRICE_ROWS:
            out.append(f"(전체 {len(rows)}행 중 앞뒤 10행만 표시)")
        out += [_format_price_row(*r) for r in shown]

        if filings:
            out.append(f"\n기간 중 공시 {len(filings)}건:")
            out += [_format_filing_row(*f) for f in filings[:MAX_PERIOD_FILINGS]]
            if len(filings) > MAX_PERIOD_FILINGS:
                out.append(f"  (전체 {len(filings)}건 중 {MAX_PERIOD_FILINGS}건만 표시)")
        else:
            # 없다는 사실을 명시해야 모델이 원인을 지어내지 않는다.
            out.append("\n기간 중 공시 없음 — 등락 원인을 이 데이터로는 설명할 수 없다.")
        return "\n".join(out)


@mcp.tool()
def graph_overview() -> str:
    """지식그래프에 어떤 회사와 관계가 들어 있는지 요약한다.

    **"무엇을 물어볼 수 있나", "데이터에 뭐가 있나" 같은 탐색적 질문이나,
    다른 도구가 빈 결과를 돌려줘 범위를 확인해야 할 때 호출한다.**
    """
    with track_tool("graph_overview"):
        names = known_companies()
        # 예시는 분석 대상 종목에서 뽑는다. 전체를 사전순으로 자르면 공시에 등장한
        # 가칭 법인('(가칭) pCAM JV')이 앞을 채워 그래프를 오해하게 만든다.
        seeds = _universe_names()
        examples = [n for n in seeds if n in set(names)] or sorted(names)[:10]
        return (
            f"지식그래프: 회사 노드 {len(names)}개.\n"
            "관계 유형: SUPPLIES_TO(공급), OWNS_STAKE(지분), INVESTS_IN(출자), "
            "GUARANTEES_DEBT_OF(채무보증), SUBSIDIARY_OF(모자), OFFICER_OF(임원), "
            "RELATED_PARTY_OF(최대주주 특수관계인 — 친인척·계열사 포함이며 "
            "임원이 아니다).\n"
            "출처: DART 주요사항보고서 (반도체·2차전지 밸류체인, 최근 3년).\n"
            f"분석 대상 종목: {', '.join(examples)}\n"
            "이 밖에 거래상대·자회사로 등장한 법인이 함께 들어 있다."
        )


@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(_request: Request) -> JSONResponse:
    """liveness probe — 의존 서비스는 확인하지 않는다.

    DB까지 검사하면 Oracle이 잠깐 흔들릴 때 쿠버네티스가 멀쩡한 파드를
    재시작한다. 준비 상태는 readiness로 따로 다뤄야 한다.
    """
    return JSONResponse({"status": "ok", "service": "kograph", "build": _build_marker()})


@mcp.custom_route("/metrics", methods=["GET"])
async def metrics(_request: Request) -> Response:
    body, content_type = metrics_payload()
    return Response(content=body, media_type=content_type)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="kograph MCP server")
    parser.add_argument("--transport", choices=["stdio", "http"], default="stdio",
                        help="stdio는 Claude Desktop용, http는 컨테이너 배포용")
    parser.add_argument("--host", default=os.getenv("KOGRAPH_HOST", "0.0.0.0"))  # noqa: S104
    parser.add_argument("--port", type=int, default=int(os.getenv("KOGRAPH_PORT", "8000")))
    args = parser.parse_args()

    set_embed_backend(os.getenv("KOGRAPH_EMBED_BACKEND", "torch"))
    # stderr로만 남긴다. stdio 전송에서 stdout은 프로토콜 채널이다.
    logger.info("starting %s", _build_marker())

    if args.transport == "stdio":
        mcp.run(transport="stdio")
        return

    import uvicorn

    app = mcp.streamable_http_app(host=args.host)
    logger.info("serving MCP on http://%s:%d/mcp (metrics: /metrics)", args.host, args.port)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
