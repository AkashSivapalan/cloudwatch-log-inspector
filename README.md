# CloudWatch Log Inspector

Backend service that queries AWS CloudWatch logs via a user-provided IAM role and diagnoses likely
root causes of issues. See [PLAN.md](PLAN.md) for the roadmap.

## Setup

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```sh
uv sync
cp .env.example .env
```

## Run

```sh
uv run uvicorn log_inspector.main:app --reload
```

- Health check: http://127.0.0.1:8000/health
- API docs: http://127.0.0.1:8000/docs

## Authentication (M6)

Every endpoint except `/health` needs an API key, sent as `Authorization: Bearer <key>`.
Users only see their own connections and investigations. Create users from the command line
(there is no sign-up endpoint):

```sh
uv run log-inspector create-user <name> [--claim-unowned]   # prints the user's first API key
uv run log-inspector create-key <user-id> --name postman
uv run log-inspector list-users
uv run log-inspector revoke-key <key-id>
```

`--claim-unowned` gives the new user any connections made before users existed. Keys are
shown once; only a hash is stored. Users can manage their own keys:

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/me` | The user the key belongs to |
| `GET` | `/me/api-keys` | Their keys (prefix and dates, never the key itself) |
| `POST` | `/me/api-keys` | Create another key (returned once) |
| `DELETE` | `/me/api-keys/{id}` | Revoke a key |

Limits, per user: 120 requests a minute, 30 log queries/analyses/diagnoses an hour, and 3
investigations running at once (`429` with `Retry-After` when exceeded). Every response has
an `X-Request-ID` header that matches the server's log line for that request.

The database schema is managed with Alembic and upgraded automatically at startup.

## AWS connections (M1)

The service reads logs by assuming an IAM role in the user's account. It never stores
long-lived keys. The service's own AWS identity comes from the standard AWS credential chain
(`AWS_PROFILE`, environment variables, or an instance/task role).

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/connections` | Create a connection. Returns the External ID and the trust and permissions policies to create the role with |
| `GET` | `/connections` | List connections |
| `GET` | `/connections/{id}` | Get one connection |
| `GET` | `/connections/{id}/setup` | Show the setup instructions again |
| `PUT` | `/connections/{id}/role` | Submit the role ARN |
| `POST` | `/connections/{id}/verify` | Assume the role and call `logs:DescribeLogGroups` |
| `DELETE` | `/connections/{id}` | Remove a connection |

## Logs (M2)

These need a verified connection.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/connections/{id}/log-groups` | List log groups (`prefix`, `limit`, `next_token`) |
| `GET` | `/canned-queries` | Ready-made Logs Insights queries for common failures |
| `POST` | `/connections/{id}/query` | Run a Logs Insights query (custom or canned) and wait for the results |

## Analysis (M3)

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/connections/{id}/analyze` | Group error and warning lines into patterns, extract stack traces, detect error spikes, and build a timeline. Secrets and personal data are redacted first |

## Diagnosis (M4)

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/connections/{id}/diagnose` | Run the M3 analysis, then ask Claude for likely root causes. Each comes with a confidence, evidence quoted from the logs (checked against the real log lines), remediation steps, and follow-up queries |

Needs `ANTHROPIC_API_KEY` in `.env`. Uses `claude-opus-5` with adaptive thinking by default (`LOG_INSPECTOR_DIAGNOSIS_MODEL`, `LOG_INSPECTOR_DIAGNOSIS_EFFORT`). Secrets and personal data are redacted before anything is sent. If no errors or warnings are found, Claude isn't called.

## Investigations (M5)

Diagnoses that run in the background and are kept as history. Prefer these over `/diagnose`,
which keeps the request open until Claude finishes.

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/investigations` | Start a diagnosis. Returns `202` at once with an `id` |
| `GET` | `/investigations/{id}` | Status (`queued` → `querying` → `analyzing` → `diagnosing` → `done`/`failed`) and, once done, the diagnosis. `?include_analysis=true` adds the M3 analysis |
| `GET` | `/investigations` | History, newest first (`connection_id`, `status`, `limit`) |

Investigations run on a small thread pool (`LOG_INSPECTOR_INVESTIGATION_WORKERS`, default 2).
Ones still running when the server stops are marked `failed` on the next start.

To try it against a real AWS account, see [docs/real-aws-testing.md](docs/real-aws-testing.md).

## Docker and deployment (M7)

```sh
docker compose up --build -d     # API + Postgres on http://127.0.0.1:8000
```

See [docs/deployment.md](docs/deployment.md) for Docker, CI, and deploying to AWS Lambda with
`scripts/deploy.ps1` (Terraform in `infra/terraform`).

## Test and lint

```sh
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

The tests mock AWS with [moto](https://docs.getmoto.org/), and never touch a real AWS account.
