from __future__ import annotations

import json
import os
import sqlite3
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable

from .models import AppPaths, Config, State


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:
        pass


def write_json_file(path: Path, payload: dict[str, Any]) -> None:
    ensure_dir(path.parent)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    try:
        tmp_path.chmod(0o600)
    except OSError:
        pass
    tmp_path.replace(path)
    try:
        path.chmod(0o600)
    except OSError:
        pass


def load_config(paths: AppPaths) -> Config:
    file_data: dict[str, Any] = {}
    if paths.config_file.exists():
        with paths.config_file.open("rb") as handle:
            file_data = tomllib.load(handle)

    env_data = {
        "client_id": os.environ.get("POLAR_CLIENT_ID"),
        "client_secret": os.environ.get("POLAR_CLIENT_SECRET"),
        "redirect_uri": os.environ.get("POLAR_REDIRECT_URI"),
        "member_id": os.environ.get("POLAR_MEMBER_ID"),
    }
    merged = {**file_data, **{k: v for k, v in env_data.items() if v is not None}}
    return Config.model_validate(merged)


def load_state(paths: AppPaths) -> State:
    if not paths.state_file.exists():
        return State()
    payload = json.loads(paths.state_file.read_text(encoding="utf-8"))
    return State.model_validate(payload)


def save_state(paths: AppPaths, state: State) -> None:
    write_json_file(paths.state_file, state.model_dump(mode="json"))


