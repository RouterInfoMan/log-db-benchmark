"""Per-database adapters: schema setup, ingestion, flush, and query execution."""
import json
import time

from . import config, httpc


class QueryResult:
    __slots__ = ("latency_s", "result_count", "response_bytes", "error", "engine_ms",
                 "lines_scanned", "bytes_scanned")

    def __init__(self, latency_s=0.0, result_count=None, response_bytes=0, error=None,
                 engine_ms=None, lines_scanned=None, bytes_scanned=None):
        self.latency_s = latency_s
        self.result_count = result_count
        self.response_bytes = response_bytes
        self.error = error
        self.engine_ms = engine_ms
        self.lines_scanned = lines_scanned
        self.bytes_scanned = bytes_scanned


class Target:
    name = "?"

    def wait_ready(self, timeout=300):
        raise NotImplementedError

    def prepare(self):
        """Create schema / clear previous data."""

    def encode(self, records):
        """Serialize a batch into this engine's wire format."""
        raise NotImplementedError

    def send(self, payload):
        """Ship one encoded batch. Raises on failure."""
        raise NotImplementedError

    def finalize(self):
        """Flush in-memory state so everything is queryable and on disk."""

    def doc_count(self, ctx):
        return None

    def run_query(self, spec, ctx, timeout=600):
        raise NotImplementedError


# --------------------------------------------------------------------------- #
#  Elasticsearch                                                              #
# --------------------------------------------------------------------------- #
class Elasticsearch(Target):
    name = "elasticsearch"

    def __init__(self, url=None, index=config.ES_INDEX, shards=1):
        self.url = (url or config.ES_URL).rstrip("/")
        self.index = index
        self.shards = shards

    def wait_ready(self, timeout=300):
        def check():
            doc, _, _ = httpc.get_json(f"{self.url}/_cluster/health", timeout=5)
            return doc.get("status") in ("green", "yellow")
        return httpc.wait_ready(check, "elasticsearch", timeout)

    def prepare(self):
        try:
            httpc.request("DELETE", f"{self.url}/{self.index}", timeout=120)
        except httpc.HttpError as e:
            if e.status != 404:
                raise
        body = {
            "settings": {
                "number_of_shards": self.shards,
                "number_of_replicas": 0,
                "refresh_interval": "1s",
                "index.translog.durability": "async",
            },
            "mappings": {"properties": {
                "timestamp": {"type": "date", "format": "epoch_millis"},
                "level": {"type": "keyword"},
                "service": {"type": "keyword"},
                "host": {"type": "keyword"},
                "region": {"type": "keyword"},
                "method": {"type": "keyword"},
                "path": {"type": "keyword"},
                "status": {"type": "integer"},
                "latency_ms": {"type": "integer"},
                "bytes": {"type": "long"},
                "user_id": {"type": "keyword"},
                "trace_id": {"type": "keyword"},
                "message": {"type": "text"},
            }},
        }
        httpc.request("PUT", f"{self.url}/{self.index}",
                      body=json.dumps(body),
                      headers={"Content-Type": "application/json"}, timeout=120)

    def encode(self, records):
        parts = []
        for r in records:
            parts.append('{"index":{}}\n')
            parts.append(json.dumps(r, separators=(",", ":")))
            parts.append("\n")
        return "".join(parts).encode()

    def send(self, payload):
        _, raw, _ = httpc.request(
            "POST", f"{self.url}/{self.index}/_bulk",
            body=payload,
            headers={"Content-Type": "application/x-ndjson"},
            params={"filter_path": "errors,items.*.error"},
            timeout=900)
        doc = json.loads(raw or b"{}")
        if doc.get("errors"):
            first = None
            for item in doc.get("items", [])[:1]:
                first = next(iter(item.values()), {}).get("error")
            raise RuntimeError(f"elasticsearch bulk errors: {first}")
        return len(payload)

    def finalize(self):
        httpc.request("POST", f"{self.url}/{self.index}/_refresh", timeout=600)
        httpc.request("POST", f"{self.url}/{self.index}/_flush", timeout=600)

    def doc_count(self, ctx):
        doc, _, _ = httpc.get_json(f"{self.url}/{self.index}/_count", timeout=120)
        return doc.get("count")

    def run_query(self, spec, ctx, timeout=600):
        body = spec["es"](ctx)
        try:
            doc, nbytes, elapsed = httpc.post_json(
                f"{self.url}/{self.index}/_search", body, timeout=timeout)
        except Exception as e:
            return QueryResult(error=str(e)[:300])
        return QueryResult(
            latency_s=elapsed,
            result_count=self._extract(spec, doc),
            response_bytes=nbytes,
            engine_ms=doc.get("took"))

    @staticmethod
    def _extract(spec, doc):
        aggs = doc.get("aggregations")
        if aggs:
            agg = next(iter(aggs.values()))
            if "buckets" in agg:
                return len(agg["buckets"])
            if "value" in agg:
                return agg["value"]
        hits = doc.get("hits", {})
        if spec["kind"] == "search":
            return len(hits.get("hits", []))
        return (hits.get("total") or {}).get("value", 0)


