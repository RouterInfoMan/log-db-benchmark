# log-db-benchmark

A reproducible benchmark rig comparing three log databases on identical data,
identical hardware limits, and semantically identical queries:

| System | Version | Role in the comparison |
|---|---|---|
| **Elasticsearch** | 8.17.3 | Inverted index; the general-purpose incumbent |
| **Grafana Loki** | 3.6.16 | Label index + unindexed chunks; the "cheap storage" model |
| **VictoriaLogs** | 1.52.0 | Purpose-built columnar log store |

It measures **query latency, ingest throughput, RAM, CPU and disk usage**, writes
everything to CSV, and ships a Grafana dashboard that streams the resource metrics
live and displays the query results after each run.

---

## Quick start

```bash
make up          # start databases + Prometheus + Grafana
make smoke       # 200k-document end-to-end check (~2 min)
make bench       # the real run: 5M documents (~20 min)
make results     # print the summary sheet
```

Then open **<http://localhost:3000/d/logdb-bench>** (anonymous access is on; the
admin login is `admin` / `admin`).

---

## What gets measured

### Query latency
Nine queries, each expressed natively in Elasticsearch Query DSL, LogQL and
LogsQL. Every query runs `--warmups 1` discarded times, then `--repeats 5` timed
times; min / p50 / mean / p95 / p99 / max are recorded.

| # | Query | What it exercises |
|---|---|---|
| q01 | `needle_rare_term` | Full-text scan for a token in ~1 of every 200k lines |
| q02 | `count_errors` | Cheap label/keyword filter plus a full-range count |
| q03 | `service_text_filter` | Label narrowing combined with a free-text term |
| q04 | `numeric_range_count` | Numeric predicates on two fields |
| q05 | `regex_path` | Anchored regex evaluation |
| q06 | `group_by_service` | Group-by aggregation over the whole dataset |
| q07 | `recent_tail` | "Show me the last 5 minutes" — the everyday query |
| q08 | `error_rate_1m` | Per-minute histogram — the query behind every dashboard |
| q09 | `unique_users_errors` | High-cardinality distinct count |

**Results are cross-checked.** The harness compares each query's result count
across the three engines and writes `result_agrees_across_dbs` (YES/NO) into
`query_summary.csv`. A fast query that returns the wrong answer is not a win, and
this column is what tells you the comparison is honest.

### Resources
A purpose-built exporter (`exporter/exporter.py`) samples the Docker API once a
second and publishes one uniform set of metrics per database:

- **CPU** — percent of one core, computed from deltas between polls
- **Memory** — working set (usage minus reclaimable page cache), the same figure
  `docker stats` shows
- **Disk** — allocated size of the data directory, walked identically for all
  three systems, so no engine gets credit for reporting its own footprint
  optimistically
- Network and block-IO counters

Samples are tagged with the current phase (`baseline`, `ingest:<db>`,
`settle:<db>`, `query:<db>:<query_id>`), so per-phase resource cost is
attributable rather than averaged across the whole run.

### Ingest
Documents per second, MB/s on the wire, wall-clock duration excluding and
including the final flush, plus the peak RAM and mean CPU spent getting there.

---

## Fairness: what is held constant

This is where log-database benchmarks usually go wrong, so the choices are
explicit:

- **Identical data.** Every record is a pure function of `(seed, document index)`,
  so all three databases receive byte-for-byte identical logs. Reproducible
  across runs and machines.
- **Identical resource limits.** Each database container is capped at
  `DB_CPUS` cores and `DB_MEM_LIMIT` memory (default 4 cores / 3 GB), set in
  `.env`. Elasticsearch's JVM heap is sized to fit inside that same cap.
- **Sequential, isolated phases.** Databases are ingested and queried one at a
  time. Nothing else is under load while a system is being measured.
- **Equivalent semantics.** Regexes are anchored in all three engines; time
  ranges are identical to the millisecond; the same logical predicate is used
  everywhere. Where engines genuinely differ, the cross-check column says so.
- **No cherry-picked flush.** All three are flushed the same way and then left
  to settle (`--settle`, default 60s) before query measurements.
- **Disk is measured at steady state, not at peak.** After the query phase the
  harness polls until every database's footprint stops moving. This matters a
  lot: Loki peaks around 2.3 GB while flushing 5M documents and then settles to
  ~0.5 GB once its WAL truncates, and Elasticsearch keeps merging segments
  downward for minutes. Recording the post-flush number would have overstated
  Loki by more than 4x. Both figures are kept — `disk_mb` (settled, the headline)
  and `disk_at_settle_mb` (peak just after flush).
- **Uncompressed payloads.** All three receive uncompressed JSON over HTTP.
  Enabling gzip would help each of them by a different amount and is a separate
  experiment.

### Things to know when reading the numbers