def connect_db(paths: AppPaths) -> sqlite3.Connection:
    ensure_dir(paths.data_dir)
    connection = sqlite3.connect(paths.db_file)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def init_db(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            polar_user_id TEXT PRIMARY KEY,
            member_id TEXT,
            first_name TEXT,
            last_name TEXT,
            birthdate TEXT,
            gender TEXT,
            weight REAL,
            height REAL,
            raw_json TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS exercises (
            id TEXT PRIMARY KEY,
            polar_user_id TEXT NOT NULL,
            start_time TEXT,
            duration TEXT,
            sport TEXT,
            distance REAL,
            calories INTEGER,
            avg_hr INTEGER,
            max_hr INTEGER,
            training_load REAL,
            resource_uri TEXT UNIQUE,
            raw_json TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS activity_summaries (
            id TEXT PRIMARY KEY,
            polar_user_id TEXT NOT NULL,
            date TEXT,
            active_calories INTEGER,
            steps INTEGER,
            distance REAL,
            resource_uri TEXT UNIQUE,
            raw_json TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS sleep (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            polar_user_id TEXT NOT NULL,
            date TEXT NOT NULL,
            duration TEXT,
            continuity REAL,
            score REAL,
            raw_json TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (polar_user_id, date)
        );

        CREATE TABLE IF NOT EXISTS nightly_recharge (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            polar_user_id TEXT NOT NULL,
            date TEXT NOT NULL,
            status TEXT,
            ans_charge TEXT,
            sleep_charge TEXT,
            raw_json TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (polar_user_id, date)
        );

        CREATE TABLE IF NOT EXISTS sync_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            success INTEGER NOT NULL,
            resource TEXT NOT NULL,
            counts_json TEXT NOT NULL,
            error_text TEXT
        );
        """
    )
    connection.commit()


def archive_json(paths: AppPaths, resource: str, payload: dict[str, Any], stem: str) -> Path:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    resource_dir = paths.raw_dir / resource
    ensure_dir(resource_dir)
    file_path = resource_dir / f"{timestamp}-{stem}.json"
    write_json_file(file_path, payload)
    return file_path


def _json(item: dict[str, Any]) -> str:
    return json.dumps(item, sort_keys=True)


def extract_polar_user_id(payload: dict[str, Any], fallback: str | None = None) -> str | None:
    for key in ("polar_user_id", "user-id", "id", "polar-user-id"):
        value = payload.get(key)
        if value is None:
            continue
        normalized = str(value).strip()
        if normalized:
            return normalized
    if fallback:
        normalized_fallback = str(fallback).strip()
        if normalized_fallback:
            return normalized_fallback
    return None


def upsert_user(
    connection: sqlite3.Connection,
    member_id: str,
    payload: dict[str, Any],
    *,
    polar_user_id: str | None = None,
) -> None:
    resolved_polar_user_id = extract_polar_user_id(payload, fallback=polar_user_id)
    if not resolved_polar_user_id:
        raise ValueError("User payload does not contain a Polar user id")
    connection.execute(
        """
        INSERT INTO users (
            polar_user_id, member_id, first_name, last_name, birthdate, gender, weight, height, raw_json, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(polar_user_id) DO UPDATE SET
            member_id = excluded.member_id,
            first_name = excluded.first_name,
            last_name = excluded.last_name,
            birthdate = excluded.birthdate,
            gender = excluded.gender,
            weight = excluded.weight,
            height = excluded.height,
            raw_json = excluded.raw_json,
            updated_at = CURRENT_TIMESTAMP
        """,
        (
            resolved_polar_user_id,
            member_id,
            payload.get("first-name") or payload.get("first_name"),
            payload.get("last-name") or payload.get("last_name"),
            payload.get("birthdate"),
            payload.get("gender"),
            payload.get("weight"),
            payload.get("height"),
            _json(payload),
        ),
    )


def upsert_exercises(
    connection: sqlite3.Connection, polar_user_id: str, items: Iterable[dict[str, Any]]
) -> int:
    count = 0
    for item in items:
        resource_uri = item.get("resource-uri") or item.get("resource_uri") or item.get("url")
        item_id = str(item.get("id") or resource_uri)
        connection.execute(
            """
            INSERT INTO exercises (
                id, polar_user_id, start_time, duration, sport, distance, calories, avg_hr, max_hr, training_load,
                resource_uri, raw_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(id) DO UPDATE SET
                polar_user_id = excluded.polar_user_id,
                start_time = excluded.start_time,
                duration = excluded.duration,
                sport = excluded.sport,
                distance = excluded.distance,
                calories = excluded.calories,
                avg_hr = excluded.avg_hr,
                max_hr = excluded.max_hr,
                training_load = excluded.training_load,
                resource_uri = excluded.resource_uri,
                raw_json = excluded.raw_json,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                item_id,
                polar_user_id,
                item.get("start-time") or item.get("start_time"),
                item.get("duration"),
                item.get("sport") or item.get("sport-name"),
                item.get("distance"),
                item.get("calories"),
                item.get("heart-rate") and item["heart-rate"].get("average") or item.get("avg_hr"),
                item.get("heart-rate") and item["heart-rate"].get("maximum") or item.get("max_hr"),
                item.get("training-load") or item.get("training_load"),
                resource_uri,
                _json(item),
            ),
        )
        count += 1
    return count


def upsert_activity_summaries(
    connection: sqlite3.Connection, polar_user_id: str, items: Iterable[dict[str, Any]]
) -> int:
    count = 0
    for item in items:
        resource_uri = item.get("resource-uri") or item.get("resource_uri") or item.get("url")
        item_id = str(item.get("id") or resource_uri or item.get("date"))
        connection.execute(
            """
            INSERT INTO activity_summaries (
                id, polar_user_id, date, active_calories, steps, distance, resource_uri, raw_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(id) DO UPDATE SET
                polar_user_id = excluded.polar_user_id,
                date = excluded.date,
                active_calories = excluded.active_calories,
                steps = excluded.steps,
                distance = excluded.distance,
                resource_uri = excluded.resource_uri,
                raw_json = excluded.raw_json,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                item_id,
                polar_user_id,
                item.get("date"),
                item.get("active-calories") or item.get("active_calories"),
                item.get("steps"),
                item.get("distance"),
                resource_uri,
                _json(item),
            ),
        )
        count += 1
    return count


def upsert_sleep(connection: sqlite3.Connection, polar_user_id: str, items: Iterable[dict[str, Any]]) -> int:
    count = 0
    for item in items:
        connection.execute(
            """
            INSERT INTO sleep (
                polar_user_id, date, duration, continuity, score, raw_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(polar_user_id, date) DO UPDATE SET
                duration = excluded.duration,
                continuity = excluded.continuity,
                score = excluded.score,
                raw_json = excluded.raw_json,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                polar_user_id,
                item.get("date"),
                item.get("sleep") and item["sleep"].get("duration") or item.get("duration"),
                item.get("continuity"),
                item.get("score"),
                _json(item),
            ),
        )
        count += 1
    return count


def upsert_nightly_recharge(
    connection: sqlite3.Connection, polar_user_id: str, items: Iterable[dict[str, Any]]
) -> int:
    count = 0
    for item in items:
        connection.execute(
            """
            INSERT INTO nightly_recharge (
                polar_user_id, date, status, ans_charge, sleep_charge, raw_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(polar_user_id, date) DO UPDATE SET
                status = excluded.status,
                ans_charge = excluded.ans_charge,
                sleep_charge = excluded.sleep_charge,
                raw_json = excluded.raw_json,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                polar_user_id,
                item.get("date"),
                item.get("status"),
                json.dumps(item.get("ans-charge") or item.get("ans_charge")),
                json.dumps(item.get("sleep-charge") or item.get("sleep_charge")),
                _json(item),
            ),
        )
        count += 1
    return count


def insert_sync_run(
    connection: sqlite3.Connection,
    *,
    started_at: datetime,
    finished_at: datetime,
    success: bool,
    resource: str,
    counts: dict[str, Any],
    error_text: str | None,
) -> None:
    connection.execute(
        """
        INSERT INTO sync_runs (started_at, finished_at, success, resource, counts_json, error_text)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            started_at.isoformat(),
            finished_at.isoformat(),
            int(success),
            resource,
            json.dumps(counts, sort_keys=True),
            error_text,
        ),
    )
    connection.commit()


def list_rows(
    connection: sqlite3.Connection, table: str, order_by: str, limit: int, since: str | None = None
) -> list[dict[str, Any]]:
    allowed_tables = {"exercises", "activity_summaries", "sleep"}
    if table not in allowed_tables:
        raise ValueError(f"Unsupported table: {table}")

    query = f"SELECT raw_json FROM {table}"
    params: list[Any] = []
    if since:
        column = "start_time" if table == "exercises" else "date"
        query += f" WHERE {column} >= ?"
        params.append(since)
    query += f" ORDER BY {order_by} DESC LIMIT ?"
    params.append(limit)
    rows = connection.execute(query, params).fetchall()
    return [json.loads(row["raw_json"]) for row in rows]
