"""공시 본문 청킹 + 로컬 CPU 임베딩 -> pgvector 적재.

실행: uv run python -m kograph.rag.embed [--limit N] [--model NAME]

GPU가 없으므로 sentence-transformers를 CPU에서 돌린다. 기본 모델 bge-m3는
한국어에 강하고 1024차원이라 doc_chunk.embedding 정의와 맞다.
(Week 3에서 이 모델을 ONNX INT8로 양자화해 처리량을 비교한다.)
"""

import argparse
import logging
import os
import re
import time
from functools import lru_cache
from pathlib import Path

import psycopg
from pgvector.psycopg import register_vector

from kograph.db.oracle import connect as oracle_connect

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "BAAI/bge-m3"
EMBED_DIM = 1024
CHUNK_CHARS = 800
CHUNK_OVERLAP = 100
BATCH_SIZE = 32
_WS = re.compile(r"[ \t]+")


def pg_dsn() -> str:
    return (
        f"host={os.getenv('POSTGRES_HOST', 'localhost')} "
        f"port={os.getenv('POSTGRES_PORT', '5432')} "
        f"dbname={os.getenv('POSTGRES_DB', 'kograph')} "
        f"user={os.getenv('POSTGRES_USER', 'kograph')} "
        f"password={os.getenv('POSTGRES_PASSWORD', 'change-me-local-only')}"
    )


def connect_pg() -> psycopg.Connection:
    conn = psycopg.connect(pg_dsn())
    register_vector(conn)
    return conn


# 백엔드는 환경변수로 바꾼다 — 평가·적재 스크립트를 고치지 않고 전환하기 위함.
#   torch(기본) | onnx | onnx-int8
EMBED_BACKEND = os.getenv("KOGRAPH_EMBED_BACKEND", "torch")
ONNX_DIR = Path(os.getenv("KOGRAPH_ONNX_DIR", "models/bge-m3-onnx"))
ONNX_FP32_FILE = "onnx/model.onnx"
ONNX_INT8_FILE = "onnx/model_qint8_avx512_vnni.onnx"
MAX_SEQ_TOKENS = 1024  # 800자 청크면 넉넉하다


class OnnxEmbedder:
    """onnxruntime 직접 추론 — SentenceTransformer와 같은 encode() 인터페이스.

    SentenceTransformer가 내보낸 ONNX는 풀링·정규화까지 그래프에 포함해
    출력이 `sentence_embedding`이다. optimum은 원시 `last_hidden_state`를
    기대하므로 그 경로로는 로드되지 않는다. 어차피 후처리가 그래프 안에 있는
    편이 빠르므로 런타임을 직접 쓴다.
    """

    def __init__(self, model_dir: Path, file_name: str):
        import onnxruntime as ort
        from transformers import AutoTokenizer

        path = model_dir / file_name
        if not path.exists():
            raise FileNotFoundError(
                f"ONNX 모델이 없습니다: {path}\n"
                "먼저 'uv run python scripts/bench_embedding.py'로 생성하세요."
            )
        options = ort.SessionOptions()
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        self.session = ort.InferenceSession(
            str(path), options, providers=["CPUExecutionProvider"]
        )
        self.tokenizer = AutoTokenizer.from_pretrained(str(model_dir))
        self._inputs = {i.name for i in self.session.get_inputs()}

    def encode(self, texts, batch_size: int = 32, normalize_embeddings: bool = True, **_):
        import numpy as np

        out = []
        for start in range(0, len(texts), batch_size):
            batch = list(texts[start:start + batch_size])
            enc = self.tokenizer(
                batch, padding=True, truncation=True,
                max_length=MAX_SEQ_TOKENS, return_tensors="np",
            )
            feed = {k: v for k, v in enc.items() if k in self._inputs}
            vectors = self.session.run(["sentence_embedding"], feed)[0]
            if normalize_embeddings:
                norms = np.linalg.norm(vectors, axis=1, keepdims=True)
                vectors = vectors / np.maximum(norms, 1e-12)
            out.append(vectors)
        return np.vstack(out) if out else np.empty((0, EMBED_DIM), dtype="float32")


@lru_cache(maxsize=3)
def get_model(name: str = DEFAULT_MODEL, backend: str = ""):
    """모델 로딩은 수 초~수십 초 걸리므로 (모델, 백엔드)당 한 번만."""
    backend = backend or EMBED_BACKEND
    logger.info("loading embedding model %s (cpu, backend=%s)", name, backend)

    if backend == "onnx":
        return OnnxEmbedder(ONNX_DIR, ONNX_FP32_FILE)
    if backend == "onnx-int8":
        return OnnxEmbedder(ONNX_DIR, ONNX_INT8_FILE)

    from sentence_transformers import SentenceTransformer

    return SentenceTransformer(name, device="cpu")


