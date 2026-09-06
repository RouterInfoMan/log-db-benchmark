#!/usr/bin/env python3
"""Generate the benchmark Grafana dashboard.

Kept as a generator rather than hand-maintained JSON so panel intent stays
readable. Run it and restart nothing -- Grafana re-reads the file on a timer.

    python3 config/grafana/make_dashboard.py
"""
import json
import os

# Categorical colors are bound to the entity, never to rank, so filtering or
# reordering series never repaints them. Validated for dark-surface CVD
# separation (worst adjacent deutan dE 9.4, normal dE 26.5).
COLORS = {
    "elasticsearch": "#3987e5",   # blue
    "loki":          "#d95926",   # orange
    "victorialogs":  "#199e70",   # aqua
}
PROM = {"type": "prometheus", "uid": "prometheus"}
_id = iter(range(1, 500))


def color_overrides(names=COLORS):
    return [{"matcher": {"id": "byName", "options": name},
             "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": color}}]}
            for name, color in names.items()]


def target(expr, legend=None, instant=False, fmt="time_series", ref="A"):
    t = {"datasource": PROM, "editorMode": "code", "expr": expr, "refId": ref,
         "instant": instant, "range": not instant, "format": fmt}
    if legend:
        t["legendFormat"] = legend
    return t


def row(title, y):
    return {"type": "row", "id": next(_id), "title": title, "collapsed": False,
            "gridPos": {"h": 1, "w": 24, "x": 0, "y": y}, "panels": []}


def timeseries(title, expr, unit, gp, desc="", decimals=None, minval=0):
    return {
        "type": "timeseries", "id": next(_id), "title": title, "description": desc,
        "datasource": PROM, "gridPos": gp,
        "targets": [target(expr, "{{db}}")],
        "fieldConfig": {
            "defaults": {
                "unit": unit, "min": minval, "decimals": decimals,
                "color": {"mode": "palette-classic"},
                "custom": {
                    "drawStyle": "line", "lineWidth": 2, "fillOpacity": 8,
                    "gradientMode": "opacity", "showPoints": "never",
                    "spanNulls": True, "pointSize": 5,
                    "lineInterpolation": "smooth",
                    "axisBorderShow": False, "axisGridShow": True,
                    "scaleDistribution": {"type": "linear"},
                },
            },
            "overrides": color_overrides(),
        },
        "options": {
            # Legend always present for >= 2 series; mean/max keep the numbers
            # off the plot area instead of labelling every point.
            "legend": {"showLegend": True, "displayMode": "table", "placement": "bottom",
                       "calcs": ["mean", "max", "lastNotNull"]},
            "tooltip": {"mode": "multi", "sort": "desc"},
        },
    }


def bargauge(title, expr, unit, gp, desc="", decimals=1):
    return {
        "type": "bargauge", "id": next(_id), "title": title, "description": desc,
        "datasource": PROM, "gridPos": gp,
        "targets": [target(expr, "{{db}}", instant=True)],
        "fieldConfig": {
            "defaults": {"unit": unit, "min": 0, "decimals": decimals,
                         "color": {"mode": "palette-classic"},
                         "thresholds": {"mode": "absolute", "steps": [{"color": "text", "value": None}]}},
            "overrides": color_overrides(),
        },
        "options": {
            "displayMode": "gradient", "orientation": "horizontal",
            "showUnfilled": True, "valueMode": "color", "minVizWidth": 8,
            "minVizHeight": 16, "namePlacement": "left", "sizing": "auto",
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
            "legend": {"showLegend": False},
        },
    }


