"""Daily KRX OHLCV ingestion for universe tickers. 워터마크 기반 증분."""

import csv
import logging
from datetime import date, datetime, timedelta
from pathlib import Path

from airflow.decorators import dag, task

logger = logging.getLogger(__name__)

JOB_NAME = "krx_prices"
BACKFILL_START = date(2021, 1, 1)  # 팩터 계산용 5년
UNIVERSE_CSV = Path("/opt/kograph/data/universe/universe_seed.csv")


def _load_universe() -> list[str]:
    if not UNIVERSE_CSV.exists():
        raise FileNotFoundError(f"universe file missing: {UNIVERSE_CSV}")
    with UNIVERSE_CSV.open(encoding="utf-8") as f:
        return [row["stock_code"].zfill(6) for row in csv.DictReader(f)]


@dag(
    dag_id="krx_prices",
    schedule="30 18 * * 1-5",  # 평일 18:30 KST (장 마감 후)
    start_date=datetime(2026, 8, 1),
    catchup=False,
    tags=["kograph", "etl"],
)
def krx_prices():
    @task
    def ingest_prices() -> int:
        from kograph.collectors.krx import fetch_ohlcv
        from kograph.db.oracle import latest_watermark, record_etl_run, upsert_prices

        wm = latest_watermark(JOB_NAME)
        begin = (
            datetime.strptime(wm, "%Y%m%d").date() + timedelta(days=1)
            if wm
            else BACKFILL_START
        )
        end = date.today()
        if begin > end:
            logger.info("nothing to ingest (watermark up to date)")
            return 0

        total = 0
        failed: list[str] = []
        try:
            for code in _load_universe():
                try:
                    total += upsert_prices(fetch_ohlcv(code, begin, end))
                except Exception:
                    # 개별 종목 실패는 격리하고 계속 (마지막에 집계 보고)
                    logger.exception("failed ticker %s", code)
                    failed.append(code)
            if failed:
                raise RuntimeError(f"{len(failed)} tickers failed: {failed[:10]}")
            record_etl_run(JOB_NAME, "SUCCESS", total, end.strftime("%Y%m%d"))
        except Exception as exc:
            record_etl_run(JOB_NAME, "FAILED", total, None, error_msg=str(exc)[:4000])
            raise
        return total

    ingest_prices()


krx_prices()
