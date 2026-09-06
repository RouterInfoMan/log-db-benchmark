#!/usr/bin/env python3
"""Diff the summary sheets of two benchmark runs.

    python3 scripts/compare_runs.py 20260906-201500 20260906-224500
"""
import csv
import os
import sys

ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "results")
METRICS = ["ingest_docs_per_sec", "disk_mb", "bytes_per_doc",
           "query_mean_ms_all", "ingest_mem_max_mb", "query_mem_max_mb"]


def load(run):
    path = os.path.join(ROOT, run, "summary.csv")
    with open(path) as fh:
        return {r["db"]: r for r in csv.DictReader(fh)}


def main():
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    a, b = sys.argv[1], sys.argv[2]
    ra, rb = load(a), load(b)
    print(f"{'database':15s} {'metric':22s} {a[:14]:>14s} {b[:14]:>14s} {'change':>10s}")
    print("-" * 80)
    for db in sorted(set(ra) & set(rb)):
        for m in METRICS:
            try:
                va, vb = float(ra[db][m]), float(rb[db][m])
            except (ValueError, KeyError, TypeError):
                continue
            delta = f"{(vb - va) / va * 100:+.1f}%" if va else "-"
            print(f"{db:15s} {m:22s} {va:14,.2f} {vb:14,.2f} {delta:>10s}")
        print()


if __name__ == "__main__":
    main()
