from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from typing import Any, Callable

from .api import PolarClient
from .models import AppPaths, State, TransactionBundle
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
        init_db(self.connection)

    def run(self, resource: str, since_days: int | None = None) -> dict[str, int]:
        started_at = datetime.now(UTC)
        counts = {"exercises": 0, "activity": 0, "sleep": 0, "nightly_recharge": 0}
        try:
            if resource in {"all", "exercises"}:
                counts["exercises"] = self.sync_exercises()
            if resource in {"all", "activity"}:
                counts["activity"] = self.sync_activity()
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

    def sync_exercises(self) -> int:
        return self._sync_transactional(
            resource_name="exercises",
            resource_path="exercise",
            list_key="exercises",
            store_fn=upsert_exercises,
        )

    def sync_activity(self) -> int:
        return self._sync_transactional(
            resource_name="activity",
            resource_path="activity",
            list_key="activities",
            store_fn=upsert_activity_summaries,
        )

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

    def _sync_transactional(
        self,
        *,
        resource_name: str,
        resource_path: str,
        list_key: str,
        store_fn: Callable[[sqlite3.Connection, str, list[dict[str, Any]]], int],
    ) -> int:
        bundle = self.client.open_transaction(self.state.polar_user_id or "", resource_path, list_key)
        details = [self.client.fetch_resource(item_url) for item_url in bundle.item_urls]

        with self.connection:
            count = store_fn(self.connection, self.state.polar_user_id or "", details)
            archive_json(self.paths, resource_name, bundle.raw_open, "transaction-open")
            archive_json(self.paths, resource_name, bundle.raw_listing, "transaction-list")
            for index, detail in enumerate(details, start=1):
                archive_json(self.paths, resource_name, detail, f"detail-{index}")

        self.client.commit_transaction(bundle)
        return count
