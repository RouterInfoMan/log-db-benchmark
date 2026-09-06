#!/usr/bin/env python3
"""Benchmark orchestrator: ingest, measure, query, report.

Each database is exercised sequentially and in isolation so that the CPU,
memory and disk numbers attributed to it are actually its own.
"""
import argparse
import concurrent.futures as futures
import datetime as dt
import json
import os
import subprocess
import sys
import time

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bench import config, generator, metrics, queries, report, targets

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def log(msg):
    print(f"[{dt.datetime.now():%H:%M:%S}] {msg}", flush=True)


def sh(cmd, check=True):
    return subprocess.run(cmd, shell=True, cwd=REPO, check=check,
                          capture_output=True, text=True)


def image_for(db):
    key = {"elasticsearch": "ES_IMAGE", "loki": "LOKI_IMAGE", "victorialogs": "VL_IMAGE"}[db]
    env_path = os.path.join(REPO, ".env")
    if os.path.exists(env_path):
        for line in open(env_path):
            if line.startswith(key + "="):
                return line.split("=", 1)[1].strip()
    return "?"


# --------------------------------------------------------------------------- #
#  Stack lifecycle                                                            #
# --------------------------------------------------------------------------- #
def wipe_data_dirs(dbs):
    """Clear the bind-mounted data dirs.

    The databases run as root inside their containers, so their files are not
    removable by the host user; a throwaway root container does the deleting.
    """
    subdirs = " ".join(f"/data/{db}" for db in dbs)
    sh("docker run --rm -v " + repr(os.path.join(REPO, "data")) + ":/data "
       "python:3.12-slim sh -c " + repr(
           f"rm -rf {subdirs} && mkdir -p {subdirs} && chmod 777 {subdirs}"))


def reset_stack(dbs):
    """Wipe every data directory so each run starts from an empty database."""
    log("resetting stack (stopping containers and clearing data dirs)")
    # --profile render also stops the optional renderer, so benchmarks run
    # against a minimal stack and the network is released cleanly.
    sh("docker compose --profile render down --remove-orphans", check=False)
    wipe_data_dirs(dbs)
    sh("docker compose up -d --build")
    log("stack restarted")


def wait_all_ready(tgts):
    for t in tgts:
        log(f"waiting for {t.name} ...")
        t.wait_ready(timeout=300)
    metrics.scrape_exporter()
    log("all systems ready")


