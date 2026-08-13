# kograph MCP 서버 이미지 (HTTP 전송)
#
# 두 단계로 나눈 이유: 빌드 도구와 캐시를 런타임 이미지에서 제외하기 위함.
# 의존성 설치를 소스 복사보다 먼저 두어, 코드만 바뀌면 레이어 캐시가 살아 있다.

FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# 의존성 레이어 — 소스가 바뀌어도 재사용된다
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev \
        --extra serve

COPY src/ ./src/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --extra serve

# 학습 스택이 딸려 들어오지 않았는지 빌드 시점에 못 박는다.
# 실수로 --extra rag가 섞이면 이미지가 GB 단위로 부푼다.
RUN test ! -d /app/.venv/lib/python3.12/site-packages/torch \
    || (echo "ERROR: torch가 서빙 이미지에 포함되었습니다" && exit 1)


FROM python:3.12-slim AS runtime

# 비루트 실행 — 컨테이너가 뚫려도 권한을 제한한다
RUN useradd --create-home --uid 10001 kograph

WORKDIR /app

COPY --from=builder --chown=kograph:kograph /app/.venv /app/.venv
COPY --chown=kograph:kograph src/ ./src/
COPY --chown=kograph:kograph data/universe/ ./data/universe/

# 모델 캐시(2.2GB)는 이미지에 굽지 않고 볼륨으로 마운트한다.
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH=/app/src \
    PYTHONUNBUFFERED=1 \
    KOGRAPH_PORT=8000 \
    HF_HOME=/home/kograph/.cache/huggingface \
    # torch가 없는 이미지이므로 ONNX 백엔드가 유일한 선택지다
    KOGRAPH_EMBED_BACKEND=onnx-int8 \
    KOGRAPH_ONNX_DIR=/models/bge-m3-onnx

USER kograph
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
    CMD python -c "import urllib.request as u; u.urlopen('http://127.0.0.1:8000/healthz', timeout=3)"

ENTRYPOINT ["python", "-m", "kograph.mcp_server.server"]
CMD ["--transport", "http", "--host", "0.0.0.0", "--port", "8000"]
