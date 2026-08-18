# kograph

> 한국 주식시장 공시로 지식그래프를 만들고, LLM이 MCP 도구로 직접 조회하는
> 금융 리서치 백엔드.

반도체·2차전지 밸류체인 16개 종목의 DART 공시에서 기업 간 관계(공급·지분·
채무보증·모자·임원)를 추출해 지식그래프를 만들고, 벡터 검색과 그래프 순회를
함께 쓰는 하이브리드 리트리버 위에 MCP 서버를 올렸다.

## 왜 그래프인가

**한 회사의 관계는 여러 공시에 흩어져 있다.** 벡터 검색은 그중 하나가 담긴
청크를 잘 찾지만 나머지를 놓친다. 그래서 "SK하이닉스에 납품하는 회사를 모두"
같은 질문에 한 곳만 답한다.

30문항 평가셋으로 재보니 이 차이가 recall에서 드러났다.

| | hit@k | **recall** |
|---|---|---|
| vector only | 70% | **56%** |
| graph only | 97% | **97%** |
| hybrid | 100% | **100%** |

예상 밖은 **single-hop 질문에서도 그래프가 앞섰다**는 점(87% vs 100%)이다.
홉 수가 아니라 **정답이 몇 개 문서에 흩어져 있는가**가 갈림길이었다.
→ [기술노트 002](notes/002-graphrag-vs-vector-eval.md)

## 측정 결과

| 항목 | 값 | 근거 |
|---|---|---|
| 관계 추출 | 공시 410/509건(80.5%), 트리플 685개, **0.4초, 비용 0원** | [001](notes/001-rule-vs-llm-extraction.md) |
| 추출 정밀도 | 수동 라벨 212건, **95.0%** (모집단 재가중) | [005](notes/005-parser-precision.md) |
| 검색 recall | vector 56% → **hybrid 100%** | [002](notes/002-graphrag-vs-vector-eval.md) |
| 임베딩 처리량 | FP32 1.43 → **INT8 2.91 chunks/s**, 가중치 2,166 → **544MB**, 검색 품질 동일 | [003](notes/003-onnx-int8-quantization.md) |
| 서빙 이미지 | 8.8GB → **913MB** | [004](notes/004-serving-deployment.md) |

데이터 규모: 공시 2,192건 · 시세 21,284행 · 벡터 청크 8,934개 ·
그래프 노드 186/관계 394.

Oracle에는 트리플 685건(공시 건별 사실)이 있고 Neo4j 관계는 394개다. 모자·임원
같은 **상태**는 언급한 공시 수만큼 중복 저장하지 않는다 — 자세한 이유는
[005](notes/005-parser-precision.md).

## 설계에서 갈렸던 지점 세 가지

**1. 관계 추출에 LLM을 쓰지 않았다.** DART 주요사항보고서는 고정 양식이라
라벨-값 구조가 일정하다. 이런 입력에서는 정규식 파서가 LLM보다 **정확하다** —
환각이 없고 결정론적이다. LLM 경로(`graph/extract.py`)는 비정형 잔여분과 품질
벤치마크용으로 남겨뒀다.

**2. ONNX 자체는 오히려 느렸다.** "ONNX로 바꾸면 빨라진다"는 기대와 달리
ONNX FP32는 torch보다 11% 느렸다(1.27 vs 1.43 chunks/s). 이득은 전부
**양자화**에서 나왔다. 여기서 측정을 멈췄다면 성능을 떨어뜨린 채 개선했다고
보고했을 것이다.

**3. 양자화 이득이 배포까지 이어졌다.** INT8 추론은 onnxruntime만 있으면 되므로
서빙 이미지에서 PyTorch를 통째로 뺄 수 있었다(8.8GB → 913MB). 학습 스택은
모델을 *만들 때* 필요하지 *쓸 때* 필요하지 않다.

## 만들면서 잡은 결함

측정과 검증이 실제로 무엇을 잡아냈는지가 이 프로젝트의 핵심이다.

