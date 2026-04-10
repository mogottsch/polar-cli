# polar-cli

Small `uv`-installable CLI for syncing Polar AccessLink data into a local SQLite cache.

## Features

- `polarctl` console command
- OAuth auth with both localhost callback login and manual `auth init` / `auth complete`
- OAuth `state` generation and validation for manual and localhost callback flows
- persistent config and auth state using XDG-style paths
- SQLite cache for workouts, activity summaries, sleep, and nightly recharge
- raw JSON payload archive for debugging
- training sync now prioritizes the working non-transactional `/v3/exercises` endpoint
- cached workout rows keep heart-rate summaries plus raw samples, zones, and route payloads when Polar includes them
- sync tolerates empty sleep / nightly recharge responses and downgrades broken activity 404s to warnings
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
polarctl sync --resource exercises --since 30 --json
polarctl workouts list --limit 20 --json
polarctl sleep list --json
polarctl activity list --json
polarctl doctor --json
```

## Training data now retrievable

`polarctl sync --resource exercises` now reads the non-transactional `GET /v3/exercises` collection first, requesting `samples=true`, `zones=true`, and `route=true` when supported.

For each stored workout, the CLI keeps:

- start time
- duration
- sport and detailed sport info when provided
- distance
- calories
- heart-rate average, maximum, and minimum when provided
- training load
- ascent and descent when provided
- average / maximum speed when provided
- average / maximum pace when provided
- average / maximum cadence when provided
- average / maximum power when provided
- heart-rate and other sample series exactly as returned by Polar, stored in the raw workout JSON and in a dedicated `samples_json` cache column
- zone payloads exactly as returned by Polar, stored in raw JSON and `zones_json`
- route payloads exactly as returned by Polar, stored in raw JSON and `route_json`

The `workouts list` command returns the stored raw Polar workout payloads, so downstream tooling can inspect heart-rate-over-time, pace, speed, and route details directly when Polar includes them.

## Endpoint behavior notes

- Exercises: uses `/v3/exercises` first instead of the older user transaction flow.
- Activity: uses `/v3/users/activity`, but if Polar returns 404 for the account the sync continues with a warning instead of failing the whole run.
- Sleep: uses `/v3/users/sleep` and accepts empty `{"nights": []}` responses.
- Nightly recharge: uses `/v3/users/nightly-recharge` and accepts empty `{"recharges": []}` responses.
- Deprecated exercise transaction flows are no longer required for training sync.

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
