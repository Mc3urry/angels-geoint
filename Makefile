.PHONY: install dev analysis ml sar forensics test lint serve web ingest reproduce check clean

install:
	pip install -e .

dev:
	pip install -e ".[dev]"

# Phase 4 stack. Heavy -- install when you get there, not before.
analysis:
	pip install -e ".[analysis]"

ml:
	pip install -e ".[ml]"

sar:
	pip install -e ".[sar]"

forensics:
	pip install -e ".[forensics]"

test:
	pytest -q

lint:
	ruff check angels tests

serve:
	uvicorn angels.api.main:app --reload

# Static server for web/. The API runs separately on 8000.
web:
	python -m http.server 5173 --directory web

ingest:
	python scripts/ingest_aviation.py

# Can someone else get this result? --check inventories; the bare target runs
# the cheap stages; --all re-runs the boundary nulls and compares them against
# the committed numbers. Output goes to data/events/reproduce/ and overwrites
# nothing.
check:
	python scripts/reproduce.py --check

reproduce:
	python scripts/reproduce.py

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache .ruff_cache
