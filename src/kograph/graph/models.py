"""관계 트리플의 공용 타입 — 규칙 파서와 LLM 추출기가 함께 사용한다."""

from enum import StrEnum

from pydantic import BaseModel, Field


class Predicate(StrEnum):
    SUPPLIES_TO = "SUPPLIES_TO"                # 공급계약: subject가 object에 공급
    OWNS_STAKE = "OWNS_STAKE"                  # 지분 보유
    INVESTS_IN = "INVESTS_IN"                  # 출자·타법인주식취득·신규투자
    GUARANTEES_DEBT_OF = "GUARANTEES_DEBT_OF"  # 채무보증
    ACQUIRES = "ACQUIRES"                      # 인수·합병·영업양수
    SUBSIDIARY_OF = "SUBSIDIARY_OF"            # 종속회사 관계
    PARTNERS_WITH = "PARTNERS_WITH"            # 합작·업무제휴
    OFFICER_OF = "OFFICER_OF"                  # 임원 재직·겸직
    # 최대주주등소유주식변동신고서의 신고 대상에는 임원뿐 아니라 친인척·
    # 계열사·재단도 포함된다. OFFICER_OF로 뭉치면 법인이 임원이 된다.
    RELATED_PARTY_OF = "RELATED_PARTY_OF"      # 최대주주 특수관계인


class Triple(BaseModel):
    subject: str = Field(description="관계의 주체 기업/인물 정식 명칭")
    predicate: Predicate
    object: str = Field(description="관계의 대상 기업/인물 정식 명칭")
    amount_krw: int | None = Field(None, description="계약·출자·보증 금액(원), 명시된 경우만")
    ratio_pct: float | None = Field(None, description="지분율·자기자본대비 비율(%), 명시된 경우만")
    start_date: str | None = Field(None, description="계약/효력 시작일 YYYY-MM-DD")
    end_date: str | None = Field(None, description="계약/효력 종료일 YYYY-MM-DD")
    note: str | None = Field(None, description="관계 파악에 중요한 부가 정보 한 줄")


class ExtractionResult(BaseModel):
    triples: list[Triple] = Field(description="공시에서 확인된 관계. 없으면 빈 배열")
