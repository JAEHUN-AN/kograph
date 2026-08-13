"""규칙 파서 단위 테스트 — 전부 합성 공시 텍스트 (실제 공시 미사용).

DART 폼의 "라벨 줄 다음이 값 줄" 구조를 그대로 재현해, 파서가 필드를
정확히 집어내는지와 라벨이 여러 번 등장할 때 섹션 범위를 지키는지 검증한다.
"""

from kograph.graph.models import Predicate
from kograph.graph.rules import parse

SUPPLY_CONTRACT = """단일판매ㆍ공급계약 체결
1. 판매ㆍ공급계약 구분
용역제공
2. 계약내역
계약금액(원)
1,000,000,000
최근매출액(원)
50,000,000,000
매출액대비(%)
2.0
3. 계약상대
(주)테스트철강
- 회사와의 관계
계열회사
5. 계약기간
시작일
2026-07-01
종료일
2027-06-30
7. 계약(수주)일자
2026-07-15
"""

STAKE_ACQUISITION = """타법인 주식 및 출자증권 취득결정
1. 발행회사
회사명
Test Semiconductor Corp.
국적
미국
회사와 관계
자회사
2. 취득내역
취득주식수(주)
1,000,000
취득금액(원)
14,428,000,000
자기자본(원)
73,915,000,000
자기자본대비(%)
19.5
3. 취득후 소유주식수 및 지분비율
소유주식수(주)
11,651,029
지분비율(%)
100.0
4. 취득방법
현금출자
6. 취득예정일자
2026-01-28
"""

DEBT_GUARANTEE = """타인에 대한 채무보증 결정
1. 채무자
Test Global Holdings Limited
-회사와의 관계
해외종속회사
2. 채권자
테스트은행
3. 채무(차입)금액(원)
131,421,800,000
4. 채무보증내역
채무보증금액(원)
132,156,000,000
자기자본대비(%)
12.95
채무보증기간
시작일
2025-11-22
종료일
2026-11-22
"""

SHAREHOLDER_CHANGE = """최대주주등소유주식변동신고서
1. 발행회사 정보
회사명
테스트반도체(주)
4. 개인별 세부변동사항
성명
홍길동
생년월일
651106
최대주주 및 발행회사와의 관계
발행회사 임원
성명
김철수
생년월일
621109
최대주주 및 발행회사와의 관계
발행회사 임원
5. 최대주주등 주식소유현황(총괄현황)
성명
생년월일 또는 사업자등록번호
성별
국내외 구분
국적
최대주주 및 발행회사와의 관계
겸직내용1
겸직내용2
"""


class TestSupplyContract:
    def test_extracts_counterparty_amount_and_period(self):
        triples = parse("테스트소재", "단일판매ㆍ공급계약체결", SUPPLY_CONTRACT)

        assert len(triples) == 1
        t = triples[0]
        assert t.subject == "테스트소재"
        assert t.predicate is Predicate.SUPPLIES_TO
        assert t.object == "(주)테스트철강"
        assert t.amount_krw == 1_000_000_000
        assert t.start_date == "2026-07-01"
        assert t.end_date == "2027-06-30"
        assert t.note == "계열회사"

    def test_returns_empty_when_counterparty_missing(self):
        text = "단일판매ㆍ공급계약 체결\n2. 계약내역\n계약금액(원)\n1,000,000\n"

        assert parse("테스트소재", "단일판매ㆍ공급계약체결", text) == []

    def test_rejects_label_in_value_position(self):
        """라벨을 먼저 모아 나열하는 표 레이아웃에서 라벨을 거래처로 삼으면 안 된다."""
        text = (
            "단일판매ㆍ공급계약 체결\n"
            "3. 계약상대방\n"
            "- 최근 매출액(원)\n"
            "- 주요사업\n"
            "-\n"
            "테스트해외법인\n"
        )

        assert parse("테스트소재", "단일판매ㆍ공급계약체결", text) == []


