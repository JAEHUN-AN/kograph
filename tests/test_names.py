"""회사명 정규화 테스트 — 합성 상호만 사용.

핵심 요구사항은 두 방향이다.
  (1) 같은 회사의 다른 표기가 같은 키로 모일 것
  (2) 다른 회사가 합쳐지지 않을 것 — 과도한 병합이 더 나쁘다
"""

import pytest

from kograph.graph.names import canonical_name


class TestMergesSameCompany:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("테스트하이닉스(Test Hynix Inc.)", "테스트하이닉스"),
            ("테스트하이닉스", "테스트하이닉스"),
            ("(주)테스트비엠", "테스트비엠"),
            ("주식회사 테스트비엠", "테스트비엠"),
            ("테스트비엠 주식회사", "테스트비엠"),
            ("㈜테스트소재", "테스트소재"),
            ("  테스트소재  ", "테스트소재"),
        ],
    )
    def test_variants_collapse(self, raw, expected):
        assert canonical_name(raw) == expected

    def test_korean_and_english_variants_match(self):
        assert canonical_name("테스트하이닉스(Test Hynix Inc.)") == canonical_name("테스트하이닉스")

    def test_legal_form_variants_match(self):
        forms = ["(주)테스트비엠", "주식회사 테스트비엠", "테스트비엠"]
        assert len({canonical_name(f) for f in forms}) == 1


class TestKeepsDistinctCompanies:
    def test_different_companies_stay_separate(self):
        assert canonical_name("테스트비엠") != canonical_name("테스트머티리얼즈")

    def test_foreign_entity_body_preserved(self):
        # 법인격만 떨어지고 상호 본체는 남아야 한다
        assert canonical_name("Test Global Holdings Limited") == "Test Global Holdings"
        assert canonical_name("Test Energy LLC") == "Test Energy"

    def test_related_but_distinct_subsidiaries_not_merged(self):
        names = {canonical_name(n) for n in
                 ["테스트글로벌헝가리", "테스트글로벌", "테스트글로벌아메리카"]}
        assert len(names) == 3


class TestEdgeCases:
    def test_empty_input(self):
        assert canonical_name("") == ""

    def test_name_that_is_only_a_legal_form_is_kept(self):
        """전부 깎이면 빈 노드가 되므로 원문을 유지해야 한다."""
        assert canonical_name("주식회사") == "주식회사"

    def test_strips_trailing_punctuation(self):
        """상호 분리 과정에서 남는 꼬리 쉼표는 엔티티 매칭을 방해한다."""
        assert canonical_name("테스트에너지 Michigan,") == "테스트에너지 Michigan"

    def test_leading_paren_name_without_english(self):
        # 한글만 있는 괄호는 영문 병기가 아니므로 보존
        assert canonical_name("테스트전자(특별계정)") == "테스트전자(특별계정)"
