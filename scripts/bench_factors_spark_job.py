"""컨테이너 안에서 spark-submit으로 실행되는 벤치마크 잡.

호스트(Windows)에서 PySpark는 winutils.exe 없이 임시 디렉터리 권한 설정에서
죽는다(HADOOP_HOME unset). winutils는 Apache 공식 배포물이 아니므로 받지 않고,
공식 apache/spark 이미지 안에서 리포 코드를 그대로 실행한다.

bench_factors.py가 docker run으로 이 파일을 spark-submit 한다. 직접 실행하지
않는다 — 타이밍 출력 형식(TIMING: ...)을 호스트 쪽이 파싱한다.
"""

import sys
import time

sys.path.insert(0, "/work/src")  # 리포 코드를 수정 없이 그대로 검증한다

from pyspark.sql import SparkSession  # noqa: E402

t0 = time.perf_counter()
spark = (SparkSession.builder.appName("kograph-factors-bench")
         .config("spark.sql.shuffle.partitions", "8")
         .config("spark.ui.enabled", "false")
         .getOrCreate())
t_start = time.perf_counter() - t0

from kograph.spark.factors import compute_factors, read_prices  # noqa: E402

t1 = time.perf_counter()
factors = compute_factors(read_prices(spark))
# coalesce(1): 호스트 pandas가 단일 CSV로 대조할 수 있게 한 파일로 모은다
(factors.coalesce(1).write.mode("overwrite")
 .option("header", True).csv("/work/data/interim/factors_spark_csv"))
n = factors.count()
t_compute = time.perf_counter() - t1

print(f"TIMING: session_start={t_start:.3f}")
print(f"TIMING: compute_write={t_compute:.3f}")
print(f"TIMING: rows={n}")
spark.stop()