- **Loki's disk figure includes its WAL and per-24h index files.** These are a
  largely fixed overhead, so Loki looks worst at small volumes and improves as
  the dataset grows. Compare at `--profile default` or larger, not `smoke`.
- **Peak disk is a real operational cost even though the headline is the
  settled figure.** Loki transiently needing 4x its final footprint is something
  you have to provision for; that is why both columns are reported.
- **The three engines index differently on purpose.** Elasticsearch indexes
  every field up front, Loki indexes only labels and parses JSON at query time,
  VictoriaLogs stores columns. Loki being slow on q04/q05/q09 is not a
  configuration mistake — it is the design trade-off being measured.
- **q09 is approximate in Elasticsearch** (HyperLogLog++) and exact in
  VictoriaLogs. Loki may exceed `max_query_series` and fail outright; a failure
  is recorded as a result rather than hidden.
- **Elasticsearch keeps `refresh_interval: 1s`**, matching the near-real-time
  visibility the other two provide by default. Setting it to `-1` during bulk
  load would raise its ingest number and is not a like-for-like comparison.
- **Single-node, single-shard.** This measures three single-node deployments.
  It says nothing about clustered behaviour.

---

## Output

Each run writes to `results/<run-id>/`, with `results/latest` symlinked to the
newest:

| File | Contents |
|---|---|
| **`summary.csv`** | **One row per database — the headline sheet.** Ingest rate, CPU, RAM, disk, bytes/doc, compression ratio, and mean latency for every query |
| `query_summary.csv` | Per query per database: min/p50/mean/p95/p99/max, result count, cross-check verdict, bytes scanned |
| `query_runs.csv` | Every individual timed iteration, warmups included |
| `ingest.csv` | Throughput, wire bytes, batch settings, document-count verification |
| `resource_summary.csv` | CPU/RAM/disk aggregated per phase per database |
| `resources.csv` | Raw 1 Hz samples — the series behind the Grafana panels |
| `run_meta.json` | Full run configuration, for reproducibility |

Compare two runs:

```bash
python3 scripts/compare_runs.py <run-id-a> <run-id-b>
```

---

## The Grafana dashboard

`http://localhost:3000/d/logdb-bench`

- **Live resource usage** — CPU, memory and disk per database, streaming at 5s
  refresh. Useful to watch *while* a benchmark runs.
- **Query performance** — a query × database latency matrix, a p95 bar chart on
  a log scale (latencies span three orders of magnitude), and a filterable
  detail table.
- **Ingest and storage** — throughput, stored size, bytes per document,
  compression ratio, and the CPU/RAM spent during ingest.

Query, ingest and storage panels are populated by the benchmark runner pushing
its results to a Prometheus Pushgateway at the end of each run; resource panels
stream continuously and need no run in progress.

Each database keeps a fixed colour everywhere — Elasticsearch blue, Loki orange,
VictoriaLogs green — validated for colour-vision deficiency against the dark
surface.

Edit panels in the UI freely, or edit `config/grafana/make_dashboard.py` and run
`make dashboard`; Grafana reloads the file within 15 seconds.

Datasources for all three log databases are provisioned too, so you can explore
the benchmark data itself in Grafana's Explore view.

To export a PNG:

```bash
make snapshot        # writes results/dashboard.png
```

---

## Resetting

There are four levels, because "reset" usually means one of four different things.

| Command | Databases | Prometheus history | Grafana dashboards & edits | `results/` CSVs |
|---|---|---|---|---|
| `make clean` | wiped | kept | kept | kept |
| `make reset-dashboards` | kept | kept | **restored to shipped** | kept |
| `make reset` | wiped | wiped | **restored to shipped** | kept |
| `make clean-results` | kept | kept | kept | **deleted** |

```bash
make reset      # full reset, asks for confirmation
make up         # rebuild the stack
make bench      # load fresh data
```

`make reset FORCE=1` skips the prompt, for scripts.

**A full wipe including the CSVs:**

```bash
make reset FORCE=1 && make clean-results FORCE=1 && make up
```

### Why resetting dashboards needs its own command

Grafana keeps dashboards in its own database, not just in the provisioning file:

- Its API **refuses to delete a provisioned dashboard** (`"provisioned dashboard
  cannot be deleted"`), so you cannot clear it that way.
- Its provisioner **skips re-import while the file checksum is unchanged**, so
  regenerating an identical JSON file and restarting does nothing.
- `allowUiUpdates: true` means edits you make in the UI are saved and win.

So `make reset-dashboards` stops Grafana, deletes `grafana.db`, and starts it
again — which re-provisions the datasources and the shipped dashboard from
`config/`. It deliberately keeps `data/grafana/plugins` (232 MB, the VictoriaLogs
plugin), so the reset is fast and works offline. Note that dropping `grafana.db`
also resets the login to `admin` / `admin` and clears any users or saved
preferences.

