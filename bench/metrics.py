"""Resource sampling and publication of benchmark results to Prometheus."""
import re
import threading
import time
import urllib.parse

from . import config, httpc

_SAMPLE_FIELDS = {
    "bench_container_cpu_percent": "cpu_percent",
    "bench_container_memory_bytes": "mem_bytes",
    "bench_container_memory_percent": "mem_percent",
    "bench_container_memory_limit_bytes": "mem_limit_bytes",
    "bench_disk_usage_bytes": "disk_bytes",
    "bench_disk_apparent_bytes": "disk_apparent_bytes",
    "bench_container_net_rx_bytes_total": "net_rx_bytes",
    "bench_container_net_tx_bytes_total": "net_tx_bytes",
    "bench_container_blkio_read_bytes_total": "blk_read_bytes",
    "bench_container_blkio_write_bytes_total": "blk_write_bytes",
}

_LINE = re.compile(r'^(?P<name>[a-zA-Z_:][\w:]*)\{db="(?P<db>[^"]*)"\}\s+(?P<value>[-\d.eE+]+)$')


def scrape_exporter(url=None, timeout=15):
    """Return {db: {field: value}} from the benchmark exporter."""
    url = (url or config.EXPORTER_URL).rstrip("/") + "/metrics"
    _, raw, _ = httpc.request("GET", url, timeout=timeout)
    out = {}
    for line in raw.decode("utf-8", "replace").splitlines():
        if not line or line[0] == "#":
            continue
        m = _LINE.match(line.strip())
        if not m:
            continue
        field = _SAMPLE_FIELDS.get(m.group("name"))
        if field:
            out.setdefault(m.group("db"), {})[field] = float(m.group("value"))
    return out


class ResourceSampler:
    """Background thread that records CPU/RAM/disk per database, tagged by phase."""

    def __init__(self, interval=2.0, url=None):
        self.interval = interval
        self.url = url
        self.rows = []
        self._phase = "idle"
        self._detail = ""
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread = None

    def set_phase(self, phase, detail=""):
        with self._lock:
            self._phase, self._detail = phase, detail

    def start(self):
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=self.interval * 3)

    def _loop(self):
        while not self._stop.is_set():
            try:
                snapshot = scrape_exporter(self.url)
                with self._lock:
                    phase, detail = self._phase, self._detail
                now = time.time()
                for db, vals in snapshot.items():
                    row = {"timestamp": now, "phase": phase, "detail": detail, "db": db}
                    row.update(vals)
                    self.rows.append(row)
            except Exception:
                pass
            self._stop.wait(self.interval)

    def snapshot_now(self, retries=3):
        for _ in range(retries):
            try:
                return scrape_exporter(self.url)
            except Exception:
                time.sleep(2)
        return {}


# --------------------------------------------------------------------------- #
#  Pushgateway                                                                #
# --------------------------------------------------------------------------- #
def _fmt_labels(labels):
    if not labels:
        return ""
    inner = ",".join(f'{k}="{str(v)}"' for k, v in sorted(labels.items()))
    return "{" + inner + "}"


def push_metrics(job, grouping, samples, url=None):
    """Publish `samples` -- (name, labels, value, type, help) -- to the Pushgateway."""
    base = (url or config.PUSHGATEWAY_URL).rstrip("/")
    path = f"{base}/metrics/job/{urllib.parse.quote(job, safe='')}"
    for k, v in sorted(grouping.items()):
        path += f"/{urllib.parse.quote(k, safe='')}/{urllib.parse.quote(str(v), safe='')}"

    seen, lines = set(), []
    for name, labels, value, mtype, helptext in samples:
        if value is None:
            continue
        if name not in seen:
            lines.append(f"# HELP {name} {helptext}")
            lines.append(f"# TYPE {name} {mtype}")
            seen.add(name)
        lines.append(f"{name}{_fmt_labels(labels)} {value}")
    body = ("\n".join(lines) + "\n").encode()
    httpc.request("PUT", path, body=body,
                  headers={"Content-Type": "text/plain; version=0.0.4"}, timeout=60)


def clear_pushgateway(job="logdb_bench", dbs=config.DATABASES, url=None):
    base = (url or config.PUSHGATEWAY_URL).rstrip("/")
    for db in dbs:
        try:
            httpc.request("DELETE", f"{base}/metrics/job/{job}/db/{db}", timeout=30)
        except Exception:
            pass
