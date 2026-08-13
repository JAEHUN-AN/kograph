"""Prometheus 메트릭 — 운영자가 실제로 봐야 하는 것만 정의한다.

계측 대상을 고른 기준:
- **도구별 호출/오류/지연**: 어떤 도구가 쓰이고 어디가 느린지가 첫 질문이다.
- **검색 축별 지연과 결과 수**: 벡터와 그래프는 성능 특성이 다르고, 결과 수가
  0으로 수렴하면 색인이나 그래프가 비었다는 신호다. 지연만 봐서는 못 잡는다.
- **임베딩 백엔드**: INT8 전환 효과를 대시보드에서 바로 확인하기 위함.

prometheus-client가 없어도 임포트가 깨지지 않게 한다 — stdio 전송만 쓰는
로컬 실행에 서버 의존성을 강제하지 않기 위해서다.
"""

import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager

logger = logging.getLogger(__name__)

try:
    from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

    METRICS_ENABLED = True
except ImportError:  # pragma: no cover - 선택적 의존성
    METRICS_ENABLED = False
    CONTENT_TYPE_LATEST = "text/plain"

    def generate_latest(*_args, **_kwargs) -> bytes:  # type: ignore[misc]
        return b"# prometheus_client not installed\n"


if METRICS_ENABLED:
    # 지연 버킷은 실측에 맞춘다: 임베딩 질의 약 0.35s, 그래프 순회 수십 ms.
    _LATENCY_BUCKETS = (0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)

    TOOL_CALLS = Counter(
        "kograph_tool_calls_total", "MCP 도구 호출 수", ["tool", "status"]
    )
    TOOL_LATENCY = Histogram(
        "kograph_tool_duration_seconds", "MCP 도구 처리 시간", ["tool"],
        buckets=_LATENCY_BUCKETS,
    )
    RETRIEVAL_LATENCY = Histogram(
        "kograph_retrieval_duration_seconds", "검색 축별 처리 시간", ["mode"],
        buckets=_LATENCY_BUCKETS,
    )
    RETRIEVAL_RESULTS = Histogram(
        "kograph_retrieval_results", "검색 축별 반환 건수", ["mode"],
        buckets=(0, 1, 3, 5, 10, 25, 50, 100),
    )
    EMBED_BACKEND_INFO = Gauge(
        "kograph_embed_backend_info", "활성 임베딩 백엔드 (값은 항상 1)", ["backend"]
    )
else:  # pragma: no cover
    TOOL_CALLS = TOOL_LATENCY = RETRIEVAL_LATENCY = RETRIEVAL_RESULTS = None
    EMBED_BACKEND_INFO = None


@contextmanager
def track_tool(name: str) -> Iterator[None]:
    """도구 호출을 계측. 예외는 status=error로 세고 그대로 다시 던진다."""
    started = time.perf_counter()
    status = "ok"
    try:
        yield
    except Exception:
        status = "error"
        raise
    finally:
        if METRICS_ENABLED:
            TOOL_LATENCY.labels(tool=name).observe(time.perf_counter() - started)
            TOOL_CALLS.labels(tool=name, status=status).inc()


def observe_retrieval(mode: str, seconds: float, n_results: int) -> None:
    """검색 축(vector/graph)의 지연과 결과 수를 기록."""
    if METRICS_ENABLED:
        RETRIEVAL_LATENCY.labels(mode=mode).observe(seconds)
        RETRIEVAL_RESULTS.labels(mode=mode).observe(n_results)


def set_embed_backend(backend: str) -> None:
    if METRICS_ENABLED:
        EMBED_BACKEND_INFO.labels(backend=backend).set(1)


def metrics_payload() -> tuple[bytes, str]:
    """(본문, Content-Type) — /metrics 핸들러가 그대로 쓴다."""
    return generate_latest(), CONTENT_TYPE_LATEST
