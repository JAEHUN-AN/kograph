"""하이브리드 리트리버: 벡터 검색 + 그래프 순회.

실행: uv run python -m kograph.rag.retriever "한미반도체는 어디에 공급하나"

- vector_search: 공시 청크 의미 검색. 답이 한 문서 안에 문장으로 적혀 있는
                 질문에 강하다.
- graph_search:  질문에서 회사명을 잡아 관계를 1~2홉 순회. "A에 납품하는 회사가
                 또 어디에 납품하나"처럼 여러 공시를 이어야 답이 나오는 질문에 강하다.
- hybrid_search: 둘을 합쳐 근거를 반환.

두 축을 분리해 둔 이유는 평가 때 각각의 기여를 따로 재기 위해서다.
"""

import argparse
import logging
import re
import sys
import time
from dataclasses import dataclass, field

from neo4j import GraphDatabase

from kograph.config import get_settings
from kograph.observability import observe_retrieval
from kograph.rag.embed import connect_pg, embed_texts

logger = logging.getLogger(__name__)

DEFAULT_K = 5
MAX_HOPS = 2


@dataclass
class Evidence:
    """검색 결과 한 건. 어느 축에서 나왔는지와 근거 공시를 항상 함께 남긴다."""

    source: str            # "vector" | "graph"
    text: str
    rcept_no: str | None = None
    score: float | None = None
    entities: list[str] = field(default_factory=list)
    # 경로상의 관계 유형. 렌더된 text를 되파싱하지 않고 정렬·필터에 쓴다.
    rel_types: list[str] = field(default_factory=list)


def vector_search(
    question: str, k: int = DEFAULT_K, company: str | None = None
) -> list[Evidence]:
    """pgvector 코사인 최근접. 임베딩은 적재 때와 동일 모델·정규화를 쓴다.

    company를 주면 그 회사의 공시로만 좁힌다. 의미 유사도는 소유권을 구분하지
    못해서, "SK하이닉스 채무보증"으로 검색하면 같은 그룹 계열사 공시가 섞인다.
    """
    started = time.perf_counter()
    results = _vector_search(question, k, company)
    observe_retrieval("vector", time.perf_counter() - started, len(results))
    return results


def _vector_search(question: str, k: int, company: str | None = None) -> list[Evidence]:
    qvec = embed_texts([question])[0]
    # 필터는 반드시 ORDER BY 앞에 온다. 상위 k를 뽑고 거르면 대부분 빈 결과가 된다.
    where, params = ["embedding IS NOT NULL"], [qvec]
    if company:
        where.append("REPLACE(corp_name, ' ', '') ILIKE %s")
        params.append(f"%{company.replace(' ', '')}%")
    params += [qvec, k]

    with connect_pg() as pg, pg.cursor() as cur:
        cur.execute(
            f"""SELECT rcept_no, corp_name, content,
                       1 - (embedding <=> %s::vector) AS score
                FROM doc_chunk
                WHERE {" AND ".join(where)}
                ORDER BY embedding <=> %s::vector
                LIMIT %s""",
            tuple(params),
        )
        return [
            Evidence(
                source="vector",
                text=content,
                rcept_no=rcept_no.strip(),
                score=float(score),
                entities=[corp_name] if corp_name else [],
            )
            for rcept_no, corp_name, content, score in cur.fetchall()
        ]


def _driver():
    s = get_settings()
    return GraphDatabase.driver(s.neo4j_uri, auth=(s.neo4j_user, s.neo4j_password))


def known_companies() -> list[str]:
    """그래프에 존재하는 회사명 목록 (질문에서 엔티티를 잡을 때 사용)."""
    with _driver() as d, d.session() as s:
        return [r["name"] for r in s.run("MATCH (c:Company) RETURN c.name AS name")]


def mentioned_companies(question: str, names: list[str] | None = None) -> list[str]:
    """질문에 등장하는 회사명을 그래프 노드와 매칭.

    NER 대신 사전 매칭을 쓴다 — 대상 노드가 수백 개로 한정적이고, 정확도가
    NER보다 높으며 모델이 필요 없다. 부분 일치를 허용해 '하이닉스'로
    'SK하이닉스(SK Hynix Inc.)'를 잡는다.
    """
    names = names if names is not None else known_companies()
    q = re.sub(r"[\s()]+", "", question)
    hits = []
    for name in names:
        key = re.sub(r"[\s()]+", "", name)
        if len(key) < 2:
            continue
        if key in q or (len(key) >= 4 and key[:4] in q):
            hits.append(name)
    # 긴 이름을 우선 (더 구체적인 매칭)
    return sorted(set(hits), key=len, reverse=True)


