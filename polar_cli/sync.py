from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from .api import PolarApiError, PolarClient
from .models import AppPaths, State
from .storage import (
    archive_json,
    init_db,
    insert_sync_run,
    upsert_activity_summaries,
    upsert_exercises,
    upsert_nightly_recharge,
    upsert_sleep,
)


class SyncEngine:
    def __init__(
        self,
        *,
        paths: AppPaths,
        state: State,
        connection: sqlite3.Connection,
        client: PolarClient,
    ):
        self.paths = paths
        self.state = state
        self.connection = connection
        self.client = client
        self.warnings: list[str] = []
        init_db(self.connection)

    def run(self, resource: str, since_days: int | None = None) -> dict[str, int]:
        started_at = datetime.now(UTC)
        counts = {"exercises": 0, "activity": 0, "sleep": 0, "nightly_recharge": 0}
        try:
            if resource in {"all", "exercises"}:
                counts["exercises"] = self.sync_exercises(since_days)
            if resource in {"all", "activity"}:
                counts["activity"] = self.sync_activity(since_days)
            if resource in {"all", "sleep"}:
                counts["sleep"] = self.sync_sleep(since_days)
            if resource in {"all", "nightly-recharge"}:
                counts["nightly_recharge"] = self.sync_nightly_recharge(since_days)
            insert_sync_run(
                self.connection,
                started_at=started_at,
                finished_at=datetime.now(UTC),
                success=True,
                resource=resource,
                counts=counts,
                error_text=None,
            )
            return counts
        except Exception as exc:
            insert_sync_run(
                self.connection,
                started_at=started_at,
                finished_at=datetime.now(UTC),
                success=False,
                resource=resource,
                counts=counts,
                error_text=str(exc),
            )
            raise

    def sync_exercises(self, since_days: int | None = None) -> int:
        items = self.client.get_exercises(since_days)
        with self.connection:
            count = upsert_exercises(self.connection, self.state.polar_user_id or "", items)
            archive_json(self.paths, "exercises", {"items": items}, "collection")
            for index, item in enumerate(items, start=1):
                archive_json(self.paths, "exercises", item, f"detail-{index}")
        return count

    def sync_activity(self, since_days: int | None = None) -> int:
        try:
            items = self.client.get_activity(since_days)
        except PolarApiError as exc:
            if exc.status_code == 404:
                self.warnings.append("Activity endpoint is unavailable for this account right now; skipped activity sync.")
                return 0
            raise
        with self.connection:
            count = upsert_activity_summaries(self.connection, self.state.polar_user_id or "", items)
            archive_json(self.paths, "activity", {"items": items}, "collection")
        return count

    def sync_sleep(self, since_days: int | None = None) -> int:
        items = self.client.get_sleep(self.state.polar_user_id or "", since_days)
        with self.connection:
            count = upsert_sleep(self.connection, self.state.polar_user_id or "", items)
            archive_json(self.paths, "sleep", {"items": items}, "collection")
        return count

    def sync_nightly_recharge(self, since_days: int | None = None) -> int:
        items = self.client.get_nightly_recharge(self.state.polar_user_id or "", since_days)
        with self.connection:
            count = upsert_nightly_recharge(self.connection, self.state.polar_user_id or "", items)
            archive_json(self.paths, "nightly-recharge", {"items": items}, "collection")
        return count
