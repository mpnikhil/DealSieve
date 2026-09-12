from __future__ import annotations

import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures"


@pytest.fixture(scope="session")
def policy():
    from dealsieve.policy import load_policy

    return load_policy(ROOT / "config" / "investment_policy.yaml")


@pytest.fixture
def repo(tmp_path):
    from dealsieve.persistence import Repo

    r = Repo(tmp_path / "test.db")
    r.init_schema()
    return r


@pytest.fixture
def scripted_backend(monkeypatch):
    monkeypatch.setenv("DEALSIEVE_MODEL_BACKEND", "scripted")
    monkeypatch.setenv("DEALSIEVE_NOTIFIER", "console")
    os.environ.pop("TELEGRAM_BOT_TOKEN", None)
    yield


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES
