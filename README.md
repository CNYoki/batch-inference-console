# Batch Inference Console

A self-hosted web console for running large-scale batch inference against OpenAI-compatible
LLM endpoints. Upload a JSONL file, pick a model, and the platform queues the job, fans out
concurrent requests, checkpoints progress, and hands back the results — no scripting required.

Built for teams: OIDC single sign-on, per-user quotas, and the option for each user to run
jobs with their **own** gateway token so usage lands on their own account.

```
Browser ──► API (FastAPI) ──► MySQL / PostgreSQL   (metadata, state)
                │
                ├──────────► Redis                 (priority queue, leases, live progress)
                │
                └──────────► Shared volume         (input JSONL, result JSONL)
                                   ▲
              Worker processes ────┘               (claim jobs, call the model, write results)
```

## Features

**Jobs**
- Upload JSONL, validate every line up front with exact error line numbers
- Priority queue with leases — a crashed worker's job is automatically requeued
- Pause / resume / cancel, and **item-level checkpointing**: resuming a job re-sends only the
  requests that never completed
- **Switch models mid-job**: a paused, canceled or failed job can be moved to another model;
  resuming sends only the remaining items to the new one
- Retry just the failed items without re-running the successful ones
- **Dry run on submit**: the first line is sent for real before the job is queued, so a wrong
  model name, an expired key or a reasoning field the gateway rejects surfaces in seconds
  instead of after the whole batch has waited in line
- Live progress, failure details, result preview, and export as raw JSONL, simplified JSONL, or CSV

**Models**
- Admin-configured **shared models**: endpoint, encrypted API key, sampling defaults, forced
  parameters, allow-list of user-overridable keys, concurrency / RPM / TPM limits, retries
- **Per-model job limit**: cap how many jobs may run at once on a given model name (across all
  workers, `*` wildcards allowed); jobs over the limit wait in the queue while other models' jobs
  go ahead
- **Personal gateway**: users supply their own token, the platform fetches the models that token
  is authorised for, and jobs run under their identity and quota
- Reasoning/thinking toggle per model: pick the request-body fragment from presets
  (`chat_template_kwargs.enable_thinking`, `reasoning_effort`, `thinking.budget_tokens`, …)
  or write your own; effort levels (`none` … `max`, or raw token budgets) are configurable
  per model and picked by users on a slider when creating a job
- Personal-gateway models aren't in the database, so their reasoning config is matched by model
  name (`qwen3-*`, first rule wins) with a gateway-wide default behind it

**Operations**
- Storage quotas, global and per user, enforced during upload
- Retention policy that clears files of finished jobs while keeping records and usage stats
- Email notifications on completion and abnormal termination, with editable templates
- Dashboard: queue depth, worker health, per-user token and storage usage, audit log

**Tools**
- Script generator: describe your local CSV / JSONL / Parquet data in the UI and download a
  standalone Python script that converts and splits it into upload-ready JSONL — your data
  never leaves your machine
