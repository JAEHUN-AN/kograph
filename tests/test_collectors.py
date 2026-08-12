"""Collector unit tests — 네트워크 없이 파싱·정규화 로직만 검증 (합성 데이터)."""

import io
import zipfile
from datetime import date

import pytest
import responses

from kograph.collectors.dart import BASE_URL, DartApiError, DartClient, Filing
from kograph.collectors.krx import DailyPrice, normalize_ohlcv

# --- 합성 fixture -----------------------------------------------------------

FAKE_FILING_ROW = {
    "rcept_no": "20260801000001",
    "corp_code": "00000001",
    "corp_name": "테스트반도체",
    "stock_code": "000001",
    "report_nm": "단일판매ㆍ공급계약체결",
    "rcept_dt": "20260801",
    "flr_nm": "테스트반도체",
    "rm": "유",
}


def _fake_corp_zip() -> bytes:
    xml = (
        "<result><list>"
        "<corp_code>00000001</corp_code>"
        "<corp_name>테스트반도체</corp_name>"
        "<stock_code> </stock_code>"
        "<modify_date>20260801</modify_date>"
        "</list></result>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("CORPCODE.xml", xml)
    return buf.getvalue()


# --- DartClient -------------------------------------------------------------


class TestDartClient:
    def test_requires_api_key(self):
        with pytest.raises(ValueError, match="DART_API_KEY"):
            DartClient(api_key="")

    @responses.activate
    def test_corp_codes_parses_zip_and_blanks_stock_code(self):
        responses.get(f"{BASE_URL}/corpCode.xml", body=_fake_corp_zip())

        corps = DartClient("fake-key").corp_codes()

        assert len(corps) == 1
        assert corps[0].corp_code == "00000001"
        assert corps[0].stock_code is None  # 공백 -> None

    @responses.activate
    def test_filings_paginates(self):
        page1 = {"status": "000", "total_page": 2, "list": [FAKE_FILING_ROW]}
        page2 = {
            "status": "000",
            "total_page": 2,
            "list": [{**FAKE_FILING_ROW, "rcept_no": "20260801000002"}],
        }
        responses.get(f"{BASE_URL}/list.json", json=page1)
        responses.get(f"{BASE_URL}/list.json", json=page2)

        result = list(DartClient("fake-key").filings(date(2026, 8, 1), date(2026, 8, 1)))

        assert [f.rcept_no for f in result] == ["20260801000001", "20260801000002"]

    @responses.activate
    def test_filings_no_data_yields_empty(self):
        responses.get(f"{BASE_URL}/list.json", json={"status": "013", "message": "no data"})

        result = list(DartClient("fake-key").filings(date(2026, 8, 1), date(2026, 8, 1)))

        assert result == []

    @responses.activate
    def test_filings_error_status_raises(self):
        responses.get(f"{BASE_URL}/list.json", json={"status": "020", "message": "limit"})

        with pytest.raises(DartApiError, match="020"):
            list(DartClient("fake-key").filings(date(2026, 8, 1), date(2026, 8, 1)))

    def test_filing_model_rejects_bad_date(self):
        with pytest.raises(ValueError):
            Filing.model_validate({**FAKE_FILING_ROW, "rcept_dt": "2026-08-01"})


# --- KRX normalize ----------------------------------------------------------


class TestNormalizeOhlcv:
    def test_maps_korean_columns(self):
        pd = pytest.importorskip("pandas")
        df = pd.DataFrame(
            {
                "시가": [100], "고가": [110], "저가": [90], "종가": [105],
                "거래량": [1000], "거래대금": [105000], "등락률": [1.5],
            },
            index=pd.to_datetime(["2026-08-01"]),
        )

        rows = normalize_ohlcv("000001", df)

        assert rows == [
            DailyPrice(
                stock_code="000001", trade_date=date(2026, 8, 1),
                open_price=100, high_price=110, low_price=90, close_price=105,
                volume=1000, trade_value=105000, change_rate=1.5,
            )
        ]

    def test_empty_dataframe(self):
        pd = pytest.importorskip("pandas")
        df = pd.DataFrame(columns=["시가", "고가", "저가", "종가", "거래량"])

        assert normalize_ohlcv("000001", df) == []
