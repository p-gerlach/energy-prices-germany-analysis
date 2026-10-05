import os
import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from oco.collectors.base import Context  # noqa: E402
from oco.settings import Paths  # noqa: E402
from oco.storage.state import StateStore  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch, tmp_path):
    # tests never see real credentials or a real data dir
    for k in ("EIA_API_KEY", "FIRMS_MAP_KEY", "CDSE_USERNAME", "CDSE_PASSWORD", "OCO_DATA_DIR"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr("oco.settings.PROJECT_ROOT", tmp_path)  # no .env pickup
    yield


@pytest.fixture
def paths(tmp_path):
    return Paths(tmp_path / "data").ensure()


@pytest.fixture
def state(paths):
    return StateStore(paths.jobs_db)


def make_ctx(paths, state, handler):
    return Context(paths=paths, state=state, transport=httpx.MockTransport(handler), sleep=lambda s: None)


@pytest.fixture
def ctx_factory(paths, state):
    def _f(handler):
        return make_ctx(paths, state, handler)
    return _f
