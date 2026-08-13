"""HTTP 전송 + 관측 엔드포인트 검증.

실행: uv run python scripts/verify_http.py

서버를 HTTP로 띄우고 다음을 확인한다.
  1. /healthz 가 200을 준다 (쿠버네티스 liveness probe가 쓸 경로)
  2. /metrics 가 Prometheus 형식을 준다
  3. MCP 도구를 호출하면 **메트릭 카운터가 실제로 올라간다**

3번이 핵심이다. 엔드포인트가 200을 준다고 계측이 동작한다는 뜻은 아니다.
"""

import subprocess
import sys
import time
import urllib.error
import urllib.request

HOST, PORT = "127.0.0.1", 8765
BASE = f"http://{HOST}:{PORT}"
BOOT_TIMEOUT = 120


def get(path: str, timeout: float = 10) -> tuple[int, str]:
    with urllib.request.urlopen(f"{BASE}{path}", timeout=timeout) as resp:  # noqa: S310
        return resp.status, resp.read().decode("utf-8", errors="replace")


def wait_for_boot() -> bool:
    deadline = time.monotonic() + BOOT_TIMEOUT
    while time.monotonic() < deadline:
        try:
            if get("/healthz", timeout=3)[0] == 200:
                return True
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            time.sleep(2)
    return False


def count_of(metrics_text: str, needle: str) -> float:
    for line in metrics_text.splitlines():
        if line.startswith(needle):
            return float(line.rsplit(maxsplit=1)[-1])
    return 0.0


def call_tool_over_http() -> bool:
    """streamable-http 전송으로 도구를 한 번 호출한다."""
    import anyio
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    async def run() -> bool:
        async with (
            streamable_http_client(f"{BASE}/mcp") as streams,
            ClientSession(streams[0], streams[1]) as session,
        ):
            await session.initialize()
            result = await session.call_tool("list_companies", {})
            text = "\n".join(
                c.text for c in result.content if getattr(c, "type", None) == "text"
            )
            first = text.splitlines()[0] if text else "(빈 응답)"
            print(f"  도구 응답 첫 줄: {first}")
            return bool(text.strip())

    return anyio.run(run)


def main() -> int:
    proc = subprocess.Popen(  # noqa: S603
        [sys.executable, "-m", "kograph.mcp_server.server",
         "--transport", "http", "--host", HOST, "--port", str(PORT)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    failures = 0
    try:
        print(f"서버 기동 대기 (최대 {BOOT_TIMEOUT}s)...")
        if not wait_for_boot():
            print("  FAIL: 서버가 뜨지 않았습니다")
            return 1
        print("  OK: /healthz 200")

        status, before = get("/metrics")
        if status != 200 or "kograph_" not in before:
            print(f"  FAIL: /metrics 응답 이상 (status={status})")
            failures += 1
        else:
            print("  OK: /metrics 가 kograph_* 메트릭을 노출")

        print("MCP 도구 호출 (HTTP 전송)...")
        if not call_tool_over_http():
            print("  FAIL: 도구 호출 실패")
            failures += 1

        _, after = get("/metrics")
        needle = 'kograph_tool_calls_total{status="ok",tool="list_companies"}'
        delta = count_of(after, needle) - count_of(before, needle)
        if delta >= 1:
            print(f"  OK: 호출 카운터 +{delta:g}")
        else:
            print(f"  FAIL: 카운터가 오르지 않음 ({needle})")
            failures += 1

        print(f"\n실패 {failures}건")
        return 1 if failures else 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    raise SystemExit(main())
