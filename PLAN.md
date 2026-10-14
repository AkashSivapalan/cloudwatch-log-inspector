# CloudWatch Log Inspector — Backend Plan

## Goal
A backend service where a user registers an IAM role, then asks questions of their CloudWatch
logs ("why did checkout fail between 2 and 3pm?") and gets back a diagnosis: what went wrong,
likely root causes, the log evidence behind each, and suggested fixes.

## Proposed stack
| Concern | Choice | Why |
|---|---|---|
| Language / API | Python 3.12 + FastAPI | boto3 is the most complete AWS SDK; free OpenAPI docs for testing each milestone |
| AWS access | boto3 + STS `AssumeRole` with an External ID | Standard cross-account pattern; the External ID blocks the "confused deputy" problem |
| Log querying | CloudWatch Logs Insights (`StartQuery` / `GetQueryResults`) | Server-side filtering and aggregation, far cheaper than pulling raw events |
| Diagnosis | Claude (Anthropic API) with structured JSON output | Reasoning over clustered errors + timelines |
| Storage | SQLite via SQLAlchemy (swap to Postgres later) | Zero setup for local development |
| Background jobs | FastAPI background tasks first, then a real queue if needed | Insights queries and LLM calls take seconds to minutes |
| Tests | pytest + `moto` (AWS mocks) + recorded LLM fixtures | Every milestone is testable without a real AWS account |

## How users grant access
1. The service has its own AWS identity (the "inspector principal").
2. When a user registers, the service generates a unique **External ID** and shows the user a
   ready-made trust policy and permissions policy.
3. The user creates a role in their account with those policies and submits the Role ARN.
4. The service calls `sts:AssumeRole(RoleArn, ExternalId)` for short-lived credentials. It never
   stores long-lived keys.

Minimum permissions (read-only):
```
logs:DescribeLogGroups, logs:DescribeLogStreams,
logs:StartQuery, logs:GetQueryResults, logs:StopQuery,
logs:FilterLogEvents, logs:GetLogEvents
```

## Diagnosis pipeline
```
question + time range + log groups
   → Logs Insights queries (errors, exceptions, 5xx, timeouts, volume per minute)
   → Preprocess: dedupe, normalize messages into patterns, cluster, find spikes, build timeline
   → Redact secrets/PII
   → LLM: structured diagnosis {summary, root_causes[{hypothesis, confidence, evidence[], remediation}]}
   → Store and return
```
Preprocessing matters: the LLM sees about 50 representative clusters with counts and first/last-seen
times, not 100k raw lines. This keeps cost down and makes the diagnosis better.

---

## Milestones
Each milestone ends with a **Checkpoint**: something you can run or check yourself before we move on.

### M0: Project skeleton ✅
- Repo layout, `pyproject.toml`, FastAPI app, settings via env vars (`.env.example`), lint/format (ruff), pytest, git init.
- `GET /health`.
- **Checkpoint:** `uvicorn` starts, `/health` returns `ok`, `/docs` shows Swagger UI, `pytest` passes.

### M1: AWS connections (IAM role registration) ✅
- `POST /connections` creates a connection, generates an External ID, and returns trust and permissions policy templates.
- `PUT /connections/{id}` submits the Role ARN. `POST /connections/{id}/verify` assumes the role and runs a test `DescribeLogGroups`.
- STS credential caching (refresh before expiry). Clear errors for a bad ARN, a trust policy mismatch, or missing permissions.
- Persist connections in SQLite.
- **Checkpoint:** moto-based tests pass. Optionally, create a real role in your account using the generated policy and see `verify` succeed.

### M2: Log discovery and querying ✅
- `GET /connections/{id}/log-groups` (paginated, prefix filter).
- `POST /connections/{id}/query` runs a Logs Insights query with a time range and polls to completion (timeout, `StopQuery` on cancel).
- A library of canned queries: error/exception search, HTTP 5xx, Lambda timeouts/OOM, error volume over time.
- **Checkpoint:** list your real log groups and run a canned error query against one from `/docs`.

### M3: Log analysis (deterministic, no LLM) ✅
- Normalize messages into templates (strip IDs, numbers, UUIDs, timestamps) and cluster them.
- Stack-trace extraction, error-spike detection (per-minute volume against a baseline), a timeline of first occurrences.
- Secret/PII redaction (AWS keys, tokens, emails, IPs; configurable).
- `POST /connections/{id}/analyze` returns the structured summary.
- **Checkpoint:** unit tests against sample log fixtures. Run it on your real logs and confirm the clusters make sense.

