# atlas-shadow — Makefile
#
# Targets:
#   setup              — create .venv and install requirements
#   shadow-run         — run benchmark on FIXTURE (default dogfood-v2-questions)
#                        Pass COMMIT=<sha> to use out-of-band ingest mode (D4).
#   shadow-grade       — re-grade an existing responses.jsonl in place
#   shadow-aggregate   — write shadow-runs/_aggregate/comparison-report.md
#   purge-orphans      — list/delete leaked atlas_shadow_* orgs in Atlas's DB.
#                        Pass DRY_RUN=1 for read-only inspection. Catches orgs
#                        that escaped the auto-rollback in ensure_org_for_commit
#                        (e.g., from crashes / kill signals).
#
#   ingest-bootstrap   — apply daemon schema to ~/.atlas-shadow/ingest.db
#   ingest-up          — run the ingest daemon in the foreground
#   ingest-up-detached — run the ingest daemon in the background (nohup);
#                        logs to .ingest-daemon.log; pid in .ingest-daemon.pid
#   ingest-down        — stop a detached ingest daemon (kill -TERM)
#   ingest-status      — print /status payload (no HTTP needed)
#   ingest-replay      — enqueue commit(s); pass COMMIT=<sha> or FROM=<sha>
#
#   grading-up         — alias for ingest-up. The daemon's FastAPI receiver
#                        handles both push (ingest) and pull_request (grading)
#                        events through a single endpoint; one process runs
#                        both surfaces. See docs/pre-merge-grading-gate.md.
#   grading-verify     — verify env vars + paths the pre-merge grading gate
#                        needs (GITHUB_WEBHOOK_SECRET, GITHUB_ATLAS_SHADOW_TOKEN,
#                        ATLAS_DB_URL chain, shadow-runs/ writable, etc.).
#                        Exit 0 when all hard requirements pass, 1 otherwise.
#
#   test               — pytest -v
#   clean              — remove .venv and __pycache__

PYTHON ?= python3
VENV ?= .venv
PY := $(VENV)/bin/python
FIXTURE ?= dogfood-v2-questions
COMMIT ?=
FROM ?=
SHADOW_CONFIG ?= shadow-config.yaml
INGEST_LOG ?= .ingest-daemon.log
INGEST_PIDFILE ?= .ingest-daemon.pid

.PHONY: setup
setup:
	$(PYTHON) -m venv $(VENV)
	$(PY) -m pip install --upgrade pip
	$(PY) -m pip install -r requirements.txt

.PHONY: shadow-run
shadow-run:
	$(PY) -m atlas_shadow.cli shadow-run \
	    --fixture $(FIXTURE) \
	    --config $(SHADOW_CONFIG) \
	    $(if $(COMMIT),--commit $(COMMIT))

.PHONY: shadow-grade
shadow-grade:
	$(PY) -m atlas_shadow.cli shadow-grade \
	    --fixture $(FIXTURE) \
	    --config $(SHADOW_CONFIG)

.PHONY: shadow-aggregate
shadow-aggregate:
	$(PY) -m atlas_shadow.cli shadow-aggregate \
	    --config $(SHADOW_CONFIG)

.PHONY: purge-orphans
purge-orphans:
	$(PY) -m atlas_shadow.cli purge-orphans \
	    --config $(SHADOW_CONFIG) \
	    $(if $(DRY_RUN),--dry-run)

.PHONY: test
test:
	$(PY) -m pytest tests/ -v

.PHONY: ingest-bootstrap
ingest-bootstrap:
	$(PY) -m atlas_shadow.ingest_daemon --config $(SHADOW_CONFIG) bootstrap

.PHONY: ingest-up
ingest-up:
	$(PY) -m atlas_shadow.ingest_daemon --config $(SHADOW_CONFIG) serve

.PHONY: ingest-up-detached
ingest-up-detached:
	nohup $(PY) -m atlas_shadow.ingest_daemon --config $(SHADOW_CONFIG) serve \
	    > $(INGEST_LOG) 2>&1 & echo $$! > $(INGEST_PIDFILE)
	@echo "ingest-daemon started (pid=$$(cat $(INGEST_PIDFILE)), log=$(INGEST_LOG))"

.PHONY: ingest-down
ingest-down:
	@if [ -f $(INGEST_PIDFILE) ]; then \
	    pid=$$(cat $(INGEST_PIDFILE)); \
	    if kill -TERM $$pid 2>/dev/null; then \
	        echo "sent SIGTERM to pid=$$pid"; \
	    else \
	        echo "pid=$$pid already dead"; \
	    fi; \
	    rm -f $(INGEST_PIDFILE); \
	else \
	    echo "no pidfile at $(INGEST_PIDFILE) — nothing to stop"; \
	fi

.PHONY: ingest-status
ingest-status:
	$(PY) -m atlas_shadow.ingest_daemon --config $(SHADOW_CONFIG) status

.PHONY: ingest-replay
ingest-replay:
	@if [ -n "$(COMMIT)" ]; then \
	    $(PY) -m atlas_shadow.ingest_daemon --config $(SHADOW_CONFIG) replay --commit $(COMMIT); \
	elif [ -n "$(FROM)" ]; then \
	    $(PY) -m atlas_shadow.ingest_daemon --config $(SHADOW_CONFIG) replay --from $(FROM); \
	else \
	    echo "Usage: make ingest-replay COMMIT=<sha>  OR  make ingest-replay FROM=<sha>"; \
	    exit 2; \
	fi

# T10 (P2 packet 2026-05-14-atlas-shadow-pre-merge-grading-gate-v1) —
# pre-merge grading gate targets.

.PHONY: grading-up
grading-up: ingest-up

.PHONY: grading-verify
grading-verify:
	$(PY) -m atlas_shadow.ingest_daemon --config $(SHADOW_CONFIG) grading-verify

.PHONY: clean
clean:
	rm -rf $(VENV) __pycache__ .pytest_cache
	find . -name '__pycache__' -type d -exec rm -rf {} +
	find . -name '*.pyc' -delete
