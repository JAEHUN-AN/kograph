"""vanilla RAG vs GraphRAG 검색 성능 비교.

실행: uv run python -m kograph.rag.evaluate [--k 5] [--out results.json]

**생성이 아니라 검색을 측정한다.** 답변 생성에는 LLM이 필요하지만, 두 방식이
갈리는 지점은 검색 단계다. 문항마다 정답 엔티티를 라벨링해 두고 "검색 결과
안에 정답이 들어왔는가"(hit)를 재면, LLM 없이도 핵심 비교가 성립하고
리트리버만 분리해 평가할 수 있다.

지표:
  hit@k  — 정답 엔티티가 검색 근거에 하나라도 등장한 비율
  recall — 문항의 정답 엔티티 중 몇 개를 회수했는지의 평균
"""

import argparse
import json
import logging
import re
import time
from pathlib import Path

from kograph.graph.names import canonical_name
from kograph.rag.retriever import graph_search, hybrid_search, vector_search

logger = logging.getLogger(__name__)

EVAL_PATH = Path("data/eval/multihop.jsonl")
_NOISE = re.compile(r"[\s()·,.]+")


def _key(name: str) -> str:
    """비교용 키 — 정규화 + 공백/구두점 제거 + 소문자."""
    return _NOISE.sub("", canonical_name(name)).lower()


def load_questions(path: Path = EVAL_PATH) -> list[dict]:
    with path.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def matched_golds(evidences, golds: list[str]) -> list[str]:
    """검색 근거 텍스트에 등장한 정답 엔티티 목록."""
    haystack = _NOISE.sub("", " ".join(e.text for e in evidences)).lower()
    return [g for g in golds if _key(g) and _key(g) in haystack]


def evaluate_one(q: dict, k: int) -> dict:
    golds = q["gold"]
    modes = {
        "vector": vector_search(q["question"], k),
        "graph": graph_search(q["question"]),
        "hybrid": hybrid_search(q["question"], k),
    }
    row = {"id": q["id"], "type": q["type"], "question": q["question"], "n_gold": len(golds)}
    for name, evidences in modes.items():
        hits = matched_golds(evidences, golds)
        row[f"{name}_hit"] = bool(hits)
        row[f"{name}_recall"] = len(hits) / len(golds) if golds else 0.0
        row[f"{name}_matched"] = hits
    return row


def summarize(rows: list[dict]) -> dict:
    def agg(subset: list[dict]) -> dict:
        if not subset:
            return {}
        return {
            mode: {
                "hit@k": sum(r[f"{mode}_hit"] for r in subset) / len(subset),
                "recall": sum(r[f"{mode}_recall"] for r in subset) / len(subset),
            }
            for mode in ("vector", "graph", "hybrid")
        }

    return {
        "all": agg(rows),
        "multi_hop": agg([r for r in rows if r["type"] == "multi_hop"]),
        "single_hop": agg([r for r in rows if r["type"] == "single_hop"]),
        "n": {"all": len(rows),
              "multi_hop": sum(r["type"] == "multi_hop" for r in rows),
              "single_hop": sum(r["type"] == "single_hop" for r in rows)},
    }


def _mark(hit: bool) -> str:
    return "  O  " if hit else "  X  "


def _print_report(rows: list[dict], summary: dict, elapsed: float) -> None:
    print(f"\n{'ID':<6} {'유형':<11} {'vector':>7} {'graph':>7} {'hybrid':>7}  질문")
    print("-" * 96)
    for r in rows:
        print(f"{r['id']:<6} {r['type']:<11} "
              f"{_mark(r['vector_hit']):>7} {_mark(r['graph_hit']):>7} {_mark(r['hybrid_hit']):>7}"
              f"  {r['question'][:48]}")

    print(f"\n{'구간':<12} {'n':>3}  {'vector hit':>11} {'graph hit':>10} {'hybrid hit':>11}"
          f" {'vector rec':>11} {'graph rec':>10} {'hybrid rec':>11}")
    print("-" * 96)
    for label, key in (("전체", "all"), ("multi-hop", "multi_hop"), ("single-hop", "single_hop")):
        s, n = summary[key], summary["n"][key]
        if not s:
            continue
        print(f"{label:<12} {n:>3}  "
              f"{s['vector']['hit@k']:>10.0%} {s['graph']['hit@k']:>10.0%}"
              f" {s['hybrid']['hit@k']:>10.0%}"
              f" {s['vector']['recall']:>10.0%} {s['graph']['recall']:>10.0%}"
              f" {s['hybrid']['recall']:>10.0%}")
    print(f"\n소요 {elapsed:.0f}초")


def run(k: int = 5, out: Path | None = None) -> dict:
    questions = load_questions()
    logger.info("evaluating %d questions", len(questions))

    started = time.monotonic()
    rows = [evaluate_one(q, k) for q in questions]
    elapsed = time.monotonic() - started

    summary = summarize(rows)
    _print_report(rows, summary, elapsed)

    if out:
        out.write_text(
            json.dumps({"summary": summary, "rows": rows, "k": k,
                        "elapsed_sec": round(elapsed, 1)},
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"결과 저장: {out}")
    return summary


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    p = argparse.ArgumentParser()
    p.add_argument("--k", type=int, default=5)
    p.add_argument("--out", type=Path, default=None)
    args = p.parse_args()
    run(args.k, args.out)
