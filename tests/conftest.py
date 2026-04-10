from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from polar_cli.models import AppPaths


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def app_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AppPaths:
    config_home = tmp_path / "config-home"
    data_home = tmp_path / "data-home"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    return AppPaths.discover()


@pytest.fixture
def credentials_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POLAR_CLIENT_ID", "client-id")
    monkeypatch.setenv("POLAR_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv("POLAR_MEMBER_ID", "moritz")


def write_state(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