def chunk_text(text: str, size: int = CHUNK_CHARS, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """줄 경계를 살린 고정 길이 청킹.

    공시는 '라벨/값'이 줄 단위라 문장 분할보다 줄 묶음이 문맥을 덜 깬다.
    """
    lines = [_WS.sub(" ", ln).strip() for ln in text.splitlines()]
    lines = [ln for ln in lines if ln]
    chunks: list[str] = []
    buf: list[str] = []
    length = 0
    for line in lines:
        if length + len(line) > size and buf:
            chunks.append("\n".join(buf))
            # overlap만큼 뒤에서 되감아 문맥을 잇는다
            back, kept = 0, []
            for prev in reversed(buf):
                if back >= overlap:
                    break
                kept.insert(0, prev)
                back += len(prev)
            buf, length = kept, back
        buf.append(line)
        length += len(line)
    if buf:
        chunks.append("\n".join(buf))
    return chunks


def embed_texts(
    texts: list[str], model_name: str = DEFAULT_MODEL, backend: str = ""
) -> list[list[float]]:
    """정규화된 임베딩 반환 (코사인 거리 사용 전제)."""
    model = get_model(model_name, backend)
    vectors = model.encode(
        texts,
        batch_size=BATCH_SIZE,
        normalize_embeddings=True,
        show_progress_bar=False,
    )
    return [v.tolist() for v in vectors]


def pending_filings(limit: int | None) -> list[tuple]:
    """본문이 있고 아직 청크가 없는 공시."""
    with connect_pg() as pg, pg.cursor() as pcur:
        pcur.execute("SELECT DISTINCT rcept_no FROM doc_chunk")
        done = {r[0].strip() for r in pcur.fetchall()}

    sql = """
        SELECT rcept_no, corp_name, report_nm, rcept_dt, doc_text
        FROM filing WHERE doc_text IS NOT NULL ORDER BY rcept_dt
    """
    rows = []
    with oracle_connect() as conn, conn.cursor() as cur:
        cur.execute(sql)
        for rcept_no, corp_name, report_nm, rcept_dt, doc_text in cur.fetchall():
            if rcept_no.strip() in done:
                continue
            text = doc_text.read() if hasattr(doc_text, "read") else doc_text
            rows.append((rcept_no, (corp_name or "").strip(),
                         (report_nm or "").strip(), rcept_dt, text))
            if limit and len(rows) >= limit:
                break
    return rows


def run(limit: int | None = None, model_name: str = DEFAULT_MODEL) -> tuple[int, int]:
    """Returns (처리 공시 수, 적재 청크 수)."""
    todo = pending_filings(limit)
    logger.info("to embed: %d filings", len(todo))
    if not todo:
        return 0, 0

    docs = 0
    total_chunks = 0
    started = time.monotonic()
    with connect_pg() as pg:
        with pg.cursor() as cur:
            for rcept_no, corp_name, report_nm, rcept_dt, text in todo:
                chunks = chunk_text(text)
                if not chunks:
                    continue
                vectors = embed_texts(chunks, model_name)
                cur.executemany(
                    """INSERT INTO doc_chunk
                       (rcept_no, corp_name, report_nm, rcept_dt, chunk_idx, content, embedding)
                       VALUES (%s, %s, %s, %s, %s, %s, %s)
                       ON CONFLICT (rcept_no, chunk_idx) DO NOTHING""",
                    [(rcept_no, corp_name, report_nm, rcept_dt, i, c, v)
                     for i, (c, v) in enumerate(zip(chunks, vectors, strict=True))],
                )
                docs += 1
                total_chunks += len(chunks)
                if docs % 25 == 0:
                    pg.commit()
                    elapsed = time.monotonic() - started
                    logger.info("progress %d/%d docs, %d chunks, %.0fs (%.1f chunks/s)",
                                docs, len(todo), total_chunks, elapsed,
                                total_chunks / max(elapsed, 1e-9))
        pg.commit()

    elapsed = time.monotonic() - started
    logger.info("done: %d docs, %d chunks, %.0fs (%.1f chunks/s)",
                docs, total_chunks, elapsed, total_chunks / max(elapsed, 1e-9))
    return docs, total_chunks


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--model", default=DEFAULT_MODEL)
    args = p.parse_args()
    run(args.limit, args.model)
