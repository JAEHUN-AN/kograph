# 기술노트 0 — 스코프 결정과 제약 정의

> 이 노트 시리즈는 "기술적 한계를 자체적으로 정의하고 해결한 사례"의 실측 기록이다.
> 각 노트는 **문제 정의 → 제약 → 시도 → before/after 수치 → 결론** 구조를 따른다.

## 프로젝트 제약 (2026-08 기준)

| 제약 | 결정 | 근거 |
|---|---|---|
| GPU 없음 | vLLM 서빙 대신 ① 임베딩 ONNX INT8 양자화 ② LLM 호출 비용/지연 최적화 ③ Colab T4 QLoRA 1회로 대체 | 로컬 GPU 부재. 최적화 "축"을 모델 서빙에서 추론 파이프라인으로 이동 |
| 기간 1개월 | 전 종목 → 반도체·2차전지 밸류체인 ~120종목, 최근 3년 | 그래프 밀도가 높은 섹터라 GraphRAG 효과가 드러남 |
| 공시 범위 | 사업보고서 전문 제외, 주요사항보고(B)·거래소공시(I)만 | 관계 추출이 명확한 유형에 집중 |

## 측정 예정 지표 (각 주차 1개 이상)

- [ ] W1: 증분 ETL 소요시간 / pandas 대비 Spark 팩터 배치 처리 시간
- [x] W2: 관계 추출 — 규칙 파서 396/509건(77.8%), 642 트리플, 0.4초, 0원 → [001](001-rule-vs-llm-extraction.md)
- [x] W2: vanilla RAG vs GraphRAG — 30문항 검색 recall 56% → 98% → [002](002-graphrag-vs-vector-eval.md)
- [x] W2: 관계 추출 정밀도 — 표본 212건, 재가중 95.0%, 오류의 91%가 정정공시 → [005](005-parser-precision.md)
- [x] W3: 임베딩 처리량 FP32 1.43 → INT8 2.91 chunks/s (2.03x), 가중치 4배 축소, 검색 품질 동일 → [003](003-onnx-int8-quantization.md)
- [ ] W3: 쿼리당 LLM 비용·p95 지연 before/after (크레딧 확보 후)
- [x] W4: 서빙 이미지 8.8GB → 913MB, 메트릭 4종 + 대시보드, CI 3중 검증 → [004](004-serving-deployment.md)
- [ ] W4: 평가셋 회귀를 CI에 추가 (DB 시드 필요)

## 노트 목록

- 000: 스코프 결정 (이 문서)
- [001](001-rule-vs-llm-extraction.md): 관계 추출 — LLM 대신 규칙 파서를 택한 이유와 실측
- [002](002-graphrag-vs-vector-eval.md): GraphRAG vs vanilla RAG 검색 성능 실측
- [003](003-onnx-int8-quantization.md): 임베딩 ONNX INT8 양자화 — CPU 처리량 2배, 품질 손실 0
- [004](004-serving-deployment.md): 서빙 배포와 관측 — 이미지 8.8GB → 913MB
- [005](005-parser-precision.md): 규칙 파서 정밀도 — 라벨링 기준과 측정 (표본 212건, 95.0%)
- ...
