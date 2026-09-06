# log-db-benchmark -- Elasticsearch vs Loki vs VictoriaLogs
.DEFAULT_GOAL := help
COMPOSE := docker compose
# Bringing the stack DOWN must include profile services (the optional renderer),
# otherwise it is left running and holds the network open.
DOWN    := docker compose --profile render down --remove-orphans

help:  ## Show this help
	@grep -hE "^[a-zA-Z0-9_-]+:.*?## " $(MAKEFILE_LIST) \
	 | awk 'BEGIN{FS=":.*?## "}{printf "  \033[1m%-14s\033[0m %s\n", $$1, $$2}'

up:  ## Start the whole stack (databases + Prometheus + Grafana)
	$(COMPOSE) up -d --build
	@echo "Grafana:    http://localhost:3000/d/logdb-bench"
	@echo "Prometheus: http://localhost:9090"

down:  ## Stop the stack, keep data
	$(DOWN)

# The databases run as root inside their containers, so their files are not
# removable by the host user -- a throwaway root container does the deleting.
WIPE = docker run --rm -v "$(CURDIR)/data:/data" python:3.12-slim sh -c

clean:  ## Delete database data only (keeps Grafana, Prometheus history, results)
	$(DOWN)
	@$(WIPE) 'rm -rf /data/elasticsearch /data/loki /data/victorialogs && \
	          mkdir -p /data/elasticsearch /data/loki /data/victorialogs && \
	          chmod 777 /data/elasticsearch /data/loki /data/victorialogs'
	@echo "Databases wiped. Grafana dashboards and Prometheus history are untouched."
	@echo "Run 'make up' to restart, or 'make reset' to also clear those."

reset:  ## FULL reset: databases + Prometheus history + Grafana state (keeps results/)
	@if [ "$(FORCE)" != "1" ]; then \
	  printf 'This deletes:\n'; \
	  printf '  - all database data (Elasticsearch, Loki, VictoriaLogs)\n'; \
	  printf '  - all Prometheus history (the live CPU/RAM/disk graphs)\n'; \
	  printf '  - all Grafana state, including dashboard edits made in the UI\n'; \
	  printf '  - the last run pushed to the Pushgateway\n'; \
	  printf 'CSV results in results/ are KEPT. Continue? [y/N] '; \
	  read ans; case "$$ans" in y|Y) ;; *) echo "aborted"; exit 1;; esac; \
	fi
	$(DOWN)
	@$(WIPE) 'rm -rf /data/elasticsearch /data/loki /data/victorialogs /data/prometheus && \
	          rm -f /data/grafana/grafana.db /data/grafana/grafana.db-wal /data/grafana/grafana.db-shm && \
	          mkdir -p /data/elasticsearch /data/loki /data/victorialogs /data/prometheus && \
	          chmod 777 /data/elasticsearch /data/loki /data/victorialogs /data/prometheus'
	@echo
	@echo "Everything cleared. Run 'make up' to rebuild the stack from config/."
	@echo "Grafana re-provisions its datasources and the shipped dashboard on start."
	@echo "Grafana's plugin cache is kept, so this works offline; to drop that too:"
	@echo "  docker run --rm -v \"$(CURDIR)/data:/data\" python:3.12-slim rm -rf /data/grafana"

reset-dashboards:  ## Discard Grafana UI edits, restore the shipped dashboard
	@# Grafana refuses to delete a provisioned dashboard over its API, and its
	@# provisioner skips re-import while the file checksum is unchanged. Dropping
	@# grafana.db is the only reliable way back to the shipped dashboard. The
	@# plugin cache lives elsewhere in the volume and is deliberately preserved.
	$(COMPOSE) stop grafana
	@$(WIPE) 'rm -f /data/grafana/grafana.db /data/grafana/grafana.db-wal /data/grafana/grafana.db-shm'
	python3 config/grafana/make_dashboard.py
	$(COMPOSE) start grafana
	@echo
	@echo "Grafana state dropped and restarted; dashboards and datasources are"
	@echo "re-provisioned from config/. The login is back to admin/admin."

clean-results:  ## Delete all CSV output in results/
	@if [ "$(FORCE)" != "1" ]; then \
	  printf 'Delete every run in results/? [y/N] '; \
	  read ans; case "$$ans" in y|Y) ;; *) echo "aborted"; exit 1;; esac; \
	fi
	rm -rf results/*/ results/latest results/*.png results/*.log
	@echo "results/ cleared."

logs:  ## Tail logs from every container
	$(COMPOSE) logs -f --tail=50

status:  ## Show container health and current resource usage
	@$(COMPOSE) ps
	@echo
	@curl -s localhost:9101/metrics | grep -E '^bench_(container_cpu|container_memory_bytes|disk_usage)' || true

smoke:  ## Fast end-to-end check (200k docs, ~2 minutes)
	python3 -m bench.run --profile smoke --settle 20 --repeats 3

bench:  ## Full benchmark run (5M docs, ~20 minutes)
	python3 -m bench.run --profile default

large:  ## Large benchmark run (25M docs, expect an hour or more)
	python3 -m bench.run --profile large

# ---- custom-size runs -------------------------------------------------------
# WINDOW defaults to holding document density constant at the default profile's
# ~208k docs/hour, so selectivity per query stays comparable across sizes.
# Override any of these on the command line.
DOCS     ?= 5000000
WINDOW   ?=
REPEATS  ?= 3
SETTLE   ?= 120
QTIMEOUT ?= 900

bench-custom:  ## Custom size: make bench-custom DOCS=20000000 [WINDOW=96] [REPEATS=3]
	@w="$(WINDOW)"; 	 if [ -z "$$w" ]; then w=$$(( $(DOCS) * 24 / 5000000 )); fi; 	 if [ "$$w" -lt 1 ]; then w=1; fi; 	 gb=$$(( $(DOCS) * 830 / 1073741824 )); 	 echo "documents : $(DOCS)"; 	 echo "window    : $$w h"; 	 echo "repeats   : $(REPEATS)   settle: $(SETTLE)s   query timeout: $(QTIMEOUT)s"; 	 echo "disk      : ~$$gb GB at peak across all three databases"; 	 echo; 	 python3 -m bench.run --profile large --docs $(DOCS) --window-hours $$w 	   --repeats $(REPEATS) --settle $(SETTLE) --query-timeout $(QTIMEOUT)

bench-20m:  ## 20M documents over 96h (allow ~45 minutes)
	@$(MAKE) --no-print-directory bench-custom DOCS=20000000

bench-50m:  ## 50M documents over 240h (allow ~2 hours)
	@$(MAKE) --no-print-directory bench-custom DOCS=50000000

queries:  ## Re-run only the query suite against already-loaded data
	python3 -m bench.run --skip-ingest --no-reset

dashboard:  ## Regenerate the Grafana dashboard JSON
	python3 config/grafana/make_dashboard.py

snapshot:  ## Render the dashboard to results/dashboard.png
	./scripts/snapshot.sh

results:  ## Print the summary sheet from the most recent run
	@column -s, -t results/latest/summary.csv | cut -c1-200

.PHONY: help up down clean reset reset-dashboards clean-results logs status \
        smoke bench large bench-custom bench-20m bench-50m \
        queries dashboard snapshot results
