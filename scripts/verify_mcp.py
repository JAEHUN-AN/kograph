"""MCP 서버 통합 검증 — 실제 클라이언트로 붙어 도구를 호출한다.

실행: uv run python scripts/verify_mcp.py

서버가 뜨는지만 보는 것은 검증이 아니다. stdio로 하위 프로세스를 띄우고
MCP 핸드셰이크 -> 도구 목록 -> 실제 호출까지 왕복해, Claude Desktop이 붙었을 때와
같은 경로를 그대로 통과시킨다.
"""

import asyncio
import sys

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

CALLS = [
    ("graph_overview", {}),
    ("list_companies", {}),
    ("get_company_relations", {"company": "SK하이닉스", "hops": 1}),
    ("find_connection", {"company_a": "한미반도체", "company_b": "주성엔지니어링"}),
    ("search_filings", {"query": "채무보증 계약 조건", "limit": 2}),
    ("get_price_series", {"stock_code": "000660",
                          "start_date": "2026-07-01", "end_date": "2026-08-12"}),
]


def _text(result) -> str:
    parts = [c.text for c in result.content if getattr(c, "type", None) == "text"]
    return "\n".join(parts)


async def main() -> int:
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "kograph.mcp_server.server"],
    )

    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        init = await session.initialize()
        # 어떤 코드에 붙었는지 먼저 찍는다. stdio 서버는 핫 리로드되지 않아서,
        # 고친 줄 알고 검증하다 옛 프로세스를 상대하는 일이 실제로 있었다.
        print(f"연결 성공: {init.server_info.version}")
        print(f"  {(session.instructions or '')[:70]}...\n")

        tools = await session.list_tools()
        print(f"등록된 도구 {len(tools.tools)}개:")
        for t in tools.tools:
            desc = (t.description or "").strip().splitlines()
            print(f"  - {t.name}: {desc[0] if desc else ''}")
        print()

        failures = 0
        for name, args in CALLS:
            print("=" * 78)
            print(f"call {name}({', '.join(f'{k}={v!r}' for k, v in args.items())})")
            try:
                result = await session.call_tool(name, args)
            except Exception as exc:  # noqa: BLE001 - 검증 스크립트는 원인을 그대로 보여준다
                print(f"  ERROR: {exc}")
                failures += 1
                continue
            body = _text(result)
            lines = body.splitlines()
            print("\n".join(lines[:8]) if lines else "  (빈 응답)")
            if len(lines) > 8:
                print(f"  ... (총 {len(lines)}줄)")
            if not body.strip():
                failures += 1

        print("=" * 78)
        print(f"실패 {failures}건 / 호출 {len(CALLS)}건")
        return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
