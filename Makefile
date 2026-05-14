# atlas-shadow — Makefile
#
# Targets:
#   setup            — create .venv and install requirements
#   shadow-run       — run benchmark on FIXTURE (default dogfood-v2-questions)
#                      Pass COMMIT=<sha> to use out-of-band ingest mode (D4).
#   shadow-grade     — re-grade an existing responses.jsonl in place
#   shadow-aggregate — write shadow-runs/_aggregate/comparison-report.md
#   purge-orphans    — list/delete leaked atlas_shadow_* orgs in Atlas's DB.
#                      Pass DRY_RUN=1 for read-only inspection. Catches orgs
#                      that escaped the auto-rollback in ensure_org_for_commit
#                      (e.g., from crashes / kill signals).
#   test             — pytest -v
#   clean            — remove .venv and __pycache__

PYTHON ?= python3
VENV ?= .venv
PY := $(VENV)/bin/python
FIXTURE ?= dogfood-v2-questions
COMMIT ?=
SHADOW_CONFIG ?= shadow-config.yaml

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

.PHONY: clean
clean:
	rm -rf $(VENV) __pycache__ .pytest_cache
	find . -name '__pycache__' -type d -exec rm -rf {} +
	find . -name '*.pyc' -delete