### M4: AI diagnosis ✅
- Prompt design: system prompt + analysis summary + user question, then a structured JSON response
  (summary, ranked root causes with confidence, evidence log lines, remediation steps, follow-up queries).
- Validate the response against a Pydantic schema. Retry on malformed output. Enforce token budgets.
- Optional agentic step: the model asks for follow-up Insights queries (for example, "show logs from service X
  around 14:02") through tool calls, up to a bounded number of rounds.
- **Checkpoint:** run it on a seeded failure scenario (for example, a Lambda timing out on a DB call) and check the diagnosis names the right cause and cites real log lines.

### M5: Investigations as async jobs ✅
- `POST /investigations` → `202` with an id. `GET /investigations/{id}` returns status and progress (`querying`, `analyzing`, `diagnosing`, `done`, `failed`).
- Store the full result: queries run, analysis, and diagnosis, so past investigations can be reviewed.
- `GET /investigations` lists history.
- **Checkpoint:** start an investigation, poll it to completion, restart the server, and confirm the history is still there.

### M6: Security and hardening ✅
- API authentication (API keys or JWT). Connections are scoped per user/tenant.
- Rate limits, and cost guards: max time range, max bytes scanned, max LLM tokens per investigation.
- Structured logging, no secrets in logs, consistent error model.
- **Checkpoint:** `/security-review` pass. Requests without a key are rejected. One user cannot see another's connections.

### M7: Packaging and deployment ✅
- Dockerfile, docker-compose (API + Postgres), CI (lint + tests).
- **Dedicated AWS account** for the service, separate from personal and other projects (for example `resumeAnalyzer`). The inspector principal ARN that users see then reveals only an account that exists to run this app.
- **The inspector principal becomes an IAM role**, `LogInspectorService`, with no path. Its only permission is `sts:AssumeRole` on `arn:aws:iam::*:role/LogInspector*`. AWS supplies its credentials automatically, so there are no access keys. Leave `LOG_INSPECTOR_AWS_PROFILE` unset in production. The local IAM user and its access key are for development only.
  - Using this role to assume a user's role is role chaining, which AWS caps at 1 hour per session. Keep `assume_role_duration_seconds` at 3600 or less.
- **Compute: Lambda or EC2.** Choose after M5. Both attach the role automatically (Lambda execution role or EC2 instance profile), and both need no code changes for identity.

  | | Lambda | EC2 |
  |---|---|---|
  | Running the app | Needs the Mangum adapter around FastAPI | `uvicorn`, as it runs locally |
  | Request time limit | API Gateway stops requests at about 30 seconds. Works once M5 makes long work run as background jobs | No limit |
  | Database | Needs Postgres (RDS) or DynamoDB. SQLite won't work on Lambda's short-lived disk | SQLite works on one instance; Postgres when scaling out |
  | Cost | Pay per request (about $0 at low traffic) plus the database | Always on, about $12/month for a `t4g.small` |
  | Maintenance | AWS manages the servers | You patch the OS and redeploy the app |

  EC2 is the quickest first deployment. Lambda becomes practical after M5.
- **Checkpoint:**
  - `docker compose up` runs the full flow end to end.
  - The deployed service's `inspector_principal_arn` is the role, not an IAM user.
  - A test customer role that trusts that ARN verifies successfully.

### M7b: AWS Lambda deployment ✅
Compute is **Lambda** (decision 6). Everything is Terraform in `infra/terraform`; see `docs/deployment.md`.
- **One container image, three functions** (`Dockerfile.lambda`, handlers in `lambda_handlers.py`): `api` (FastAPI behind API Gateway, via Mangum), `worker` (runs investigations from SQS) and `admin` (runs `log-inspector` commands, because the database is private).
- **Investigations run through SQS** with a dead-letter queue; the worker handles one at a time, at most 5 at once.
- **Postgres on RDS** in private subnets, with outbound traffic through a NAT instance. The database password lives in Secrets Manager and the Anthropic key in SSM, fetched at cold start.
- **Production identity:** the service role `LogInspectorService` (decision 4).
- `scripts/deploy.ps1` builds the image (tagged with the git commit), applies the Terraform and runs migrations; `scripts/admin.ps1` runs admin commands.

---

## Decisions
1. **Language:** Python/FastAPI. *Decided.*
2. **LLM provider:** Claude through the Anthropic API. *Decided.*
3. **Deployment model:** a multi-tenant SaaS (you host it, customers bring roles), or self-hosted per user? This mainly affects M6. *Open.*
4. **Production AWS account and service role name.** Every user's trust policy names the service's ARN, so changing it later breaks every connection until each user updates their role. *Decided (2026-10-13): the current AWS account and the role `LogInspectorService` are production.* Keep them fixed: IAM ties a trust policy to the role's internal ID, so deleting and recreating the role, even under the same name, also breaks every connection. The role has `prevent_destroy` in `infra/terraform/iam.tf` to guard against that. Moving to a separate production account later means every user redoes their trust policy.
5. **User sign-up and login:** for now, users are created by an admin with the `log-inspector` CLI and use API keys. Once the frontend exists, add an identity provider (for example Amazon Cognito): the backend checks its login tokens and creates a user record on each new user's first request. API keys stay for programmatic access. *Decided.*
6. **Compute:** Lambda or EC2 (see M7). *Decided: Lambda (M7b).*

---

## Frontend

### Decisions
- **Stack:** Angular (current version), TypeScript, Angular Material. Chosen partly as a learning project.
- **Repository:** separate from this one: `AkashSivapalan/aws-cloudwatch-log-inspector-frontend` (private). It owns its hosting (S3 + CloudFront, via its own Terraform). Live at https://dgc5idmxco766.cloudfront.net.
- **Hosting:** S3 + CloudFront with HTTPS, deployed by a script like `scripts/deploy.ps1`.
- **Login:** Amazon Cognito (see decision 5 above). The user pool lives in **this** repository's Terraform, because the API must check its tokens. The frontend reads the pool's IDs from this repo's outputs.
- **Role setup:** users copy-paste the trust policy from the app. No CloudFormation link for now.

### Backend work the frontend needs
| # | Change | Needed by | Status |
|---|---|---|---|
| B1 | **CORS**: a `cors_origins` setting (localhost for development, the CloudFront URL in production) | F1 | ✅ |
| B2 | **Cognito**: a user pool and app client in Terraform. The API accepts Cognito access tokens alongside API keys, checking them against the pool's published keys. A user record is created on first login (new `cognito_sub` column, migration 0004) | F1 | ✅ |

### Milestones (in the frontend repository)
| # | Milestone | Checkpoint | Status |
|---|---|---|---|
| F0 | Angular skeleton, Material, lint, tests, CI, hosting on S3 + CloudFront | A placeholder page loads from the CloudFront URL | ✅ |
| F1 | Sign-up and login with Cognito; the API is called with the user's token | Sign up, log in, see your name from `/me`; logged-out users are sent to login | ✅ |
| F2 | Connections: list, add, copy the trust policy (with a guide), set the role, verify | Connect an account entirely from the UI; failures explain what to fix | ✅ |
| F3 | New investigation: pick connection, log groups, time range, question | Start an investigation from the form | ✅ |
| F4 | Investigation page: live progress, then the diagnosis | Watch it go from querying to done, then read the causes, evidence and fixes | ✅ |
| F5 | Analysis visuals: error volume chart with spikes, timeline, patterns table | The demo's error spike is visible | ✅ |
| F6 | History, empty states, mobile layout | Past investigations listed | ✅ |

**Left out of F6 on purpose:** the API keys page. The web app does not need it, since browser users sign in with Cognito. Keys are still created by an admin with `scripts/admin.ps1 create-key`. Build the page if users ever need API access of their own.

---

## After the milestones
Changes made once everything above was running, from using it:
- **Claude time limit.** The 300 s timeout only caught a silent stream, so a steady one plus the SDK's default retries could outlast the worker Lambda and leave an investigation stuck. The Claude step now has a wall-clock limit (`diagnosis_timeout_seconds`, 480 s), an idle timeout (90 s) and one retry, and the worker passes in the time Lambda has left, minus a 30 s margin, so the run ends as a recorded failure.
- **Stale investigations settle on read.** A worker killed from outside records nothing. The API now fails runs that have outlived `investigation_timeout_seconds` (900 s, measured from when they started, else when they were requested) when they are fetched, listed or counted, so they stop showing as in progress and stop counting against the limit of 3 active investigations.
- **Production identity decided** (decision 4), with `prevent_destroy` on the service role.

## Still open
- **Decision 3:** multi-tenant SaaS or self-hosted per user.
- **M7's "dedicated AWS account"** goal versus decision 4, which names the current account as production. Check that the account really is dedicated to this service.
- **The dead-letter queue has no alarm or consumer**, so a message that lands there goes unnoticed.
- **Dark mode** is not supported in the web app.
- **API keys page** (see F6).
