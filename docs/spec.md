# Polar CLI

## Goal
Build a tiny Python CLI that lets Hermes fetch and cache Moritz's Polar Flow data with as little friction as possible.

Primary UX:
- one-time OAuth linking in browser
- local persistent token/state on the homeserver
- Hermes can call a stable CLI later without manual login each time
- machine-friendly JSON output
- safe local cache so transactional Polar data is never lost

## Context
- Repo: `https://github.com/mogottsch/polar-cli`
- Target install path for Hermes container: install as a uv tool directly from the public GitHub repo
- Secrets will live in Vaultwarden; Hermes can fetch them and export env vars before invoking the CLI
- Polar watch model is not important for the integration as long as data lands in Polar Flow

## Product decision
Keep the CLI focused on Polar AccessLink only.
Do not build Vaultwarden integration into the CLI itself in v1.
Instead, the CLI should read credentials from env/config, and Hermes will inject them from Vaultwarden when needed.

This keeps the package portable and avoids coupling it to one secret manager.

## Important auth constraint
Hermes cannot rely on receiving a live localhost callback from Moritz's browser during normal use.
So auth must support both:
1. clean localhost callback flow for regular local users
2. manual copy/paste completion flow for Hermes usage

Practical Hermes flow:
- CLI prints the Polar authorization URL
- Moritz opens it in his own browser
- Polar redirects to the configured localhost callback URL with `code=...`
- Moritz can either paste the full redirected URL into chat or store it in Vaultwarden
- CLI provides a command to finalize auth from the pasted callback URL or raw authorization code

The localhost callback server can still exist for completeness, but manual completion is mandatory for v1.

## Official Polar API facts to design around
- Polar AccessLink is the official API
- Auth flow is OAuth2 authorization code flow
- User must be registered with `POST /v3/users` after authorization before data access works
- AccessLink v3 docs say access tokens do not expire unless revoked
- Some endpoints are transactional and must be committed only after successful local persistence
- Transactional endpoints can return up to 50 entities per transaction, but some documented transaction flows are currently unreliable in real use
- Uncommitted transactions are disbanded after ~10 minutes and then reappear later
- Non-transactional endpoints exist for some resources like exercises, sleep, and nightly recharge and should be preferred when they work reliably

## Scope for v1
Implement the smallest useful CLI that Hermes can rely on.

### Must-have
1. OAuth login flow with local callback server
2. Persistent local config/state
3. User registration
4. User info / auth status command
5. Sync command for:
   - exercises / workouts
   - daily activity summaries
   - sleep
   - nightly recharge
6. Local cache in SQLite
7. Raw JSON archival of fetched payloads for debugging
8. JSON output mode for all user-facing commands
9. Safe transaction handling: commit only after local write succeeds
10. Tests for auth/state/transaction logic

### Nice-to-have but not required in first pass
- webhook support
- GPX/FIT/TCX export download
- continuous heart rate or extra biometrics
- dashboard/report generation
- direct secret-manager plugins

## Tech stack
- Python 3.12+
- `uv` project
- `typer` for CLI
- `httpx` for HTTP
- `pydantic` for config/models
- `sqlite3` from stdlib for cache
- `pytest` for tests

Keep deps small. No heavy ORM.

## Packaging and install
Repository should be a normal uv Python package with a console entrypoint.

Preferred package/install shape:
- package name: `polar-cli`
- console command: `polarctl`

Expected install inside Hermes container:
- `uv tool install git+https://github.com/mogottsch/polar-cli.git`

Expected upgrade path:
- reinstall/upgrade from the same GitHub URL

## Config and state layout
Use XDG-ish defaults.

### Config file
Path:
- `~/.config/polar-cli/config.toml`

Config values:
- `client_id`
- `client_secret`
- `redirect_uri` (default `http://127.0.0.1:8765/callback`)
- `member_id` (stable local identifier, default `moritz` or explicit user-provided value)
- `base_url` default `https://www.polaraccesslink.com`
- `auth_base_url` default `https://flow.polar.com`

### State file
Path:
- `~/.local/share/polar-cli/state.json`

State values:
- `access_token`
- `polar_user_id`
- `member_id`
- `registered_at`
- `last_sync_at`
- optional metadata from registration/user info

### Cache DB
Path:
- `~/.local/share/polar-cli/cache.db`

### Raw payload archive
Path:
- `~/.local/share/polar-cli/raw/`

## Credential sources
Priority order:
1. env vars
2. config file

Supported env vars:
- `POLAR_CLIENT_ID`
- `POLAR_CLIENT_SECRET`
- `POLAR_REDIRECT_URI`
- `POLAR_MEMBER_ID`

Do not require interactive secret entry in normal Hermes usage.

## CLI commands

### `polarctl auth login`
Behavior:
- load client credentials from env/config
- generate auth URL
- optionally start a tiny localhost callback server
- optionally open browser, otherwise print URL
- either receive authorization code via callback or instruct user to complete auth manually
- exchange code for token when available
- persist token to state file
- print JSON with success and token metadata, but never print raw secret/token unless `--show-token` is explicitly set

Flags:
- `--port` default `8765`
- `--open/--no-open`
- `--listen/--no-listen`
- `--timeout` default 180
- `--json`

### `polarctl auth init`
Behavior:
- print the Polar authorization URL and expected redirect URI
- intended for Hermes/manual flow
- no local server required

