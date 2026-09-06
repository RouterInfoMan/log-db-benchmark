"""Shared configuration for the benchmark harness."""
import os

HOST = os.environ.get("BENCH_HOST", "127.0.0.1")

ES_URL = os.environ.get("ES_URL", f"http://{HOST}:9200")
LOKI_URL = os.environ.get("LOKI_URL", f"http://{HOST}:3100")
VL_URL = os.environ.get("VL_URL", f"http://{HOST}:9428")
PROM_URL = os.environ.get("PROM_URL", f"http://{HOST}:9090")
PUSHGATEWAY_URL = os.environ.get("PUSHGATEWAY_URL", f"http://{HOST}:9091")
EXPORTER_URL = os.environ.get("EXPORTER_URL", f"http://{HOST}:9101")

ES_INDEX = "logs-bench"
DATABASES = ("elasticsearch", "loki", "victorialogs")

# Named workload sizes. window_hours is the time span the synthetic logs cover.
PROFILES = {
    "smoke":  {"docs":    200_000, "window_hours":  2, "batch_size":  5_000, "workers": 4},
    "small":  {"docs":  1_000_000, "window_hours":  6, "batch_size": 10_000, "workers": 6},
    "default":{"docs":  5_000_000, "window_hours": 24, "batch_size": 20_000, "workers": 8},
    "large":  {"docs": 25_000_000, "window_hours": 72, "batch_size": 25_000, "workers": 8},
}

SEED = 20260906

# Query repetitions. Cold runs are discarded, then `repeats` timed runs are kept.
WARMUP_RUNS = 1
QUERY_REPEATS = 5

# Seconds to idle after ingest so background merges/compaction settle before
# disk footprint and query latency are measured.
SETTLE_SECONDS = 60

RESULTS_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "results")
