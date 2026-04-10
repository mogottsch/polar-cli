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
            detailed_sport_info TEXT,
            distance REAL,
            calories INTEGER,
            avg_hr INTEGER,
            max_hr INTEGER,
            min_hr INTEGER,
            training_load REAL,
            ascent REAL,
            descent REAL,
            average_speed REAL,
            maximum_speed REAL,
            average_pace REAL,
            maximum_pace REAL,
            cadence_avg REAL,
            cadence_max REAL,
            power_avg REAL,
            power_max REAL,
            route_points INTEGER,
            samples_json TEXT,
            zones_json TEXT,
            route_json TEXT,
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
    _ensure_columns(
        connection,
        "exercises",
        {
            "detailed_sport_info": "TEXT",
            "min_hr": "INTEGER",
            "ascent": "REAL",
            "descent": "REAL",
            "average_speed": "REAL",
            "maximum_speed": "REAL",
            "average_pace": "REAL",
            "maximum_pace": "REAL",
            "cadence_avg": "REAL",
            "cadence_max": "REAL",
            "power_avg": "REAL",
            "power_max": "REAL",
            "route_points": "INTEGER",
            "samples_json": "TEXT",
            "zones_json": "TEXT",
            "route_json": "TEXT",
        },
    )
    connection.commit()


def _ensure_columns(connection: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
    existing = {row[1] for row in connection.execute(f"PRAGMA table_info({table})").fetchall()}
    for column, definition in columns.items():
        if column in existing:
            continue
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def archive_json(paths: AppPaths, resource: str, payload: dict[str, Any], stem: str) -> Path:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    resource_dir = paths.raw_dir / resource
    ensure_dir(resource_dir)
    file_path = resource_dir / f"{timestamp}-{stem}.json"
    write_json_file(file_path, payload)
    return file_path


def _json(item: Any) -> str:
    return json.dumps(item, sort_keys=True)


def _series_payload(item: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = item.get(key)
        if value is not None:
            return value
    return None


def _metric_value(metric: Any, *keys: str) -> Any:
    if isinstance(metric, dict):
        for key in keys:
            if key in metric and metric[key] is not None:
                return metric[key]
    return None


def _route_points(route: Any) -> int | None:
    if isinstance(route, list):
        return len(route)
    if isinstance(route, dict):
        for key in ("points", "coordinates", "route"):
            value = route.get(key)
            if isinstance(value, list):
                return len(value)
    return None


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
        heart_rate = item.get("heart-rate") or item.get("heart_rate") or {}
        speed = item.get("speed") or {}
        pace = item.get("pace") or {}
        cadence = item.get("cadence") or {}
        power = item.get("power") or {}
        samples = _series_payload(item, "samples", "sample-data", "sample_data")
        zones = _series_payload(item, "zones", "heart-rate-zones", "heart_rate_zones")
        route = _series_payload(item, "route")

        connection.execute(
            """
            INSERT INTO exercises (
                id, polar_user_id, start_time, duration, sport, detailed_sport_info, distance, calories,
                avg_hr, max_hr, min_hr, training_load, ascent, descent, average_speed, maximum_speed,
                average_pace, maximum_pace, cadence_avg, cadence_max, power_avg, power_max, route_points,
                samples_json, zones_json, route_json, resource_uri, raw_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(id) DO UPDATE SET
                polar_user_id = excluded.polar_user_id,
                start_time = excluded.start_time,
                duration = excluded.duration,
                sport = excluded.sport,
                detailed_sport_info = excluded.detailed_sport_info,
                distance = excluded.distance,
                calories = excluded.calories,
                avg_hr = excluded.avg_hr,
                max_hr = excluded.max_hr,
                min_hr = excluded.min_hr,
                training_load = excluded.training_load,
                ascent = excluded.ascent,
                descent = excluded.descent,
                average_speed = excluded.average_speed,
                maximum_speed = excluded.maximum_speed,
                average_pace = excluded.average_pace,
                maximum_pace = excluded.maximum_pace,
                cadence_avg = excluded.cadence_avg,
                cadence_max = excluded.cadence_max,
                power_avg = excluded.power_avg,
                power_max = excluded.power_max,
                route_points = excluded.route_points,
                samples_json = excluded.samples_json,
                zones_json = excluded.zones_json,
                route_json = excluded.route_json,
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
                item.get("detailed-sport-info") or item.get("detailed_sport_info"),
                item.get("distance"),
                item.get("calories"),
                _metric_value(heart_rate, "average", "avg") or item.get("avg_hr"),
                _metric_value(heart_rate, "maximum", "max") or item.get("max_hr"),
                _metric_value(heart_rate, "minimum", "min") or item.get("min_hr"),
                item.get("training-load") or item.get("training_load"),
                item.get("ascent"),
                item.get("descent"),
                _metric_value(speed, "average") or item.get("average-speed") or item.get("average_speed"),
                _metric_value(speed, "maximum") or item.get("maximum-speed") or item.get("maximum_speed"),
                _metric_value(pace, "average") or item.get("average-pace") or item.get("average_pace"),
                _metric_value(pace, "maximum") or item.get("maximum-pace") or item.get("maximum_pace"),
                _metric_value(cadence, "average") or item.get("cadence_avg"),
                _metric_value(cadence, "maximum") or item.get("cadence_max"),
                _metric_value(power, "average") or item.get("power_avg"),
                _metric_value(power, "maximum") or item.get("power_max"),
                _route_points(route),
                _json(samples) if samples is not None else None,
                _json(zones) if zones is not None else None,
                _json(route) if route is not None else None,
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