class TestStakeAcquisition:
    def test_emits_invests_in_and_owns_stake(self):
        triples = parse("테스트전자", "타법인주식및출자증권취득결정", STAKE_ACQUISITION)

        assert [t.predicate for t in triples] == [Predicate.INVESTS_IN, Predicate.OWNS_STAKE]
        invest, stake = triples
        assert invest.object == "Test Semiconductor Corp."
        assert invest.amount_krw == 14_428_000_000
        assert invest.ratio_pct == 19.5          # 자기자본대비
        assert invest.start_date == "2026-01-28"
        assert invest.note == "자회사"
        assert stake.ratio_pct == 100.0          # 취득 후 지분비율

    def test_section_scoping_picks_stake_not_equity_ratio(self):
        """지분비율(%)과 자기자본대비(%)가 한 문서에 공존해도 섞이지 않아야 한다."""
        triples = parse("테스트전자", "타법인주식및출자증권취득결정", STAKE_ACQUISITION)

        assert triples[1].ratio_pct != triples[0].ratio_pct


class TestDebtGuarantee:
    def test_extracts_debtor_and_guarantee_amount(self):
        triples = parse("테스트케미칼", "타인에대한채무보증결정", DEBT_GUARANTEE)

        assert len(triples) == 1
        t = triples[0]
        assert t.predicate is Predicate.GUARANTEES_DEBT_OF
        assert t.object == "Test Global Holdings Limited"
        # 채무(차입)금액이 아니라 채무보증금액을 우선 채택
        assert t.amount_krw == 132_156_000_000
        assert t.ratio_pct == 12.95
        assert t.start_date == "2025-11-22"
        assert t.end_date == "2026-11-22"
        assert t.note == "해외종속회사"


class TestShareholderChange:
    def test_emits_one_officer_edge_per_person(self):
        triples = parse("테스트반도체", "최대주주등소유주식변동신고서", SHAREHOLDER_CHANGE)

        assert [t.subject for t in triples] == ["홍길동", "김철수"]
        assert all(t.predicate is Predicate.OFFICER_OF for t in triples)
        assert all(t.object == "테스트반도체" for t in triples)
        assert all(t.note == "발행회사 임원" for t in triples)

    def test_ignores_summary_table_headers(self):
        """'총괄현황' 표는 머리글만 나열되므로 이름으로 채택하면 안 된다."""
        triples = parse("테스트반도체", "최대주주등소유주식변동신고서", SHAREHOLDER_CHANGE)

        names = {t.subject for t in triples}
        assert "생년월일 또는 사업자등록번호" not in names
        assert "성별" not in names


SUBSIDIARY_SUPPLY = """단일판매ㆍ공급계약체결
자회사인
주식회사 테스트에이치엔
의 주요경영사항신고
2. 계약내역
계약금액 총액(원)
19,833,165,000
3. 계약상대방
(주)테스트고객사
-회사와의 관계
없음
5. 계약기간
시작일
2026-02-17
종료일
2027-07-15
"""

STAKE_WITH_NATIONALITY = """타법인 주식 및 출자증권 취득결정
1. 발행회사
회사명(국적)
주식회사 테스트글로벌 (대한민국)
회사와 관계
종속회사
2. 취득내역
취득금액(원)
150,000,000,000
자기자본대비(%)
12.3
3. 취득후 소유주식수 및 지분비율
지분비율(%)
100
6. 취득예정일자
2026-08-31
"""


