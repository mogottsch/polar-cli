from __future__ import annotations

import json
import secrets
import sqlite3
import threading
import webbrowser
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import typer

from .api import PolarApiError, PolarClient
from .models import AppPaths, Config, State
from .storage import (
    connect_db,
    ensure_dir,
    extract_polar_user_id,
    init_db,
    list_rows,
    load_config,
    load_state,
    save_state,
    upsert_user,
)
from .sync import SyncEngine

app = typer.Typer(no_args_is_help=True, pretty_exceptions_enable=False)
auth_app = typer.Typer(no_args_is_help=True)
user_app = typer.Typer(no_args_is_help=True)
workouts_app = typer.Typer(no_args_is_help=True)
sleep_app = typer.Typer(no_args_is_help=True)
activity_app = typer.Typer(no_args_is_help=True)

app.add_typer(auth_app, name="auth")
app.add_typer(user_app, name="user")
app.add_typer(workouts_app, name="workouts")
app.add_typer(sleep_app, name="sleep")
app.add_typer(activity_app, name="activity")


class CliError(RuntimeError):
    pass


class CallbackServer:
    def __init__(self, host: str, port: int, path: str):
        self.host = host
        self.port = port
        self.path = path
        self.event = threading.Event()
        self.code: str | None = None
        self.error: str | None = None
        self.oauth_state: str | None = None
        self.server = HTTPServer((host, port), self._handler())
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                parsed = urlparse(self.path)
                if parsed.path != outer.path:
                    self.send_response(404)
                    self.end_headers()
                    return
                query = parse_qs(parsed.query)
                outer.code = query.get("code", [None])[0]
                outer.error = query.get("error", [None])[0]
                outer.oauth_state = query.get("state", [None])[0]
                outer.event.set()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"<html><body>Polar CLI login complete. You can close this window.</body></html>")

            def log_message(self, format: str, *args: Any) -> None:
                return

        return Handler

    def start(self) -> None:
        self.thread.start()

    def wait(self, timeout: int) -> tuple[str | None, str | None, str | None]:
        self.event.wait(timeout)
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=1)
        return self.code, self.error, self.oauth_state


def main() -> None:
    app()


def get_runtime() -> tuple[AppPaths, Config, State]:
    paths = AppPaths.discover()
    config = load_config(paths)
    state = load_state(paths)
    if not state.member_id:
        state.member_id = config.member_id
    return paths, config, state


def create_client(config: Config, state: State) -> PolarClient:
    return PolarClient(config=config, access_token=state.access_token)


def output_payload(payload: dict[str, Any], json_output: bool, *, default_json: bool = False) -> None:
    if json_output or default_json:
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
        return

    if payload["ok"]:
        typer.echo(f"{payload['command']}: ok")
    else:
        typer.echo(f"{payload['command']}: {payload.get('error', 'failed')}", err=True)
    for key, value in payload.get("data", {}).items():
        if isinstance(value, (dict, list)):
            typer.echo(f"{key}: {json.dumps(value, sort_keys=True)}")
        else:
            typer.echo(f"{key}: {value}")
    for warning in payload.get("warnings", []):
        typer.echo(f"warning: {warning}")


def ok(command: str, data: dict[str, Any] | None = None, warnings: list[str] | None = None) -> dict[str, Any]:
    return {"ok": True, "command": command, "data": data or {}, "warnings": warnings or []}


def fail(command: str, message: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"ok": False, "command": command, "data": data or {}, "warnings": [], "error": message}


def require_credentials(config: Config) -> None:
    if not config.has_credentials:
        raise CliError("Missing client credentials. Set POLAR_CLIENT_ID and POLAR_CLIENT_SECRET or add config.toml.")


def require_access_token(state: State) -> None:
    if not state.access_token:
        raise CliError("No access token present. Run `polarctl auth login` or `polarctl auth complete`.")


def require_registered(state: State) -> None:
    if not state.polar_user_id:
        raise CliError("User is not registered. Run `polarctl user register`.")


