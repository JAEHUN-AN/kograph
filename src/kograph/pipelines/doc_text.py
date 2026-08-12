"""Backfill filing.doc_text from the DART document API.

실행: uv run python -m kograph.pipelines.doc_text [--limit N]

- doc_fetched_at IS NULL 인 공시만 처리 (재실행 안전)
- 실패한 건도 doc_fetched_at을 찍어 무한 재시도를 막고, 로그로 드러낸다
- 배치 커밋(50건) + 스로틀
"""

import argparse
import logging
import time

from kograph.collectors.dart import DartApiError, DartClient
from kograph.config import get_settings
from kograph.db.oracle import connect

logger = logging.getLogger(__name__)

THROTTLE_SECONDS = 0.15  # DART 분당 1,000회 제한 대비 여유
COMMIT_EVERY = 50
MAX_DOC_CHARS = 400_000  # 비정상적으로 큰 첨부 방어


def pending_rcept_nos(limit: int | None) -> list[str]:
    sql = "SELECT rcept_no FROM filing WHERE doc_fetched_at IS NULL ORDER BY rcept_dt"
    if limit:
        sql += f" FETCH FIRST {int(limit)} ROWS ONLY"
    with connect() as conn, conn.cursor() as cur:
        cur.execute(sql)
        return [r[0] for r in cur.fetchall()]


def backfill(limit: int | None = None) -> tuple[int, int]:
    """Returns (성공 건수, 실패 건수)."""
    todo = pending_rcept_nos(limit)
    if not todo:
        logger.info("nothing to backfill")
        return 0, 0

    client = DartClient(get_settings().dart_api_key)
    ok = failed = 0
    started = time.monotonic()

    with connect() as conn, conn.cursor() as cur:
        for i, rcept_no in enumerate(todo, 1):
            try:
                text = client.document_text(rcept_no)[:MAX_DOC_CHARS]
                cur.execute(
                    "UPDATE filing SET doc_text = :1, doc_fetched_at = SYSTIMESTAMP"
                    " WHERE rcept_no = :2",
                    [text, rcept_no],
                )
                ok += 1
            except DartApiError as exc:
                # 본문 미제공 공시(정정 전 문서 등)는 시도 기록만 남기고 계속
                logger.warning("doc fetch failed %s: %s", rcept_no, exc)
                cur.execute(
                    "UPDATE filing SET doc_fetched_at = SYSTIMESTAMP WHERE rcept_no = :1",
                    [rcept_no],
                )
                failed += 1

            if i % COMMIT_EVERY == 0:
                conn.commit()
                elapsed = time.monotonic() - started
                logger.info("progress %d/%d (%.0fs elapsed)", i, len(todo), elapsed)
            time.sleep(THROTTLE_SECONDS)
        conn.commit()

    logger.info("done: ok=%d failed=%d elapsed=%.0fs", ok, failed, time.monotonic() - started)
    return ok, failed


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()
    backfill(args.limit)
