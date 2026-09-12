.PHONY: setup test test-live lint api demo reset frontend

PY := .venv/bin/python

setup:
	uv venv --python 3.12 .venv
	uv pip install --python $(PY) -e '.[dev]'

test:
	$(PY) -m pytest -q

test-live:
	$(PY) -m pytest -q -m live

lint:
	$(PY) -m ruff check dealsieve tests scripts

reset:
	rm -f data/dealsieve.db && $(PY) scripts/seed_demo.py

api:
	$(PY) -m uvicorn dealsieve.api.app:app --reload --port 8000

demo: reset
	$(PY) scripts/inject_email.py fixtures/emails/01_initial_offer.eml
	$(PY) scripts/inject_email.py fixtures/emails/02_price_drop.eml