def generate_oauth_state() -> str:
    return secrets.token_urlsafe(32)


def remember_oauth_session(paths: AppPaths, state: State, *, redirect_uri: str, oauth_state: str) -> None:
    state.metadata["oauth"] = {
        "state": oauth_state,
        "redirect_uri": redirect_uri,
        "created_at": datetime.now(UTC).isoformat(),
    }
    save_state(paths, state)


def pending_oauth_session(state: State) -> dict[str, Any] | None:
    payload = state.metadata.get("oauth")
    if not isinstance(payload, dict):
        return None
    return payload


def clear_oauth_session(state: State) -> None:
    state.metadata.pop("oauth", None)


def validate_oauth_state(state: State, returned_state: str | None) -> None:
    payload = pending_oauth_session(state)
    if not payload:
        return
    expected_state = payload.get("state")
    if not isinstance(expected_state, str) or not expected_state:
        return
    if not returned_state:
        raise CliError("Missing OAuth state in callback. Start a new login with `polarctl auth init` or `polarctl auth login`.")
    if not secrets.compare_digest(expected_state, returned_state):
        raise CliError("OAuth state mismatch. Start a new login with `polarctl auth init` or `polarctl auth login`.")


def pending_redirect_uri(state: State, default_redirect_uri: str) -> str:
    payload = pending_oauth_session(state)
    if not payload:
        return default_redirect_uri
    redirect_uri = payload.get("redirect_uri")
    if isinstance(redirect_uri, str) and redirect_uri:
        return redirect_uri
    return default_redirect_uri


def parse_callback_code(
    callback_url: str | None,
    code: str | None,
    oauth_state: str | None,
) -> tuple[str, str | None]:
    if bool(callback_url) == bool(code):
        raise CliError("Provide exactly one of --callback-url or --code.")
    if code:
        return code, oauth_state
    parsed = urlparse(callback_url or "")
    query = parse_qs(parsed.query)
    if error := query.get("error", [None])[0]:
        raise CliError(f"Authorization denied: {error}")
    parsed_code = query.get("code", [None])[0]
    if not parsed_code:
        raise CliError("No authorization code found in callback URL.")
    return parsed_code, query.get("state", [None])[0]


def effective_redirect_uri(config: Config, port: int) -> str:
    parsed = urlparse(config.redirect_uri)
    if not parsed.scheme or not parsed.netloc:
        raise CliError("Configured redirect URI is invalid.")
    host = parsed.hostname or "127.0.0.1"
    path = parsed.path or "/callback"
    return f"{parsed.scheme}://{host}:{port}{path}"


def emit_authorization_url(authorization_url: str, *, json_output: bool) -> None:
    typer.echo(f"authorization_url: {authorization_url}", err=json_output)


def probe_writable_parent(path: Path) -> bool:
    ensure_dir(path.parent)
    probe_path = path.parent / f".{path.name}.write-test"
    probe_path.write_text("{}", encoding="utf-8")
    probe_path.unlink()
    return True


def probe_writable_directory(path: Path) -> bool:
    ensure_dir(path)
    probe_path = path / ".polar-cli-write-test"
    probe_path.write_text("{}", encoding="utf-8")
    probe_path.unlink()
    return True


def login_from_code(
    *,
    command: str,
    paths: AppPaths,
    config: Config,
    state: State,
    code: str,
    redirect_uri: str,
    show_token: bool = False,
) -> dict[str, Any]:
    client = create_client(config, state)
    try:
        token_payload = client.exchange_code(code, redirect_uri=redirect_uri)
    finally:
        client.close()
    state.access_token = str(token_payload.get("access_token") or token_payload.get("access-token") or "")
    if not state.access_token:
        raise CliError("Token exchange succeeded but no access token was returned.")
    state.token_acquired_at = datetime.now(UTC)
    state.member_id = state.member_id or config.member_id
    state.metadata.update({"token": {k: v for k, v in token_payload.items() if k != "access_token"}})
    clear_oauth_session(state)
    save_state(paths, state)

    data = {
        "member_id": state.member_id,
        "redirect_uri": redirect_uri,
        "token_acquired_at": state.token_acquired_at.isoformat(),
        "has_access_token": True,
    }
    if show_token:
        data["access_token"] = state.access_token
    return ok(command, data)


