from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from umdhub.core.config import REPO_ROOT, Config
from umdhub.core.state import State

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    raw = yaml.safe_load((REPO_ROOT / "config.yaml").read_text(encoding="utf-8"))
    study = tmp_path / "study"
    study.mkdir()
    shutil.copy(FIXTURES / "backlog.md", study / "backlog.md")
    raw["paths"].update({
        "db": str(tmp_path / "state.sqlite"),
        "lock": str(tmp_path / "refresh.lock"),
        "study_repo": str(study),
        "backlog_file": "backlog.md",
    })
    raw["web"]["auth_mode"] = "lan"
    raw["extract"]["enabled"] = False
    raw["notify"]["enabled"] = False
    return Config.model_validate(raw)


@pytest.fixture
def state(cfg: Config):
    st = State(cfg.db_path())
    yield st
    st.close()


@pytest.fixture
def seeded(cfg: Config, state: State) -> State:
    from umdhub.sources.seed import load_seed
    for it in load_seed(cfg.seed_path(), set(cfg.course_codes())):
        state.upsert_item(it)
    return state


@pytest.fixture
def client(cfg: Config, monkeypatch):
    from fastapi.testclient import TestClient
    from umdhub.app import create_app
    monkeypatch.setenv("HUB_SHARED_SECRET", "testsecret")
    app = create_app(cfg)
    with TestClient(app, base_url="http://testserver", client=("127.0.0.1", 40000)) as c:
        yield c
