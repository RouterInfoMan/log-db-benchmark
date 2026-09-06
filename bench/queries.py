"""The query suite.

Each entry expresses the *same logical question* in each engine's native
language. Where the engines differ (anchored vs unanchored regex, analyzed vs
substring text matching) the expressions are chosen so all three return the
same answer -- the harness cross-checks result counts and flags disagreement,
so a mismatch shows up as a data point instead of hiding in the latency numbers.

kinds:
  search  -- return up to `limit` raw log lines
  count   -- return a single scalar count
  groups  -- return one row per group
  series  -- return a time series of buckets
"""

LOKI_SELECTOR = '{job="bench"}'
TAIL_SECONDS = 300
REGEX_PATH = "/api/v[0-9]+/(checkout|payment)"

QUERIES = [
    {
        "id": "q01_needle_rare_term",
        "title": "Needle in a haystack: rare token over the full range",
        "kind": "search",
        "limit": 100,
        "note": "Full-text scan for a token present in ~1 of every 200k lines.",
        "es": lambda c: {"size": 100, "track_total_hits": True,
                         "query": {"bool": {"filter": [
                             {"range": {"timestamp": {"gte": c["start_ms"], "lt": c["end_ms"]}}},
                             {"match": {"message": "CANARY7"}}]}}},
        "loki": lambda c: ("query_range", {
            "query": f'{LOKI_SELECTOR} |= "CANARY7"',
            "start": c["start_ns"], "end": c["end_ns"], "limit": 100, "direction": "backward"}),
        "vl": lambda c: ("query", {
            "query": f'{c["vl_time"]} "CANARY7"', "limit": 100}),
    },
    {
        "id": "q02_count_errors",
        "title": "Count all ERROR-level events over the full range",
        "kind": "count",
        "note": "Cheap label/keyword filter plus a full-range count.",
        "es": lambda c: {"size": 0, "track_total_hits": True,
                         "query": {"bool": {"filter": [
                             {"range": {"timestamp": {"gte": c["start_ms"], "lt": c["end_ms"]}}},
                             {"term": {"level": "ERROR"}}]}}},
        "loki": lambda c: ("query", {
            "query": f'sum(count_over_time({{level="ERROR", job="bench"}}[{c["range_str"]}]))',
            "time": c["end_ns"]}),
        "vl": lambda c: ("stats_query", {
            "query": f'{c["vl_time"]} level:ERROR | stats count() as hits'}),
    },
    {
        "id": "q03_service_text_filter",
        "title": "One service AND free-text 'timeout'",
        "kind": "search",
        "limit": 500,
        "note": "Stream/label narrowing combined with a full-text term.",
        "es": lambda c: {"size": 500, "track_total_hits": True,
                         "query": {"bool": {"filter": [
                             {"range": {"timestamp": {"gte": c["start_ms"], "lt": c["end_ms"]}}},
                             {"term": {"service": "checkout-api"}},
                             {"match": {"message": "timeout"}}]}}},
        "loki": lambda c: ("query_range", {
            "query": f'{{service="checkout-api", job="bench"}} |= "timeout"',
            "start": c["start_ns"], "end": c["end_ns"], "limit": 500, "direction": "backward"}),
        "vl": lambda c: ("query", {
            "query": f'{c["vl_time"]} service:checkout-api "timeout"', "limit": 500}),
    },
    {
        "id": "q04_numeric_range_count",
        "title": "Slow failures: status=500 AND latency_ms > 900",
        "kind": "count",
        "note": "Numeric predicates on two fields. Loki must JSON-parse at query time.",
        "es": lambda c: {"size": 0, "track_total_hits": True,
                         "query": {"bool": {"filter": [
                             {"range": {"timestamp": {"gte": c["start_ms"], "lt": c["end_ms"]}}},
                             {"term": {"status": 500}},
                             {"range": {"latency_ms": {"gt": 900}}}]}}},
        "loki": lambda c: ("query", {
            "query": (f'sum(count_over_time({LOKI_SELECTOR} | json '
                      f'| status = 500 | latency_ms > 900 [{c["range_str"]}]))'),
            "time": c["end_ns"]}),
        "vl": lambda c: ("stats_query", {
            "query": f'{c["vl_time"]} status:=500 latency_ms:>900 | stats count() as hits'}),
    },
    {
        "id": "q05_regex_path",
        "title": "Anchored regex over the request path",
        "kind": "count",
        "note": "Regex evaluation; anchored in all three engines for identical semantics.",
        "es": lambda c: {"size": 0, "track_total_hits": True,
                         "query": {"bool": {"filter": [
                             {"range": {"timestamp": {"gte": c["start_ms"], "lt": c["end_ms"]}}},
                             {"regexp": {"path": REGEX_PATH}}]}}},
        "loki": lambda c: ("query", {
            "query": (f'sum(count_over_time({LOKI_SELECTOR} | json '
                      f'| path =~ "{REGEX_PATH}" [{c["range_str"]}]))'),
            "time": c["end_ns"]}),
        "vl": lambda c: ("stats_query", {
            "query": f'{c["vl_time"]} path:~"^{REGEX_PATH}$" | stats count() as hits'}),
    },
    {
        "id": "q06_group_by_service",
        "title": "Count grouped by service (top-N aggregation)",
        "kind": "groups",
        "note": "Group-by over the whole dataset.",
        "es": lambda c: {"size": 0, "track_total_hits": False,
                         "query": {"range": {"timestamp": {"gte": c["start_ms"], "lt": c["end_ms"]}}},
                         "aggs": {"by_service": {"terms": {"field": "service", "size": 20}}}},
        "loki": lambda c: ("query", {
            "query": f'sum by (service) (count_over_time({LOKI_SELECTOR}[{c["range_str"]}]))',
            "time": c["end_ns"]}),
        "vl": lambda c: ("stats_query", {
            "query": f'{c["vl_time"]} * | stats by (service) count() as hits'}),
    },
    {
        "id": "q07_recent_tail",
        "title": "Tail the last 5 minutes of the dataset",
        "kind": "search",
        "limit": 1000,
        "note": "The everyday 'show me what just happened' query.",
        "es": lambda c: {"size": 1000, "track_total_hits": True,
                         "sort": [{"timestamp": "desc"}],
                         "query": {"range": {"timestamp": {"gte": c["tail_start_ms"], "lt": c["end_ms"]}}}},
        "loki": lambda c: ("query_range", {
            "query": LOKI_SELECTOR, "start": c["tail_start_ns"], "end": c["end_ns"],
            "limit": 1000, "direction": "backward"}),
        "vl": lambda c: ("query", {
            "query": f'{c["vl_tail_time"]} *', "limit": 1000}),
    },
    {
        "id": "q08_error_rate_1m",
        "title": "Per-minute ERROR histogram across the full range",
        "kind": "series",
        "note": "The query behind every error-rate dashboard panel.",
        "es": lambda c: {"size": 0, "track_total_hits": False,
                         "query": {"bool": {"filter": [
                             {"range": {"timestamp": {"gte": c["start_ms"], "lt": c["end_ms"]}}},
                             {"term": {"level": "ERROR"}}]}},
                         "aggs": {"per_min": {"date_histogram": {
                             "field": "timestamp", "fixed_interval": "1m", "min_doc_count": 1}}}},
        "loki": lambda c: ("query_range", {
            "query": 'sum(count_over_time({level="ERROR", job="bench"}[1m]))',
            "start": c["start_ns"], "end": c["end_ns"], "step": "60s"}),
        "vl": lambda c: ("stats_query_range", {
            "query": f'level:ERROR | stats count() as hits',
            "start": c["start_s"], "end": c["end_s"], "step": "1m"}),
    },
    {
        "id": "q09_unique_users_errors",
        "title": "Distinct users that hit a 500",
        "kind": "count",
        "optional": True,
        "note": ("High-cardinality distinct count. Exact in VictoriaLogs, approximate "
                 "(HyperLogLog++) in Elasticsearch; Loki must build one series per user "
                 "and may exceed max_query_series -- a failure here is itself a result."),
        "es": lambda c: {"size": 0, "track_total_hits": False,
                         "query": {"bool": {"filter": [
                             {"range": {"timestamp": {"gte": c["start_ms"], "lt": c["end_ms"]}}},
                             {"term": {"status": 500}}]}},
                         "aggs": {"uniq": {"cardinality": {"field": "user_id"}}}},
        "loki": lambda c: ("query", {
            "query": (f'count(sum by (user_id) (count_over_time({LOKI_SELECTOR} | json '
                      f'| status = 500 [{c["range_str"]}])))'),
            "time": c["end_ns"]}),
        "vl": lambda c: ("stats_query", {
            "query": f'{c["vl_time"]} status:=500 | stats count_uniq(user_id) as uniq'}),
    },
]


def build_context(start_ms, end_ms):
    """Everything the per-engine builders need to express the same time range."""
    from datetime import datetime, timezone

    def rfc(ms):
        return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

    # Coerced to int: a float epoch is rejected by Elasticsearch (epoch_millis)
    # and by Loki (nanosecond timestamps), while VictoriaLogs silently accepts it.
    start_ms, end_ms = int(start_ms), int(end_ms)
    tail_start_ms = max(start_ms, end_ms - TAIL_SECONDS * 1000)
    return {
        "start_ms": start_ms, "end_ms": end_ms,
        "start_s": start_ms // 1000, "end_s": (end_ms + 999) // 1000,
        "start_ns": start_ms * 1_000_000, "end_ns": end_ms * 1_000_000,
        "tail_start_ms": tail_start_ms, "tail_start_ns": tail_start_ms * 1_000_000,
        "range_str": f"{max(1, (end_ms - start_ms) // 1000)}s",
        "vl_time": f"_time:[{rfc(start_ms)}, {rfc(end_ms)}]",
        "vl_tail_time": f"_time:[{rfc(tail_start_ms)}, {rfc(end_ms)}]",
    }


def by_id(qid):
    for q in QUERIES:
        if q["id"] == qid:
            return q
    raise KeyError(qid)
