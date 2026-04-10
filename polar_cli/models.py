from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


class Config(BaseModel):
    client_id: str | None = None
    client_secret: str | None = None
    redirect_uri: str = "http://127.0.0.1:8765/callback"
    member_id: str = "moritz"
    base_url: str = "https://www.polaraccesslink.com"
    auth_base_url: str = "https://flow.polar.com"

    @property
    def has_credentials(self) -> bool:
        return bool(self.client_id and self.client_secret)


class State(BaseModel):
    access_token: str | None = None
    polar_user_id: str | None = None
    member_id: str | None = None
    registered_at: datetime | None = None
    last_sync_at: datetime | None = None
    token_acquired_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


@dataclass(slots=True)
class AppPaths:
    config_file: Path
    state_file: Path
    db_file: Path
    raw_dir: Path

    @property
    def config_dir(self) -> Path:
        return self.config_file.parent

    @property
    def data_dir(self) -> Path:
        return self.state_file.parent

    @classmethod
    def discover(cls) -> "AppPaths":
        config_home = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
        data_home = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share")))

        return cls(
            config_file=config_home / "polar-cli" / "config.toml",
            state_file=data_home / "polar-cli" / "state.json",
            db_file=data_home / "polar-cli" / "cache.db",
            raw_dir=data_home / "polar-cli" / "raw",
        )

    def as_dict(self) -> dict[str, str]:
        return {
            "config_file": str(self.config_file),
            "state_file": str(self.state_file),
            "db_file": str(self.db_file),
            "raw_dir": str(self.raw_dir),
        }


@dataclass(slots=True)
class TransactionBundle:
    resource: str
    transaction_url: str
    commit_url: str
    item_urls: list[str]
    raw_open: dict[str, Any]
    raw_listing: dict[str, Any]
