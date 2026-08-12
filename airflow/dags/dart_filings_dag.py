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
        # DART 정책: corp_code 미지정 조회는 검색기간 3개월 제한.
        # -> 유니버스 종목별 corp_code 지정 조회 (기간 무제한, 트래픽도 적음)
        from kograph.collectors.dart import DartClient
        from kograph.config import get_settings
        from kograph.db.oracle import (
            corp_codes_for_stocks,
            latest_watermark,
            record_etl_run,
            upsert_filings,
        )

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

        universe = _load_universe()
        mapping = corp_codes_for_stocks(universe)
        missing = sorted(set(universe) - set(mapping))
        if missing:
            # corp 마스터에 없는 종목은 건너뛰되 반드시 드러낸다
            logger.warning("no corp_code for %d tickers: %s", len(missing), missing)

        client = DartClient(get_settings().dart_api_key)
        total = 0
        try:
            for stock_code, corp_code in mapping.items():
                for pblntf_ty in TARGET_TYPES:
                    batch = list(
                        client.filings(begin, end, corp_code=corp_code, pblntf_ty=pblntf_ty)
                    )
                    total += upsert_filings(batch)
                logger.info("ingested %s (corp_code=%s)", stock_code, corp_code)
            record_etl_run(JOB_NAME, "SUCCESS", total, end.strftime("%Y%m%d"))
        except Exception as exc:
            record_etl_run(JOB_NAME, "FAILED", total, None, error_msg=str(exc)[:4000])
            raise
        return total

    sync_corp_codes() >> ingest_filings()


dart_filings()
