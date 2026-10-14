# Running and deploying

## Locally with Docker (API + Postgres)

Needs Docker Desktop running, and `.env` set up as in `docs/real-aws-testing.md`. The
container uses your `~/.aws` (read-only) for the `log-inspector` profile.

```powershell
docker compose up --build -d                        # http://127.0.0.1:8000
docker compose exec api log-inspector create-user <name>
docker compose logs -f api                          # JSON logs
docker compose down                                 # stop (data kept)
docker compose down -v                              # stop and delete the database
```

If a local `uvicorn` is already using port 8000, run with another port: set
`$env:API_PORT = "8080"` before `docker compose up`. On Windows, a process bound to
`127.0.0.1:8000` answers before Docker does, even when both start successfully.

The compose database is separate from the SQLite file `uv run uvicorn` uses, so users and
connections must be created again there.

## CI

`.github/workflows/ci.yml` runs on every push and pull request once the repository is on
GitHub:

- **Lint:** `ruff check` and `ruff format --check`.
- **Tests:** the full suite, with the Postgres tests run against a real Postgres.
- **Image:** builds the Docker image and checks that `/health` responds.

To run the Postgres tests locally:

```powershell
docker run -d --rm --name li-test-pg -e POSTGRES_USER=log_inspector -e POSTGRES_PASSWORD=log_inspector -e POSTGRES_DB=log_inspector -p 5433:5432 postgres:16-alpine
$env:TEST_POSTGRES_URL = "postgresql+psycopg://log_inspector:log_inspector@localhost:5433/log_inspector"
uv run pytest -q
docker stop li-test-pg
```

## Production: what the service assumes

These hold for **one server process**, which is how the image runs:

| Concern | Today | With several instances |
|---|---|---|
| Rate limits | In memory, per process | Each instance enforces its own limits; use Redis for shared limits |
| Investigation queue | A thread pool in the process | A stopped instance's queued work is lost; use a queue (e.g. SQS) |
| Startup recovery | Marks *all* unfinished investigations failed | Would fail jobs another instance is running |
| Migrations | Run at startup | Run `log-inspector migrate` as a separate deploy step instead |

So, until those change, run a single instance (one EC2 instance, or one ECS task).

### Configuration in production

| Setting | Value |
|---|---|
| `LOG_INSPECTOR_DATABASE_URL` | `postgresql+psycopg://...` (e.g. Amazon RDS), from a secret store, not the image |
| `ANTHROPIC_API_KEY` | From a secret store (e.g. SSM Parameter Store SecureString or Secrets Manager) |
| `LOG_INSPECTOR_AWS_PROFILE` | **Unset**: the service uses the role attached to its compute |
| `LOG_INSPECTOR_LOG_FORMAT` | `json` |
| `LOG_INSPECTOR_ENVIRONMENT` | `production` |

### The service's AWS identity

The compute's role (e.g. EC2 instance profile or ECS task role) is the inspector principal.
It needs only:

```json
{"Effect": "Allow", "Action": "sts:AssumeRole", "Resource": "arn:aws:iam::*:role/LogInspector*"}
```

Give the role no path, so the ARN users trust can be derived from its credentials.

On EC2, set the instance metadata **hop limit to 2**. Containers are one network hop
further away than the host; with the default of 1 they can't reach the role's credentials.

## Deploying to AWS (Lambda)

```
Internet ─► API Gateway (HTTP API, throttled) ─► Lambda "api" ─┬─► RDS Postgres (private)
                                                               └─► SQS ─► Lambda "worker" ─┐
           Lambda "admin" (migrations, users) ─► RDS            outbound via NAT instance ◄┘
                                                                 (Anthropic, STS, CloudWatch)
```

Everything lives in `infra/terraform`. All three functions run the same image
(`Dockerfile.lambda`) with a different handler, and share the IAM role
`LogInspectorService`: that role's ARN is what users' trust policies name.

### Rough monthly cost (us-east-1, low traffic)

| Resource | Cost |
|---|---|
| RDS `db.t4g.micro` + 20 GB | ~$15 (free for 12 months on new accounts) |
| NAT instance `t4g.nano` (fck-nat) | ~$3 |
| Secrets Manager secret (RDS password) | $0.40 |
| Lambda, API Gateway, SQS, ECR, logs | ~$0–2 at low traffic |
| Claude | Per diagnosis, a few cents each |

### One-time setup

1. **An admin AWS profile for Terraform.** Your `log-inspector` profile can only assume roles.
   In IAM Identity Center (recommended), or as an IAM user with `AdministratorAccess`,
   create credentials and run `aws configure sso --profile admin` (or
   `aws configure --profile admin`). Check: `aws sts get-caller-identity --profile admin`.
2. **Docker Desktop running**, and Terraform 1.9+.

### Deploy (and redeploy)

```powershell
git commit ...                 # the image is tagged with the commit
.\scripts\deploy.ps1           # builds, pushes, shows the Terraform plan, asks for "yes"
```

### After the first deploy

1. **Set the Anthropic key** (Terraform created the parameter with a placeholder):
   ```powershell
   aws ssm put-parameter --name /log-inspector/anthropic-api-key --type SecureString `
     --overwrite --profile admin --value (Read-Host "Anthropic API key")
   ```
   Lambda reads it at cold start. To pick up a changed key immediately, redeploy or update
   any function setting.
2. **Create your user** (the key is printed once):
   ```powershell
   .\scripts\admin.ps1 create-user akash
   ```
3. **Point Postman at production:** set `baseUrl` to the `api_url` output and `apiKey` to
   the new key.
4. **Connect an account.** Production is a new database with a new service identity
   (`role/LogInspectorService`), so create the connection again with `POST /connections` and
   update your `LogInspectorReadOnly` role's trust policy to the one it returns. To keep
   local testing working too, put both statements in the trust policy.

### Operating it

| Task | How |
|---|---|
| Logs | CloudWatch log groups `/aws/lambda/log-inspector-{api,worker,admin}` (JSON) and `/aws/apigateway/log-inspector` |
| Failed investigation messages | The `log-inspector-investigations-dlq` queue; the investigations themselves are marked failed after 15 minutes |
| Admin commands | `.\scripts\admin.ps1 list-users`, `create-key`, `revoke-key`, `migrate` |
| Tear down | Set `deletion_protection = false` on the database, apply, then `terraform destroy`. A final DB snapshot is kept |

### Differences from running locally

- **Use `/investigations`, not `/diagnose`.** API Gateway stops waiting after 30 seconds,
  and a diagnosis usually takes longer.
- **Rate limits are per Lambda instance,** so they're looser than configured when traffic
  spreads over several instances. API Gateway's throttle (20 requests/second, burst 40)
  caps the total.
- **Worker concurrency is capped at 5** (`maximum_concurrency` in `lambda.tf`), to keep
  Claude spending bounded.
