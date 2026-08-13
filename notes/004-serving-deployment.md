# 기술노트 4 — 서빙 배포와 관측: 이미지 8.8GB → 913MB

## 무엇을 만들었나

MCP 서버는 stdio 전용이라 배포·모니터링 대상이 될 수 없었다. HTTP 표면을
붙이고, 컨테이너로 묶고, 메트릭을 노출해 Prometheus·Grafana로 관측 가능하게
만들었다.

- `--transport http`: streamable-http 전송 + `/healthz` + `/metrics`
- 멀티스테이지 Dockerfile, 비루트 실행
- docker compose에 kograph-mcp / prometheus / grafana 추가
- k8s 매니페스트(Deployment·Service·ConfigMap·Secret·PVC) + kustomize
- GitHub Actions CI: 린트, 테스트 65건, 이미지 빌드·검증

## 첫 이미지가 8.8GB였다

빌드는 성공했지만 서빙 이미지로 쓸 수 없는 크기였다. 원인은 `rag` extra가
**PyTorch와 sentence-transformers 전 스택**을 끌고 오는 것이었다.

그런데 노트 003에서 만든 `OnnxEmbedder`는 torch도 sentence-transformers도
쓰지 않는다. onnxruntime과 토크나이저만 있으면 된다. 학습 스택은 모델을
**만들 때** 필요하지 **쓸 때** 필요하지 않다.

서빙 전용 `serve` extra를 만들어 학습 스택을 들어냈다.

| | 이미지 |
|---|---|
| `--extra rag --extra graph --extra mcp` | 8,800 MB |
| `--extra serve` | **913 MB** (9.6배 축소) |

INT8 양자화의 이득이 추론 속도에서 끝나지 않고 배포까지 이어졌다. 노트 003의
작업이 없었다면 이 축소는 불가능했다 — torch를 뺄 수 있었던 이유가 ONNX
런타임으로 추론이 되기 때문이다.

**이 조건을 코드로 못 박았다.** Dockerfile 빌드 단계와 CI 양쪽에서 `torch`
존재를 검사하고, CI는 이미지가 1.5GB를 넘으면 실패시킨다. 의존성은 한 줄
실수로 새어 들어오고, 그때 이미지는 조용히 다시 부푼다.

## 무엇을 계측할지 고른 기준

메트릭은 많이 만들수록 좋은 게 아니라 **볼 것만** 있어야 한다. 네 가지를 골랐다.

| 메트릭 | 왜 |
|---|---|
| `kograph_tool_calls_total{tool,status}` | 어떤 도구가 쓰이는지, 오류가 나는지 |
| `kograph_tool_duration_seconds` | p95로 본다. 평균은 느린 꼬리를 감춘다 |
| `kograph_retrieval_duration_seconds{mode}` | 벡터와 그래프는 성능 특성이 다르다 |
| `kograph_retrieval_results{mode}` | **0으로 수렴 = 색인/그래프가 빔.** 지연만 봐서는 못 잡는 장애다 |

마지막 항목이 중요하다. 색인이 비면 검색은 **빠르게** 아무것도 못 찾는다.
지연 대시보드만 보면 오히려 좋아 보인다.

## 실측: 대시보드가 설계를 확인해줬다

컨테이너에 부하를 주고 Prometheus에 질의한 결과다.

| 지표 | 값 |
|---|---|
| 검색 p95 — graph | 0.048초 |
| 검색 p95 — vector | 0.248초 |
| 검색 평균 반환 — graph / vector | 30건 / 3건 |
| 총 호출 / 오류 | 27 / **0** |

vector 0.248초는 노트 003의 INT8 벤치마크(344ms/청크)와 맞물린다.
**벡터 검색 지연은 사실상 임베딩 비용**이라는 것이 대시보드에서 그대로 보인다.
그래프 순회는 그 1/5이다. 두 축을 한 메트릭으로 합쳤다면 이 사실이 평균에
묻혔을 것이다.

## liveness에 DB를 넣지 않은 이유

`/healthz`는 프로세스만 확인하고 Oracle·Postgres·Neo4j를 건드리지 않는다.
의존 서비스까지 검사하면 DB가 잠깐 흔들릴 때 쿠버네티스가 **멀쩡한 파드를
재시작**한다. 재시작해도 DB는 그대로이므로 아무것도 해결되지 않고, 오히려
모델을 다시 로딩하며 상황을 악화시킨다.

같은 이유로 startup probe를 따로 뒀다. INT8 모델 로딩이 수십 초 걸리는데
liveness만 있으면 초기 로딩 중에 파드가 죽는다.

## CI 설계

단위 테스트 65건은 **DB도 API 키도 쓰지 않는다.** 전부 합성 데이터라
서비스 컨테이너 없이 돌고, 그래서 CI가 빠르고 흔들리지 않는다. 이는 우연이
아니라 처음부터 테스트를 그렇게 쓴 결과다(노트 001·002).

이미지 잡은 빌드에 더해 세 가지를 검증한다 — torch 미포함, 크기 1.5GB 이하,
컨테이너를 띄워 `/healthz` 200과 `/metrics`의 `kograph_` 노출. 빌드가 성공하는
것과 실제로 뜨는 것은 다른 문제다.

## 삽질 기록

- Grafana 기본 포트 3000이 로컬에서 이미 사용 중이라 3001로 옮겼다. Airflow가
  8080 충돌로 8081로 갔던 것과 같은 상황.
- 첫 메트릭 확인 때 `histogram_quantile`이 NaN이었다. 스크레이프가 한 번밖에
  지나지 않아 `rate()` 창에 표본이 부족했던 것으로, 계측 결함이 아니었다.
  부하를 주고 다시 보니 정상 값이 나왔다. 버스트 트래픽에서 일부 시계열이
  NaN으로 남는 것은 Prometheus의 정상 동작이다.

## 남은 작업

- k3d로 실제 클러스터에 적용해 매니페스트를 런타임 검증 (현재는 kustomize
  렌더까지만 확인)
- 평가셋 회귀를 CI에 추가 — DB가 필요하므로 서비스 컨테이너 + 시드 데이터가
  선행되어야 한다
- 알림 규칙(오류율, 검색 결과 0건 지속)을 Alertmanager로
