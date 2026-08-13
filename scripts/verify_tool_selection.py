"""모델이 도구를 **스스로 고르는지** 검증한다.

실행: uv run python scripts/verify_tool_selection.py

verify_mcp.py는 도구를 직접 지정해 부른다. 그건 도구가 동작하는지를 볼 뿐,
Claude Desktop에서 실제로 벌어지는 일 — 모델이 질문을 보고 도구를 고르는 일 —
은 검증하지 못한다.

MCP 서버가 광고하는 도구 스키마를 그대로 Anthropic API에 넘기고, 대표 질문에
어떤 도구를 골랐는지 확인한다. Claude Desktop이 하는 일과 같은 경로다.

가장 흔한 실패는 오류가 아니라 **도구를 아예 안 부르고 모델이 자기 지식으로
답해버리는 것**이다. 로그에는 아무것도 남지 않는다.
"""

from __future__ import annotations

import asyncio
import os
import sys

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

MODEL = os.getenv("KOGRAPH_SELECTION_MODEL", "claude-sonnet-4-5")

# (질문, 허용 도구). 값이 여럿인 항목은 어느 쪽을 골라도 합리적인 경우다.
CASES: tuple[tuple[str, set[str]], ...] = (
    ("한미반도체는 어느 회사에 납품하나?", {"get_company_relations"}),
    ("한미반도체와 주성엔지니어링이 서로 엮여 있어?", {"find_connection"}),
    ("SK하이닉스 채무보증 공시의 보증 기간 산정 근거가 뭐야?", {"search_filings"}),
    ("SK하이닉스 2026년 7월 주가 흐름 알려줘", {"get_price_series"}),
    ("이 데이터로 뭘 물어볼 수 있어?", {"graph_overview", "list_companies"}),
)

SYSTEM = (
    "너는 한국 주식시장 리서치 어시스턴트다. "
    "제공된 도구로 확인할 수 있는 사실은 반드시 도구를 호출해 확인한다."
)


async def _tool_schemas() -> list[dict]:
    """MCP 서버가 광고하는 스키마를 그대로 가져온다.

    여기서 스키마를 손으로 다시 쓰면 검증이 무의미해진다. 실제로 모델에게
    전달되는 것과 같은 설명·인자여야 한다.
    """
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "kograph.mcp_server.server"]
    )
    async with stdio_client(params) as (read, write), ClientSession(read, write) as s:
        await s.initialize()
        tools = await s.list_tools()
        return [
            {
                "name": t.name,
                "description": t.description or "",
                # SDK 2.0은 snake_case다 (inputSchema 아님)
                "input_schema": t.input_schema,
            }
            for t in tools.tools
        ]


def _chosen_tools(message) -> list[str]:
    return [b.name for b in message.content if getattr(b, "type", None) == "tool_use"]


def main() -> int:
    # 키는 .env에 있다. 환경변수로 따로 export하게 만들면 아무도 안 돌린다.
    from kograph.config import get_settings

    api_key = get_settings().anthropic_api_key or os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        print("ANTHROPIC_API_KEY가 없습니다. .env에 설정하세요.")
        return 1

    import anthropic

    schemas = asyncio.run(_tool_schemas())
    print(f"MCP 서버가 광고하는 도구 {len(schemas)}개로 검증합니다 (모델: {MODEL}).\n")

    client = anthropic.Anthropic(api_key=api_key)
    failures = 0

    for question, allowed in CASES:
        message = client.messages.create(
            model=MODEL,
            max_tokens=1024,
            system=SYSTEM,
            tools=schemas,
            messages=[{"role": "user", "content": question}],
        )
        chosen = _chosen_tools(message)

        if not chosen:
            # 도구를 안 부른 것이 가장 흔하고 가장 위험한 실패다.
            verdict, failures = "FAIL (도구 미호출)", failures + 1
        elif set(chosen) & allowed:
            verdict = "OK"
        else:
            verdict, failures = "FAIL (다른 도구)", failures + 1

        print(f"  [{verdict}] {question}")
        print(f"        고른 도구: {', '.join(chosen) or '없음'}"
              f"  / 기대: {', '.join(sorted(allowed))}")

    print(f"\n실패 {failures}건 / {len(CASES)}건")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
