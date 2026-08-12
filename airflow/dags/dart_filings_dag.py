"""Daily DART filings ingestion.

증분 전략: etl_run 워터마크(마지막 성공 rcept_dt) 다음 날부터 오늘까지 수집.
첫 실행은 BACKFILL_START부터. 유니버스 종목만 대상.
"""

import csv
import logging
from datetime import date, datetime, timedelta
from pathlib import Path

from airflow.decorators import dag, task

logger = logging.getLogger(__name__)

JOB_NAME = "dart_filings"
BACKFILL_START = date(2023, 1, 1)  # 최근 3년 스코프
UNIVERSE_CSV = Path("/opt/kograph/data/universe/universe_seed.csv")
# 관계 추출이 명확한 공시 유형만: B(주요사항보고) I(거래소공시-공급계약 등)
TARGET_TYPES = ["B", "I"]


def _load_universe() -> list[str]:
    if not UNIVERSE_CSV.exists():
        raise FileNotFoundError(f"universe file missing: {UNIVERSE_CSV}")
    with UNIVERSE_CSV.open(encoding="utf-8") as f:
        return [row["stock_code"].zfill(6) for row in csv.DictReader(f)]


@dag(
    dag_id="dart_filings",
    schedule="0 20 * * 1-5",  # 평일 20:00 KST 이후 (당일 공시 마감분)
    start_date=datetime(2026, 8, 1),
    catchup=False,
    tags=["kograph", "etl"],
)
def dart_filings():
    @task
    def sync_corp_codes() -> int:
        """기업 고유번호 마스터 갱신 (주 1회면 충분하지만 멱등이라 매일 수행)."""
        from kograph.collectors.dart import DartClient
        from kograph.config import get_settings
        from kograph.db.oracle import upsert_corps

        client = DartClient(get_settings().dart_api_key)
        return upsert_corps(client.corp_codes())

    @task
    def ingest_filings() -> int:
        from kograph.collectors.dart import DartClient
        from kograph.config import get_settings
        from kograph.db.oracle import latest_watermark, record_etl_run, upsert_filings

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

        universe = set(_load_universe())
        client = DartClient(get_settings().dart_api_key)
        total = 0
        try:
            for pblntf_ty in TARGET_TYPES:
                batch = [
                    f
                    for f in client.filings(begin, end, pblntf_ty=pblntf_ty)
                    if f.stock_code in universe
                ]
                total += upsert_filings(batch)
            record_etl_run(JOB_NAME, "SUCCESS", total, end.strftime("%Y%m%d"))
        except Exception as exc:
            record_etl_run(JOB_NAME, "FAILED", total, None, error_msg=str(exc)[:4000])
            raise
        return total

    sync_corp_codes() >> ingest_filings()


dart_filings()