class TestSubsidiaryFiling:
    def test_emits_parent_child_edge_and_reassigns_subject(self):
        """모회사 대리 공시는 모자 관계를 남기고 계약 주체를 자회사로 바로잡는다."""
        triples = parse("테스트지주", "단일판매ㆍ공급계약체결(자회사의 주요경영사항)",
                        SUBSIDIARY_SUPPLY)

        assert len(triples) == 2
        sub_edge, supply = triples
        assert sub_edge.subject == "주식회사 테스트에이치엔"
        assert sub_edge.predicate is Predicate.SUBSIDIARY_OF
        assert sub_edge.object == "테스트지주"
        # 계약 주체는 제출사(모회사)가 아니라 자회사
        assert supply.subject == "주식회사 테스트에이치엔"
        assert supply.predicate is Predicate.SUPPLIES_TO
        assert supply.object == "(주)테스트고객사"

    def test_plain_filing_has_no_subsidiary_edge(self):
        triples = parse("테스트소재", "단일판매ㆍ공급계약체결", SUPPLY_CONTRACT)

        assert all(t.predicate is not Predicate.SUBSIDIARY_OF for t in triples)


class TestNationalitySuffix:
    def test_strips_nationality_from_issuer_name(self):
        triples = parse("테스트비엠", "타법인주식및출자증권취득결정", STAKE_WITH_NATIONALITY)

        assert triples[0].object == "주식회사 테스트글로벌"

    def test_keeps_leading_paren_company_names(self):
        triples = parse("테스트소재", "단일판매ㆍ공급계약체결", SUPPLY_CONTRACT)

        assert triples[0].object == "(주)테스트철강"


MULTI_ISSUER_DISPOSAL = """타법인 주식 및 출자증권 처분결정
1. 발행회사
회사명(국적)
테스트에이과기유한공사(중국), 테스트비전자재료유한공사(중국)
회사와 관계
종속회사
2. 처분내역
처분금액(원)
10,000,000,000
"""

MULTI_ISSUER_ACQUISITION = """타법인 주식 및 출자증권 취득결정
1. 발행회사
회사명(국적)
테스트에이과기유한공사(중국), 테스트비전자재료유한공사(중국)
회사와 관계
종속회사
2. 취득내역
취득금액(원)
10,000,000,000
3. 취득후 소유주식수 및 지분비율
지분비율(%)
100
"""


class TestDisposalVsAcquisition:
    def test_disposal_does_not_create_investment_edge(self):
        """처분은 투자의 반대 — INVESTS_IN을 만들면 그래프가 사실과 어긋난다."""
        triples = parse("테스트케미칼", "타법인주식및출자증권처분결정", MULTI_ISSUER_DISPOSAL)

        assert all(t.predicate is not Predicate.INVESTS_IN for t in triples)

    def test_acquisition_still_creates_investment_edge(self):
        triples = parse("테스트케미칼", "타법인주식및출자증권취득결정", MULTI_ISSUER_ACQUISITION)

        assert any(t.predicate is Predicate.INVESTS_IN for t in triples)


class TestMultipleIssuers:
    def test_splits_comma_separated_companies(self):
        triples = parse("테스트케미칼", "타법인주식및출자증권취득결정", MULTI_ISSUER_ACQUISITION)

        assert [t.object for t in triples] == [
            "테스트에이과기유한공사", "테스트비전자재료유한공사",
        ]

    def test_aggregate_amount_not_attributed_to_each_company(self):
        """합계 금액을 회사별로 복제하면 금액이 부풀려진다."""
        triples = parse("테스트케미칼", "타법인주식및출자증권취득결정", MULTI_ISSUER_ACQUISITION)

        assert all(t.amount_krw is None for t in triples)

    def test_single_company_keeps_amount(self):
        triples = parse("테스트전자", "타법인주식및출자증권취득결정", STAKE_ACQUISITION)

        assert triples[0].amount_krw == 14_428_000_000


class TestDispatch:
    def test_unsupported_report_type_yields_nothing(self):
        assert parse("테스트소재", "분기보고서", SUPPLY_CONTRACT) == []

    def test_blank_inputs_are_safe(self):
        assert parse("", "단일판매ㆍ공급계약체결", SUPPLY_CONTRACT) == []
        assert parse("테스트소재", "단일판매ㆍ공급계약체결", "") == []
