from __future__ import annotations

import json
import sqlite3

from polar_cli import cli
from polar_cli.storage import connect_db, init_db, upsert_activity_summaries, upsert_exercises, upsert_sleep


def test_workout_sleep_activity_list_commands(runner, app_paths):
    connection = connect_db(app_paths)
    try:
        init_db(connection)
        with connection:
            upsert_exercises(
                connection,
                "polar-user",
                [{"id": "exercise-1", "start-time": "2024-01-02T09:00:00Z", "sport": "RUNNING"}],
            )
            upsert_sleep(connection, "polar-user", [{"date": "2024-01-01", "duration": "PT7H"}])
            upsert_activity_summaries(
                connection,
                "polar-user",
                [{"id": "activity-1", "date": "2024-01-01", "steps": 3210}],
            )
    finally:
        connection.close()

    workouts = runner.invoke(cli.app, ["workouts", "list", "--json"])
    sleep = runner.invoke(cli.app, ["sleep", "list", "--json"])
    activity = runner.invoke(cli.app, ["activity", "list", "--json"])

    assert workouts.exit_code == 0
    assert sleep.exit_code == 0
    assert activity.exit_code == 0
    assert json.loads(workouts.stdout)["data"]["count"] == 1
    assert json.loads(sleep.stdout)["data"]["count"] == 1
    assert json.loads(activity.stdout)["data"]["count"] == 1


def test_doctor_fails_without_credentials(runner, app_paths):
    result = runner.invoke(cli.app, ["doctor", "--json"])
    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["data"]["sqlite_ok"] is True
    assert payload["data"]["state_path_writable"] is True
    assert payload["data"]["raw_path_writable"] is True
    assert "Missing Polar client credentials" in payload["error"]
    assert "Missing access token" in payload["error"]
    assert "Missing user registration" in payload["error"]


def test_doctor_fails_when_state_or_raw_paths_are_unwritable(runner, credentials_env, monkeypatch):
    monkeypatch.setattr(cli, "probe_writable_parent", lambda path: (_ for _ in ()).throw(OSError("no write")))
    monkeypatch.setattr(cli, "probe_writable_directory", lambda path: (_ for _ in ()).throw(OSError("no write")))

    result = runner.invoke(cli.app, ["doctor", "--json"])

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["data"]["state_path_writable"] is False
    assert payload["data"]["raw_path_writable"] is False
    assert "State path is not writable" in payload["error"]
    assert "Raw archive path is not writable" in payload["error"]


def test_workouts_list_returns_structured_sqlite_failure(runner, monkeypatch):
    monkeypatch.setattr(cli, "connect_db", lambda paths: (_ for _ in ()).throw(sqlite3.OperationalError("db locked")))

    result = runner.invoke(cli.app, ["workouts", "list", "--json"])

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["error"] == "db locked"


def test_user_info_uses_cached_polar_user_id_when_payload_id_missing(runner, app_paths, credentials_env, monkeypatch):
    app_paths.state_file.parent.mkdir(parents=True, exist_ok=True)
    app_paths.state_file.write_text(
        json.dumps({"access_token": "secret-token", "polar_user_id": "known-user", "member_id": "moritz"}),
        encoding="utf-8",
    )

    class FakeUserInfoClient:
        def get_user_info(self) -> dict[str, str]:
            return {"first-name": "Ada", "last-name": "Lovelace"}

        def close(self) -> None:
            return

    monkeypatch.setattr(cli, "create_client", lambda config, state: FakeUserInfoClient())

    result = runner.invoke(cli.app, ["user", "info", "--json"])

    assert result.exit_code == 0
    connection = connect_db(app_paths)
    try:
        init_db(connection)
        row = connection.execute("SELECT polar_user_id FROM users").fetchone()
    finally:
        connection.close()
    assert row[0] == "known-user"
