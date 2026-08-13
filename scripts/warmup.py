"""시연 직전 컨테이너를 데운다.

막 기동한 서버는 첫 검색에서 INT8 모델을 로딩하느라 6.7초가 걸린다
(이후 호출은 100~120ms). 그 한 번이 Prometheus 히스토그램에 남아 p95를
250ms 위로 끌어올리고, 대시보드를 가리키며 설명하는 중에 숫자가 설명과
어긋난다. 시연 직전에 실행할 것.

실행: uv run python scripts/warmup.py [--base http://localhost:8000]
"""

from __future__ import annotations

import argparse
import time
import urllib.error
import urllib.request

import anyio
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

# 벡터 축(임베딩)과 그래프 축을 모두 태워야 한다. 한쪽만 데우면
# 시연 중 나머지 축의 첫 호출에서 같은 문제가 다시 난다.
_VECTOR_QUERIES = (
    "HBM 공급 계약",
    "채무보증 결정",
    "타법인 주식 취득",
    "양극재 공급 계약",
)
_GRAPH_COMPANIES = ("SK하이닉스", "한미반도체")

_SLOW_THRESHOLD_SEC = 1.0
_READY_TIMEOUT_SEC = 120


def _wait_ready(base: str, timeout: int = _READY_TIMEOUT_SEC) -> bool:
    """/healthz가 200을 줄 때까지 기다린다.

    이 스크립트는 스택을 막 올린 직후에 실행되는 일이 많다. 대기 없이
    붙으면 'Server disconnected'로 죽는데, 그러면 정작 시연 직전에
    워밍업이 안 된 채로 넘어간다.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{base}/healthz", timeout=5) as resp:
                if resp.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(2)
    return False


async def _warm(base: str) -> list[float]:
    latencies: list[float] = []
    async with (
        streamable_http_client(f"{base}/mcp") as streams,
        ClientSession(streams[0], streams[1]) as session,
    ):
        await session.initialize()

        calls = [("search_filings", {"query": q}) for q in _VECTOR_QUERIES]
        calls += [("get_company_relations", {"company": c}) for c in _GRAPH_COMPANIES]

        for name, args in calls:
            started = time.perf_counter()
            await session.call_tool(name, args)
            elapsed = time.perf_counter() - started
            latencies.append(elapsed)
            label = args.get("query") or args.get("company")
            print(f"  {elapsed * 1000:7.0f} ms  {name}({label})")
    return latencies


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://localhost:8000")
    args = parser.parse_args()

    print(f"워밍업: {args.base}")
    if not _wait_ready(args.base):
        print(f"실패: {_READY_TIMEOUT_SEC}초 안에 서버가 준비되지 않았습니다.")
        return 1

    latencies = anyio.run(_warm, args.base)

    # 첫 호출만 느리고 나머지가 안정적이면 모델 로딩이 끝난 것이다.
    tail = latencies[1:] or latencies
    slowest = max(tail)
    print(f"\n첫 호출 {latencies[0] * 1000:.0f} ms, 이후 최대 {slowest * 1000:.0f} ms")

    if slowest > _SLOW_THRESHOLD_SEC:
        print("경고: 여전히 느립니다. 컨테이너 로그를 확인하세요.")
        return 1
    print("시연 준비 완료.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
