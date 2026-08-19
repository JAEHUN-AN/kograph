"""팩터 배치: pandas vs PySpark 처리 시간 비교 (notes/000 W1 지표).

실행: uv run python scripts/bench_factors.py

동일 계산(mom_20/mom_60/vol_20/turnover_20)을 두 엔진으로 돌려 시간을 잰다.
Spark는 세션 기동과 계산을 분리해 잰다 — 합치면 어느 쪽이 병목인지 안 보인다.

공정성 장치:
- pandas rolling.std는 ddof=1(표본)이라 Spark stddev와 정의가 같다
- min_periods=2로 맞춰 창 초입의 null 처리를 Spark 윈도우와 정렬한다
- 두 결과를 (종목, 일자)로 조인해 값이 실제로 같은지 검증한다.
  시간만 재고 결과가 다르면 벤치마크가 아니라 서로 다른 프로그램이다.
"""

from __future__ import annotations

import os
import time

import pandas as pd

MOM_SHORT, MOM_LONG, VOL_WINDOW = 20, 60, 20
OJDBC = "com.oracle.database.jdbc:ojdbc11:23.5.0.24.07"


def load_prices_pandas() -> pd.DataFrame:
    from kograph.db.oracle import connect

    with connect() as conn, conn.cursor() as cur:
        cur.execute("""SELECT stock_code, trade_date, close_price, trade_value
                       FROM price_daily""")
        rows = cur.fetchall()
    return pd.DataFrame(rows, columns=["STOCK_CODE", "TRADE_DATE",
                                       "CLOSE_PRICE", "TRADE_VALUE"])


def compute_pandas(prices: pd.DataFrame) -> pd.DataFrame:
    df = prices.sort_values(["STOCK_CODE", "TRADE_DATE"]).copy()
    g = df.groupby("STOCK_CODE", sort=False)

    close = df["CLOSE_PRICE"].astype(float)
    df["daily_ret"] = close / g["CLOSE_PRICE"].shift(1).astype(float) - 1
    df["mom_20"] = close / g["CLOSE_PRICE"].shift(MOM_SHORT).astype(float) - 1
    df["mom_60"] = close / g["CLOSE_PRICE"].shift(MOM_LONG).astype(float) - 1
    gg = df.groupby("STOCK_CODE", sort=False)
    df["vol_20"] = (gg["daily_ret"]
                    .rolling(VOL_WINDOW, min_periods=2).std()
                    .reset_index(level=0, drop=True))
    df["turnover_20"] = (gg["TRADE_VALUE"]
                         .rolling(VOL_WINDOW, min_periods=1).mean()
                         .reset_index(level=0, drop=True))
    return (df.dropna(subset=["mom_20"])
              [["STOCK_CODE", "TRADE_DATE", "mom_20", "mom_60",
                "vol_20", "turnover_20"]])


def run_spark() -> tuple[pd.DataFrame, float, float]:
    """(결과, 세션 기동 초, 계산+쓰기 초) — 공식 apache/spark 컨테이너에서 실행.

    Windows 호스트의 PySpark는 winutils.exe(비공식 바이너리) 없이는 임시
    디렉터리 권한 설정에서 죽는다. 서드파티 실행파일을 받는 대신 공식
    이미지 안에서 리포 코드를 그대로 돌린다. 자격증명은 env로만 전달하고
    출력하지 않는다.
    """
    import glob
    import re
    import subprocess

    from kograph.config import get_settings

    s = get_settings()
    repo = os.getcwd().replace("\\", "/")
    cmd = [
        "docker", "run", "--rm", "-u", "0",
        "-v", f"{repo}:/work",
        "-e", "ORACLE_HOST=host.docker.internal",
        "-e", f"ORACLE_PORT={s.oracle_port}",
        "-e", f"ORACLE_SERVICE={s.oracle_service}",
        "-e", f"ORACLE_USER={s.oracle_user}",
        "-e", f"ORACLE_PASSWORD={s.oracle_password}",
        "-e", "DART_API_KEY=unused-in-benchmark",
        "apache/spark:3.5.1",
        "bash", "-c",
        "pip install -q pydantic-settings && "
        "/opt/spark/bin/spark-submit "
        f"--packages {OJDBC} --conf spark.jars.ivy=/tmp/.ivy "
        "/work/scripts/bench_factors_spark_job.py",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    timings = dict(re.findall(r"TIMING: (\w+)=([\d.]+)", proc.stdout or ""))
    if proc.returncode != 0 or "compute_write" not in timings:
        # 비밀값이 섞이지 않는 stderr 꼬리만 보여준다
        tail = (proc.stderr or "")[-1500:]
        msg = f"spark 컨테이너 실패 (rc={proc.returncode})"
        raise RuntimeError(msg + "\n" + tail)

    part = sorted(glob.glob("data/interim/factors_spark_csv/part-*.csv"))
    result = pd.read_csv(part[0])
    return result, float(timings["session_start"]), float(timings["compute_write"])


def main() -> int:
    print("=== pandas ===")
    t0 = time.perf_counter()
    prices = load_prices_pandas()
    t_load = time.perf_counter() - t0
    t1 = time.perf_counter()
    pd_out = compute_pandas(prices)
    t_pd = time.perf_counter() - t1
    print(f"  적재 {t_load:.2f}s / 계산 {t_pd:.2f}s / 총 {t_load + t_pd:.2f}s"
          f" ({len(pd_out):,}행)")

    print("=== PySpark (local) ===")
    sp_out, t_start, t_compute = run_spark()
    print(f"  세션 기동 {t_start:.2f}s / 읽기+계산+수집 {t_compute:.2f}s"
          f" / 총 {t_start + t_compute:.2f}s ({len(sp_out):,}행)")

    # 결과 동일성: 행수 + 전 컬럼 수치 대조
    if len(pd_out) != len(sp_out):
        print(f"FAIL: 행수 불일치 pandas={len(pd_out)} spark={len(sp_out)}")
        return 1
    key = ["STOCK_CODE", "TRADE_DATE"]
    pd_out = pd_out.assign(
        TRADE_DATE=pd.to_datetime(pd_out["TRADE_DATE"]).dt.strftime("%Y-%m-%d"))
    sp_out = sp_out.assign(
        TRADE_DATE=pd.to_datetime(sp_out["TRADE_DATE"]).dt.strftime("%Y-%m-%d"),
        STOCK_CODE=sp_out["STOCK_CODE"].astype(str).str.zfill(6))
    merged = pd_out.merge(sp_out, on=key, suffixes=("_pd", "_sp"))
    if len(merged) != len(pd_out):
        print(f"FAIL: 키 조인 불일치 ({len(merged)}/{len(pd_out)})")
        return 1
    worst = 0.0
    for col in ("mom_20", "mom_60", "vol_20", "turnover_20"):
        a, b = merged[f"{col}_pd"].astype(float), merged[f"{col}_sp"].astype(float)
        diff = (a - b).abs() / b.abs().clip(lower=1e-12)
        d = float(diff.fillna(0).max())
        worst = max(worst, d)
        print(f"  {col:12} 최대 상대오차 {d:.2e}")
    if worst > 1e-9:
        print("FAIL: 결과 불일치 — 시간 비교가 무의미하다")
        return 1

    ratio = (t_start + t_compute) / max(t_load + t_pd, 1e-9)
    print(f"\n결론: 이 규모({len(prices):,}행)에서 Spark 총시간은 pandas의 {ratio:.1f}배")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
