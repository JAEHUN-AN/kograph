"""kg_triple 스테이징 → Neo4j 지식그래프 적재.

실행: uv run python -m kograph.graph.load_neo4j

그래프 모델:
- (:Company {name, corp_code?, stock_code?, sector?, in_universe})
- (subject)-[:SUPPLIES_TO|OWNS_STAKE|... {rcept_no, amount_krw, ratio_pct, ...}]->(object)
- 관계마다 근거 공시(rcept_no)를 속성으로 보존 → 답변 시 출처 제시 가능

멱등: 노드는 name 기준 MERGE, 관계는 (양끝, 타입, rcept_no) 기준 MERGE.
"""

import json
import logging

from neo4j import GraphDatabase

from kograph.config import get_settings
from kograph.db.oracle import connect

logger = logging.getLogger(__name__)

_CONSTRAINTS = [
    "CREATE CONSTRAINT company_name IF NOT EXISTS "
    "FOR (c:Company) REQUIRE c.name IS UNIQUE",
]

_MERGE_RELATION = """
MERGE (s:Company {name: $subject})
MERGE (o:Company {name: $object})
MERGE (s)-[r:%s {rcept_no: $rcept_no}]->(o)
SET r += $props
"""


def load_universe_companies(session) -> int:
    """유니버스 종목을 Company 노드로 선적재 (섹터·종목코드 속성 부여)."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT c.corp_name, c.corp_code, c.stock_code
               FROM corp c WHERE c.stock_code IS NOT NULL AND c.in_universe = 1"""
        )
        rows = cur.fetchall()
    for corp_name, corp_code, stock_code in rows:
        session.run(
            "MERGE (c:Company {name: $name}) "
            "SET c.corp_code = $corp_code, c.stock_code = $stock_code, c.in_universe = true",
            name=corp_name, corp_code=corp_code, stock_code=stock_code,
        )
    return len(rows)


def load_triples(session) -> int:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT t.subject_name, t.predicate, t.object_name, t.props_json, t.rcept_no
               FROM kg_triple t"""
        )
        rows = []
        for subject, predicate, obj, props_json, rcept_no in cur.fetchall():
            raw = props_json.read() if hasattr(props_json, "read") else props_json
            rows.append((subject, predicate, obj, json.loads(raw) if raw else {}, rcept_no))

    count = 0
    for subject, predicate, obj, props, rcept_no in rows:
        # predicate는 kg_triple에 Enum 값으로만 저장되므로 안전하게 포맷 가능
        session.run(
            _MERGE_RELATION % predicate,
            subject=subject, object=obj, rcept_no=rcept_no, props=props,
        )
        count += 1
    return count


def main() -> None:
    s = get_settings()
    driver = GraphDatabase.driver(s.neo4j_uri, auth=(s.neo4j_user, s.neo4j_password))
    try:
        with driver.session() as session:
            for stmt in _CONSTRAINTS:
                session.run(stmt)
            n_companies = load_universe_companies(session)
            n_rels = load_triples(session)
            logger.info("loaded: %d universe companies, %d relations", n_companies, n_rels)
    finally:
        driver.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    main()
