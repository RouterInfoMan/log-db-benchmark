#!/usr/bin/env python3
"""Prometheus exporter for the log-DB benchmark rig.

Publishes one uniform view of the three systems under test:
  * CPU / memory / network / block-IO, read from the Docker API
  * on-disk footprint, measured by walking the bind-mounted data dirs

Stdlib only -- the Docker API is spoken over its unix socket by hand.
"""
import http.client
import json
import os
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DOCKER_SOCK = "/var/run/docker.sock"
DATA_ROOT = "/benchdata"
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "9101"))
SCRAPE_INTERVAL = float(os.environ.get("SCRAPE_INTERVAL", "2"))
DISK_INTERVAL = float(os.environ.get("DISK_INTERVAL", "10"))

# container name -> (db label, data subdirectory)
TARGETS = {
    "bench-elasticsearch": ("elasticsearch", "elasticsearch"),
    "bench-loki": ("loki", "loki"),
    "bench-victorialogs": ("victorialogs", "victorialogs"),
}

_state_lock = threading.Lock()
_stats = {}   # db -> dict of metric name -> value
_disk = {}    # db -> dict of metric name -> value


class UnixHTTPConnection(http.client.HTTPConnection):
    """HTTPConnection that dials a unix socket instead of TCP."""

    def __init__(self, sock_path, timeout=30):
        super().__init__("localhost", timeout=timeout)
        self.sock_path = sock_path

    def connect(self):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        s.connect(self.sock_path)
        self.sock = s


def docker_get(path, timeout=30):
    conn = UnixHTTPConnection(DOCKER_SOCK, timeout=timeout)
    try:
        conn.request("GET", path, headers={"Host": "docker", "Accept": "application/json"})
        resp = conn.getresponse()
        body = resp.read()
        if resp.status != 200:
            return None
        return json.loads(body)
    except Exception:
        return None
    finally:
        conn.close()


# Previous cumulative CPU counters per container, for delta computation.
_prev_cpu = {}


def cpu_percent(cname, st, now):
    """Instantaneous CPU%, in percent of one core (400 = 4 saturated cores).

    Docker's one-shot stats endpoint returns zeroed `precpu_stats`, so the
    deltas are kept here between polls rather than trusting the API to
    provide them.
    """
    try:
        cpu = st["cpu_stats"]
        total = cpu["cpu_usage"]["total_usage"]          # nanoseconds of CPU time
        system = cpu.get("system_cpu_usage", 0)
        ncpu = cpu.get("online_cpus") or len(cpu["cpu_usage"].get("percpu_usage") or []) or 1
    except (KeyError, TypeError):
        return 0.0

    prev = _prev_cpu.get(cname)
    _prev_cpu[cname] = (total, system, now)
    if not prev:
        return 0.0
    ptotal, psystem, pnow = prev

    cd = total - ptotal
    if cd <= 0:
        return 0.0
    sd = system - psystem
    if sd > 0:
        return (cd / sd) * ncpu * 100.0
    # No host-wide CPU counter: fall back to wall-clock elapsed time.
    elapsed_ns = (now - pnow) * 1e9
    return (cd / elapsed_ns) * 100.0 if elapsed_ns > 0 else 0.0


def mem_used(st):
    """Working set: usage minus reclaimable page cache (matches `docker stats`)."""
    try:
        m = st["memory_stats"]
        usage = m.get("usage", 0)
        inactive = m.get("stats", {}).get("inactive_file", 0)
        return max(usage - inactive, 0), m.get("limit", 0)
    except Exception:
        return 0, 0


def sum_io(entries, ops):
    total = 0
    for e in entries or []:
        if e.get("op", "").lower() in ops:
            total += e.get("value", 0)
    return total


