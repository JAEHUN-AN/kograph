"""PySpark batch factor computation over price_daily.

산출 팩터 (종목·일 단위):
- mom_20 / mom_60: 20·60일 모멘텀 (수익률)
- vol_20: 20일 수익률 표준편차 (연율화 없음, raw)
- turnover_20: 20일 평균 거래대금

실행:
    spark-submit --jars ojdbc11.jar src/kograph/spark/factors.py
(Week 1 벤치마크: 동일 계산의 pandas 버전과 처리 시간 비교 — notes/에 기록)
"""

import logging

from pyspark.sql import DataFrame, SparkSession, Window
from pyspark.sql import functions as F

from kograph.config import get_settings

logger = logging.getLogger(__name__)

MOM_SHORT = 20
MOM_LONG = 60
VOL_WINDOW = 20


def read_prices(spark: SparkSession) -> DataFrame:
    s = get_settings()
    return (
        spark.read.format("jdbc")
        .option("url", f"jdbc:oracle:thin:@//{s.oracle_host}:{s.oracle_port}/{s.oracle_service}")
        .option("dbtable", "price_daily")
        .option("user", s.oracle_user)
        .option("password", s.oracle_password)
        .option("driver", "oracle.jdbc.OracleDriver")
        .option("fetchsize", 10000)
        .load()
    )


def compute_factors(prices: DataFrame) -> DataFrame:
    """윈도우 함수 기반 팩터 계산. 입력: stock_code, trade_date, close_price, trade_value."""
    w = Window.partitionBy("STOCK_CODE").orderBy("TRADE_DATE")
    w_vol = w.rowsBetween(-(VOL_WINDOW - 1), 0)

    daily_ret = F.col("CLOSE_PRICE") / F.lag("CLOSE_PRICE", 1).over(w) - 1

    return (
        prices.withColumn("daily_ret", daily_ret)
        .withColumn("mom_20", F.col("CLOSE_PRICE") / F.lag("CLOSE_PRICE", MOM_SHORT).over(w) - 1)
        .withColumn("mom_60", F.col("CLOSE_PRICE") / F.lag("CLOSE_PRICE", MOM_LONG).over(w) - 1)
        .withColumn("vol_20", F.stddev("daily_ret").over(w_vol))
        .withColumn("turnover_20", F.avg("TRADE_VALUE").over(w_vol))
        .select(
            "STOCK_CODE", "TRADE_DATE", "mom_20", "mom_60", "vol_20", "turnover_20"
        )
        .na.drop(subset=["mom_20"])
    )


def main() -> None:
    spark = (
        SparkSession.builder.appName("kograph-factors")
        .config("spark.sql.shuffle.partitions", "8")  # 로컬 규모에 맞춤
        .getOrCreate()
    )
    try:
        factors = compute_factors(read_prices(spark))
        # Week 1: parquet 출력. (Week 3에서 Oracle FACTOR 테이블 적재로 교체)
        factors.write.mode("overwrite").parquet("data/interim/factors.parquet")
        logger.info("factors written: %d rows", factors.count())
    finally:
        spark.stop()


if __name__ == "__main__":
    main()
