"""CSV output and console summary."""
import csv
import os
import statistics


def percentile(values, q):
    """Linear-interpolation percentile. q in [0, 1]."""
    if not values:
        return None
    xs = sorted(values)
    if len(xs) == 1:
        return xs[0]
    pos = q * (len(xs) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


def _write(path, fieldnames, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)
    return path


def write_query_runs(outdir, rows):
    return _write(os.path.join(outdir, "query_runs.csv"), [
        "run_id", "db", "query_id", "kind", "iteration", "warmup",
        "latency_ms", "result_count", "response_bytes", "engine_ms",
        "lines_scanned", "bytes_scanned", "error",
    ], rows)


def write_query_summary(outdir, rows):
    return _write(os.path.join(outdir, "query_summary.csv"), [
        "run_id", "db", "query_id", "title", "kind", "runs", "errors",
        "min_ms", "p50_ms", "mean_ms", "p95_ms", "p99_ms", "max_ms", "stddev_ms",
        "result_count", "result_agrees_across_dbs", "response_bytes",
        "engine_ms", "lines_scanned", "bytes_scanned", "note",
    ], rows)


def write_ingest(outdir, rows):
    return _write(os.path.join(outdir, "ingest.csv"), [
        "run_id", "db", "docs", "wire_bytes", "raw_json_bytes",
        "send_duration_s", "duration_s", "docs_per_sec", "wire_mb_per_sec",
        "batches", "batch_size", "workers",
        "errors", "docs_verified", "docs_match",
    ], rows)


def write_resources(outdir, rows):
    return _write(os.path.join(outdir, "resources.csv"), [
        "run_id", "iso_time", "timestamp", "phase", "detail", "db",
        "cpu_percent", "mem_bytes", "mem_percent", "mem_limit_bytes",
        "disk_bytes", "disk_apparent_bytes",
        "net_rx_bytes", "net_tx_bytes", "blk_read_bytes", "blk_write_bytes",
    ], rows)


def write_resource_summary(outdir, rows):
    return _write(os.path.join(outdir, "resource_summary.csv"), [
        "run_id", "phase", "db", "samples", "duration_s",
        "cpu_avg_percent", "cpu_p95_percent", "cpu_max_percent",
        "mem_avg_mb", "mem_max_mb", "mem_max_percent_of_limit",
        "disk_start_mb", "disk_end_mb", "disk_growth_mb",
        "blk_write_mb", "blk_read_mb",
    ], rows)


def write_summary(outdir, rows, query_ids):
    fields = [
        "run_id", "profile", "db", "image", "docs",
        "ingest_send_s", "ingest_duration_s", "ingest_docs_per_sec", "ingest_mb_per_sec",
        "ingest_cpu_avg_percent", "ingest_cpu_max_percent",
        "ingest_mem_avg_mb", "ingest_mem_max_mb",
        "disk_mb", "disk_at_settle_mb", "bytes_per_doc", "compression_ratio_vs_raw_json",
        "query_cpu_avg_percent", "query_cpu_max_percent", "query_mem_max_mb",
        "query_mean_ms_all", "query_p95_ms_all", "query_errors",
    ] + [f"{qid}_mean_ms" for qid in query_ids]
    return _write(os.path.join(outdir, "summary.csv"), fields, rows)


def stats_for(latencies_ms):
    if not latencies_ms:
        return {}
    return {
        "min_ms": round(min(latencies_ms), 3),
        "p50_ms": round(percentile(latencies_ms, 0.50), 3),
        "mean_ms": round(statistics.fmean(latencies_ms), 3),
        "p95_ms": round(percentile(latencies_ms, 0.95), 3),
        "p99_ms": round(percentile(latencies_ms, 0.99), 3),
        "max_ms": round(max(latencies_ms), 3),
        "stddev_ms": round(statistics.stdev(latencies_ms), 3) if len(latencies_ms) > 1 else 0.0,
    }


# --------------------------------------------------------------------------- #
#  Console rendering                                                          #
# --------------------------------------------------------------------------- #
def _fmt(v, width, prec=1):
    if v is None or v == "":
        return "-".rjust(width)
    if isinstance(v, float):
        return f"{v:,.{prec}f}".rjust(width)
    if isinstance(v, int):
        return f"{v:,}".rjust(width)
    return str(v).rjust(width)


def print_table(title, headers, rows, widths):
    print()
    print(f"  {title}")
    print("  " + "-" * (sum(widths) + 2 * (len(widths) - 1)))
    print("  " + "  ".join(h.rjust(w) for h, w in zip(headers, widths)))
    print("  " + "-" * (sum(widths) + 2 * (len(widths) - 1)))
    for row in rows:
        print("  " + "  ".join(_fmt(v, w) for v, w in zip(row, widths)))


def print_console_report(summary_rows, query_summary_rows, dbs, query_ids, query_titles):
    print_table(
        "INGEST",
        ["database", "docs", "send s", "docs/sec", "MB/s", "cpu avg%", "cpu max%", "mem max MB"],
        [[r["db"], r["docs"], r["ingest_send_s"], r["ingest_docs_per_sec"],
          r["ingest_mb_per_sec"], r["ingest_cpu_avg_percent"], r["ingest_cpu_max_percent"],
          r["ingest_mem_max_mb"]] for r in summary_rows],
        [14, 11, 9, 11, 8, 9, 9, 11])

    print_table(
        "STORAGE  (settled = steady state; peak = right after flush)",
        ["database", "disk MB", "peak MB", "bytes/doc", "vs raw JSON"],
        [[r["db"], r["disk_mb"], r["disk_at_settle_mb"], r["bytes_per_doc"],
          f'{r["compression_ratio_vs_raw_json"]}x'] for r in summary_rows],
        [14, 12, 11, 11, 13])

    by_q = {}
    for r in query_summary_rows:
        by_q.setdefault(r["query_id"], {})[r["db"]] = r
    rows = []
    for qid in query_ids:
        row = [qid.split("_", 1)[1][:26]]
        for db in dbs:
            r = by_q.get(qid, {}).get(db)
            p50 = (r or {}).get("p50_ms")
            row.append("FAILED" if (r and int(r["errors"]) and p50 is None) else p50)
        rows.append(row)
    print_table("QUERY LATENCY p50 (ms)", ["query"] + list(dbs), rows,
                [28] + [14] * len(dbs))

    mismatches = [r for r in query_summary_rows if r.get("result_agrees_across_dbs") == "NO"]
    if mismatches:
        seen = sorted({r["query_id"] for r in mismatches})
        print(f"\n  NOTE: result counts differ across engines for: {', '.join(seen)}")
        print("        (see query_summary.csv -- engines answer these questions differently)")