def latency_matrix(gp):
    """Query x database matrix of p50 latency."""
    return {
        "type": "table", "id": next(_id), "gridPos": gp,
        "title": "Query latency p50 by database (ms)",
        "description": "Median of the timed iterations from the most recent run. Lower is better.",
        "datasource": PROM,
        "targets": [target('bench_query_latency_ms{stat="p50"}', instant=True, fmt="table")],
        "transformations": [
            {"id": "organize", "options": {"excludeByName": {
                "Time": True, "__name__": True, "instance": True, "job": True, "stat": True}}},
            {"id": "groupingToMatrix", "options": {
                "columnField": "db", "rowField": "query", "valueField": "Value"}},
        ],
        "fieldConfig": {
            "defaults": {"unit": "ms", "decimals": 2, "custom": {
                "align": "right", "cellOptions": {"type": "color-text"}, "filterable": False}},
            "overrides": [
                {"matcher": {"id": "byName", "options": "query\\db"},
                 "properties": [{"id": "custom.align", "value": "left"},
                                {"id": "custom.width", "value": 240}]},
            ] + [
                {"matcher": {"id": "byName", "options": db},
                 "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": c}}]}
                for db, c in COLORS.items()
            ],
        },
        "options": {"showHeader": True, "cellHeight": "sm",
                    "sortBy": [{"desc": False, "displayName": "query\\db"}]},
    }


def latency_bars(gp):
    return {
        "type": "barchart", "id": next(_id), "gridPos": gp,
        "title": "Query latency p95 by database (ms, log scale)",
        "description": ("Log scale: latencies span several orders of magnitude, so a "
                        "linear axis would flatten every fast query to zero."),
        "datasource": PROM,
        "targets": [target('bench_query_latency_ms{stat="p95"}', instant=True, fmt="table")],
        "transformations": [
            {"id": "organize", "options": {"excludeByName": {
                "Time": True, "__name__": True, "instance": True, "job": True, "stat": True}}},
            {"id": "groupingToMatrix", "options": {
                "columnField": "db", "rowField": "query", "valueField": "Value"}},
        ],
        "fieldConfig": {
            "defaults": {"unit": "ms", "min": 0.1, "color": {"mode": "palette-classic"},
                         "custom": {"lineWidth": 0, "fillOpacity": 90, "gradientMode": "none",
                                    "axisPlacement": "auto", "axisBorderShow": False,
                                    "scaleDistribution": {"type": "log", "log": 10},
                                    "thresholdsStyle": {"mode": "off"}}},
            "overrides": color_overrides(),
        },
        "options": {
            "orientation": "horizontal", "xField": "query\\db",
            "barWidth": 0.8, "groupWidth": 0.75, "showValue": "never",
            "stacking": "none", "xTickLabelRotation": 0, "xTickLabelSpacing": 0,
            "legend": {"showLegend": True, "displayMode": "list", "placement": "bottom"},
            "tooltip": {"mode": "multi", "sort": "desc"},
        },
    }


def detail_table(gp):
    return {
        "type": "table", "id": next(_id), "gridPos": gp,
        "title": "Query detail: all latency percentiles and result counts",
        "datasource": PROM,
        "targets": [
            target('bench_query_latency_ms', instant=True, fmt="table", ref="A"),
            target('bench_query_result_count', instant=True, fmt="table", ref="B"),
        ],
        "transformations": [
            {"id": "organize", "options": {"excludeByName": {
                "Time": True, "Time 1": True, "Time 2": True, "__name__": True,
                "__name__ 1": True, "__name__ 2": True, "instance": True,
                "instance 1": True, "instance 2": True, "job": True,
                "job 1": True, "job 2": True}}},
        ],
        "fieldConfig": {
            "defaults": {"custom": {"align": "auto", "filterable": True},
                         "decimals": 2},
            "overrides": [
                {"matcher": {"id": "byName", "options": "Value #A"},
                 "properties": [{"id": "displayName", "value": "latency"},
                                {"id": "unit", "value": "ms"}]},
                {"matcher": {"id": "byName", "options": "Value #B"},
                 "properties": [{"id": "displayName", "value": "result count"},
                                {"id": "unit", "value": "short"}]},
            ],
        },
        "options": {"showHeader": True, "cellHeight": "sm", "footer": {"show": False}},
    }


