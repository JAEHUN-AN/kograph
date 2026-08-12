"""Oracle raw-layer loader. MERGE 기반 멱등 적재 — 재실행해도 중복 없음."""

import json
import logging
from contextlib import contextmanager

import oracledb

from kograph.collectors.dart import CorpInfo, Filing
from kograph.collectors.krx import DailyPrice
from kograph.config import get_settings

logger = logging.getLogger(__name__)

_MERGE_CORP = """
MERGE INTO corp t
USING (SELECT :corp_code AS corp_code FROM dual) s
ON (t.corp_code = s.corp_code)
WHEN MATCHED THEN UPDATE SET
    t.stock_code = :stock_code, t.corp_name = :corp_name, t.modify_date = :modify_date
WHEN NOT MATCHED THEN INSERT (corp_code, stock_code, corp_name, modify_date)
VALUES (:corp_code, :stock_code, :corp_name, :modify_date)
"""

_MERGE_FILING = """
MERGE INTO filing t
USING (SELECT :rcept_no AS rcept_no FROM dual) s
ON (t.rcept_no = s.rcept_no)
WHEN NOT MATCHED THEN INSERT
    (rcept_no, corp_code, corp_name, stock_code, report_nm, rcept_dt, flr_nm, rm, raw_json)
VALUES (:rcept_no, :corp_code, :corp_name, :stock_code, :report_nm, :rcept_dt,
        :flr_nm, :rm, :raw_json)
"""

_MERGE_PRICE = """
MERGE INTO price_daily t
USING (SELECT :stock_code AS stock_code, :trade_date AS trade_date FROM dual) s
ON (t.stock_code = s.stock_code AND t.trade_date = s.trade_date)
WHEN NOT MATCHED THEN INSERT
    (stock_code, trade_date, open_price, high_price, low_price, close_price,
     volume, trade_value, change_rate)
VALUES (:stock_code, :trade_date, :open_price, :high_price, :low_price, :close_price,
        :volume, :trade_value, :change_rate)
"""


@contextmanager
def connect():
    s = get_settings()
    conn = oracledb.connect(user=s.oracle_user, password=s.oracle_password, dsn=s.oracle_dsn)
    try:
        yield conn
    finally:
        conn.close()


def upsert_corps(corps: list[CorpInfo]) -> int:
    if not corps:
        return 0
    rows = [c.model_dump() for c in corps]
    with connect() as conn, conn.cursor() as cur:
        cur.executemany(_MERGE_CORP, rows)
        conn.commit()
    logger.info("upsert_corps: %d rows", len(rows))
    return len(rows)


def upsert_filings(filings: list[Filing]) -> int:
    if not filings:
        return 0
    rows = [
        {**f.model_dump(), "raw_json": json.dumps(f.model_dump(), ensure_ascii=False)}
        for f in filings
    ]
    with connect() as conn, conn.cursor() as cur:
        cur.executemany(_MERGE_FILING, rows)
        conn.commit()
    logger.info("upsert_filings: %d rows", len(rows))
    return len(rows)


def upsert_prices(prices: list[DailyPrice]) -> int:
    if not prices:
        return 0
    rows = [p.model_dump() for p in prices]
    with connect() as conn, conn.cursor() as cur:
        cur.executemany(_MERGE_PRICE, rows)
        conn.commit()
    logger.info("upsert_prices: %d rows", len(rows))
    return len(rows)


def corp_codes_for_stocks(stock_codes: list[str]) -> dict[str, str]:
    """상장 종목코드 -> DART corp_code 매핑. corp 마스터 선행 적재가 전제."""
    if not stock_codes:
        return {}
    with connect() as conn, conn.cursor() as cur:
        placeholders = ",".join(f":{i}" for i in range(1, len(stock_codes) + 1))
        cur.execute(
            f"SELECT stock_code, corp_code FROM corp WHERE stock_code IN ({placeholders})",
            stock_codes,
        )
        return {row[0]: row[1] for row in cur.fetchall()}


def record_etl_run(
    job_name: str, status: str, row_count: int, watermark: str | None, error_msg: str | None = None
) -> None:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO etl_run (job_name, watermark, row_count, status,
                                    started_at, finished_at, error_msg)
               VALUES (:1, :2, :3, :4, SYSTIMESTAMP, SYSTIMESTAMP, :5)""",
            [job_name, watermark, row_count, status, error_msg],
        )
        conn.commit()


def latest_watermark(job_name: str) -> str | None:
    """마지막 성공 워터마크 (증분 수집 시작점)."""
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT watermark FROM etl_run
               WHERE job_name = :1 AND status = 'SUCCESS'
               ORDER BY started_at DESC FETCH FIRST 1 ROWS ONLY""",
            [job_name],
        )
        row = cur.fetchone()
    return row[0] if row else None