- Personal API tokens plus a dependency-free CLI and a Claude Code plugin, so the whole
  prepare → upload → dry run → submit → merge results loop can run from a local terminal
  (see [Command line and Claude Code](#command-line-and-claude-code))

## Stack

| Layer | Choice |
|---|---|
| Backend | FastAPI, SQLAlchemy 2 (async), Alembic |
| Worker | Standalone `asyncio` processes, horizontally scalable |
| Queue | Redis sorted set with leases and control signals |
| Database | MySQL 8 / PostgreSQL / SQLite (dev only) |
| Frontend | React 18, TypeScript, Vite, Ant Design |
| Auth | OIDC (authorization code + PKCE) and/or local accounts |

## Quick start

```bash
make install
cp .env.example .env      # then edit — see Configuration below
make migrate
```

Run the three processes (production style, single port, API also serves the built frontend):

```bash
make build                 # build the frontend once
scripts/run.sh start 2     # API + 2 workers on 0.0.0.0:8000
scripts/run.sh status
scripts/run.sh stop        # workers finish in-flight jobs before exiting
```

Or run them separately for frontend hot reload:

```bash
make dev-api      # http://127.0.0.1:8000
make dev-worker   # start as many as you like
make dev-web      # http://127.0.0.1:5180
```

Log in with `BOOTSTRAP_ADMIN_USERNAME` / `BOOTSTRAP_ADMIN_PASSWORD`, then add a model under
**Models → New model**. `base_url` goes up to and including `/v1`.

## Configuration

Everything lives in `.env`. The settings you must review:

| Variable | Notes |
|---|---|
| `SECRET_KEY` | `openssl rand -hex 32`. Also derives the encryption key for stored API keys and user tokens — **changing it invalidates every stored secret**, and API and workers must share the same value |
| `DATABASE_URL` | `mysql+asyncmy://…`, `postgresql+asyncpg://…`, or `sqlite+aiosqlite:///./data/app.db` |
| `REDIS_URL` | `redis://:password@host:6379/0` |
| `QUEUE_KEY_PREFIX` | **Give every deployment its own prefix.** Two deployments sharing one prefix will steal each other's jobs, and since their `DATA_DIR`s differ the jobs will fail |
| `DATA_DIR` | Uploads and results. Relative paths resolve against the repo root, not the working directory, so API and workers agree regardless of where they are started |
| `BOOTSTRAP_ADMIN_PASSWORD` | Creates the first admin on startup. Clear it once the account exists |

`scripts/init_mysql.sh` creates the database and a non-privileged application account, then
writes `DATABASE_URL` back into `.env`.

## Input format

One JSON object per line. All three shapes are accepted and may be mixed:

```jsonl
{"custom_id": "req-1", "method": "POST", "url": "/v1/chat/completions", "body": {"messages": [{"role": "user", "content": "Summarise this."}]}}
{"custom_id": "req-2", "messages": [{"role": "user", "content": "…"}], "temperature": 0.2}
{"custom_id": "req-3", "prompt": "Write a haiku"}
```

The first is byte-compatible with the OpenAI Batch API, so existing datasets work as-is.
`custom_id` is optional but strongly recommended — it is how results map back to your source rows.
Per-line parameters win over job-level ones, unless the model config marks them as forced.

## Output format

Successful and failed items are written to separate files, both carrying `_index`:

```jsonl
{"_index": 0, "custom_id": "req-1", "response": {"status_code": 200, "body": { … full response … }}, "error": null, "latency_ms": 812, "attempts": 1}
```

Downloads offer raw JSONL, raw + failures, simplified JSONL (`custom_id` + output + usage), and
CSV with a BOM so Excel reads UTF-8 correctly. Only the raw file exists on disk; the other formats
are converted on the fly while streaming, so they cost no extra storage.

Two things worth knowing:

- **Results are written in completion order, not source order** (same as the OpenAI Batch API).
  Sort by `_index` or `custom_id` to restore it.
- **Downloads are only available once a job has finished** (succeeded / completed / canceled).
  While a job runs the result file is still being appended to, so an export would be truncated.
  The in-page result preview works at any time.

## Concurrency

Three independent layers:

| Layer | Controlled by | Default |
|---|---|---|
| Worker processes | `scripts/run.sh start N`, or compose replicas | 2 |
| Jobs per worker | `WORKER_MAX_CONCURRENT_JOBS` | 4 |
| Requests per job | job parameter → model's `max_concurrency` → `DEFAULT_ITEM_CONCURRENCY` | 8 |

RPM/TPM token buckets are per job, so several jobs on the same model sum their limits. For a hard
global cap, enforce it at the gateway.

## Docker

```bash
cp .env.example .env
make up                # MySQL + Redis + migrate + API + 2 workers + nginx
make up-external       # same, but against your existing MySQL and Redis
```

nginx on `:8080` is the only entry point — it proxies `/api/` to the API container over the Docker
network, and the API port is deliberately not published. To reach the API directly for debugging,
layer in `docker-compose.expose-api.yml` (binds `127.0.0.1` only).

Notes that cost time if missed:

- A one-shot `migrate` service runs `alembic upgrade head`; API and workers wait for it. Do not rely
  on the app's `create_all` — it creates tables but never alters them, so upgrades would silently
  miss new columns. The compose files set `AUTO_CREATE_TABLES=false`.
- Workers share the API's image but do not listen on a port, so the image healthcheck is disabled
  for them in compose.
- `DATABASE_URL` and `REDIS_URL` must be reachable **from inside a container**: `127.0.0.1` means
  the container itself. Use the host's LAN IP for host-local services; the Docker bridge gateway
  address is often blocked by host firewalls.
- With OIDC, point `FRONTEND_BASE_URL` and `OIDC_REDIRECT_URI` at the nginx port and re-register the
  callback with your IdP.

To keep data on a host path instead of a named volume:

```bash
DATA_HOST_DIR=/srv/batch-inference/data \
  docker compose -f docker-compose.external.yml -f docker-compose.bind-data.yml up -d
```

The directory is created automatically. Under **rootless** Docker the override's `user: "0:0"` is
what makes the files belong to you on the host — without it the container's `appuser` maps to a
high subordinate UID and you cannot read your own data. Under rootful Docker, drop those `user`
lines and `chown -R 1000:1000` the directory instead.

## OIDC

Standard authorization code + PKCE, endpoints discovered automatically. Works with Keycloak,
Authentik, Logto, Pocket ID, Azure AD, and anything else that publishes a discovery document.

```bash
OIDC_ENABLED=true
OIDC_ISSUER=https://idp.example.com     # copy the `issuer` from the discovery document verbatim
OIDC_CLIENT_ID=…
OIDC_CLIENT_SECRET=…
OIDC_REDIRECT_URI=https://console.example.com/api/auth/oidc/callback
OIDC_SCOPES=openid profile email groups
OIDC_ADMIN_GROUP=platform-admins        # optional; empty disables role sync
```

Run `backend/.venv/bin/python scripts/check_oidc.py` before trying it in a browser. It verifies
discovery reachability, issuer match, PKCE and signing algorithm support, scope/claim consistency,
and sends a preflight request to confirm your redirect URI is actually registered.

Behaviour worth noting: the first user to sign in via OIDC becomes an admin, so a fresh deployment
is never locked out. An existing local account with the same username is linked rather than
duplicated. When `OIDC_ADMIN_GROUP` is set, role membership syncs **both ways** — except that the
last remaining active admin is never demoted. `groups` must be in `OIDC_SCOPES` or the claim never
arrives and admin sync silently does nothing.

## Command line and Claude Code

OIDC users have no password to script with, so the console issues **personal API tokens**.
**Profile → API Token** creates one: the plaintext is shown once, only a SHA-256 hash is stored,
and expiry ranges from 30 days to never. Every endpoint accepts it as
`Authorization: Bearer bic_…`, with the user's normal permissions. Two deliberate limits: a token
cannot create or revoke tokens (that needs a browser session, so a leaked token can't renew
itself), and disabling a user cuts off their tokens immediately. Run `make migrate` after
upgrading — it adds the `api_tokens` table.

The token page shows a one-line command that writes `~/.config/bic/config.json`; the
`BIC_URL` / `BIC_TOKEN` environment variables override it.

**CLI** — `plugins/batch-inference/scripts/bic.py` is a single file with no dependencies
(Python 3.9+; `pyarrow` only for Parquet). Every command prints JSON on stdout:

```bash
bic.py inspect data.csv --schema-only        # columns, types, encoding — offline
bic.py prep --config prep.json --limit 20    # the platform generates the split script; it runs locally
bic.py upload batch_input/                   # validates every line, writes bic_manifest.json
bic.py dry-run --manifest batch_input/bic_manifest.json --model qwen3
bic.py submit --manifest batch_input/bic_manifest.json --model qwen3 --name reviews        # prints the plan only
bic.py submit --manifest batch_input/bic_manifest.json --model qwen3 --name reviews --yes  # one job per shard
bic.py wait --manifest batch_input/bic_manifest.json
bic.py download --manifest batch_input/bic_manifest.json -o results
bic.py join --config batch_input/bic_prep_config.json --results results/*.jsonl -o merged.csv
```

`join` rebuilds each `custom_id` with the same rules as the generated script, so results line up
with source rows even though they arrive in completion order. Rows skipped during preparation
show up as `missing`.

**Claude Code plugin** — this repository is also a plugin marketplace:

```text
/plugin marketplace add CNYoki/batch-inference-console
/plugin install batch-inference@batch-inference-console
```

It ships three skills — `batch-prep`, `batch-submit` and `batch-results` — that drive the CLI.
They read column schemas before any data values and ask before reading sample rows, never pass
`--yes` to `submit` without explicit confirmation, and tell users to paste tokens into their own
terminal rather than the chat.

## Development

```bash
make test    # 164 backend tests
make lint    # ruff
make build   # frontend; the API serves ./frontend/dist when present
```

The test suite runs generated preprocessing scripts as subprocesses and validates their output with
the platform's own parser, so the script generator is verified end to end rather than by string
comparison. Email delivery is tested against a real in-process SMTP server.

## Known limitations

**Workers cannot run on a different machine from the API.** Input and result files are passed
through the filesystem, so every process needs the same `DATA_DIR` on shared storage. Worse, the
failure is asymmetric: a missing input file fails loudly, but results would be written silently to
the wrong host. If you do use shared storage, `DATA_DIR` must be the *identical path* everywhere.
Supporting true multi-machine deployment means making the storage layer pluggable (S3/MinIO) and
reworking incremental result writes and resume.

**One queue per deployment.** Two deployments sharing a Redis prefix will claim each other's jobs.
The dashboard warns when online workers report different data directories, and the resulting job
failure names the cause explicitly, but the fix is to set distinct `QUEUE_KEY_PREFIX` values.

## Security notes

- Model API keys and personal gateway tokens are encrypted with Fernet; APIs return masked values
  only and plaintext never leaves the backend
- Sessions are httpOnly cookies — set `SESSION_COOKIE_SECURE=true` behind HTTPS
- Upload filenames embed the user ID, so pending uploads are isolated per user; accessing another
  user's job returns 404 rather than 403, which avoids confirming that it exists
- Users cannot override `model` or `stream`, nor bypass a model's forced parameters or `max_tokens`
  cap, and cannot submit a model their own token has no access to
- `DATA_DIR` holds real payloads — the database stores only metadata. Back it up, and delete jobs
  through the UI or the retention policy rather than removing files by hand

## Licence

MIT
