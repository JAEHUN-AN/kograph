"""리트리버 순수 로직 테스트 — DB 미접근, 합성 입력만 사용."""

from kograph.rag.retriever import _render_path, mentioned_companies

NAMES = ["테스트하이닉스", "테스트반도체", "테스트소재"]


class TestRenderPath:
    def test_forward_edge_points_right(self):
        text = _render_path(["테스트반도체", "테스트하이닉스"], ["SUPPLIES_TO"], ["테스트반도체"])

        assert text == "테스트반도체 -[SUPPLIES_TO]-> 테스트하이닉스"

    def test_reverse_edge_points_left(self):
        """무방향 순회에서 엣지가 반대면 화살표도 뒤집혀야 사실이 보존된다."""
        text = _render_path(["테스트하이닉스", "홍길동"], ["OFFICER_OF"], ["홍길동"])

        assert text == "테스트하이닉스 <-[OFFICER_OF]- 홍길동"

    def test_two_hop_mixed_directions(self):
        text = _render_path(
            ["테스트하이닉스", "테스트반도체", "테스트고객사"],
            ["SUPPLIES_TO", "SUPPLIES_TO"],
            ["테스트반도체", "테스트반도체"],
        )

        assert text == "테스트하이닉스 <-[SUPPLIES_TO]- 테스트반도체 -[SUPPLIES_TO]-> 테스트고객사"


class TestMentionedCompanies:
    def test_matches_exact_name(self):
        assert "테스트하이닉스" in mentioned_companies("테스트하이닉스 실적", NAMES)

    def test_matches_despite_spaces_and_parens(self):
        names = ["테스트하이닉스(Test Hynix Inc.)"]

        assert mentioned_companies("테스트하이닉스 는 어디에 공급하나", names) == names

    def test_returns_empty_when_no_company_mentioned(self):
        assert mentioned_companies("오늘 환율이 어떻게 되나", NAMES) == []

    def test_prefers_longer_names_first(self):
        names = ["테스트", "테스트하이닉스"]
        hits = mentioned_companies("테스트하이닉스 관계", names)

        assert hits[0] == "테스트하이닉스"
