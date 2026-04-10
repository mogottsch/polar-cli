from __future__ import annotations

import json
import sqlite3

import pytest

from polar_cli.models import State
from polar_cli.storage import connect_db, init_db
from polar_cli.sync import SyncEngine


class FakeSyncClient:
    def __init__(self):
        self.commits: list[str] = []

    def open_transaction(self, polar_user_id: str, resource_path: str, list_key: str):
        return type(
            "Bundle",
            (),
            {
                "resource": resource_path,
                "transaction_url": f"https://api/{resource_path}/transactions/1",
                "commit_url": f"https://api/{resource_path}/transactions/1/commit",
                "item_urls": [f"https://api/{resource_path}/1"],
                "raw_open": {"transaction": resource_path},
                "raw_listing": {list_key: [{"url": f"https://api/{resource_path}/1"}]},
            },
        )()

    def fetch_resource(self, url: str) -> dict:
        if "exercise" in url:
            return {
                "id": "exercise-1",
                "resource-uri": url,
                "start-time": "2024-01-01T10:00:00Z",
                "duration": "PT45M",
                "sport": "RUNNING",
            }
        return {
            "id": "activity-1",
            "resource-uri": url,
            "date": "2024-01-01",
            "steps": 1000,
        }

    def commit_transaction(self, bundle) -> dict:
        self.commits.append(bundle.commit_url)
        return {}

    def get_sleep(self, polar_user_id: str, since_days: int | None = None) -> list[dict]:
        return [{"date": "2024-01-01", "duration": "PT8H", "score": 80}]

    def get_nightly_recharge(self, polar_user_id: str, since_days: int | None = None) -> list[dict]:
        return [{"date": "2024-01-01", "status": "OK"}]


def test_transaction_commits_after_persistence(app_paths):
    connection = connect_db(app_paths)
    client = FakeSyncClient()
    state = State(access_token="token", polar_user_id="polar-user", member_id="moritz")

    try:
        engine = SyncEngine(paths=app_paths, state=state, connection=connection, client=client)
        counts = engine.run("all", since_days=7)
    finally:
        connection.close()

    assert counts == {"exercises": 1, "activity": 1, "sleep": 1, "nightly_recharge": 1}
    assert len(client.commits) == 2

    connection = sqlite3.connect(app_paths.db_file)
    try:
        exercise_count = connection.execute("SELECT COUNT(*) FROM exercises").fetchone()[0]
        activity_count = connection.execute("SELECT COUNT(*) FROM activity_summaries").fetchone()[0]
    finally:
        connection.close()
    assert exercise_count == 1
    assert activity_count == 1


def test_transaction_does_not_commit_if_local_write_fails(app_paths, monkeypatch):
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

    assert client.commits == []

    connection = sqlite3.connect(app_paths.db_file)
    try:
        init_db(connection)
        stored = connection.execute("SELECT COUNT(*) FROM exercises").fetchone()[0]
    finally:
        connection.close()
    assert stored == 0