@auth_app.command("init")
def auth_init(json_output: bool = typer.Option(False, "--json")) -> None:
    command = "auth init"
    try:
        paths, config, state = get_runtime()
        require_credentials(config)
        oauth_state = generate_oauth_state()
        client = PolarClient(config=config)
        try:
            url = client.build_authorization_url(oauth_state=oauth_state)
        finally:
            client.close()
        remember_oauth_session(paths, state, redirect_uri=config.redirect_uri, oauth_state=oauth_state)
        output_payload(
            ok(command, {"authorization_url": url, "redirect_uri": config.redirect_uri, "member_id": config.member_id}),
            json_output,
        )
    except (CliError, PolarApiError, OSError) as exc:
        output_payload(fail(command, str(exc)), json_output)
        raise typer.Exit(1)


@auth_app.command("complete")
def auth_complete(
    callback_url: str | None = typer.Option(None, "--callback-url"),
    code: str | None = typer.Option(None, "--code"),
    oauth_state: str | None = typer.Option(None, "--oauth-state"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    command = "auth complete"
    try:
        paths, config, state = get_runtime()
        require_credentials(config)
        auth_code, returned_state = parse_callback_code(callback_url, code, oauth_state)
        validate_oauth_state(state, returned_state)
        redirect_uri = pending_redirect_uri(state, config.redirect_uri)
        payload = login_from_code(
            command=command,
            paths=paths,
            config=config,
            state=state,
            code=auth_code,
            redirect_uri=redirect_uri,
        )
        output_payload(payload, json_output)
    except (CliError, PolarApiError, OSError) as exc:
        output_payload(fail(command, str(exc)), json_output)
        raise typer.Exit(1)


@auth_app.command("login")
def auth_login(
    port: int = typer.Option(8765, "--port"),
    open_browser: bool = typer.Option(True, "--open/--no-open"),
    listen: bool = typer.Option(True, "--listen/--no-listen"),
    timeout: int = typer.Option(180, "--timeout"),
    json_output: bool = typer.Option(False, "--json"),
    show_token: bool = typer.Option(False, "--show-token"),
) -> None:
    command = "auth login"
    try:
        paths, config, state = get_runtime()
        require_credentials(config)
        redirect_uri = effective_redirect_uri(config, port)
        oauth_state = generate_oauth_state()
        client = PolarClient(config=config)
        try:
            authorization_url = client.build_authorization_url(redirect_uri=redirect_uri, oauth_state=oauth_state)
        finally:
            client.close()
        remember_oauth_session(paths, state, redirect_uri=redirect_uri, oauth_state=oauth_state)

        if not listen:
            payload = ok(
                command,
                {
                    "authorization_url": authorization_url,
                    "redirect_uri": redirect_uri,
                    "mode": "manual",
                },
                warnings=["Local callback listener disabled. Finish with `polarctl auth complete`."],
            )
            output_payload(payload, json_output)
            return

        parsed = urlparse(redirect_uri)
        server = CallbackServer(parsed.hostname or "127.0.0.1", parsed.port or port, parsed.path or "/callback")
        server.start()
        if open_browser:
            webbrowser.open(authorization_url)
        else:
            emit_authorization_url(authorization_url, json_output=json_output)
        code_value, error_value, returned_state = server.wait(timeout)
        if error_value:
            raise CliError(f"Authorization denied: {error_value}")
        if not code_value:
            raise CliError("Callback timeout while waiting for authorization code.")
        validate_oauth_state(state, returned_state)
        payload = login_from_code(
            command=command,
            paths=paths,
            config=config,
            state=state,
            code=code_value,
            redirect_uri=redirect_uri,
            show_token=show_token,
        )
        payload["data"]["authorization_url"] = authorization_url
        output_payload(payload, json_output)
    except (CliError, PolarApiError, OSError, sqlite3.Error) as exc:
        output_payload(fail(command, str(exc)), json_output)
        raise typer.Exit(1)


@auth_app.command("status")
def auth_status(json_output: bool = typer.Option(False, "--json")) -> None:
    command = "auth status"
    paths, config, state = get_runtime()
    payload = ok(
        command,
        {
            "has_client_credentials": config.has_credentials,
            "has_access_token": bool(state.access_token),
            "is_registered": bool(state.polar_user_id),
            "member_id": state.member_id or config.member_id,
            "paths": paths.as_dict(),
        },
    )
    output_payload(payload, json_output, default_json=True)


@user_app.command("register")
def user_register(json_output: bool = typer.Option(False, "--json")) -> None:
    command = "user register"
    try:
        paths, config, state = get_runtime()
        require_access_token(state)
        if state.polar_user_id:
            output_payload(
                ok(
                    command,
                    {
                        "polar_user_id": state.polar_user_id,
                        "member_id": state.member_id or config.member_id,
                        "registered_at": state.registered_at.isoformat() if state.registered_at else None,
                        "cached": True,
                    },
                ),
                json_output,
            )
            return

        client = create_client(config, state)
        try:
            payload = client.register_user(state.member_id or config.member_id)
        finally:
            client.close()

        state.polar_user_id = extract_polar_user_id(payload)
        if not state.polar_user_id:
            raise CliError("Registration succeeded but no Polar user id was returned.")
        state.registered_at = datetime.now(UTC)
        state.member_id = state.member_id or config.member_id
        state.metadata["user_registration"] = payload
        save_state(paths, state)

        connection = connect_db(paths)
        try:
            init_db(connection)
            with connection:
                upsert_user(connection, state.member_id, payload)
        finally:
            connection.close()

        output_payload(
            ok(
                command,
                {
                    "polar_user_id": state.polar_user_id,
                    "member_id": state.member_id,
                    "registered_at": state.registered_at.isoformat(),
                },
            ),
            json_output,
        )
    except (CliError, PolarApiError, OSError, sqlite3.Error, ValueError) as exc:
        output_payload(fail(command, str(exc)), json_output)
        raise typer.Exit(1)


@user_app.command("info")
def user_info(json_output: bool = typer.Option(False, "--json")) -> None:
    command = "user info"
    try:
        paths, config, state = get_runtime()
        require_access_token(state)
        client = create_client(config, state)
        try:
            payload = client.get_user_info()
        finally:
            client.close()

        polar_user_id = extract_polar_user_id(payload, fallback=state.polar_user_id)
        if polar_user_id:
            state.polar_user_id = polar_user_id
        state.metadata["user_info"] = payload
        save_state(paths, state)

        connection = connect_db(paths)
        try:
            init_db(connection)
            if state.polar_user_id:
                with connection:
                    upsert_user(
                        connection,
                        state.member_id or config.member_id,
                        payload,
                        polar_user_id=state.polar_user_id,
                    )
        finally:
            connection.close()
        output_payload(ok(command, payload), json_output)
    except (CliError, PolarApiError, OSError, sqlite3.Error, ValueError) as exc:
        output_payload(fail(command, str(exc)), json_output)
        raise typer.Exit(1)


@app.command("sync")
def sync_command(
    since: int | None = typer.Option(None, "--since"),
    resource: str = typer.Option("all", "--resource"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    command = "sync"
    try:
        if resource not in {"exercises", "activity", "sleep", "nightly-recharge", "all"}:
            raise CliError("Invalid resource. Choose exercises, activity, sleep, nightly-recharge, or all.")
        paths, config, state = get_runtime()
        require_access_token(state)
        require_registered(state)
        connection = connect_db(paths)
        client = create_client(config, state)
        try:
            engine = SyncEngine(paths=paths, state=state, connection=connection, client=client)
            counts = engine.run(resource=resource, since_days=since)
        finally:
            client.close()
            connection.close()
        state.last_sync_at = datetime.now(UTC)
        save_state(paths, state)
        output_payload(
            ok(
                command,
                {
                    "resource": resource,
                    "counts": counts,
                    "last_sync_at": state.last_sync_at.isoformat(),
                },
            ),
            json_output,
        )
    except (CliError, PolarApiError, OSError, sqlite3.Error, ValueError) as exc:
        output_payload(fail(command, str(exc)), json_output)
        raise typer.Exit(1)


@workouts_app.command("list")
def workouts_list(
    limit: int = typer.Option(20, "--limit"),
    since: str | None = typer.Option(None, "--since"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    command = "workouts list"
    connection: sqlite3.Connection | None = None
    try:
        paths, _, _ = get_runtime()
        connection = connect_db(paths)
        init_db(connection)
        items = list_rows(connection, "exercises", "start_time", limit, since)
    except sqlite3.Error as exc:
        output_payload(fail(command, str(exc)), json_output)
        raise typer.Exit(1)
    finally:
        if connection is not None:
            connection.close()
    output_payload(ok(command, {"items": items, "count": len(items)}), json_output)


@sleep_app.command("list")
def sleep_list(
    limit: int = typer.Option(20, "--limit"),
    since: str | None = typer.Option(None, "--since"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    command = "sleep list"
    connection: sqlite3.Connection | None = None
    try:
        paths, _, _ = get_runtime()
        connection = connect_db(paths)
        init_db(connection)
        items = list_rows(connection, "sleep", "date", limit, since)
    except sqlite3.Error as exc:
        output_payload(fail(command, str(exc)), json_output)
        raise typer.Exit(1)
    finally:
        if connection is not None:
            connection.close()
    output_payload(ok(command, {"items": items, "count": len(items)}), json_output)


@activity_app.command("list")
def activity_list(
    limit: int = typer.Option(20, "--limit"),
    since: str | None = typer.Option(None, "--since"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    command = "activity list"
    connection: sqlite3.Connection | None = None
    try:
        paths, _, _ = get_runtime()
        connection = connect_db(paths)
        init_db(connection)
        items = list_rows(connection, "activity_summaries", "date", limit, since)
    except sqlite3.Error as exc:
        output_payload(fail(command, str(exc)), json_output)
        raise typer.Exit(1)
    finally:
        if connection is not None:
            connection.close()
    output_payload(ok(command, {"items": items, "count": len(items)}), json_output)


@app.command("doctor")
def doctor(json_output: bool = typer.Option(False, "--json")) -> None:
    command = "doctor"
    paths, config, state = get_runtime()
    checks: dict[str, Any] = {
        "has_client_credentials": config.has_credentials,
        "has_access_token": bool(state.access_token),
        "is_registered": bool(state.polar_user_id),
        "config_dir_exists": paths.config_dir.exists(),
    }

    issues: list[str] = []
    if not checks["has_client_credentials"]:
        issues.append("Missing Polar client credentials")
    if not checks["has_access_token"]:
        issues.append("Missing access token")
    if not checks["is_registered"]:
        issues.append("Missing user registration")

    try:
        checks["state_path_writable"] = probe_writable_parent(paths.state_file)
    except OSError:
        checks["state_path_writable"] = False
        issues.append("State path is not writable")

    try:
        checks["raw_path_writable"] = probe_writable_directory(paths.raw_dir)
    except OSError:
        checks["raw_path_writable"] = False
        issues.append("Raw archive path is not writable")

    try:
        connection = connect_db(paths)
        try:
            init_db(connection)
            checks["sqlite_ok"] = True
        finally:
            connection.close()
    except (OSError, sqlite3.Error):
        checks["sqlite_ok"] = False
        issues.append("SQLite database is not writable")

    checks["paths"] = paths.as_dict()
    if issues:
        output_payload(fail(command, "; ".join(issues), checks), json_output, default_json=True)
        raise typer.Exit(1)
    output_payload(ok(command, checks), json_output, default_json=True)