def build():
    panels, y = [], 0

    panels.append(row("Live resource usage  ·  streams continuously", y)); y += 1
    panels += [
        timeseries("CPU usage", "bench_container_cpu_percent", "percent",
                   {"h": 9, "w": 12, "x": 0, "y": y},
                   "Percent of a single core. Each container is capped at DB_CPUS "
                   "cores, so 400 means 4 fully saturated cores.", decimals=1),
        timeseries("Memory usage (working set)", "bench_container_memory_bytes", "bytes",
                   {"h": 9, "w": 12, "x": 12, "y": y},
                   "Resident memory minus reclaimable page cache -- the same figure "
                   "`docker stats` reports."),
    ]; y += 9
    panels += [
        timeseries("Disk usage (data directory)", "bench_disk_usage_bytes", "bytes",
                   {"h": 9, "w": 12, "x": 0, "y": y},
                   "Allocated on-disk size of each database's data directory, "
                   "measured identically for all three."),
        bargauge("Current disk footprint", "bench_disk_usage_bytes", "bytes",
                 {"h": 4, "w": 12, "x": 12, "y": y}),
    ]
    panels.append(bargauge("Current memory", "bench_container_memory_bytes", "bytes",
                           {"h": 5, "w": 12, "x": 12, "y": y + 4}))
    y += 9

    panels.append(row("Query performance  ·  most recent benchmark run", y)); y += 1
    # h=12 so all nine query rows fit without the table scrolling.
    panels += [latency_matrix({"h": 12, "w": 12, "x": 0, "y": y}),
               latency_bars({"h": 12, "w": 12, "x": 12, "y": y})]
    y += 12
    panels.append(detail_table({"h": 10, "w": 24, "x": 0, "y": y})); y += 10

    panels.append(row("Ingest and storage  ·  most recent benchmark run", y)); y += 1
    panels += [
        bargauge("Ingest throughput", "bench_ingest_docs_per_second", "none",
                 {"h": 7, "w": 8, "x": 0, "y": y}, "Documents per second.", decimals=0),
        bargauge("Stored size on disk", "bench_stored_megabytes", "decmbytes",
                 {"h": 7, "w": 8, "x": 8, "y": y}, "After ingest and settle."),
        bargauge("Bytes stored per document", "bench_bytes_per_document", "bytes",
                 {"h": 7, "w": 8, "x": 16, "y": y}, "Lower is denser storage.", decimals=1),
    ]; y += 7
    panels += [
        bargauge("Peak memory during ingest", "bench_ingest_mem_max_megabytes", "decmbytes",
                 {"h": 7, "w": 8, "x": 0, "y": y}),
        bargauge("Mean CPU during ingest", "bench_ingest_cpu_avg_percent", "percent",
                 {"h": 7, "w": 8, "x": 8, "y": y}),
        bargauge("Compression vs raw JSON", "bench_compression_ratio", "none",
                 {"h": 7, "w": 8, "x": 16, "y": y},
                 "Raw JSON bytes divided by bytes actually stored. Higher is better.", decimals=2),
    ]

    return {
        "uid": "logdb-bench",
        "title": "Log DB Benchmark - Elasticsearch vs Loki vs VictoriaLogs",
        "description": ("Resource usage streams live; query, ingest and storage results "
                        "come from the most recent `python3 -m bench.run`."),
        "tags": ["benchmark", "logs"],
        "timezone": "browser",
        "editable": True,
        "graphTooltip": 1,
        "schemaVersion": 39,
        "version": 1,
        "refresh": "5s",
        "time": {"from": "now-30m", "to": "now"},
        "timepicker": {"refresh_intervals": ["5s", "10s", "30s", "1m", "5m"]},
        "panels": panels,
    }


if __name__ == "__main__":
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "dashboards", "log-db-benchmark.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w") as fh:
        json.dump(build(), fh, indent=2)
    print(f"wrote {out}")
