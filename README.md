# polar-cli

Small `uv`-installable CLI for syncing Polar AccessLink data into a local SQLite cache.

## Features

- `polarctl` console command
- OAuth auth with both localhost callback login and manual `auth init` / `auth complete`
- OAuth `state` generation and validation for manual and localhost callback flows
- persistent config and auth state using XDG-style paths
- SQLite cache for workouts, activity summaries, sleep, and nightly recharge
- raw JSON payload archive for debugging
- safe transactional sync: local persistence happens before Polar commit
- JSON output for automation-friendly commands

## Install

```bash
uv tool install .
```

From GitHub:

```bash
uv tool install git+https://github.com/mogottsch/polar-cli.git
```

## Configuration

Credentials can come from environment variables or `~/.config/polar-cli/config.toml`.

Supported environment variables:

- `POLAR_CLIENT_ID`
- `POLAR_CLIENT_SECRET`
- `POLAR_REDIRECT_URI`
- `POLAR_MEMBER_ID`

Example config file:

```toml
client_id = "your-client-id"
client_secret = "your-client-secret"
redirect_uri = "http://127.0.0.1:8765/callback"
member_id = "moritz"
base_url = "https://www.polaraccesslink.com"
auth_base_url = "https://flow.polar.com"
```

## Auth flows

Manual flow for Hermes:

```bash
polarctl auth init --json
polarctl auth complete --callback-url "http://127.0.0.1:8765/callback?code=...&state=..." --json
```

Optional localhost callback flow:

```bash
polarctl auth login --open --listen --json
```

If you do not want a local listener:

```bash
polarctl auth login --no-listen --no-open --json
```

If you keep the local listener but disable browser opening, `polarctl auth login --no-open` prints the authorization URL before it starts waiting for the callback.

## First run

```bash
polarctl auth status
polarctl user register --json
polarctl sync --json
```

## Useful commands

```bash
polarctl auth status
polarctl user info --json
polarctl workouts list --limit 20 --json
polarctl sleep list --json
polarctl activity list --json
polarctl doctor --json
```

## Paths

- Config: `~/.config/polar-cli/config.toml`
- State: `~/.local/share/polar-cli/state.json`
- Cache DB: `~/.local/share/polar-cli/cache.db`
- Raw archive: `~/.local/share/polar-cli/raw/`

## Development

```bash
uv sync --extra dev
uv run python -m pytest
```