To drop the plugin cache as well, for a truly bare volume:

```bash
docker run --rm -v "$PWD/data:/data" python:3.12-slim rm -rf /data/grafana
```

### Notes

- Everything is scoped to the `logdb-bench` Compose project, so other containers
  on the machine are never touched.
- The Pushgateway holds the last run's query results in memory only, so it is
  cleared whenever the stack goes down.
- Teardown includes the optional `render` profile; otherwise the renderer keeps
  running and holds the Docker network open.
- `bench.run` already wipes the databases at the start of every run, so you do
  not need to reset by hand between benchmarks. If a run is interrupted after
  that wipe but before ingest finishes, you are left with empty databases and a
  dashboard still showing the previous run — `make reset` is the way out.

## Configuration

Resource limits, image versions and ports live in `.env`:

```ini
DB_MEM_LIMIT=3g    # applied identically to all three databases
DB_CPUS=4
ES_HEAP=1536m      # must fit inside DB_MEM_LIMIT
```

Workload sizes are in `bench/config.py`:

| Profile | Documents | Time span | Roughly |
|---|---|---|---|
| `smoke` | 200k | 2h | 2 minutes |
| `small` | 1M | 6h | 5 minutes |
| `default` | 5M | 24h | 20 minutes |
| `large` | 25M | 72h | an hour or more |

### Custom sizes

```bash
make bench-20m                      # 20M documents over 96h
make bench-50m                      # 50M documents over 240h
make bench-custom DOCS=8000000      # anything else
```

`bench-custom` accepts `DOCS`, and optionally `WINDOW` (hours), `REPEATS`,
`SETTLE` and `QTIMEOUT`:

```bash
make bench-custom DOCS=20000000 WINDOW=48 REPEATS=5
```

**`WINDOW` defaults to holding document density constant** at the default
profile's ~208k documents per hour (5M over 24h). That is deliberate: it keeps
each query's selectivity proportional as the dataset grows, so a 50M run is
comparable to a 5M one instead of just being denser. Set `WINDOW` explicitly if
you want to vary density on purpose.

| Target | Documents | Window | Peak disk | Expect |
|---|---|---|---|---|
| `bench-20m` | 20M | 96h | ~15 GB | ~45 minutes |
| `bench-50m` | 50M | 240h | ~38 GB | ~2 hours |

The large targets default to `REPEATS=3` (rather than 5), `SETTLE=120` and a
15-minute query timeout, because Loki's unindexed queries grow roughly with the
dataset: q06 takes ~20s at 5M, so budget minutes per iteration at 50M. Most of
the wall-clock time in a big run is Loki's query phase, not ingestion.

Before ingesting, a **disk pre-flight** estimates the peak requirement (~830
bytes per document across all three databases, measured at the 5M profile) and
aborts if the data volume cannot hold it plus 30% headroom. Override with
`--skip-disk-check` if you know better.

Useful flags:

```bash
python3 -m bench.run --profile small           # smaller workload
python3 -m bench.run --docs 2000000            # override the document count
python3 -m bench.run --databases loki,victorialogs   # subset
python3 -m bench.run --repeats 10              # more timed iterations
python3 -m bench.run --skip-ingest --no-reset  # re-query existing data
python3 -m bench.run --only-queries q05_regex_path   # one query
```

`bench.run` resets the stack and wipes all database data before ingesting, so
every run starts from empty. Use `--no-reset` to keep what is there.

One caveat on `--skip-ingest`: the query time window is derived from the current
clock, so re-querying data ingested a while ago covers a window shifted away
from the original. The three engines still agree with each other (the comparison
stays valid), but absolute result counts will not match the run that loaded the
data. For run-to-run comparisons of absolute numbers, do a full run.

---

## Layout

```
docker-compose.yml        the stack: 3 databases + exporter + Prometheus + Grafana
.env                      resource limits, image versions, ports
bench/
  generator.py            deterministic synthetic log generation
  targets.py              per-database adapters (schema, ingest, flush, query)
  queries.py              the nine queries in three query languages
  metrics.py              resource sampling and Pushgateway publication
  report.py               CSV writers and console tables
  run.py                  orchestrator
exporter/exporter.py      CPU/RAM/disk exporter (stdlib only, Docker API by hand)
config/
  loki-config.yaml        monolithic Loki, limits raised so the engine is measured
  prometheus.yml          scrape config
  grafana/                datasource + dashboard provisioning
  grafana/make_dashboard.py   dashboard generator
scripts/
  snapshot.sh             render the dashboard to PNG
  compare_runs.py         diff two runs
results/<run-id>/         CSV output
```

## Requirements

Docker with Compose v2, and Python 3.9+ for the harness. The harness uses only
the standard library — nothing to install.

Give Docker at least 12 GB of memory for the default profile: three databases at
3 GB each plus the observability stack.
