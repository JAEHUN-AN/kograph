"""MCP 서버 단위 테스트 — DB가 필요 없는 부분만 합성 데이터로 검증.

도구 등록 여부와 순수 포매팅/판정 로직을 다룬다. 실제 조회 경로(Neo4j,
pgvector, Oracle)는 통합 검증에서 별도로 확인한다.
"""

from datetime import date

from kograph.mcp_server.server import (
    _format_price_row,
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


class TestServerMetadata:
    def test_server_has_a_name(self):
        assert mcp.name == "kograph"

    def test_instructions_mention_tool_selection(self):
        """모델이 도구를 고를 근거가 instructions에 있어야 한다."""
        assert "get_company_relations" in mcp.instructions
        assert "search_filings" in mcp.instructions


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
