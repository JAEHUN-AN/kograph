"""규칙 파서 정밀도 측정용 표본을 뽑는다 (노트 005).

실행: uv run python scripts/sample_for_labeling.py [--target 200] [--seed 42]

커버리지(78%)는 이미 알지만 정밀도는 잰 적이 없다. 추출된 642개 중 몇 개가
실제로 맞는지 모르는 채로 "규칙 파서가 LLM보다 정확하다"고 주장해 왔다.

라벨러가 파서 논리를 알면 자기 출력을 합리화한다. 그래서 각 트리플에 **공시
원문 근거**를 함께 담아, 코드가 아니라 원문만 보고 판정할 수 있게 만든다.
"""

from __future__ import annotations

import argparse
import json
import random
from pathlib import Path

OUT = Path("data/eval/parser_precision.jsonl")
EVIDENCE_WINDOW = 400   # 대상 이름 앞뒤로 볼 글자 수
MIN_PER_PREDICATE = 20  # 희소 술어도 정밀도를 낼 수 있을 만큼은 뽑는다


def _evidence(doc_text: str, subject: str, object_name: str,
              corp_name: str) -> tuple[str, bool]:
    """판정에 필요한 원문 구간. (근거, 상대측_이름을_원문에서_찾음)

    제출사 이름을 기준으로 창을 잡으면 안 된다. 공시 맨 앞 '1. 발행회사 정보'에
    항상 나오므로 창이 머리말에 걸리고, 정작 판정에 필요한 기록 구간이 빠진다.
    OFFICER_OF/RELATED_PARTY_OF는 대상이 곧 제출사라 특히 그렇다.

    그래서 **제출사가 아닌 쪽**을 기준으로 잡는다. 양쪽 다 제출사가 아니면
    두 구간을 모두 담는다.
    """
    text = " ".join((doc_text or "").split())
    filer = (corp_name or "").replace(" ", "")

    def _is_filer(name: str) -> bool:
        n = (name or "").replace(" ", "")
        return bool(n) and (n in filer or filer in n)

    targets = [n for n in (subject, object_name) if n and not _is_filer(n)]
    if not targets:  # 양쪽 다 제출사 표기 — 드물지만 방어
        targets = [n for n in (object_name, subject) if n]

    spans, found = [], False
    for name in targets:
        pos = text.find(name)
        if pos < 0:
            continue
        found = True
        spans.append((max(0, pos - EVIDENCE_WINDOW),
                      min(len(text), pos + len(name) + EVIDENCE_WINDOW)))
    if not spans:
        return text[: EVIDENCE_WINDOW * 2], False

    merged = []                      # 겹치는 구간은 합쳐 중복 인용을 막는다
    for a, b in sorted(spans):
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return " […] ".join(text[a:b] for a, b in merged), found


def _allocation(counts: dict[str, int], target: int) -> dict[str, int]:
    """층화 배분. 희소 술어에 최소 인원을 보장한다.

    비례 배분만 하면 GUARANTEES_DEBT_OF가 12건이라 정밀도 추정이 흔들린다.
    대신 전체 정밀도는 실제 분포로 재가중해야 한다(노트에 기록).
    """
    total = sum(counts.values())
    return {
        p: min(n, max(MIN_PER_PREDICATE, round(n * target / total)))
        for p, n in counts.items()
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", type=int, default=200)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    from kograph.db.oracle import connect

    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT predicate, COUNT(*) FROM kg_triple GROUP BY predicate")
        counts = dict(cur.fetchall())

        cur.execute("""
            SELECT t.triple_id, t.rcept_no, t.subject_name, t.predicate,
                   t.object_name, t.props_json,
                   f.corp_name, f.report_nm, f.doc_text
            FROM kg_triple t JOIN filing f ON f.rcept_no = t.rcept_no
            ORDER BY t.triple_id
        """)
        rows = []
        for tid, rcept, subj, pred, obj, props, corp, report, doc in cur.fetchall():
            rows.append((
                tid, rcept, subj, pred, obj,
                props.read() if hasattr(props, "read") else props,
                corp, report,
                doc.read() if hasattr(doc, "read") else doc,
            ))

    alloc = _allocation(counts, args.target)
    by_pred: dict[str, list] = {}
    for r in rows:
        by_pred.setdefault(r[3], []).append(r)

    rng = random.Random(args.seed)
    picked = []
    for pred, want in sorted(alloc.items()):
        pool = by_pred.get(pred, [])
        picked += rng.sample(pool, min(want, len(pool)))
    rng.shuffle(picked)  # 라벨링 중 술어 순서를 보고 추측하지 않도록 섞는다

    OUT.parent.mkdir(parents=True, exist_ok=True)
    missing = 0
    with OUT.open("w", encoding="utf-8") as f:
        for i, (tid, rcept, subj, pred, obj, props, corp, report, doc) in enumerate(picked, 1):
            evidence, found = _evidence(doc, subj, obj, corp)
            missing += not found
            f.write(json.dumps({
                "id": i,
                "triple_id": tid,
                "rcept_no": rcept,
                "corp_name": corp,
                "report_nm": report,
                "subject": subj,
                "predicate": pred,
                "object": obj,
                "props": json.loads(props) if props else {},
                "evidence": evidence,
                "name_in_text": found,
                "label": None,       # True=정확, False=오류
                # 오류일 때만: wrong_object / wrong_direction /
                # hallucinated / wrong_predicate / wrong_amount
                "error_kind": "",
                "note": "",
            }, ensure_ascii=False) + "\n")

    print(f"표본 {len(picked)}건 -> {OUT}")
    print(f"{'술어':22}{'모집단':>7}{'표본':>7}{'비율':>8}")
    for pred in sorted(alloc):
        n, k = counts[pred], sum(1 for r in picked if r[3] == pred)
        print(f"  {pred:20}{n:>7}{k:>7}{k/n*100:>7.0f}%")
    print(f"  {'합계':20}{sum(counts.values()):>7}{len(picked):>7}")
    if missing:
        print(f"\n원문에서 이름을 못 찾은 표본 {missing}건 — 오추출 후보다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