| 결함 | 어떻게 드러났나 |
|---|---|
| 채무보증액 대신 **차입 원금**을 기록 | 단위 테스트 (다중 라벨 우선순위 버그) |
| `처분결정`에 `INVESTS_IN` 부여 — **사실과 정반대** | 코드 리뷰 |
| 표 머리글 `생년월일 또는 사업자등록번호`가 **최다 연결 노드** | 적재 후 그래프 통계 점검 |
| `SK하이닉스`와 `SK하이닉스(SK Hynix Inc.)`가 별개 노드 → **2-hop 단절** | 그래프 점검 |
| 관계 방향이 뒤집혀 렌더 (`A -[OFFICER_OF]-> 사람`) | MCP 클라이언트 왕복 출력 확인 |
| 허브 노드에서 정답이 `LIMIT` 밖으로 밀려남 (702경로 중 223번째) | 평가셋 실패 4문항 추적 |
| **`OFFICER_OF` 323건 중 임원은 129건뿐** — 법인·친인척까지 임원으로 표기 | 저장해두고 안 보던 `note` 필드의 값 분포 집계 |

마지막 세 건은 **단위 테스트로는 잡히지 않았다.** 실제 출력을 읽고, 적재 후
통계를 보고, 저장만 해둔 필드를 열어봐야 보였다.

가장 늦게 잡힌 건 마지막 항목이다. 최대주주 변동 공시의 신고 대상에는 임원뿐
아니라 친인척·계열사·재단이 들어오는데 전부 `OFFICER_OF`로 묶고 있었다.
그래프 관계의 절반이 이 술어라 **`한미컴퍼니가 한미반도체의 임원`** 같은 관계가
50%를 차지했다. 공시의 '관계' 값을 `note`에 저장해두고 한 번도 집계해보지
않은 것이 원인이었다. `RELATED_PARTY_OF`로 분리한 뒤 엣지 수는 642로 동일하고
검색 성능도 변하지 않았다 — **구조가 아니라 의미만 틀려 있었고, 그래서 어떤
성능 지표로도 드러나지 않았다.**

## 아키텍처

```
DART OpenAPI ─┐
              ├─ Airflow ─→ Oracle 23ai ─┬─→ 규칙 파서 ─→ Neo4j (그래프)
pykrx (KRX) ──┘   (증분 ETL)  (원천)      └─→ 청킹+임베딩 ─→ pgvector
                                                    │
                                    하이브리드 리트리버 (벡터 + 그래프 순회)
                                                    │
                                          MCP 서버 (stdio / HTTP)
                                                    │
                                    Claude Desktop        Prometheus ─ Grafana
```

## MCP 서버

```bash
uv run python -m kograph.mcp_server.server                     # stdio
uv run python -m kograph.mcp_server.server --transport http    # 컨테이너 배포
uv run python scripts/verify_mcp.py                            # 클라이언트 왕복 검증
```

| 도구 | 언제 호출되나 |
|---|---|
| `graph_overview` | 데이터에 뭐가 있는지 모를 때 |
| `list_companies` | 조회 가능한 종목·섹터 확인 |
| `get_company_relations` | 공급·지분·보증·모자 관계 (1~2홉) |
| `find_connection` | 두 회사의 연결 경로·공통 거래처 |
| `search_filings` | 계약 조건·투자 목적 등 서술형 내용 (회사 한정 가능) |
| `get_price_series` | 일별 시세·기간 수익률 + **같은 기간의 공시** |

**모든 결과에 근거 공시번호가 붙는다.** 금융 도메인에서 출처를 못 대는 답변은
쓸 수 없다. 도구 설명에는 "무엇을 하는지"가 아니라 **"언제 호출해야 하는지"**를
적었다 — 최신 모델일수록 도구를 보수적으로 고르기 때문이다.

설계 판단의 근거와 재현 절차는 별도 문서로 정리했다:
**[MCP 서버 설계·구현 매뉴얼](docs/mcp-server-guide.md)** — 도구 경계 정하기,
설명 쓰는 법, 응답 설계, 계측, 배포, 출시 전 점검표.

### Claude Desktop 연결