### `polarctl auth complete`
Behavior:
- accept either full redirected callback URL or raw authorization code
- parse/extract code
- exchange code for token
- persist token to state

Flags:
- `--callback-url`
- `--code`
- `--json`

### `polarctl auth status`
Behavior:
- show whether client credentials exist
- show whether access token exists
- show whether user is registered
- show configured paths
- JSON output by default or via `--json`

### `polarctl user register`
Behavior:
- call `POST /v3/users` with `member-id`
- persist `polar_user_id` and returned profile info
- if already registered, return current state without breaking

### `polarctl user info`
Behavior:
- fetch current user info if possible
- merge useful metadata into local state

### `polarctl sync`
Behavior:
- run all supported sync steps in a safe order:
  1. exercises via the non-transactional `/v3/exercises` collection, requesting samples / zones / route extras when available
  2. activity summaries via `/v3/users/activity` when available
  3. physical info can be skipped in v1 unless easy
  4. sleep via `/v3/users/sleep`
  5. nightly recharge via `/v3/users/nightly-recharge`
- write normalized rows into sqlite
- archive raw payloads
- keep the full raw exercise payload so heart-rate-over-time and similar series remain available from cache
- if activity is unavailable for an account, continue sync with a warning instead of failing the whole run
- print summary JSON with counts per resource and timestamps

Flags:
- `--since DAYS` optional hint for collection backfill reads
- `--resource exercises|activity|sleep|nightly-recharge|all`
- `--json`

### `polarctl workouts list`
Behavior:
- list cached workouts from sqlite
- defaults to recent first

Useful flags:
- `--limit` default 20
- `--since`
- `--json`

### `polarctl sleep list`
Behavior:
- list cached sleep entries from sqlite

### `polarctl activity list`
Behavior:
- list cached activity summaries from sqlite

### `polarctl doctor`
Behavior:
- validate config, writable paths, auth presence, registration state, sqlite health
- exit nonzero on broken setup

## Data model
Use sqlite with simple tables and unique constraints.

### `users`
- `polar_user_id` primary key
- `member_id`
- `first_name`
- `last_name`
- `birthdate`
- `gender`
- `weight`
- `height`
- `raw_json`
- timestamps

### `exercises`
- `id` primary key
- `polar_user_id`
- `start_time`
- `duration`
- `sport`
- `detailed_sport_info`
- `distance`
- `calories`
- `avg_hr`
- `max_hr`
- `min_hr`
- `training_load`
- `ascent`
- `descent`
- `average_speed`
- `maximum_speed`
- `average_pace`
- `maximum_pace`
- `cadence_avg`
- `cadence_max`
- `power_avg`
- `power_max`
- `route_points`
- `samples_json`
- `zones_json`
- `route_json`
- `resource_uri` unique if available
- `raw_json`
- timestamps

### `activity_summaries`
- `id` primary key
- `polar_user_id`
- `date`
- `active_calories`
- `steps`
- `distance`
- `resource_uri` unique if available
- `raw_json`
- timestamps

### `sleep`
- `date` + `polar_user_id` unique
- selected sleep summary fields
- `raw_json`

### `nightly_recharge`
- `date` + `polar_user_id` unique
- selected recharge fields
- `raw_json`

### `sync_runs`
- local run id
- started_at
- finished_at
- success
- resource
- counts json
- error text nullable

## Transaction safety rules
For transactional endpoints:
1. create transaction
2. list resource URLs/items
3. fetch all details needed
4. persist to sqlite in a local transaction
5. archive raw payloads
6. only then commit Polar transaction

If any local write fails:
- do not commit Polar transaction
- exit nonzero
- keep enough logs to retry safely

## Output conventions
Default to concise human-readable output, with `--json` available everywhere.
For commands Hermes is likely to call, JSON should be stable and easy to parse.

Recommended JSON envelope:
- `ok`
- `command`
- `data`
- `warnings`

## Error handling
Need clear errors for:
- missing client credentials
- callback timeout
- auth denied
- token exchange failure
- user not registered
- 403 due to missing Polar consents
- filesystem/sqlite write failures
- transaction commit failures

## Security rules
- never print client secret by default
- never print access token by default
- redact secrets in logs/errors
- file permissions should be user-only where practical

## Minimal implementation plan
1. Initialize uv package, console script, tests, lint config, gitignore
2. Add config/state/path helpers
3. Add Polar HTTP client and auth exchange
4. Add localhost callback auth flow
5. Add user register/info/status commands
6. Add sqlite cache and raw archive helpers
7. Add transactional sync for exercises and activities
8. Add non-transactional sync for sleep and nightly recharge
9. Add cached list commands
10. Add doctor command and tests
11. Update README with setup/install/examples

## Hermes integration expectation
Hermes should eventually be able to do roughly this:
1. fetch Polar client secrets from Vaultwarden
2. export env vars
3. call `polarctl auth login` once during setup
4. call `polarctl user register`
5. call `polarctl sync --json`
6. read cached data later with list commands or direct sqlite access if ever needed

## Review checklist
Implementation is good enough for v1 if:
- fresh clone installs with uv
- `polarctl --help` works
- auth flow is implemented
- user registration is implemented
- sync persists to sqlite safely
- transaction commit logic is correct
- tests cover the dangerous paths
- README explains install and first-run flow
- package can be installed from public GitHub as a uv tool
