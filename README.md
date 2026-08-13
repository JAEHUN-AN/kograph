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
- [x] **Week 2 — 지식그래프 + RAG**: 규칙 기반 관계 추출 → Neo4j, 하이브리드 리트리버, 평가셋 30문항
- [x] **Week 3a — MCP 서버**: 도구 6종, stdio 통합 검증
- [ ] **Week 3b — 최적화**: 임베딩 ONNX INT8, LLM 라우팅/캐싱
- [ ] **Week 4 — MLOps**: k3s 배포, 회귀평가 CI, Prometheus/Grafana

측정 결과와 그 과정에서 잡은 결함은 [notes/](notes/)에 있다.

| 지표 | 값 |
|---|---|
| 관계 추출 | 공시 396/509건, 트리플 642개, 0.4초, 비용 0원 ([001](notes/001-rule-vs-llm-extraction.md)) |
| 검색 recall | vector 56% → hybrid 98% ([002](notes/002-graphrag-vs-vector-eval.md)) |
| 지식그래프 | 노드 184, 관계 642 |
| 벡터 인덱스 | 공시 2,178건 → 8,934 청크 (bge-m3, CPU) |

## MCP 서버

공시 지식그래프를 LLM이 직접 조회하도록 도구로 노출한다.

```bash
uv run python -m kograph.mcp_server.server   # stdio
uv run python scripts/verify_mcp.py          # 클라이언트로 왕복 검증
```

| 도구 | 언제 쓰나 |
|---|---|
| `graph_overview` | 데이터에 뭐가 있는지 모를 때 |
| `list_companies` | 조회 가능한 종목·섹터 확인 |
| `get_company_relations` | 공급·지분·보증·모자 관계 조회 (1~2홉) |
| `find_connection` | 두 회사가 어떻게 엮이는지, 공통 거래처 |
| `search_filings` | 계약 조건·투자 목적 등 서술형 내용 |
| `get_price_series` | 일별 시세·기간 수익률 |

모든 결과에 근거 공시번호가 붙어 모델이 출처를 인용할 수 있다.

### Claude Desktop 연결

`claude_desktop_config.json`에 추가한다 (macOS는
`~/Library/Application Support/Claude/`, Windows는 `%APPDATA%\Claude\`).

```json
{
  "mcpServers": {
    "kograph": {
      "command": "uv",
      "args": ["--directory", "C:\\workspace\\kograph",
               "run", "python", "-m", "kograph.mcp_server.server"]
    }
  }
}
```

`docker compose up -d`로 DB가 떠 있어야 한다.

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
