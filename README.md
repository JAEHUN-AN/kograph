# kograph

> GraphRAG agent & MCP server for Korean equity markets.
> DART filings → knowledge graph → multi-hop financial reasoning.

한국 주식시장(반도체·2차전지 밸류체인)의 공시·시세 데이터를 수집하고,
기업 간 관계(공급망·지분·임원 겸직)를 **지식그래프**로 구축하여,
LLM이 **MCP 툴**로 자율 조회하며 multi-hop 질문에 답하는 금융 리서치 에이전트.

**왜 GraphRAG인가?** — *"삼성전자에 HBM 소재를 납품하는 2차 협력사 중 최근 분기
실적이 개선된 곳은?"* 같은 질문은 벡터 유사도 검색으로 풀리지 않는다.
그래프 2-hop 순회 + 재무 시계열 조인이 필요하다. 이 프로젝트는 vanilla RAG와
GraphRAG를 동일 평가셋으로 정량 비교한다.

## Architecture

```
DART OpenAPI ─┐                              ┌─ Neo4j (knowledge graph)
pykrx (KRX) ──┼─ Airflow ─ Oracle 23ai ──────┤
              │   (ETL)    (raw layer)       └─ pgvector (embeddings)
              │                                     │
              └─ PySpark (factor batch)             ▼
                                     LangGraph hybrid retriever
                                              │
                                              ▼
                                    FastMCP server ⇄ Claude
                              (query_graph / search_filings / get_factors)
```

## Roadmap

- [x] **Week 1 — ETL 기반**: DART·KRX 수집기, Oracle 스키마, Airflow DAG, Spark 팩터 배치
- [ ] **Week 2 — 지식그래프 + RAG**: LLM 관계 추출 → Neo4j, 하이브리드 리트리버, 평가셋 30문항
- [ ] **Week 3 — MCP 서버 + 최적화**: FastMCP 툴 4종, 임베딩 ONNX INT8, LLM 라우팅/캐싱
- [ ] **Week 4 — MLOps**: k3s 배포, RAGAS 회귀평가 CI, Prometheus/Grafana

성능·비용 최적화 실측 기록은 [notes/](notes/)에 있다.

## Quick start

```bash
cp .env.example .env         # DART_API_KEY 등 입력
docker compose up -d          # Oracle + Neo4j + pgvector + Airflow
uv sync --extra dev           # 또는: pip install -e ".[dev]"
pytest                        # 단위 테스트
```

Airflow UI: http://localhost:8081 (admin/admin) — `dart_filings`, `krx_prices` DAG 활성화.

## Stack

Python 3.12 · Airflow 2.10 · Oracle 23ai Free · PySpark · Neo4j 5 · pgvector ·
LangGraph · FastMCP · Claude API

## Data sources

- [DART OpenAPI](https://opendart.fss.or.kr) — 공시 (금융감독원)
- [pykrx](https://github.com/sharebook-kr/pykrx) — KRX 시세

## License

MIT