def poll_stats():
    while True:
        snapshot = {}
        now = time.monotonic()
        for cname, (db, _) in TARGETS.items():
            # one-shot returns immediately; deltas are computed locally.
            st = docker_get(f"/containers/{cname}/stats?stream=false&one-shot=true")
            if not st:
                _prev_cpu.pop(cname, None)
                continue
            used, limit = mem_used(st)
            nets = st.get("networks") or {}
            blk = (st.get("blkio_stats") or {}).get("io_service_bytes_recursive") or []
            snapshot[db] = {
                "bench_container_up": 1,
                "bench_container_cpu_percent": round(cpu_percent(cname, st, now), 4),
                "bench_container_memory_bytes": used,
                "bench_container_memory_limit_bytes": limit,
                "bench_container_memory_percent": round(100.0 * used / limit, 4) if limit else 0.0,
                "bench_container_pids": (st.get("pids_stats") or {}).get("current", 0),
                "bench_container_net_rx_bytes_total": sum(n.get("rx_bytes", 0) for n in nets.values()),
                "bench_container_net_tx_bytes_total": sum(n.get("tx_bytes", 0) for n in nets.values()),
                "bench_container_blkio_read_bytes_total": sum_io(blk, {"read"}),
                "bench_container_blkio_write_bytes_total": sum_io(blk, {"write"}),
            }
        for db in {d for d, _ in TARGETS.values()}:
            snapshot.setdefault(db, {"bench_container_up": 0})
        with _state_lock:
            _stats.clear()
            _stats.update(snapshot)
        time.sleep(SCRAPE_INTERVAL)


def dir_usage(path):
    """(apparent bytes, allocated bytes, file count) for a directory tree."""
    apparent = allocated = files = 0
    stack = [path]
    while stack:
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            s = entry.stat(follow_symlinks=False)
                            apparent += s.st_size
                            allocated += s.st_blocks * 512
                            files += 1
                    except OSError:
                        continue
        except OSError:
            continue
    return apparent, allocated, files


def poll_disk():
    while True:
        snapshot = {}
        for _, (db, subdir) in TARGETS.items():
            path = os.path.join(DATA_ROOT, subdir)
            if not os.path.isdir(path):
                continue
            apparent, allocated, files = dir_usage(path)
            snapshot[db] = {
                "bench_disk_usage_bytes": allocated,
                "bench_disk_apparent_bytes": apparent,
                "bench_disk_files": files,
            }
        with _state_lock:
            _disk.clear()
            _disk.update(snapshot)
        time.sleep(DISK_INTERVAL)


HELP = {
    "bench_container_up": ("gauge", "1 if the database container is running"),
    "bench_container_cpu_percent": ("gauge", "CPU usage in percent of one core (400 = 4 full cores)"),
    "bench_container_memory_bytes": ("gauge", "Memory working set in bytes"),
    "bench_container_memory_limit_bytes": ("gauge", "Container memory limit in bytes"),
    "bench_container_memory_percent": ("gauge", "Memory working set as percent of the limit"),
    "bench_container_pids": ("gauge", "Number of processes in the container"),
    "bench_container_net_rx_bytes_total": ("counter", "Bytes received over the network"),
    "bench_container_net_tx_bytes_total": ("counter", "Bytes transmitted over the network"),
    "bench_container_blkio_read_bytes_total": ("counter", "Bytes read from block devices"),
    "bench_container_blkio_write_bytes_total": ("counter", "Bytes written to block devices"),
    "bench_disk_usage_bytes": ("gauge", "Allocated on-disk size of the data directory in bytes"),
    "bench_disk_apparent_bytes": ("gauge", "Apparent (logical) size of the data directory in bytes"),
    "bench_disk_files": ("gauge", "Number of files in the data directory"),
}


def render():
    with _state_lock:
        merged = {}
        for db, vals in _stats.items():
            merged.setdefault(db, {}).update(vals)
        for db, vals in _disk.items():
            merged.setdefault(db, {}).update(vals)

    by_metric = {}
    for db, vals in merged.items():
        for name, value in vals.items():
            by_metric.setdefault(name, []).append((db, value))

    out = []
    for name in HELP:
        if name not in by_metric:
            continue
        mtype, helptext = HELP[name]
        out.append(f"# HELP {name} {helptext}")
        out.append(f"# TYPE {name} {mtype}")
        for db, value in sorted(by_metric[name]):
            out.append(f'{name}{{db="{db}"}} {value}')
    out.append("")
    return "\n".join(out)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.split("?")[0] not in ("/metrics", "/"):
            self.send_error(404)
            return
        body = render().encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


def main():
    threading.Thread(target=poll_stats, daemon=True).start()
    threading.Thread(target=poll_disk, daemon=True).start()
    print(f"bench-exporter listening on :{LISTEN_PORT}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", LISTEN_PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
