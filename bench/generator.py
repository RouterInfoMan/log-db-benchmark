"""Deterministic synthetic log generation.

Every record is a pure function of (seed, global document index), so all three
databases receive byte-for-byte identical data and any run can be reproduced.
Timestamps increase monotonically with the document index, which keeps Loki's
per-stream ordering requirement satisfied without special-casing it.
"""
import random

SERVICES = [
    "checkout-api", "payment-gateway", "auth-service", "search-indexer",
    "cart-service", "shipping-worker", "notification-svc", "user-profile",
]
HOSTS = [f"node-{i:02d}" for i in range(1, 13)]
REGIONS = ["us-east", "eu-west", "ap-south"]
METHODS = ["GET", "POST", "PUT", "DELETE", "PATCH"]
PATHS = [
    "/api/v1/checkout", "/api/v1/payment", "/api/v2/checkout", "/api/v2/payment",
    "/api/v1/cart", "/api/v1/search", "/api/v1/login", "/api/v2/profile",
    "/health", "/metrics", "/api/v1/orders", "/api/v3/recommendations",
]
# level -> cumulative probability
LEVELS = [("INFO", 0.70), ("DEBUG", 0.85), ("WARN", 0.95), ("ERROR", 1.0)]

ERROR_KINDS = [
    "connection timeout after 30000ms",
    "upstream returned 503 service unavailable",
    "database deadlock detected, transaction rolled back",
    "payment authorization declined by issuer",
    "circuit breaker open for downstream dependency",
]
WARN_KINDS = [
    "slow query detected, consider adding an index",
    "retry attempt 2 of 3 after timeout",
    "cache miss ratio above threshold",
    "connection pool nearing capacity",
]

# A rare literal token planted at a fixed stride so the "needle in a haystack"
# query has an exactly known, verifiable hit count.
CANARY_TOKEN = "CANARY7"
CANARY_STRIDE = 200_000
CANARY_OFFSET = 137


def canary_hits(total_docs):
    """How many documents carry CANARY_TOKEN for a dataset of this size."""
    if total_docs <= CANARY_OFFSET:
        return 0
    return (total_docs - 1 - CANARY_OFFSET) // CANARY_STRIDE + 1


def _level(u):
    for name, cutoff in LEVELS:
        if u < cutoff:
            return name
    return "ERROR"


def make_record(idx, ts_ms, rng):
    """Build one log record. `rng` must be seeded from the batch index."""
    service = SERVICES[idx % len(SERVICES)]
    host = HOSTS[(idx // 7) % len(HOSTS)]
    region = REGIONS[HOSTS.index(host) % len(REGIONS)]
    level = _level(rng.random())
    method = METHODS[rng.randrange(len(METHODS))]
    path = PATHS[rng.randrange(len(PATHS))]

    if level == "ERROR":
        status = rng.choice([500, 502, 503, 500, 500])
        latency = rng.randint(400, 4000)
        detail = ERROR_KINDS[rng.randrange(len(ERROR_KINDS))]
    elif level == "WARN":
        status = rng.choice([200, 400, 404, 429])
        latency = rng.randint(200, 1800)
        detail = WARN_KINDS[rng.randrange(len(WARN_KINDS))]
    else:
        status = rng.choice([200, 200, 200, 201, 204, 304])
        latency = rng.randint(2, 350)
        detail = "request completed"

    user_id = f"u-{rng.randrange(500_000):06d}"
    trace_id = f"{rng.getrandbits(64):016x}"
    nbytes = rng.randint(180, 48_000)

    message = (
        f"{method} {path} status={status} latency_ms={latency} "
        f"bytes={nbytes} trace={trace_id} user={user_id} - {detail}"
    )
    if idx % CANARY_STRIDE == CANARY_OFFSET:
        message = f"{message} {CANARY_TOKEN}"

    return {
        "timestamp": ts_ms,
        "level": level,
        "service": service,
        "host": host,
        "region": region,
        "method": method,
        "path": path,
        "status": status,
        "latency_ms": latency,
        "bytes": nbytes,
        "user_id": user_id,
        "trace_id": trace_id,
        "message": message,
    }


def batch(batch_idx, batch_size, total_docs, t_start_ms, ms_per_doc, seed):
    """Deterministically produce the records for one batch.

    Returns a list of dicts with `timestamp` as epoch milliseconds.
    """
    start = batch_idx * batch_size
    count = min(batch_size, total_docs - start)
    if count <= 0:
        return []
    rng = random.Random((seed * 1_000_003) ^ (batch_idx * 2_654_435_761))
    out = []
    for i in range(count):
        idx = start + i
        out.append(make_record(idx, int(t_start_ms + idx * ms_per_doc), rng))
    return out