_PATH_CYPHER = """
    MATCH path = (a:Company)-[r*{depth}..{depth}]-(b:Company)
    WHERE a.name IN $seeds AND a <> b
    RETURN DISTINCT [x IN relationships(path) | type(x)] AS rels,
           [x IN relationships(path) | startNode(x).name] AS starts,
           [x IN relationships(path) | x.rcept_no] AS rcepts,
           [n IN nodes(path) | n.name] AS names
    LIMIT $limit
"""


def _render_path(names: list[str], rels: list[str], starts: list[str]) -> str:
    """경로를 방향까지 살려 문장으로 만든다.

    순회는 무방향(-[r]-)이라 경로상의 다음 노드가 엣지의 끝점이 아닐 수 있다.
    그때 화살표를 그대로 오른쪽으로 그리면 사실이 뒤집힌다
    ('테스트하이닉스 -[OFFICER_OF]-> 홍길동'). 엣지의 실제 시작 노드와 대조해
    역방향이면 왼쪽 화살표로 표기한다.
    """
    text = names[0]
    for i, (rel, nxt) in enumerate(zip(rels, names[1:], strict=True)):
        forward = starts[i] == names[i]
        text += f" -[{rel}]-> {nxt}" if forward else f" <-[{rel}]- {nxt}"
    return text


def graph_search(question: str, hops: int = MAX_HOPS, limit: int = 60) -> list[Evidence]:
    """질문 속 회사에서 출발해 관계를 순회하고, 경로를 문장으로 반환.

    홉 길이별로 나눠 질의해 **직접 관계를 먼저** 담는다. 1..2홉을 한 번에
    조회하면 Neo4j가 임의 순서로 돌려주므로, 허브 노드(2홉 경로 700개 이상)에서
    정작 필요한 1홉 이웃이 상한 밖으로 밀려난다.
    """
    started = time.perf_counter()
    results = _graph_search(question, hops, limit)
    observe_retrieval("graph", time.perf_counter() - started, len(results))
    return results


def _graph_search(question: str, hops: int, limit: int) -> list[Evidence]:
    seeds = mentioned_companies(question)
    if not seeds:
        return []

    out: list[Evidence] = []
    seen: set[str] = set()
    with _driver() as d, d.session() as s:
        for depth in range(1, hops + 1):
            budget = limit - len(out)
            if budget <= 0:
                break
            for rec in s.run(_PATH_CYPHER.format(depth=depth), seeds=seeds[:3], limit=budget):
                names, rels, starts = rec["names"], rec["rels"], rec["starts"]
                hop_text = _render_path(names, rels, starts)
                if hop_text in seen:  # 정정공시 탓에 같은 관계가 여러 엣지로 존재
                    continue
                seen.add(hop_text)
                rcepts = [r for r in rec["rcepts"] if r]
                out.append(Evidence(
                    source="graph",
                    text=hop_text,
                    rcept_no=rcepts[0] if rcepts else None,
                    entities=list(names),
                    rel_types=list(rels),
                ))
    return out


def hybrid_search(question: str, k: int = DEFAULT_K) -> list[Evidence]:
    """벡터 + 그래프. 그래프 경로를 앞에 둔다 — 관계형 질문의 답이 여기 있다."""
    return graph_search(question) + vector_search(question, k)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("question")
    p.add_argument("--k", type=int, default=DEFAULT_K)
    p.add_argument("--mode", choices=["vector", "graph", "hybrid"], default="hybrid")
    args = p.parse_args()

    fn = {"vector": lambda q: vector_search(q, args.k),
          "graph": graph_search,
          "hybrid": lambda q: hybrid_search(q, args.k)}[args.mode]

    results = fn(args.question)
    if not results:
        print("(근거 없음)")
        sys.exit(0)
    for i, ev in enumerate(results, 1):
        head = f"[{i}] ({ev.source}"
        head += f", {ev.score:.3f})" if ev.score is not None else ")"
        body = ev.text if ev.source == "graph" else ev.text.replace("\n", " ")[:160]
        print(f"{head} {body}")
        if ev.rcept_no:
            print(f"     근거공시: {ev.rcept_no}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    main()
