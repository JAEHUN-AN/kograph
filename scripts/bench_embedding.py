"""임베딩 백엔드 벤치마크 — torch FP32 vs ONNX FP32 vs ONNX INT8 (CPU).

실행: uv run python scripts/bench_embedding.py [--n 60] [--out bench.json]

GPU가 없는 환경에서 임베딩은 파이프라인의 병목이다(초기 실측 1.1 chunks/s).
ONNX Runtime + 동적 INT8 양자화로 얼마나 회복되는지 잰다.

**속도만 재면 최적화가 아니라 성능 저하일 수 있다.** 그래서 처리량과 함께
FP32 대비 임베딩 일치도(코사인 유사도)를 같이 잰다. 검색 품질 자체는
평가셋으로 따로 확인한다.
"""

import argparse
import json
import statistics
import time
from pathlib import Path

from kograph.db.oracle import connect as oracle_connect
from kograph.rag.embed import CHUNK_CHARS, DEFAULT_MODEL, chunk_text

WARMUP = 2
BATCH = 8
# bge-m3는 2.2GB라 ONNX가 external data 형식(model.onnx + model.onnx_data)으로
# 저장된다. HF 캐시 안에서는 이 파일들이 심볼릭 링크라 양자화기가 외부 가중치를
# 열지 못한다. 실제 파일로 펼친 로컬 디렉터리에서 변환한다.
LOCAL_DIR = Path("models/bge-m3-onnx")
INT8_FILE = "onnx/model_qint8_avx512_vnni.onnx"


def sample_chunks(n: int) -> list[str]:
    """실제 공시 본문에서 벤치마크 입력을 만든다 (합성 문장은 길이 분포가 다르다)."""
    with oracle_connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT doc_text FROM filing WHERE doc_text IS NOT NULL "
            "AND LENGTH(doc_text) BETWEEN 1000 AND 20000 "
            "ORDER BY rcept_dt FETCH FIRST 80 ROWS ONLY"
        )
        chunks: list[str] = []
        for (clob,) in cur.fetchall():
            text = clob.read() if hasattr(clob, "read") else clob
            chunks.extend(chunk_text(text))
            if len(chunks) >= n:
                break
    return chunks[:n]


def model_size_mb(kind: str) -> float:
    """디스크에 올라간 가중치 크기 (ONNX는 external data 포함)."""
    from kograph.rag.embed import ONNX_FP32_FILE, ONNX_INT8_FILE

    if kind == "onnx":
        graph = LOCAL_DIR / ONNX_FP32_FILE
        data = graph.with_name(graph.name + "_data")
        total = graph.stat().st_size + (data.stat().st_size if data.exists() else 0)
        return round(total / 1024**2, 1)
    if kind == "onnx-int8":
        return round((LOCAL_DIR / ONNX_INT8_FILE).stat().st_size / 1024**2, 1)

    # torch: HF 캐시의 safetensors
    import os

    hub = Path(os.getenv("HF_HOME", Path.home() / ".cache" / "huggingface")) / "hub"
    weights = list((hub / "models--BAAI--bge-m3").rglob("*.safetensors"))
    return round(max(w.stat().st_size for w in weights) / 1024**2, 1) if weights else float("nan")


def load_backend(kind: str):
    """embed.get_model과 같은 경로를 쓴다 — 벤치마크와 실사용이 갈리면 의미가 없다."""
    from kograph.rag.embed import get_model

    return get_model(DEFAULT_MODEL, backend=kind)


def ensure_quantized() -> None:
    """로컬 ONNX 사본과 INT8 모델을 준비한다 (최초 1회, 수 분 소요)."""
    from sentence_transformers import SentenceTransformer
    from sentence_transformers.backend import export_dynamic_quantized_onnx_model

    if (LOCAL_DIR / INT8_FILE).exists():
        print(f"INT8 모델: 기존 파일 사용 ({LOCAL_DIR / INT8_FILE})")
        return

    if not (LOCAL_DIR / "onnx" / "model.onnx").exists():
        print(f"ONNX 모델을 {LOCAL_DIR}에 펼치는 중 (약 2.2GB)...", flush=True)
        LOCAL_DIR.parent.mkdir(parents=True, exist_ok=True)
        SentenceTransformer(DEFAULT_MODEL, backend="onnx", device="cpu").save(str(LOCAL_DIR))
        print("펼치기 완료", flush=True)

    print("INT8 동적 양자화 중 (수 분 소요)...", flush=True)
    base = SentenceTransformer(str(LOCAL_DIR), backend="onnx", device="cpu")
    export_dynamic_quantized_onnx_model(base, "avx512_vnni", str(LOCAL_DIR))
    print("INT8 모델 생성 완료", flush=True)


def bench(kind: str, chunks: list[str]) -> dict:
    model = load_backend(kind)
    model.encode(chunks[:WARMUP], batch_size=BATCH, normalize_embeddings=True)

    started = time.perf_counter()
    vectors = model.encode(chunks, batch_size=BATCH, normalize_embeddings=True,
                           show_progress_bar=False)
    elapsed = time.perf_counter() - started

    return {
        "backend": kind,
        "chunks": len(chunks),
        "elapsed_sec": round(elapsed, 2),
        "chunks_per_sec": round(len(chunks) / elapsed, 2),
        "ms_per_chunk": round(elapsed / len(chunks) * 1000, 1),
        "model_mb": model_size_mb(kind),
        "_vectors": vectors,
    }


def agreement(reference, other) -> dict:
    """FP32 임베딩과의 코사인 유사도 (둘 다 정규화되어 있으므로 내적)."""
    sims = [float((a * b).sum()) for a, b in zip(reference, other, strict=True)]
    return {
        "cos_sim_mean": round(statistics.fmean(sims), 5),
        "cos_sim_min": round(min(sims), 5),
    }


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--n", type=int, default=60, help="벤치마크에 쓸 청크 수")
    p.add_argument("--out", type=Path, default=None)
    args = p.parse_args()

    chunks = sample_chunks(args.n)
    lengths = [len(c) for c in chunks]
    print(f"입력: 청크 {len(chunks)}개 "
          f"(평균 {statistics.fmean(lengths):.0f}자, 상한 {CHUNK_CHARS}자)\n")

    ensure_quantized()

    results = []
    reference = None
    for kind in ("torch", "onnx", "onnx-int8"):
        print(f"[{kind}] 측정 중...", flush=True)
        row = bench(kind, chunks)
        vectors = row.pop("_vectors")
        if kind == "torch":
            reference = vectors
        else:
            row.update(agreement(reference, vectors))
        results.append(row)

    base = results[0]["chunks_per_sec"]
    print(f"\n{'백엔드':<12} {'chunks/s':>9} {'ms/청크':>9} {'가중치MB':>9}"
          f" {'속도배수':>9} {'FP32유사도':>11}")
    print("-" * 66)
    for row in results:
        speedup = row["chunks_per_sec"] / base
        sim = row.get("cos_sim_mean")
        shown = f"{sim:.5f}" if sim is not None else "(기준)"
        print(f"{row['backend']:<12} {row['chunks_per_sec']:>9.2f} {row['ms_per_chunk']:>9.1f}"
              f" {row['model_mb']:>9.1f} {speedup:>8.2f}x {shown:>11}")

    if args.out:
        args.out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n결과 저장: {args.out}")


if __name__ == "__main__":
    main()