`claude_desktop_config.json` (macOS `~/Library/Application Support/Claude/`,
Windows `%APPDATA%\Claude\`):

```json
{
  "mcpServers": {
    "kograph": {
      "command": "uv",
      "args": ["--directory", "/path/to/kograph",
               "run", "python", "-m", "kograph.mcp_server.server"]
    }
  }
}
```

## 배포와 관측

```bash
docker compose up -d kograph-mcp prometheus grafana
uv run python scripts/verify_http.py   # 헬스체크·메트릭·카운터 증가 검증
```

| 주소 | 용도 |
|---|---|
| `:8000/mcp` | MCP (streamable-http) |
| `:8000/metrics` | Prometheus 메트릭 |
| `:9090` | Prometheus |
| `:3001` | Grafana (대시보드 자동 프로비저닝) |

계측은 볼 것만 골랐다. 그중 **검색 축별 반환 건수**가 중요한데, 색인이 비면
검색은 *빠르게* 아무것도 못 찾아서 지연 대시보드만 보면 오히려 건강해 보이기
때문이다.

실측(컨테이너): 그래프 순회 p95 **48ms**, 벡터 검색 p95 **248ms**. 후자가
INT8 벤치마크(344ms/청크)와 맞물려 **벡터 지연 = 임베딩 비용**임을 보여준다.

`/healthz`는 프로세스만 확인하고 DB를 건드리지 않는다. 의존 서비스까지
검사하면 DB가 잠깐 흔들릴 때 쿠버네티스가 멀쩡한 파드를 재시작한다.

## Quick start

```bash
cp .env.example .env          # DART_API_KEY 발급 후 입력
docker compose up -d          # Oracle + Neo4j + pgvector + Airflow
uv sync --extra dev --extra graph --extra rag --extra mcp
uv run pytest                 # 단위 테스트 72건 (DB 불필요)
```

파이프라인 순서:

```bash
# 1. 수집 — Airflow UI(:8081, admin/admin)에서 dart_filings, krx_prices 실행
# 2. 공시 본문
uv run python -m kograph.pipelines.doc_text
# 3. 관계 추출 → 그래프
uv run python -m kograph.graph.rules
uv run python -m kograph.graph.load_neo4j
# 4. 임베딩 → pgvector
uv run python -m kograph.rag.embed
# 5. 평가
uv run python -m kograph.rag.evaluate
```

## 기술 스택

| 계층 | 사용 |
|---|---|
| 수집·오케스트레이션 | Airflow 2.10, DART OpenAPI, pykrx |
| 저장 | Oracle 23ai Free(원천), Neo4j 5(그래프), pgvector(임베딩) |
| 검색 | bge-m3 임베딩(ONNX INT8, CPU), 자체 하이브리드 리트리버 |
| 서빙 | MCP SDK 2.0, Starlette/uvicorn |
| 운영 | Docker, Prometheus, Grafana, Kubernetes(kustomize), GitHub Actions |

## 범위와 한계

코드가 있는 것과 검증된 것은 다르므로 구분해 적는다.

**검증됨** — ETL 2개 DAG 실전 실행, 규칙 파서(테스트 72건), 지식그래프 적재,
하이브리드 검색 + 30문항 평가, MCP 서버(stdio·HTTP 양쪽 클라이언트 왕복),
ONNX INT8 벤치마크, 컨테이너 + Prometheus 수집 실측.

**코드만 있고 미검증** — PySpark 팩터 배치(작성만, 미실행), LLM 관계 추출
(API 크레딧 부족으로 미실행), k8s 매니페스트(kustomize 렌더까지만, 클러스터
미적용).

**알려진 한계** — 파서가 못 읽은 공시 99건의 관계는 그래프에 없고, 어떤
리트리버도 없는 사실은 찾지 못한다. 평가셋 30문항은 작고 정답을 같은
그래프에서 도출했으므로 hybrid 100%는 "이 시스템이 금융 질문을 다 푼다"가
아니라 **"적재된 관계에 대한 질문이면 회수해 온다"**는 뜻이다.

## 기술노트

| | |
|---|---|
| [000](notes/000-scope.md) | 스코프와 제약 정의 (GPU 없음 → 최적화 축 이동) |
| [001](notes/001-rule-vs-llm-extraction.md) | 관계 추출: LLM 대신 규칙 파서를 택한 이유 |
| [002](notes/002-graphrag-vs-vector-eval.md) | GraphRAG vs vanilla RAG 검색 성능 실측 |
| [003](notes/003-onnx-int8-quantization.md) | ONNX INT8 양자화: 처리량 2배, 품질 손실 0 |
| [004](notes/004-serving-deployment.md) | 서빙 배포와 관측: 이미지 8.8GB → 913MB |
| [005](notes/005-parser-precision.md) | 규칙 파서 정밀도 95.0% — 오류의 91%가 정정공시 |

## 데이터 출처

- [DART OpenAPI](https://opendart.fss.or.kr) — 전자공시 (금융감독원)
- [pykrx](https://github.com/sharebook-kr/pykrx) — KRX 시세

공개된 전자공시 정보만 사용하며, 투자 판단의 근거로 쓸 수 없다.

## License

MIT
