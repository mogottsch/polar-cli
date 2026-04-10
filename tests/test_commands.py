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
                [
                    {
                        "id": "exercise-1",
                        "start-time": "2024-01-02T09:00:00Z",
                        "sport": "RUNNING",
                        "heart-rate": {"average": 150, "maximum": 176},
                        "samples": {"heart-rate": [{"value": 140}]},
                    }
                ],
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


def test_workouts_list_returns_stored_training_session_payload(runner, app_paths):
    connection = connect_db(app_paths)
    try:
        init_db(connection)
        with connection:
            upsert_exercises(
                connection,
                "polar-user",
                [
                    {
                        "id": "exercise-42",
                        "start-time": "2024-01-02T09:00:00Z",
                        "sport": "RUNNING",
                        "duration": "PT55M",
                        "heart-rate": {"average": 151, "maximum": 184, "minimum": 96},
                        "speed": {"average": 3.9, "maximum": 5.2},
                        "pace": {"average": 255},
                        "samples": {
                            "heart-rate": [{"date-time": "2024-01-02T09:00:10Z", "value": 128}],
                            "speed": [{"date-time": "2024-01-02T09:00:10Z", "value": 3.3}],
                        },
                        "zones": [{"index": 1, "in-zone": "PT12M"}],
                        "route": [{"latitude": 1.23, "longitude": 4.56}],
                    }
                ],
            )
    finally:
        connection.close()

    workouts = runner.invoke(cli.app, ["workouts", "list", "--json"])

    assert workouts.exit_code == 0
    payload = json.loads(workouts.stdout)
    item = payload["data"]["items"][0]
    assert item["id"] == "exercise-42"
    assert item["heart-rate"]["average"] == 151
    assert item["samples"]["heart-rate"][0]["value"] == 128
    assert item["zones"][0]["in-zone"] == "PT12M"
    assert item["route"][0]["latitude"] == 1.23


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
        json.dumps({"access_token": "***", "polar_user_id": "known-user", "member_id": "moritz"}),
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


def test_sync_command_surfaces_activity_warning(runner, app_paths, credentials_env, monkeypatch):
    app_paths.state_file.parent.mkdir(parents=True, exist_ok=True)
    app_paths.state_file.write_text(
        json.dumps({"access_token": "token", "polar_user_id": "polar-user", "member_id": "moritz"}),
        encoding="utf-8",
    )

    class FakeEngine:
        def __init__(self, **kwargs):
            self.warnings = ["Activity endpoint is unavailable for this account right now; skipped activity sync."]

        def run(self, resource: str, since_days: int | None = None) -> dict[str, int]:
            assert resource == "all"
            assert since_days == 7
            return {"exercises": 1, "activity": 0, "sleep": 0, "nightly_recharge": 0}

    class FakeClient:
        def close(self) -> None:
            return

    monkeypatch.setattr(cli, "SyncEngine", FakeEngine)
    monkeypatch.setattr(cli, "create_client", lambda config, state: FakeClient())

    result = runner.invoke(cli.app, ["sync", "--since", "7", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["warnings"] == ["Activity endpoint is unavailable for this account right now; skipped activity sync."]
    assert payload["data"]["counts"]["activity"] == 0
