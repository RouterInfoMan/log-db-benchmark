"""Minimal timed HTTP client (stdlib only) used by every adapter."""
import gzip
import json
import time
import urllib.error
import urllib.parse
import urllib.request


class HttpError(Exception):
    def __init__(self, status, body, url):
        super().__init__(f"HTTP {status} for {url}: {body[:400]}")
        self.status = status
        self.body = body
        self.url = url


def request(method, url, body=None, headers=None, timeout=900, params=None):
    """Perform a request; returns (status, response_bytes, elapsed_seconds)."""
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    hdrs = {"Accept-Encoding": "gzip"}
    if headers:
        hdrs.update(headers)
    if isinstance(body, str):
        body = body.encode()
    req = urllib.request.Request(url, data=body, headers=hdrs, method=method)

    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            if resp.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
            elapsed = time.perf_counter() - t0
            return resp.status, raw, elapsed
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            if e.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
        except Exception:
            pass
        raise HttpError(e.code, raw.decode("utf-8", "replace"), url) from None


def get_json(url, params=None, timeout=900):
    _, raw, elapsed = request("GET", url, headers={"Accept": "application/json"},
                              timeout=timeout, params=params)
    return json.loads(raw or b"{}"), len(raw), elapsed


def post_json(url, payload, params=None, timeout=900):
    body = json.dumps(payload).encode()
    _, raw, elapsed = request(
        "POST", url, body=body,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        timeout=timeout, params=params)
    return json.loads(raw or b"{}"), len(raw), elapsed


def wait_ready(check, name, timeout=300, interval=2):
    """Poll `check()` until it returns truthy or the timeout expires."""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            if check():
                return True
        except Exception as e:
            last = e
        time.sleep(interval)
    raise TimeoutError(f"{name} not ready after {timeout}s (last error: {last})")
