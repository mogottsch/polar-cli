from __future__ import annotations

import json
from urllib.parse import parse_qs, urlparse

import pytest

from polar_cli import cli
from polar_cli.models import State


class FakeTokenClient:
    def __init__(self, token_payload: dict[str, str]):
        self.token_payload = token_payload

    def exchange_code(self, code: str, redirect_uri: str | None = None) -> dict[str, str]:
        assert code == "abc123"
        assert redirect_uri
        return self.token_payload

    def close(self) -> None:
        return


def authorization_state(url: str) -> str:
    return parse_qs(urlparse(url).query)["state"][0]


def test_auth_complete_persists_state(runner, app_paths, credentials_env, monkeypatch):
    monkeypatch.setattr(
        cli,
        "create_client",
        lambda config, state: FakeTokenClient({"access_token": "secret-token", "x_user_id": "ignored"}),
    )

    result = runner.invoke(cli.app, ["auth", "complete", "--code", "abc123", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["data"]["has_access_token"] is True

    saved = State.model_validate_json(app_paths.state_file.read_text(encoding="utf-8"))
    assert saved.access_token == "secret-token"
    assert saved.member_id == "moritz"
    assert "oauth" not in saved.metadata


def test_auth_complete_extracts_code_from_callback_url(runner, app_paths, credentials_env, monkeypatch):
    observed: dict[str, str] = {}

    class ObservedClient(FakeTokenClient):
        def exchange_code(self, code: str, redirect_uri: str | None = None) -> dict[str, str]:
            observed["code"] = code
            return {"access_token": "secret-token"}

    monkeypatch.setattr(cli, "create_client", lambda config, state: ObservedClient({"access_token": "unused"}))

    result = runner.invoke(
        cli.app,
        [
            "auth",
            "complete",
            "--callback-url",
            "http://127.0.0.1:8765/callback?code=abc123",
            "--json",
        ],
    )

    assert result.exit_code == 0
    assert observed["code"] == "abc123"


def test_auth_init_persists_oauth_state(runner, app_paths, credentials_env, monkeypatch):
    class FakePolarClient:
        def __init__(self, config):
            self.config = config

        def build_authorization_url(self, redirect_uri: str | None = None, oauth_state: str | None = None) -> str:
            assert redirect_uri is None
            assert oauth_state
            return f"https://flow.polar.com/oauth2/authorization?state={oauth_state}"

        def close(self) -> None:
            return

    monkeypatch.setattr(cli, "PolarClient", FakePolarClient)

    result = runner.invoke(cli.app, ["auth", "init", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    saved = State.model_validate_json(app_paths.state_file.read_text(encoding="utf-8"))
    assert payload["data"]["authorization_url"].endswith(saved.metadata["oauth"]["state"])
    assert saved.metadata["oauth"]["redirect_uri"] == "http://127.0.0.1:8765/callback"


def test_auth_login_no_listen_prints_manual_flow(runner, credentials_env, monkeypatch):
    class FakePolarClient:
        def __init__(self, config):
            self.config = config

        def build_authorization_url(self, redirect_uri: str | None = None, oauth_state: str | None = None) -> str:
            assert redirect_uri == "http://localhost:9999/callback"
            assert oauth_state
            return f"https://flow.polar.com/oauth2/authorization?client_id=client-id&state={oauth_state}"

        def close(self) -> None:
            return

    monkeypatch.setattr(cli, "PolarClient", FakePolarClient)

    result = runner.invoke(
        cli.app,
        ["auth", "login", "--no-listen", "--no-open", "--port", "9999", "--json"],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["data"]["mode"] == "manual"
    assert payload["data"]["redirect_uri"] == "http://localhost:9999/callback"
    assert payload["warnings"]


def test_auth_complete_validates_persisted_oauth_state(runner, app_paths, credentials_env, monkeypatch):
    monkeypatch.setattr(
        cli,
        "create_client",
        lambda config, state: FakeTokenClient({"access_token": "secret-token"}),
    )

    init_result = runner.invoke(cli.app, ["auth", "init", "--json"])
    assert init_result.exit_code == 0
    oauth_state = authorization_state(json.loads(init_result.stdout)["data"]["authorization_url"])

    complete_result = runner.invoke(
        cli.app,
        [
            "auth",
            "complete",
            "--callback-url",
            f"http://127.0.0.1:8765/callback?code=abc123&state={oauth_state}",
            "--json",
        ],
    )

    assert complete_result.exit_code == 0


def test_auth_complete_rejects_mismatched_oauth_state(runner, credentials_env, monkeypatch):
    class FakePolarClient:
        def __init__(self, config):
            self.config = config

        def build_authorization_url(self, redirect_uri: str | None = None, oauth_state: str | None = None) -> str:
            return f"https://flow.polar.com/oauth2/authorization?state={oauth_state}"

        def close(self) -> None:
            return

    monkeypatch.setattr(cli, "PolarClient", FakePolarClient)

    init_result = runner.invoke(cli.app, ["auth", "init", "--json"])
    assert init_result.exit_code == 0

    result = runner.invoke(
        cli.app,
        [
            "auth",
            "complete",
            "--callback-url",
            "http://127.0.0.1:8765/callback?code=abc123&state=wrong-state",
            "--json",
        ],
    )

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert "OAuth state mismatch" in payload["error"]


def test_auth_login_no_open_emits_authorization_url_before_waiting(runner, credentials_env, monkeypatch):
    class FakePolarClient:
        def __init__(self, config):
            self.config = config

        def build_authorization_url(self, redirect_uri: str | None = None, oauth_state: str | None = None) -> str:
            return f"https://flow.polar.com/oauth2/authorization?state={oauth_state}"

        def close(self) -> None:
            return

    class FakeCallbackServer:
        def __init__(self, host: str, port: int, path: str):
            self.host = host
            self.port = port
            self.path = path

        def start(self) -> None:
            return

        def wait(self, timeout: int) -> tuple[str | None, str | None, str | None]:
            return None, None, None

    monkeypatch.setattr(cli, "PolarClient", FakePolarClient)
    monkeypatch.setattr(cli, "CallbackServer", FakeCallbackServer)

    result = runner.invoke(cli.app, ["auth", "login", "--no-open", "--timeout", "1", "--json"])

    assert result.exit_code == 1
    assert "authorization_url:" in result.stderr
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert "Callback timeout" in payload["error"]


def test_auth_status_defaults_to_json(runner, app_paths):
    result = runner.invoke(cli.app, ["auth", "status"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["command"] == "auth status"
    assert payload["data"]["paths"]["state_file"] == str(app_paths.state_file)


def test_auth_complete_requires_one_input(runner, credentials_env):
    result = runner.invoke(cli.app, ["auth", "complete", "--json"])
    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert "exactly one" in payload["error"]


def test_auth_complete_code_mode_accepts_explicit_oauth_state(runner, app_paths, credentials_env, monkeypatch):
    monkeypatch.setattr(
        cli,
        "create_client",
        lambda config, state: FakeTokenClient({"access_token": "secret-token"}),
    )

    init_result = runner.invoke(cli.app, ["auth", "init", "--json"])
    oauth_state = authorization_state(json.loads(init_result.stdout)["data"]["authorization_url"])

    result = runner.invoke(
        cli.app,
        ["auth", "complete", "--code", "abc123", "--oauth-state", oauth_state, "--json"],
    )

    assert result.exit_code == 0
