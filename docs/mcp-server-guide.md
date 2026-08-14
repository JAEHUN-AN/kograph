# MCP 서버 설계·구현 매뉴얼

> LLM이 직접 호출하는 도구 서버를 처음부터 만드는 방법. kograph를 만들며
> 겪은 실패를 기준으로 썼다. 코드는 전부 이 리포에서 동작하는 것이다.

대상 독자는 "REST API는 만들어봤는데 MCP 서버는 처음"인 사람이다.
프레임워크 사용법보다 **판단이 갈리는 지점**에 분량을 뒀다.

- [0. MCP가 REST와 다른 점](#0-mcp가-rest와-다른-점)
- [1. 도구 경계 정하기](#1-도구-경계-정하기)
- [2. 도구 설명 쓰기 — 가장 큰 레버](#2-도구-설명-쓰기--가장-큰-레버)
- [3. 응답 설계](#3-응답-설계)
- [4. 최소 구현](#4-최소-구현)
- [5. 전송 방식 두 가지](#5-전송-방식-두-가지)
- [6. 계측](#6-계측)
- [7. 테스트](#7-테스트)
- [8. 배포](#8-배포)
- [9. 출시 전 점검표](#9-출시-전-점검표)

---

## 0. MCP가 REST와 다른 점

기술적으로는 JSON-RPC 위의 도구 호출 프로토콜이다. 하지만 설계에서 중요한
차이는 하나다.

**REST는 사람이 호출 순서를 정하고, MCP는 모델이 정한다.**

REST 클라이언트를 짤 때는 `GET /companies` 다음에 `GET /companies/{id}/relations`를
부르라고 개발자가 코드에 박는다. MCP에서는 모델이 사용자 질문을 보고 어떤
도구를 부를지, 부를지 말지, 몇 번 부를지를 매번 판단한다.

여기서 두 가지가 따라온다.

1. **도구 설명이 코드만큼 중요하다.** 설명이 곧 라우팅 로직이다.
2. **모델이 도구를 안 부르는 것이 가장 흔한 실패다.** 500 에러보다 "도구가
   있는데 안 쓰임"이 훨씬 자주 일어나고, 로그만 봐서는 안 보인다.

---

## 1. 도구 경계 정하기

### 개수: 5~8개

너무 적으면 도구 하나가 만능이 되어 인자가 늘고, 모델이 인자를 잘못 채운다.
너무 많으면 고르지 못한다. kograph는 6개다.

| 도구 | 담당 |
|---|---|
| `graph_overview` | 데이터에 뭐가 있는지 |
| `list_companies` | 조회 가능한 대상 |
| `get_company_relations` | 한 회사의 관계 |
| `find_connection` | 두 회사 사이 |
| `search_filings` | 서술형 본문 |
| `get_price_series` | 시계열 |

### 경계는 "질문의 종류"로 가른다

기술 스택이 아니라 사용자 질문 유형으로 나눈다. `search_filings`(벡터)와
`get_company_relations`(그래프)는 백엔드가 다르지만, 그래서 나눈 게 아니다.
**"서술형 내용을 묻는가, 관계를 묻는가"**가 달라서 나눴다.

반대로 `find_connection`은 `get_company_relations`를 두 번 부르면 되는 것
아니냐고 할 수 있다. 실제로 해보면 모델이 두 번 부르고 **직접 대조하지 않는다.**
교집합 계산은 코드가 확실히 하고 도구로 노출하는 편이 낫다.

> **원칙**: 모델에게 시키면 자주 틀리는 일은 도구로 만든다.

### 탐색용 도구를 반드시 하나 넣는다

`graph_overview` 같은 것. 모델은 데이터 범위를 모르는 상태에서 시작하고,
범위를 모르면 엉뚱한 회사명으로 조회해 빈 결과를 받은 뒤 **포기한다.**
"뭐가 있는지 묻는" 도구가 없으면 이 실패에서 회복하지 못한다.

---

## 2. 도구 설명 쓰기 — 가장 큰 레버

여기가 이 문서에서 제일 중요한 절이다. 구현을 아무리 잘해도 설명이 나쁘면
도구는 호출되지 않는다.

### 무엇을 하는지가 아니라 **언제 부르는지**를 쓴다

```python
# 나쁨 — 무엇을 하는지만 있다
"""기업 간 관계를 그래프에서 조회한다."""

# 좋음 — 호출 조건이 있다
"""한 기업의 거래·지배 관계를 그래프에서 조회한다.

**다음과 같은 질문에 호출한다**: 누가 누구에게 납품하는가, 지분을 누가
보유하는가, 자회사가 어디인가, 채무보증 대상이 어디인가. 관계가 여러
공시에 흩어져 있어도 그래프가 모아서 돌려주므로, 이런 질문에는
search_filings보다 이 도구가 정확하다.
"""
```

최신 모델일수록 도구를 보수적으로 고른다. 확신이 없으면 안 부르고 자기
지식으로 답해버린다. **호출 조건을 문장으로 못 박아야** 호출률이 올라간다.

### 겹치는 도구는 서로를 언급해 구분선을 긋는다

`search_filings`와 `get_company_relations`는 둘 다 "회사에 대해 알려주는"
도구라 모델이 헷갈린다. 그래서 양쪽 설명에 **상대 도구를 명시**했다.

```python
# search_filings 설명 中
"""**관계가 아니라 서술형 내용을 물을 때 호출한다** — 계약 조건, 투자 목적,
금액 산정 근거 등. 기업 간 관계 자체를 묻는 질문이면
get_company_relations가 더 정확하다."""
```

### 서버 수준 instructions로 라우팅을 한 번 더 준다

```python
mcp = MCPServer(
    name="kograph",
    instructions=(
        "한국 반도체·2차전지 밸류체인의 공시 지식그래프. ... "
        "관계를 묻는 질문은 get_company_relations 또는 find_connection을, "
        "계약 조건·투자 목적처럼 서술형 내용은 search_filings를 쓴다."
    ),
)
```

도구 설명은 도구를 볼 때 읽히고, `instructions`는 대화 시작 시 읽힌다.
둘 다 있어야 한다.

### 인자 제약은 설명에 쓰고 코드로도 막는다

형식을 설명에 적어도 모델은 틀린 형식을 보낸다. 반드시 코드에서도 검증하고,
**예외 대신 모델이 고칠 수 있는 문장**을 돌려준다.

```python
try:
    begin, end = date.fromisoformat(start_date), date.fromisoformat(end_date)
except ValueError:
    return "날짜는 YYYY-MM-DD 형식이어야 합니다 (예: 2026-01-31)."
```

예외를 던지면 모델은 대개 포기한다. 문자열로 돌려주면 고쳐서 다시 부른다.

---

## 3. 응답 설계

### 빈 결과에 다음 행동을 적는다

```python
if not evidences:
    return (f"'{company}'의 관계를 찾지 못했습니다. "
            "list_companies로 조회 가능한 회사명을 확인하세요.")
```

빈 배열이나 "결과 없음"만 돌려주면 모델은 거기서 멈춘다. **회복 경로를
문장으로 주면** 다른 도구를 부른다. 이름 표기가 달라서 못 찾은 경우가
대부분이라 실제로 이 한 줄이 성공률을 바꾼다.

### JSON 덤프 대신 압축된 텍스트

```
'SK하이닉스' 관련 관계 29건 (hops=1):
  SK하이닉스 <-[SUPPLIES_TO]- 한미반도체  [공시 20260114800028]
  SK하이닉스 -[INVESTS_IN]-> SK hynix NAND Product Solutions  [공시 20260213800001]
```

같은 내용을 JSON으로 돌려주면 토큰은 여러 배 쓰고 모델이 읽기는 더 어렵다.
사람이 읽을 수 있게 쓰면 모델도 잘 읽는다.

### 중요한 것을 먼저 놓는다

모델은 긴 결과를 잘라 읽거나 요약한다. **뒤쪽은 버려질 수 있다고 가정**하고
정렬한다. kograph는 거래·지배 관계를 임원·특수관계인보다 앞에 둔다.

```python
_RELATION_PRIORITY = (
    "SUPPLIES_TO", "INVESTS_IN", "OWNS_STAKE", "GUARANTEES_DEBT_OF",
    "SUBSIDIARY_OF", "OFFICER_OF", "RELATED_PARTY_OF",
)
```

이걸 안 했을 때 "SK하이닉스와 관계된 회사"의 첫 6줄이 전부 개인 이름이었다.
정렬 하나로 도구의 체감 품질이 달라진다.

### 출처를 모든 행에 붙인다

```python
cite = f"  [공시 {ev.rcept_no}]" if ev.rcept_no else ""
```

모델이 출처를 인용할 수 있어야 한다. 금융·의료·법률에서 출처 없는 답변은
쓸 수 없고, 사용자가 검증할 수단도 사라진다.

### "없다"는 사실도 명시해서 돌려준다

모델은 데이터가 없으면 자기 지식으로 메운다. 특히 인과를 물었을 때 그렇다.
kograph의 시세 도구는 같은 기간의 공시를 함께 주는데, 없으면 없다고 말한다.

```python
else:
    # 없다는 사실을 명시해야 모델이 원인을 지어내지 않는다.
    out.append("\n기간 중 공시 없음 — 등락 원인을 이 데이터로는 설명할 수 없다.")
```

이 한 줄을 빼면 모델은 "시장 전반의 조정으로 보입니다" 같은 문장을 만들어낸다.
근거 없는 그럴듯한 답이 근거 없는 침묵보다 위험하다.

### 의미 검색에 소유권 필터를 따로 준다

벡터 검색은 의미로 찾지 **소유권으로 거르지 않는다.** "SK하이닉스 채무보증"으로
검색하면 같은 그룹 계열사 공시가 유사도 상위에 섞여 들어온다. 회사명을 질의
문자열에 넣는 것으로는 해결되지 않는다.

필터는 **반드시 상위 k를 뽑기 전에** 건다.

```python
# 필터는 반드시 ORDER BY 앞에 온다. 상위 k를 뽑고 거르면 대부분 빈 결과가 된다.
where = ["embedding IS NOT NULL"]
if company:
    where.append("REPLACE(corp_name, ' ', '') ILIKE %s")
```

호출부에서 후처리로 거르는 구현을 자주 보는데, k=5로 뽑아 필터하면 대개
0~1건이 남는다. 도구가 조용히 쓸모없어진다.

### 길이 상한을 두고, 잘랐다고 알린다

```python
MAX_LIMIT = 20
MAX_PRICE_ROWS = 250

shown = rows if len(rows) <= MAX_PRICE_ROWS else rows[:10] + rows[-10:]
if len(rows) > MAX_PRICE_ROWS:
    out.append(f"(전체 {len(rows)}행 중 앞뒤 10행만 표시)")
```

조용히 자르면 모델은 그게 전부인 줄 알고 틀린 결론을 낸다.

---

## 4. 최소 구현

MCP Python SDK 2.0 기준이다. 인터넷에 도는 `fastmcp` 예제와 import 경로가
다르니 주의한다.

```python
from mcp.server.mcpserver import MCPServer

mcp = MCPServer(name="kograph", instructions="...")

@mcp.tool()
def list_companies() -> str:
    """분석 대상 기업 목록과 섹터를 반환한다.

    다른 도구를 호출하기 전에 **어떤 회사를 조회할 수 있는지 모를 때** 먼저
    호출한다.
    """
    return "..."
```

- 함수의 **타입 힌트가 곧 도구 스키마**가 된다. 반드시 붙인다.
- **독스트링이 곧 도구 설명**이다. 모델이 읽는 유일한 문서다.
- 반환 타입은 `str`이 다루기 쉽다. 복잡한 구조가 필요하면 그때 바꾼다.

### 헬스체크·메트릭 같은 부가 라우트

```python
from starlette.requests import Request
from starlette.responses import JSONResponse

@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(_request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "service": "kograph"})
```

---

## 5. 전송 방식 두 가지

```python
def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    args = parser.parse_args()

    if args.transport == "stdio":
        mcp.run(transport="stdio")
        return

    import uvicorn
    app = mcp.streamable_http_app(host=args.host)
    uvicorn.run(app, host=args.host, port=args.port)
```

| | stdio | streamable-http |
|---|---|---|
| 용도 | Claude Desktop 등 로컬 클라이언트 | 컨테이너 배포, 원격 |
| 프로세스 | 클라이언트가 띄움 | 상시 실행 |
| 관측 | 어려움 | `/metrics` 노출 가능 |

**둘 다 지원해야 한다.** stdio만 만들면 배포·모니터링 대상이 될 수 없고,
http만 만들면 Claude Desktop에서 못 쓴다. 코드 대부분은 공유되므로 비용이
크지 않다.

### stdio에서 stdout을 쓰면 안 된다

stdio 전송은 stdout이 곧 프로토콜 채널이다. `print()` 한 줄이 JSON-RPC
스트림을 깨뜨린다. 로깅은 반드시 stderr로 보낸다(`logging` 기본값이 stderr다).

### Claude Desktop 연결

```json
{
  "mcpServers": {
    "kograph": {
      "command": "uv",
      "args": ["--directory", "/path/to/project",
               "run", "python", "-m", "your_pkg.server"]
    }
  }
}
```

---

## 6. 계측

도구 호출은 실패해도 조용하다. 모델이 알아서 다른 걸 시도하기 때문에
사용자도 개발자도 모른다. 계측이 없으면 영원히 모른다.

```python
@contextmanager
def track_tool(name: str) -> Iterator[None]:
    """도구 호출을 계측. 예외는 status=error로 세고 그대로 다시 던진다."""
    started = time.perf_counter()
    status = "ok"
    try:
        yield
    except Exception:
        status = "error"
        raise
    finally:
        TOOL_LATENCY.labels(tool=name).observe(time.perf_counter() - started)
        TOOL_CALLS.labels(tool=name, status=status).inc()
```

```python
def get_company_relations(company: str, hops: int = 1) -> str:
    with track_tool("get_company_relations"):
        ...
```

### 무엇을 잴 것인가

| 메트릭 | 왜 |
|---|---|
| 도구별 호출 수 + status | **안 불리는 도구**를 찾는다. 설명을 고쳐야 한다는 신호다 |
| 도구별 지연 (히스토그램) | p95로 본다. 평균은 꼬리를 감춘다 |
| 백엔드 축별 지연 | 벡터와 그래프는 성능 특성이 다르다. 합치면 평균에 묻힌다 |
| **결과 건수** | 0으로 수렴 = 색인이 빔. 지연만 보면 오히려 빨라 보이는 장애다 |

마지막 항목이 자주 빠진다. 색인이 비면 검색은 **빠르게** 아무것도 못 찾는다.

### 히스토그램 버킷과 표본 수의 함정

버킷을 `(0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)`으로 뒀을 때,
6.4초 표본 하나가 `≤10` 버킷에 들어가면 `histogram_quantile`이 5~10 구간을
보간해 **9초**를 낸다. 실제보다 40% 큰 값이다.

더 중요한 건 표본 수다. **p95도 표본이 10개면 이상치 하나에 지배된다.**
평균 대신 p95를 쓰는 것만으로는 부족하다.

---

## 7. 테스트

### 단위 테스트는 DB 없이 돌게 만든다

kograph의 테스트 72건은 DB도 API 키도 쓰지 않는다. 순수 함수(포매팅, 판정,
정렬)를 분리해두면 자연히 그렇게 된다. CI가 빠르고 안 흔들린다.

```python
def test_business_relations_come_before_people(self):
    evidences = [
        FakeRelEvidence(["RELATED_PARTY_OF"], "특수관계인"),
        FakeRelEvidence(["SUPPLIES_TO"], "공급"),
    ]
    ordered = [e.text for e in sorted(evidences, key=_relation_sort_key)]
    assert ordered == ["공급", "특수관계인"]
```

### 도구 등록 여부를 테스트한다

데코레이터는 조용히 빠진다. 리팩터링 중 `@mcp.tool()`을 잃어버려도 코드는
멀쩡히 돌고 도구만 사라진다.

```python
def test_all_tools_are_registered(self):
    names = {t.name for t in anyio.run(mcp.list_tools)}
    assert names == {"list_companies", "get_company_relations", ...}
```

설명 규약도 테스트로 강제할 수 있다. 이 절의 원칙("언제 부르는지를 쓴다")을
사람 리뷰에 맡기지 않는 방법이다.

```python
def test_every_tool_description_states_when_to_call(self):
    for tool in anyio.run(mcp.list_tools):
        assert tool.description, f"{tool.name}에 설명이 없다"
        assert "호출한다" in tool.description
```

### 클라이언트 왕복을 반드시 한 번 한다

**이게 없으면 안 된다.** 단위 테스트가 전부 통과하는 상태에서 실제 도구
출력이 `회사 -[OFFICER_OF]-> 사람`처럼 **사실이 뒤집혀** 나가고 있었다.
무방향 순회에서 화살표를 항상 오른쪽으로 그린 탓인데, 클라이언트로 붙어
출력을 눈으로 읽기 전까지 아무 테스트도 잡지 못했다.

```python
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

async with (
    streamable_http_client(f"{BASE}/mcp") as streams,
    ClientSession(streams[0], streams[1]) as session,
):
    await session.initialize()
    result = await session.call_tool("list_companies", {})
```

`streamable_http_client`는 튜플을 돌려준다. `streams[0], streams[1]`로 푼다.

---

## 8. 배포

### 서빙 이미지에 학습 스택을 넣지 않는다

kograph의 첫 이미지는 8.8GB였다. 원인은 PyTorch였는데, 추론은 ONNX Runtime만
있으면 됐다. 서빙 전용 의존성 그룹을 만들어 **913MB**로 줄였다.

빌드 단계에서 못 박는다.

```dockerfile
RUN test ! -d /app/.venv/lib/python3.12/site-packages/torch \
    || (echo "ERROR: torch가 서빙 이미지에 포함되었습니다" && exit 1)
```

의존성은 한 줄 실수로 새어 들어오고, 그때 이미지는 조용히 다시 부푼다.

### liveness에 DB를 넣지 않는다

```python
@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(_request: Request) -> JSONResponse:
    """의존 서비스는 확인하지 않는다."""
    return JSONResponse({"status": "ok", "service": "kograph"})
```

DB까지 검사하면 DB가 잠깐 흔들릴 때 쿠버네티스가 **멀쩡한 파드를 재시작**한다.
재시작해도 DB는 그대로라 아무것도 해결되지 않고, 모델을 다시 로딩하며
상황만 악화된다. 준비 상태는 readiness로 따로 다룬다.

### 모델 로딩이 있으면 startup probe를 둔다

임베딩 모델 로딩에 수 초가 걸린다. liveness만 있으면 초기 로딩 중에 파드가
죽는다.

같은 이유로 **첫 호출이 느리다.** kograph는 재시작 직후 첫 검색이 6.65초,
이후 100~120ms다. 시연이나 벤치마크 전에는 예열해야 하고, 예열은 이미
기록된 느린 표본을 지우지 못하므로 **측정 창에서 빠져나갈 때까지 기다려야**
한다.

---

## 9. 출시 전 점검표

구현이 아니라 **검증** 목록이다. 순서대로 하면 kograph에서 겪은 실패를
대부분 피할 수 있다.

- [ ] 클라이언트로 붙어 **모든 도구의 출력을 눈으로 읽었다**
      (단위 테스트가 통과해도 사실이 뒤집혀 나갈 수 있다)
- [ ] 도구가 전부 등록되어 있다 (`list_tools` 개수 확인)
- [ ] 실제 모델에게 대표 질문 5개를 시켜 **의도한 도구가 불렸는지** 확인했다
- [ ] 빈 결과에 다음 행동이 적혀 있다
- [ ] 잘못된 인자에 예외 대신 고칠 수 있는 문장을 돌려준다
- [ ] 잘린 결과에 잘렸다는 표시가 있다
- [ ] 모든 사실에 출처가 붙어 있다
- [ ] stdio에서 stdout에 아무것도 쓰지 않는다
- [ ] 도구 호출 수·지연·**결과 건수**를 계측한다
- [ ] 단위 테스트가 DB 없이 돈다
- [ ] `/healthz`가 의존 서비스를 검사하지 않는다
- [ ] 서빙 이미지에 학습 스택이 없다

---

## 참고

- kograph 구현: [`src/kograph/mcp_server/server.py`](../src/kograph/mcp_server/server.py)
- 왕복 검증: [`scripts/verify_mcp.py`](../scripts/verify_mcp.py) (stdio),
  [`scripts/verify_http.py`](../scripts/verify_http.py) (HTTP)
- 예열 스크립트: [`scripts/warmup.py`](../scripts/warmup.py)
- 배포·관측 기록: [`notes/004-serving-deployment.md`](../notes/004-serving-deployment.md)
