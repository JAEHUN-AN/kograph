"""KRX daily OHLCV collector (pykrx wrapper).

pykrx는 KRX 웹을 스크래핑하므로 과도한 호출을 피하고(스로틀), 응답 스키마를
여기서 고정된 dict 리스트로 정규화해 하류(Oracle 적재)가 pykrx 버전에
의존하지 않게 한다.
"""

import logging
import math
import time
from datetime import date

from pydantic import BaseModel

logger = logging.getLogger(__name__)

THROTTLE_SECONDS = 0.5  # KRX 서버 예의상 호출 간격

# pykrx 컬럼명(한글) -> 표준 필드
_COLUMN_MAP = {
    "시가": "open_price",
    "고가": "high_price",
    "저가": "low_price",
    "종가": "close_price",
    "거래량": "volume",
    "거래대금": "trade_value",
    "등락률": "change_rate",
}


class DailyPrice(BaseModel):
    stock_code: str
    trade_date: date
    open_price: int
    high_price: int
    low_price: int
    close_price: int
    volume: int
    trade_value: int | None = None
    change_rate: float | None = None


def fetch_ohlcv(stock_code: str, begin: date, end: date) -> list[DailyPrice]:
    """단일 종목 기간 시세. 거래 없는 기간은 빈 리스트."""
    from pykrx import stock as krx  # import 지연: pykrx 미설치 환경(테스트) 보호

    df = krx.get_market_ohlcv(
        begin.strftime("%Y%m%d"), end.strftime("%Y%m%d"), stock_code
    )
    time.sleep(THROTTLE_SECONDS)
    if df is None or df.empty:
        logger.warning("no OHLCV for %s (%s~%s)", stock_code, begin, end)
        return []
    return normalize_ohlcv(stock_code, df)


def _opt_float(v) -> float | None:
    """NaN/None -> None (Oracle은 NaN 바인딩을 거부한다)."""
    if v is None:
        return None
    f = float(v)
    return None if math.isnan(f) else f


def _opt_int(v) -> int | None:
    f = _opt_float(v)
    return None if f is None else int(f)


def normalize_ohlcv(stock_code: str, df) -> list[DailyPrice]:
    """pykrx DataFrame -> 검증된 DailyPrice 목록. (순수 함수: 단위 테스트 대상)"""
    rows: list[DailyPrice] = []
    renamed = df.rename(columns=_COLUMN_MAP)
    for idx, row in renamed.iterrows():
        rows.append(
            DailyPrice(
                stock_code=stock_code,
                trade_date=idx.date() if hasattr(idx, "date") else idx,
                open_price=int(row["open_price"]),
                high_price=int(row["high_price"]),
                low_price=int(row["low_price"]),
                close_price=int(row["close_price"]),
                volume=int(row["volume"]),
                trade_value=_opt_int(row.get("trade_value")),
                # 상장 첫날 등 전일 종가가 없으면 pykrx가 NaN을 반환한다
                change_rate=_opt_float(row.get("change_rate")),
            )
        )
    return rows