# --------------------------------------------------------------------------- #
#  Loki                                                                       #
# --------------------------------------------------------------------------- #
class Loki(Target):
    name = "loki"
    STREAM_LABELS = ("service", "host", "level")

    def __init__(self, url=None):
        self.url = (url or config.LOKI_URL).rstrip("/")

    def wait_ready(self, timeout=300):
        def check():
            _, raw, _ = httpc.request("GET", f"{self.url}/ready", timeout=5)
            return raw.strip() == b"ready"
        return httpc.wait_ready(check, "loki", timeout)

    def encode(self, records):
        streams = {}
        for r in records:
            key = (r["service"], r["host"], r["level"])
            entry = [str(r["timestamp"] * 1_000_000), json.dumps(r, separators=(",", ":"))]
            streams.setdefault(key, []).append(entry)
        payload = {"streams": [
            {"stream": {"job": "bench", "service": s, "host": h, "level": lv}, "values": v}
            for (s, h, lv), v in streams.items()]}
        return json.dumps(payload, separators=(",", ":")).encode()

    def send(self, payload):
        httpc.request("POST", f"{self.url}/loki/api/v1/push", body=payload,
                      headers={"Content-Type": "application/json"}, timeout=900)
        return len(payload)

    def finalize(self):
        # Force the ingester to cut and flush chunks to the filesystem store so
        # the on-disk measurement reflects reality rather than what is still in RAM.
        for path in ("/flush", "/ingester/flush"):
            try:
                httpc.request("POST", f"{self.url}{path}", timeout=600)
                break
            except Exception:
                continue
        time.sleep(5)

    def doc_count(self, ctx):
        res = self.run_query(
            {"kind": "count",
             "loki": lambda c: ("query", {
                 "query": f'sum(count_over_time({{job="bench"}}[{c["range_str"]}]))',
                 "time": c["end_ns"]})},
            ctx, timeout=900)
        return None if res.error else res.result_count

    def run_query(self, spec, ctx, timeout=600):
        endpoint, params = spec["loki"](ctx)
        try:
            doc, nbytes, elapsed = httpc.get_json(
                f"{self.url}/loki/api/v1/{endpoint}", params=params, timeout=timeout)
        except Exception as e:
            return QueryResult(error=str(e)[:300])
        summary = ((doc.get("data") or {}).get("stats") or {}).get("summary") or {}
        return QueryResult(
            latency_s=elapsed,
            result_count=self._extract(spec, doc),
            response_bytes=nbytes,
            engine_ms=round(summary.get("execTime", 0) * 1000, 3) or None,
            lines_scanned=summary.get("totalLinesProcessed"),
            bytes_scanned=summary.get("totalBytesProcessed"))

    @staticmethod
    def _extract(spec, doc):
        data = doc.get("data") or {}
        result = data.get("result") or []
        kind = spec["kind"]
        if kind == "search":
            return sum(len(s.get("values", [])) for s in result)
        if kind == "count":
            if not result:
                return 0
            val = result[0].get("value")
            if val:
                return float(val[1])
            vals = result[0].get("values") or []
            return float(vals[-1][1]) if vals else 0
        if kind == "groups":
            return len(result)
        return sum(len(s.get("values", [])) for s in result)


# --------------------------------------------------------------------------- #
#  VictoriaLogs                                                               #
# --------------------------------------------------------------------------- #
class VictoriaLogs(Target):
    name = "victorialogs"

    def __init__(self, url=None):
        self.url = (url or config.VL_URL).rstrip("/")

    def wait_ready(self, timeout=300):
        def check():
            status, _, _ = httpc.request("GET", f"{self.url}/", timeout=5)
            return status == 200
        return httpc.wait_ready(check, "victorialogs", timeout)

    def encode(self, records):
        return "\n".join(json.dumps(r, separators=(",", ":")) for r in records).encode() + b"\n"

    def send(self, payload):
        httpc.request(
            "POST", f"{self.url}/insert/jsonline", body=payload,
            headers={"Content-Type": "application/stream+json"},
            params={"_time_field": "timestamp", "_msg_field": "message",
                    "_stream_fields": "service,host,level"},
            timeout=900)
        return len(payload)

    def finalize(self):
        try:
            httpc.request("GET", f"{self.url}/internal/force_flush", timeout=600)
        except Exception:
            pass
        time.sleep(5)

    def doc_count(self, ctx):
        res = self.run_query(
            {"kind": "count",
             "vl": lambda c: ("stats_query", {"query": f'{c["vl_time"]} * | stats count() as hits'})},
            ctx, timeout=900)
        return None if res.error else res.result_count

    def run_query(self, spec, ctx, timeout=600):
        endpoint, params = spec["vl"](ctx)
        url = f"{self.url}/select/logsql/{endpoint}"
        try:
            if endpoint == "query":
                # Raw log search streams back NDJSON, one JSON object per line.
                _, raw, elapsed = httpc.request("GET", url, params=params, timeout=timeout)
                count = sum(1 for line in raw.split(b"\n") if line.strip())
                return QueryResult(latency_s=elapsed, result_count=count, response_bytes=len(raw))
            doc, nbytes, elapsed = httpc.get_json(url, params=params, timeout=timeout)
        except Exception as e:
            return QueryResult(error=str(e)[:300])
        return QueryResult(latency_s=elapsed, result_count=self._extract(spec, doc),
                           response_bytes=nbytes)

    @staticmethod
    def _extract(spec, doc):
        result = ((doc.get("data") or {}).get("result")) or []
        kind = spec["kind"]
        if kind == "count":
            if not result:
                return 0
            val = result[0].get("value")
            if val:
                return float(val[1])
            vals = result[0].get("values") or []
            return float(vals[-1][1]) if vals else 0
        if kind == "groups":
            return len(result)
        if kind == "series":
            return sum(len(s.get("values", [])) for s in result)
        return len(result)


def build_targets(names=None):
    all_targets = {
        "elasticsearch": Elasticsearch,
        "loki": Loki,
        "victorialogs": VictoriaLogs,
    }
    chosen = names or list(all_targets)
    return [all_targets[n]() for n in chosen]
