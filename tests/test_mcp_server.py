"""MCP 서버 단위 테스트 — DB가 필요 없는 부분만 합성 데이터로 검증.

도구 등록 여부와 순수 포매팅/판정 로직을 다룬다. 실제 조회 경로(Neo4j,
pgvector, Oracle)는 통합 검증에서 별도로 확인한다.
"""

from datetime import date

import anyio

from kograph.mcp_server.server import (
    _format_price_row,
    _relation_sort_key,
    _shared_partners,
    list_companies,
    mcp,
)


class FakeEvidence:
    """retriever.Evidence의 최소 대역 — entities만 쓰는 로직 검증용."""

    def __init__(self, entities):
        self.entities = entities
        self.text = " -> ".join(entities)
        self.rcept_no = None


class FakeRelEvidence:
    """정렬 검증용 대역 — rel_types와 text만 쓴다."""

    def __init__(self, rel_types, text="edge"):
        self.rel_types = rel_types
        self.text = text
        self.entities = []
        self.rcept_no = None


class TestRelationOrdering:
    def test_business_relations_come_before_people(self):
        """공급·투자가 임원·특수관계인보다 앞에 와야 한다. 리서치 질문의
        답이 사람 명단에 묻히면 도구가 쓸모없어진다."""
        evidences = [
            FakeRelEvidence(["RELATED_PARTY_OF"], "특수관계인"),
            FakeRelEvidence(["OFFICER_OF"], "임원"),
            FakeRelEvidence(["SUPPLIES_TO"], "공급"),
            FakeRelEvidence(["GUARANTEES_DEBT_OF"], "보증"),
        ]

        ordered = [e.text for e in sorted(evidences, key=_relation_sort_key)]

        assert ordered == ["공급", "보증", "임원", "특수관계인"]

    def test_shorter_paths_first_within_same_rank(self):
        """같은 우선순위면 1홉이 2홉보다 먼저 — 직접 관계가 더 강한 근거다."""
        evidences = [
            FakeRelEvidence(["SUPPLIES_TO", "SUPPLIES_TO"], "2홉"),
            FakeRelEvidence(["SUPPLIES_TO"], "1홉"),
        ]

        ordered = [e.text for e in sorted(evidences, key=_relation_sort_key)]

        assert ordered == ["1홉", "2홉"]

    def test_unknown_relation_type_sorts_last_without_error(self):
        """미등록 관계가 생겨도 정렬이 깨지지 않아야 한다."""
        evidences = [
            FakeRelEvidence(["MYSTERY_REL"], "미등록"),
            FakeRelEvidence(["SUPPLIES_TO"], "공급"),
            FakeRelEvidence([], "관계없음"),
        ]

        ordered = [e.text for e in sorted(evidences, key=_relation_sort_key)]

        assert ordered[0] == "공급"


class TestServerMetadata:
    def test_server_has_a_name(self):
        assert mcp.name == "kograph"

    def test_instructions_mention_tool_selection(self):
        """모델이 도구를 고를 근거가 instructions에 있어야 한다."""
        assert "get_company_relations" in mcp.instructions
        assert "search_filings" in mcp.instructions

    def test_all_tools_are_registered(self):
        """@mcp.tool() 데코레이터는 조용히 빠진다. 잃어버려도 코드는
        멀쩡히 돌고 도구만 사라지므로 개수와 이름을 못 박아둔다."""
        names = {t.name for t in anyio.run(mcp.list_tools)}

        assert names == {
            "list_companies",
            "get_company_relations",
            "find_connection",
            "search_filings",
            "get_price_series",
            "graph_overview",
        }

    def test_every_tool_description_states_when_to_call(self):
        """설명이 곧 라우팅 로직이다. '무엇을 하는지'만 있으면 모델이
        도구를 안 고른다."""
        for tool in anyio.run(mcp.list_tools):
            assert tool.description, f"{tool.name}에 설명이 없다"
            assert "호출한다" in tool.description, (
                f"{tool.name} 설명에 호출 조건이 없다"
            )


class TestFormatPriceRow:
    def test_includes_change_rate_when_present(self):
        row = _format_price_row(date(2026, 7, 1), 105000, 1000000, 1.5)

        assert "2026-07-01" in row
        assert "105,000" in row
        assert "+1.50%" in row

    def test_omits_change_rate_when_missing(self):
        """상장 첫날처럼 등락률이 없는 행에서 깨지면 안 된다."""
        row = _format_price_row(date(2026, 7, 1), 105000, 1000000, None)

        assert "105,000" in row
        assert "%" not in row

    def test_negative_change_keeps_sign(self):
        assert "-2.30%" in _format_price_row(date(2026, 7, 1), 100, 10, -2.3)


class TestSharedPartners:
    def test_finds_common_counterparty(self):
        evidences = [
            FakeEvidence(["테스트에이", "테스트고객사"]),
            FakeEvidence(["테스트비", "테스트고객사"]),
        ]

        assert _shared_partners(evidences, "테스트에이", "테스트비") == {"테스트고객사"}

    def test_excludes_the_two_queried_companies(self):
        """A와 B 자신은 '공통 상대'가 아니다."""
        evidences = [FakeEvidence(["테스트에이", "테스트비"])]

        assert _shared_partners(evidences, "테스트에이", "테스트비") == set()

    def test_returns_empty_when_no_overlap(self):
        evidences = [
            FakeEvidence(["테스트에이", "고객1"]),
            FakeEvidence(["테스트비", "고객2"]),
        ]

        assert _shared_partners(evidences, "테스트에이", "테스트비") == set()


class TestListCompanies:
    def test_lists_universe_with_sectors(self):
        out = list_companies()

        assert "분석 대상" in out
        assert "005930" in out   # 종목코드 컬럼 반영
        assert "반도체" in out    # 섹터 컬럼 반영