# --------------------------------------------------------------------------- #
#  Ingest                                                                     #
# --------------------------------------------------------------------------- #
def ingest(target, plan, sampler):
    """Push the whole dataset into one database and time it.

    Batches are dispatched in index order with a bounded number in flight, which
    keeps timestamps close to monotonic -- Loki requires near-ordered writes per
    stream, and holding all three to the same dispatch pattern keeps it fair.
    """
    total_batches = plan["batches"]
    sampler.set_phase("ingest", target.name)

    wire_bytes = raw_bytes = 0
    errors = []
    t0 = time.perf_counter()

    def work(batch_idx):
        recs = generator.batch(batch_idx, plan["batch_size"], plan["docs"],
                               plan["start_ms"], plan["ms_per_doc"], plan["seed"])
        if not recs:
            return 0, 0
        raw = sum(len(json.dumps(r, separators=(",", ":"))) + 1 for r in recs)
        payload = target.encode(recs)
        for attempt in range(3):
            try:
                return target.send(payload), raw
            except Exception as e:
                if attempt == 2:
                    raise
                time.sleep(1.5 * (attempt + 1))

    with futures.ThreadPoolExecutor(max_workers=plan["workers"]) as pool:
        inflight, next_idx = {}, 0
        window = plan["workers"] * 2
        while next_idx < total_batches and len(inflight) < window:
            inflight[pool.submit(work, next_idx)] = next_idx
            next_idx += 1
        done_count = 0
        while inflight:
            done, _ = futures.wait(inflight, return_when=futures.FIRST_COMPLETED)
            for fut in done:
                idx = inflight.pop(fut)
                try:
                    wb, rb = fut.result()
                    wire_bytes += wb
                    raw_bytes += rb
                except Exception as e:
                    errors.append(f"batch {idx}: {str(e)[:200]}")
                done_count += 1
                if done_count % max(1, total_batches // 20) == 0:
                    pct = 100.0 * done_count / total_batches
                    rate = (done_count * plan["batch_size"]) / max(time.perf_counter() - t0, 1e-9)
                    log(f"  {target.name}: {pct:5.1f}%  ~{rate:,.0f} docs/s")
                if next_idx < total_batches:
                    inflight[pool.submit(work, next_idx)] = next_idx
                    next_idx += 1

    send_seconds = time.perf_counter() - t0
    log(f"  {target.name}: flushing")
    target.finalize()
    duration = time.perf_counter() - t0

    return {
        "docs": plan["docs"],
        "wire_bytes": wire_bytes,
        "raw_json_bytes": raw_bytes,
        "duration_s": round(duration, 3),
        "send_seconds": round(send_seconds, 3),
        "docs_per_sec": round(plan["docs"] / max(send_seconds, 1e-9), 1),
        "wire_mb_per_sec": round(wire_bytes / 2**20 / max(send_seconds, 1e-9), 2),
        "batches": total_batches,
        "batch_size": plan["batch_size"],
        "workers": plan["workers"],
        "errors": len(errors),
        "error_detail": errors[:3],
    }


# Peak bytes on disk per document, measured across all three databases at the
# 5M-document profile (Loki and Elasticsearch both transiently exceed their
# settled size, so this is sized against the peak, not the steady state).
PEAK_BYTES_PER_DOC = 830


def preflight_disk(docs, dbs, skip=False):
    """Refuse to start a long run that cannot possibly fit on disk."""
    est = docs * PEAK_BYTES_PER_DOC * (len(dbs) / 3.0)
    st = os.statvfs(os.path.join(REPO, "data"))
    free = st.f_bavail * st.f_frsize
    est_gb, free_gb = est / 2**30, free / 2**30
    log(f"disk preflight: ~{est_gb:,.1f} GB needed at peak, {free_gb:,.1f} GB free")
    if est * 1.3 > free:
        msg = (f"estimated peak need {est_gb:,.1f} GB (plus headroom) exceeds "
               f"{free_gb:,.1f} GB free on the data volume")
        if skip:
            log(f"  WARNING: {msg} -- continuing because --skip-disk-check was given")
        else:
            raise SystemExit(f"\nAborting: {msg}.\n"
                             f"Free some space, lower --docs, or pass --skip-disk-check.\n")


def wait_for_stable_disk(sampler, dbs, max_wait=600, interval=10, tolerance=0.005):
    """Poll until every database's on-disk size stops moving.

    Loki truncates its WAL and Elasticsearch merges segments for minutes after a
    bulk load; sampling too early overstates both (Loki by ~4.5x in practice).
    This waits for the footprint to actually settle before it is recorded.
    """
    log("waiting for on-disk size to stabilise")
    history = {db: [] for db in dbs}
    deadline = time.time() + max_wait
    while time.time() < deadline:
        snap = sampler.snapshot_now()
        for db in dbs:
            history[db].append(snap.get(db, {}).get("disk_bytes", 0))
        if all(len(v) >= 3 for v in history.values()):
            stable = True
            for db, vals in history.items():
                window = vals[-3:]
                hi = max(window)
                if hi and (hi - min(window)) / hi > tolerance:
                    stable = False
                    break
            if stable:
                break
        time.sleep(interval)

    final = {db: (history[db][-1] if history[db] else 0) for db in dbs}
    for db in dbs:
        log(f"  {db}: settled at {final[db]/2**20:,.1f} MB")
    return final


# --------------------------------------------------------------------------- #
#  Queries                                                                    #
# --------------------------------------------------------------------------- #
def run_queries(target, ctx, specs, repeats, warmups, sampler, run_id, timeout):
    sampler.set_phase("query", target.name)
    runs, summaries = [], []
    for spec in specs:
        sampler.set_phase("query", f"{target.name}:{spec['id']}")
        for w in range(warmups):
            r = target.run_query(spec, ctx, timeout=timeout)
            runs.append(_run_row(run_id, target, spec, -1 - w, True, r))

        lat, last, errs = [], None, 0
        for i in range(repeats):
            r = target.run_query(spec, ctx, timeout=timeout)
            runs.append(_run_row(run_id, target, spec, i, False, r))
            if r.error:
                errs += 1
            else:
                lat.append(r.latency_s * 1000)
                last = r

        s = {"run_id": run_id, "db": target.name, "query_id": spec["id"],
             "title": spec["title"], "kind": spec["kind"], "runs": repeats,
             "errors": errs, "note": spec["note"],
             "result_count": last.result_count if last else None,
             "response_bytes": last.response_bytes if last else None,
             "engine_ms": last.engine_ms if last else None,
             "lines_scanned": last.lines_scanned if last else None,
             "bytes_scanned": last.bytes_scanned if last else None}
        s.update(report.stats_for(lat))
        summaries.append(s)

        if errs == repeats:
            log(f"  {target.name} {spec['id']}: FAILED ({errs}/{repeats})")
        else:
            log(f"  {target.name} {spec['id']}: p50={s.get('p50_ms'):>10,.1f} ms  "
                f"result={s['result_count']}")
    return runs, summaries


def _run_row(run_id, target, spec, iteration, warmup, r):
    return {"run_id": run_id, "db": target.name, "query_id": spec["id"],
            "kind": spec["kind"], "iteration": iteration, "warmup": int(warmup),
            "latency_ms": round(r.latency_s * 1000, 3), "result_count": r.result_count,
            "response_bytes": r.response_bytes, "engine_ms": r.engine_ms,
            "lines_scanned": r.lines_scanned, "bytes_scanned": r.bytes_scanned,
            "error": r.error or ""}


# --------------------------------------------------------------------------- #
#  Aggregation                                                                #
# --------------------------------------------------------------------------- #
def summarize_resources(run_id, rows):
    """Collapse raw samples into one row per (phase, detail-db) pair."""
    groups = {}
    for r in rows:
        groups.setdefault((r["phase"], r.get("detail", ""), r["db"]), []).append(r)

    out = []
    for (phase, detail, db), rs in sorted(groups.items()):
        rs.sort(key=lambda x: x["timestamp"])
        cpu = [x.get("cpu_percent", 0) for x in rs]
        mem = [x.get("mem_bytes", 0) for x in rs]
        disk = [x.get("disk_bytes", 0) for x in rs]
        limit = max((x.get("mem_limit_bytes", 0) for x in rs), default=0)
        label = phase if not detail else f"{phase}:{detail}"
        out.append({
            "run_id": run_id, "phase": label, "db": db, "samples": len(rs),
            "duration_s": round(rs[-1]["timestamp"] - rs[0]["timestamp"], 1),
            "cpu_avg_percent": round(sum(cpu) / len(cpu), 2),
            "cpu_p95_percent": round(report.percentile(cpu, 0.95), 2),
            "cpu_max_percent": round(max(cpu), 2),
            "mem_avg_mb": round(sum(mem) / len(mem) / 2**20, 1),
            "mem_max_mb": round(max(mem) / 2**20, 1),
            "mem_max_percent_of_limit": round(100.0 * max(mem) / limit, 2) if limit else None,
            "disk_start_mb": round(disk[0] / 2**20, 2),
            "disk_end_mb": round(disk[-1] / 2**20, 2),
            "disk_growth_mb": round((disk[-1] - disk[0]) / 2**20, 2),
            "blk_write_mb": round((rs[-1].get("blk_write_bytes", 0)
                                   - rs[0].get("blk_write_bytes", 0)) / 2**20, 2),
            "blk_read_mb": round((rs[-1].get("blk_read_bytes", 0)
                                  - rs[0].get("blk_read_bytes", 0)) / 2**20, 2),
        })
    return out


def phase_stats(res_summary, db, prefix):
    """Merge every phase row for `db` whose label starts with `prefix`."""
    rows = [r for r in res_summary if r["db"] == db and r["phase"].startswith(prefix)]
    if not rows:
        return {}
    total = sum(r["samples"] for r in rows) or 1
    return {
        "cpu_avg": round(sum(r["cpu_avg_percent"] * r["samples"] for r in rows) / total, 2),
        "cpu_max": round(max(r["cpu_max_percent"] for r in rows), 2),
        "mem_avg": round(sum(r["mem_avg_mb"] * r["samples"] for r in rows) / total, 1),
        "mem_max": round(max(r["mem_max_mb"] for r in rows), 1),
    }


def cross_check(query_summaries, query_ids, dbs):
    """Flag queries where the engines disagree on the answer (>1% apart)."""
    for qid in query_ids:
        rows = [r for r in query_summaries if r["query_id"] == qid and not r["errors"]]
        vals = [r["result_count"] for r in rows if isinstance(r["result_count"], (int, float))]
        agree = "n/a"
        if len(vals) >= 2:
            lo, hi = min(vals), max(vals)
            agree = "YES" if hi - lo <= max(1.0, 0.01 * hi) else "NO"
        for r in query_summaries:
            if r["query_id"] == qid:
                r["result_agrees_across_dbs"] = agree


def push_to_prometheus(run_id, profile, summary_rows, query_summaries):
    for row in summary_rows:
        db = row["db"]
        samples = [
            ("bench_ingest_docs_per_second", {"db": db}, row["ingest_docs_per_sec"],
             "gauge", "Ingestion throughput in documents per second"),
            ("bench_ingest_mb_per_second", {"db": db}, row["ingest_mb_per_sec"],
             "gauge", "Ingestion throughput in MB per second"),
            ("bench_ingest_duration_seconds", {"db": db}, row["ingest_duration_s"],
             "gauge", "Total wall-clock seconds to ingest the dataset"),
            ("bench_ingest_docs_total", {"db": db}, row["docs"],
             "gauge", "Documents ingested"),
            ("bench_stored_megabytes", {"db": db}, row["disk_mb"],
             "gauge", "On-disk footprint after ingest and settle, in MB"),
            ("bench_bytes_per_document", {"db": db}, row["bytes_per_doc"],
             "gauge", "Average stored bytes per document"),
            ("bench_compression_ratio", {"db": db}, row["compression_ratio_vs_raw_json"],
             "gauge", "Raw JSON size divided by stored size"),
            ("bench_ingest_cpu_avg_percent", {"db": db}, row["ingest_cpu_avg_percent"],
             "gauge", "Mean CPU percent during ingest"),
            ("bench_ingest_mem_max_megabytes", {"db": db}, row["ingest_mem_max_mb"],
             "gauge", "Peak memory during ingest, in MB"),
            ("bench_query_mem_max_megabytes", {"db": db}, row["query_mem_max_mb"],
             "gauge", "Peak memory during the query phase, in MB"),
            ("bench_run_info", {"db": db, "run_id": run_id, "profile": profile,
                                "image": row["image"]}, 1, "gauge", "Metadata for the last run"),
        ]
        for q in query_summaries:
            if q["db"] != db:
                continue
            for stat in ("min_ms", "p50_ms", "mean_ms", "p95_ms", "p99_ms", "max_ms"):
                samples.append(("bench_query_latency_ms",
                                {"db": db, "query": q["query_id"], "stat": stat.replace("_ms", "")},
                                q.get(stat), "gauge", "Benchmark query latency in milliseconds"))
            samples.append(("bench_query_result_count", {"db": db, "query": q["query_id"]},
                            q.get("result_count"), "gauge", "Rows/value returned by the query"))
            samples.append(("bench_query_errors", {"db": db, "query": q["query_id"]},
                            q.get("errors"), "gauge", "Failed iterations for this query"))
        try:
            metrics.push_metrics("logdb_bench", {"db": db}, samples)
        except Exception as e:
            log(f"  warning: pushgateway publish failed for {db}: {e}")


# --------------------------------------------------------------------------- #
#  Main                                                                       #
# --------------------------------------------------------------------------- #
def main(argv=None):
    p = argparse.ArgumentParser(description="Benchmark Elasticsearch vs Loki vs VictoriaLogs")
    p.add_argument("--profile", default="default", choices=sorted(config.PROFILES))
    p.add_argument("--docs", type=int, help="override the profile's document count")
    p.add_argument("--window-hours", type=float,
                   help="override the time span the generated logs cover")
    p.add_argument("--batch-size", type=int, help="override documents per write request")
    p.add_argument("--workers", type=int, help="override concurrent write workers")
    p.add_argument("--skip-disk-check", action="store_true",
                   help="run even if the estimated disk requirement exceeds free space")
    p.add_argument("--databases", default=",".join(config.DATABASES),
                   help="comma-separated subset to test")
    p.add_argument("--repeats", type=int, default=config.QUERY_REPEATS)
    p.add_argument("--warmups", type=int, default=config.WARMUP_RUNS)
    p.add_argument("--settle", type=int, default=config.SETTLE_SECONDS,
                   help="idle seconds after ingest before measuring disk and querying")
    p.add_argument("--query-timeout", type=int, default=600)
    p.add_argument("--sample-interval", type=float, default=2.0)
    p.add_argument("--no-reset", action="store_true", help="keep existing data and containers")
    p.add_argument("--skip-ingest", action="store_true", help="query data already loaded")
    p.add_argument("--only-queries", default="", help="comma-separated query ids to run")
    p.add_argument("--run-id", default=None)
    args = p.parse_args(argv)

    dbs = [d.strip() for d in args.databases.split(",") if d.strip()]
    profile = dict(config.PROFILES[args.profile])
    for key, override in (("docs", args.docs), ("window_hours", args.window_hours),
                          ("batch_size", args.batch_size), ("workers", args.workers)):
        if override:
            profile[key] = override
    run_id = args.run_id or dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    outdir = os.path.join(config.RESULTS_ROOT, run_id)
    os.makedirs(outdir, exist_ok=True)

    specs = queries.QUERIES
    if args.only_queries:
        wanted = {q.strip() for q in args.only_queries.split(",")}
        specs = [s for s in specs if s["id"] in wanted]
    query_ids = [s["id"] for s in specs]

    # The dataset covers [end - window, end); queries use exactly this range.
    end_ms = int(time.time() * 1000)
    window_ms = int(profile["window_hours"] * 3600 * 1000)
    start_ms = end_ms - window_ms
    plan = {
        "docs": profile["docs"], "batch_size": profile["batch_size"],
        "workers": profile["workers"], "seed": config.SEED,
        "start_ms": start_ms, "end_ms": end_ms,
        "ms_per_doc": window_ms / profile["docs"],
        "batches": (profile["docs"] + profile["batch_size"] - 1) // profile["batch_size"],
    }
    ctx = queries.build_context(start_ms, end_ms)

    print("=" * 78)
    print(f"  log-db-benchmark   run {run_id}")
    print(f"  profile={args.profile}  docs={plan['docs']:,}  "
          f"window={profile['window_hours']}h  databases={', '.join(dbs)}")
    print(f"  queries={len(specs)}  repeats={args.repeats}  settle={args.settle}s")
    print("=" * 78)

    if not args.skip_ingest:
        preflight_disk(plan["docs"], dbs, args.skip_disk_check)

    if not args.no_reset and not args.skip_ingest:
        reset_stack(dbs)

    tgts = targets.build_targets(dbs)
    wait_all_ready(tgts)

    sampler = metrics.ResourceSampler(interval=args.sample_interval).start()
    sampler.set_phase("baseline")
    log("recording 20s idle baseline")
    time.sleep(20)

    ingest_rows, disk_at_settle, disk_after = [], {}, {}
    if args.skip_ingest:
        log("skipping ingest (--skip-ingest)")
        snap = sampler.snapshot_now()
        for db in dbs:
            disk_at_settle[db] = snap.get(db, {}).get("disk_bytes", 0)
    else:
        for t in tgts:
            log(f"preparing {t.name}")
            t.prepare()
        for t in tgts:
            log(f"ingesting {plan['docs']:,} docs into {t.name}")
            res = ingest(t, plan, sampler)
            res.update({"run_id": run_id, "db": t.name})
            if res["errors"]:
                log(f"  {t.name}: {res['errors']} batch errors; first: {res['error_detail'][:1]}")
            log(f"  {t.name}: {res['docs_per_sec']:,.0f} docs/s "
                f"({res['wire_mb_per_sec']:.1f} MB/s) in {res['duration_s']:.1f}s")

            sampler.set_phase("settle", t.name)
            log(f"  {t.name}: settling {args.settle}s for merges/compaction")
            time.sleep(args.settle)

            snap = sampler.snapshot_now()
            disk_at_settle[t.name] = snap.get(t.name, {}).get("disk_bytes", 0)
            verified = t.doc_count(ctx)
            res["docs_verified"] = int(verified) if verified is not None else None
            res["docs_match"] = ("YES" if verified is not None
                                 and abs(verified - plan["docs"]) <= plan["docs"] * 0.001
                                 else ("NO" if verified is not None else "unknown"))
            log(f"  {t.name}: disk={disk_at_settle[t.name]/2**20:,.1f} MB  "
                f"docs_verified={res['docs_verified']} ({res['docs_match']})")
            ingest_rows.append(res)
            sampler.set_phase("idle")

    log("running query suite")
    all_runs, all_summaries = [], []
    for t in tgts:
        runs, summaries = run_queries(t, ctx, specs, args.repeats, args.warmups,
                                      sampler, run_id, args.query_timeout)
        all_runs += runs
        all_summaries += summaries
        sampler.set_phase("idle")

    sampler.set_phase("stabilize")
    disk_after = wait_for_stable_disk(sampler, dbs)
    sampler.stop()
    cross_check(all_summaries, query_ids, dbs)

    # ---- assemble ---- #
    for r in sampler.rows:
        r["run_id"] = run_id
        r["iso_time"] = dt.datetime.fromtimestamp(r["timestamp"]).isoformat(timespec="seconds")
    res_summary = summarize_resources(run_id, sampler.rows)

    ingest_by_db = {r["db"]: r for r in ingest_rows}
    summary_rows = []
    for db in dbs:
        ing = ingest_by_db.get(db, {})
        ip = phase_stats(res_summary, db, f"ingest:{db}")
        qp = phase_stats(res_summary, db, "query:")
        qrows = [q for q in all_summaries if q["db"] == db and q.get("mean_ms") is not None]
        disk = disk_after.get(db, 0)
        docs = ing.get("docs") or plan["docs"]
        raw = ing.get("raw_json_bytes") or 0
        summary_rows.append({
            "run_id": run_id, "profile": args.profile, "db": db, "image": image_for(db),
            "docs": docs,
            "ingest_send_s": ing.get("send_seconds"),
            "ingest_duration_s": ing.get("duration_s"),
            "ingest_docs_per_sec": ing.get("docs_per_sec"),
            "ingest_mb_per_sec": ing.get("wire_mb_per_sec"),
            "ingest_cpu_avg_percent": ip.get("cpu_avg"),
            "ingest_cpu_max_percent": ip.get("cpu_max"),
            "ingest_mem_avg_mb": ip.get("mem_avg"),
            "ingest_mem_max_mb": ip.get("mem_max"),
            "disk_mb": round(disk / 2**20, 2),
            "disk_at_settle_mb": round(disk_at_settle.get(db, 0) / 2**20, 2),
            "bytes_per_doc": round(disk / docs, 2) if docs else None,
            "compression_ratio_vs_raw_json": round(raw / disk, 2) if disk and raw else None,
            "query_cpu_avg_percent": qp.get("cpu_avg"),
            "query_cpu_max_percent": qp.get("cpu_max"),
            "query_mem_max_mb": qp.get("mem_max"),
            "query_mean_ms_all": round(sum(q["mean_ms"] for q in qrows) / len(qrows), 2) if qrows else None,
            "query_p95_ms_all": round(report.percentile([q["p95_ms"] for q in qrows], 0.95), 2) if qrows else None,
            "query_errors": sum(q["errors"] for q in all_summaries if q["db"] == db),
            **{f"{q['query_id']}_mean_ms": q.get("mean_ms")
               for q in all_summaries if q["db"] == db},
        })

    paths = [
        report.write_summary(outdir, summary_rows, query_ids),
        report.write_query_summary(outdir, all_summaries),
        report.write_query_runs(outdir, all_runs),
        report.write_ingest(outdir, ingest_rows),
        report.write_resource_summary(outdir, res_summary),
        report.write_resources(outdir, sampler.rows),
    ]
    with open(os.path.join(outdir, "run_meta.json"), "w") as fh:
        json.dump({"run_id": run_id, "profile": args.profile, "plan": plan,
                   "databases": dbs, "queries": query_ids, "repeats": args.repeats,
                   "settle_seconds": args.settle,
                   "images": {db: image_for(db) for db in dbs},
                   "started": dt.datetime.now().isoformat(timespec="seconds")}, fh, indent=2)

    push_to_prometheus(run_id, args.profile, summary_rows, all_summaries)

    print("\n" + "=" * 78)
    report.print_console_report(summary_rows, all_summaries, dbs, query_ids,
                                {s["id"]: s["title"] for s in specs})
    print("\n  CSV output:")
    for path in paths:
        print(f"    {os.path.relpath(path, REPO)}")
    print(f"\n  Grafana: http://localhost:3000/d/logdb-bench (anonymous access enabled)")
    print("=" * 78)

    # Convenience pointer to the newest run.
    latest = os.path.join(config.RESULTS_ROOT, "latest")
    if os.path.islink(latest) or os.path.exists(latest):
        try:
            os.remove(latest)
        except OSError:
            pass
    try:
        os.symlink(run_id, latest)
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
