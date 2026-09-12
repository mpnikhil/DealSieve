# DealSieve developer entry points. `.env` (see .env.example) is loaded automatically if present.
-include .env
export

.PHONY: setup test test-live lint reset demo demo-offline api frontend clean

PY := .venv/bin/python
PORT ?= 8000

setup:            ## create the Python 3.12 venv and install everything (needs uv)
	uv venv --python 3.12 .venv
	uv pip install --python $(PY) -e '.[dev]'

test:             ## deterministic tests, no model calls
	$(PY) -m pytest -q

test-live:        ## tests that call the configured model backend
	$(PY) -m pytest -q -m live

lint:
	$(PY) -m ruff check dealsieve tests scripts

reset:            ## wipe the local DB and seed the watchlist with synthetic Sacramento deals
	$(PY) scripts/seed_demo.py

demo: reset       ## the story, with the configured model backend (default: cli)
	$(PY) scripts/inject_email.py fixtures/emails/01_initial_offer.eml
	$(PY) scripts/inject_email.py fixtures/emails/02_price_drop.eml

demo-offline:     ## the same story with the scripted model backend: no model calls, runs in seconds
	DEALSIEVE_MODEL_BACKEND=scripted $(PY) scripts/seed_demo.py
	DEALSIEVE_MODEL_BACKEND=scripted $(PY) scripts/inject_email.py fixtures/emails/01_initial_offer.eml
	DEALSIEVE_MODEL_BACKEND=scripted $(PY) scripts/inject_email.py fixtures/emails/02_price_drop.eml

api:              ## serve the API and the built dashboard on http://localhost:$(PORT)
	$(PY) -m uvicorn dealsieve.api.app:app --port $(PORT)

frontend:         ## build the dashboard into frontend/dist (served by `make api`)
	npm --prefix frontend ci
	npm --prefix frontend run build

clean:
	rm -rf data/dealsieve.db data/dealsieve.db-* .pytest_cache .ruff_cache
