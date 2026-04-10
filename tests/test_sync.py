from __future__ import annotations

import json
import sqlite3

import pytest

from polar_cli.api import PolarApiError
from polar_cli.models import State
from polar_cli.storage import connect_db, init_db
from polar_cli.sync import SyncEngine


class FakeSyncClient:
    def __init__(self):
        self.exercise_since_days: int | None = None
        self.activity_since_days: int | None = None
        self.sleep_since_days: int | None = None
        self.recharge_since_days: int | None = None

    def get_exercises(self, since_days: int | None = None) -> list[dict]:
        self.exercise_since_days = since_days
        return [
            {
                "id": "exercise-1",
                "resource-uri": "https://api/exercise/1",
                "start-time": "2024-01-01T10:00:00Z",
                "duration": "PT45M",
                "sport": "RUNNING",
                "detailed-sport-info": "ROAD_RUNNING",
                "distance": 10000,
                "calories": 650,
                "heart-rate": {"average": 146, "maximum": 181, "minimum": 92},
                "training-load": 123.4,
                "ascent": 110,
                "descent": 108,
                "speed": {"average": 3.7, "maximum": 5.1},
                "pace": {"average": 270, "maximum": 180},
                "cadence": {"average": 82, "maximum": 91},
                "power": {"average": 250, "maximum": 410},
                "samples": {
                    "heart-rate": [{"date-time": "2024-01-01T10:00:00Z", "value": 120}],
                    "speed": [{"date-time": "2024-01-01T10:00:00Z", "value": 3.2}],
                },
                "zones": [{"index": 1, "lower-limit": 90, "upper-limit": 120, "in-zone": "PT10M"}],
                "route": [{"latitude": 60.1, "longitude": 24.9}],
            }
        ]

    def get_activity(self, since_days: int | None = None) -> list[dict]:
        self.activity_since_days = since_days
        return [
            {
                "id": "activity-1",
                "resource-uri": "https://api/activity/1",
                "date": "2024-01-01",
                "steps": 1000,
            }
        ]

    def get_sleep(self, polar_user_id: str, since_days: int | None = None) -> list[dict]:
        assert polar_user_id == "polar-user"
        self.sleep_since_days = since_days
        return [{"date": "2024-01-01", "duration": "PT8H", "score": 80}]

    def get_nightly_recharge(self, polar_user_id: str, since_days: int | None = None) -> list[dict]:
        assert polar_user_id == "polar-user"
        self.recharge_since_days = since_days
        return [{"date": "2024-01-01", "status": "OK"}]


def test_sync_uses_non_transactional_collections_and_persists_training_details(app_paths):
    connection = connect_db(app_paths)
    client = FakeSyncClient()
    state = State(access_token="token", polar_user_id="polar-user", member_id="moritz")

    try:
        engine = SyncEngine(paths=app_paths, state=state, connection=connection, client=client)
        counts = engine.run("all", since_days=7)
    finally:
        connection.close()

    assert counts == {"exercises": 1, "activity": 1, "sleep": 1, "nightly_recharge": 1}
    assert engine.warnings == []
    assert client.exercise_since_days == 7
    assert client.activity_since_days == 7
    assert client.sleep_since_days == 7
    assert client.recharge_since_days == 7

    connection = sqlite3.connect(app_paths.db_file)
    try:
        init_db(connection)
        row = connection.execute(
            "SELECT avg_hr, max_hr, min_hr, average_speed, maximum_speed, average_pace, cadence_avg, power_avg, route_points, samples_json, zones_json, route_json FROM exercises"
        ).fetchone()
        activity_count = connection.execute("SELECT COUNT(*) FROM activity_summaries").fetchone()[0]
    finally:
        connection.close()

    assert row[0:9] == (146, 181, 92, 3.7, 5.1, 270, 82, 250, 1)
    assert json.loads(row[9])["heart-rate"][0]["value"] == 120
    assert json.loads(row[10])[0]["upper-limit"] == 120
    assert json.loads(row[11])[0]["latitude"] == 60.1
    assert activity_count == 1


def test_sync_exercises_does_not_commit_partial_local_write(app_paths, monkeypatch):
    connection = connect_db(app_paths)
    client = FakeSyncClient()
    state = State(access_token="token", polar_user_id="polar-user", member_id="moritz")

    def explode(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("polar_cli.sync.archive_json", explode)

    try:
        engine = SyncEngine(paths=app_paths, state=state, connection=connection, client=client)
        with pytest.raises(OSError):
            engine.sync_exercises()
    finally:
        connection.close()

    connection = sqlite3.connect(app_paths.db_file)
    try:
        init_db(connection)
        stored = connection.execute("SELECT COUNT(*) FROM exercises").fetchone()[0]
    finally:
        connection.close()
    assert stored == 0


class MissingActivityClient(FakeSyncClient):
    def get_activity(self, since_days: int | None = None) -> list[dict]:
        raise PolarApiError("activity fetch failed: HTTP 404 Not Found", status_code=404)


def test_sync_activity_downgrades_404_to_warning(app_paths):
    connection = connect_db(app_paths)
    client = MissingActivityClient()
    state = State(access_token="token", polar_user_id="polar-user", member_id="moritz")

    try:
        engine = SyncEngine(paths=app_paths, state=state, connection=connection, client=client)
        counts = engine.run("all", since_days=3)
    finally:
        connection.close()

    assert counts == {"exercises": 1, "activity": 0, "sleep": 1, "nightly_recharge": 1}
    assert engine.warnings == ["Activity endpoint is unavailable for this account right now; skipped activity sync."]
