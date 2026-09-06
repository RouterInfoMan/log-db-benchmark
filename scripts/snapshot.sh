#!/usr/bin/env bash
# Render the benchmark dashboard to a PNG.
# Needs the optional renderer: docker compose --profile render up -d renderer
set -euo pipefail
cd "$(dirname "$0")/.."

OUT="${1:-results/dashboard.png}"
FROM="${FROM:-now-1h}"
WIDTH="${WIDTH:-1600}"
HEIGHT="${HEIGHT:-2200}"

if ! docker ps --format '{{.Names}}' | grep -q '^bench-renderer$'; then
  echo "Starting the image renderer (first run pulls ~400MB)..."
  docker compose --profile render up -d renderer
  sleep 15
fi

mkdir -p "$(dirname "$OUT")"
curl -sf -u admin:admin -o "$OUT" \
  "http://localhost:3000/render/d/logdb-bench/log-db-benchmark?orgId=1&from=${FROM}&to=now&width=${WIDTH}&height=${HEIGHT}&kiosk&theme=dark"
echo "wrote $OUT"
