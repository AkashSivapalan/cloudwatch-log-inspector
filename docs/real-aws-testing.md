# Testing M1 against a real AWS account

This connects the service to a real AWS account, end to end. It uses one AWS account playing both
parts: the **service** (the "inspector principal") and the **customer** (the account whose logs
are read). Cross-account setups work the same way.

Everything the service does in your account is read-only. The only resources you create are one
IAM user, one IAM role, and their policies. Step 7 removes them.

Commands are for **PowerShell** and need the AWS CLI v2 plus an admin-capable profile. Below,
that profile is `default`. Swap in yours with `--profile <name>`.

---

## 0. Check your admin profile

```powershell
aws sts get-caller-identity                # shows which account and identity 'default' is
aws iam get-account-summary > $null; $?    # True means this profile can read IAM
```

If the first command fails, the `default` profile has no credentials. Run `aws configure` and
enter an access key for an IAM user with administrator access. You can also use
`aws configure sso` if your account uses IAM Identity Center. Note the 12-digit account ID it
prints: you'll see it again in the role ARN.

## 1. Create the service's identity (the inspector principal)

In production this is the service's own task or instance role. Locally, use a dedicated IAM user
that can do only one thing: assume roles named `LogInspector*`.

```powershell
aws iam create-user --user-name log-inspector-service

@'
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": "sts:AssumeRole",
    "Resource": "arn:aws:iam::*:role/LogInspector*"
  }]
}
'@ | Out-File -Encoding ascii service-policy.json

aws iam put-user-policy --user-name log-inspector-service `
  --policy-name assume-log-inspector-roles --policy-document file://service-policy.json

aws iam create-access-key --user-name log-inspector-service
```

Put the returned access key into a new CLI profile:

```powershell
aws configure --profile log-inspector      # paste the key id + secret; region e.g. us-east-1
aws sts get-caller-identity --profile log-inspector
```

The last command should print `arn:aws:iam::<ACCOUNT_ID>:user/log-inspector-service`.

> **Why not your SSO login?** SSO sessions are roles with a path
> (`role/aws-reserved/sso.amazonaws.com/...`) that can't be recovered from the caller ARN. If you
> use one anyway, set `LOG_INSPECTOR_INSPECTOR_PRINCIPAL_ARN` to the full role ARN, including the
> path.

## 1b. (Optional) Create demo logs

Skip this if the region already has log groups with real application logs. Otherwise, this
creates a small log group containing a staged failure: database timeouts that cause checkout
errors. It will be useful again in M2 through M4. Its retention is set to 1 day, so AWS deletes
the events automatically.

```powershell
$group = "/log-inspector/demo"
aws logs create-log-group --log-group-name $group
aws logs put-retention-policy --log-group-name $group --retention-in-days 1
aws logs create-log-stream --log-group-name $group --log-stream-name checkout-service

$now = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
$messages = @(
  "INFO  Checkout request received order_id=1001",
  "INFO  Payment authorized order_id=1001",
  "WARN  DB connection pool at 90% capacity pool=orders-db",
  "ERROR Database connection timeout after 30000ms host=orders-db.internal order_id=1002",
  "ERROR Checkout failed: upstream dependency error status=503 order_id=1002",
  "ERROR Database connection timeout after 30000ms host=orders-db.internal order_id=1003",
  "ERROR Checkout failed: upstream dependency error status=503 order_id=1003",
  "INFO  Health check ok"
)
$i = 0
$messages | ForEach-Object { @{ timestamp = $now + ($i++ * 100); message = $_ } } |
  ConvertTo-Json | Out-File -Encoding ascii events.json
aws logs put-log-events --log-group-name $group --log-stream-name checkout-service `
  --log-events file://events.json
```

**No admin CLI profile?** Run the bash version in **AWS CloudShell** instead. Click the terminal
icon in the AWS console's top bar, in region `us-east-1`. CloudShell runs as your console
login, so it has the permissions your local profile lacks.

```bash
GROUP=/log-inspector/demo
aws logs create-log-group --log-group-name $GROUP 2>/dev/null || echo "log group already exists"
aws logs put-retention-policy --log-group-name $GROUP --retention-in-days 1
aws logs create-log-stream --log-group-name $GROUP --log-stream-name checkout-service 2>/dev/null || echo "log stream already exists"
NOW=$(date +%s%3N) python3 - > events.json <<'EOF'
import json, os
now = int(os.environ["NOW"])
messages = [
    "INFO  Checkout request received order_id=1001",
    "INFO  Payment authorized order_id=1001",
    "WARN  DB connection pool at 90% capacity pool=orders-db",
    "ERROR Database connection timeout after 30000ms host=orders-db.internal order_id=1002",
    "ERROR Checkout failed: upstream dependency error status=503 order_id=1002",
    "ERROR Database connection timeout after 30000ms host=orders-db.internal order_id=1003",
    "ERROR Checkout failed: upstream dependency error status=503 order_id=1003",
    "INFO  Health check ok",
]
print(json.dumps([{"timestamp": now + i * 100, "message": m} for i, m in enumerate(messages)]))
EOF
aws logs put-log-events --log-group-name $GROUP --log-stream-name checkout-service \
  --log-events file://events.json
```

Run it again whenever you want fresh events. The 1-day retention removes old ones, and queries
default to the last hour.

> Events are stamped with the current time on purpose. Logs Insights doesn't search events
> timestamped before their log group was created. FilterLogEvents still returns them, but
> queries and analysis don't.

## 2. Start the service as that identity

Tell the service which profile holds its credentials by putting this in `.env` (copied from
`.env.example`):

```
LOG_INSPECTOR_AWS_PROFILE=log-inspector
```

Then start it:

```powershell
uv run uvicorn log_inspector.main:app --reload
```

The service asks AWS who it is with those credentials and puts that ARN in every trust policy
it generates. Don't set `LOG_INSPECTOR_INSPECTOR_PRINCIPAL_ARN`: it's derived automatically, and a
value that doesn't match the credentials makes every verify fail.

Keep this running. Run the remaining steps in a **second** PowerShell window, which uses your
admin profile.

## 2b. Create your user and API key (M6)

Every request needs an API key. Create a user once; `--claim-unowned` gives it any
connections you made before users existed:

```powershell
uv run log-inspector create-user <your-name> --claim-unowned
```

Copy the printed key (it's shown once). In this guide's PowerShell examples, set:

```powershell
$headers = @{ Authorization = "Bearer li_..." }
```

and add `-Headers $headers` to every `Invoke-RestMethod` call. In Postman, set the
collection's `apiKey` variable.

## 3. Create a connection and save the generated policies

Use the region where your log groups live.

```powershell
$api = "http://127.0.0.1:8000"
$created = Invoke-RestMethod -Method Post "$api/connections" -ContentType "application/json" `
  -Body '{"name": "my-account", "region": "us-east-1"}'
$id = $created.connection.id

$created.setup.inspector_principal_arn    # should be ...:user/log-inspector-service
$created.setup.trust_policy       | ConvertTo-Json -Depth 10 | Out-File -Encoding ascii trust.json
$created.setup.permissions_policy | ConvertTo-Json -Depth 10 | Out-File -Encoding ascii permissions.json
```

You can also do this in Swagger at http://127.0.0.1:8000/docs and copy the JSON by hand.

## 4. Create the customer role (as your admin profile)

This is what a real user of the app does in their own account.

```powershell
$role = aws iam create-role --role-name LogInspectorReadOnly `
  --assume-role-policy-document file://trust.json --query Role.Arn --output text
aws iam put-role-policy --role-name LogInspectorReadOnly `
  --policy-name cloudwatch-logs-read-only --policy-document file://permissions.json
$role
```

## 5. Register the role and verify

```powershell
Invoke-RestMethod -Method Put "$api/connections/$id/role" -ContentType "application/json" `
  -Body (@{ role_arn = $role } | ConvertTo-Json)

Invoke-RestMethod -Method Post "$api/connections/$id/verify" | ConvertTo-Json
```

Expected result: `"verified": true`, `"status": "verified"`, your account ID, and up to 5 of your
log group names. If the region has no log groups, `sample_log_groups` is empty, but verification
still succeeds.

> A new role can take roughly 10 seconds to become assumable. If the first verify fails with
> an "Access denied" message, wait a moment and run it again.

## 6. Check the security properties

**a. The External ID protects the role.** Create a second connection that points at the *same*
role. It gets a different External ID, so AWS must refuse it:

```powershell
$other = (Invoke-RestMethod -Method Post "$api/connections" -ContentType "application/json" `
  -Body '{"name": "impostor"}').connection.id
Invoke-RestMethod -Method Put "$api/connections/$other/role" -ContentType "application/json" `
  -Body (@{ role_arn = $role } | ConvertTo-Json)
Invoke-RestMethod -Method Post "$api/connections/$other/verify" | ConvertTo-Json
```

Expected result: `"verified": false` and an error that mentions the trust policy.

**b. Missing permissions are reported clearly.** Remove the role's logs permissions and verify again:

```powershell
aws iam delete-role-policy --role-name LogInspectorReadOnly --policy-name cloudwatch-logs-read-only
Invoke-RestMethod -Method Post "$api/connections/$id/verify" | ConvertTo-Json
```

Expected result: `"verified": false`, with an error saying the role can't call
`logs:DescribeLogGroups`. Put the policy back with the `put-role-policy` command from step 4
before moving on to M2.

**c. The role can't write.** In the CloudTrail console (Event history, filtered by user name
`log-inspector-<first 8 chars of connection id>`), you'll see only `AssumeRole` and
`DescribeLogGroups` calls.

## M2. Query your logs

These calls need a **verified** connection; unverified connections get `409`. A Logs Insights
query costs $0.005 per GB scanned, which is effectively free on the demo log group.

**List log groups.** Use `prefix` to filter and `next_token` to page through results:

```powershell
Invoke-RestMethod "$api/connections/$id/log-groups" | ConvertTo-Json -Depth 5
Invoke-RestMethod "$api/connections/$id/log-groups?prefix=/log-inspector/" | ConvertTo-Json -Depth 5
```

**See the ready-made queries:**

```powershell
Invoke-RestMethod "$api/canned-queries" | Format-Table name, description
```

**Run a ready-made query.** The time range defaults to the last hour:

```powershell
$body = @{ log_groups = @("/log-inspector/demo"); canned_query = "errors" } | ConvertTo-Json
Invoke-RestMethod -Method Post "$api/connections/$id/query" -ContentType "application/json" -Body $body |
  ConvertTo-Json -Depth 5
```

With the demo logs, expect 4 rows: the timeouts and the checkout failures. Then try each of the
other canned queries:

| `canned_query` | Expected on the demo logs |
|---|---|
| `errors` | 4 ERROR lines |
| `http_5xx` | The 2 `status=503` lines |
| `lambda_failures` | 0 rows (the demo isn't a Lambda), and no error |
| `error_volume` | 1 or 2 rows of counts per 5 minutes |
| `recent` | All 8 lines |

These queries only pass mocked tests so far, because the mock ignores Insights filters. A
`400` with "CloudWatch rejected the query" on any of them means its syntax needs fixing, so
report it.

**Run your own query and time range.** Times without a timezone are treated as UTC:

```powershell
$body = @{
  log_groups = @("/log-inspector/demo")
  query      = "fields @timestamp, @message | filter @message like /timeout/ | sort @timestamp desc"
  start_time = (Get-Date).ToUniversalTime().AddHours(-3).ToString("o")
  end_time   = (Get-Date).ToUniversalTime().ToString("o")
} | ConvertTo-Json
Invoke-RestMethod -Method Post "$api/connections/$id/query" -ContentType "application/json" -Body $body |
  ConvertTo-Json -Depth 5
```

**Calls that should fail:**

| Request | Expected |
|---|---|
| `query: "this is not a query"` | `400`: CloudWatch rejected the query |
| `log_groups: ["/does/not/exist"]` | `404`: Log group not found |
| `start_time` more than 7 days before `end_time` | `400`: time range limit |
| Both `query` and `canned_query`, or neither | `422` |

## M3. Analyze your logs

Needs a verified connection and demo events written within the time range (step 1b).

```powershell
$body = @{ log_groups = @("/log-inspector/demo") } | ConvertTo-Json
Invoke-RestMethod -Method Post "$api/connections/$id/analyze" -ContentType "application/json" -Body $body |
  ConvertTo-Json -Depth 6
```

Expected on the demo logs:

| Field | Expected |
|---|---|
| `total_events` / `total_errors` | 8 / 4 |
| `clusters` | 3 patterns: the database timeout (2x), the checkout failure (2x), and the connection-pool warning (1x), with errors first |
| `clusters[].template` | Variable parts replaced, e.g. `... after <num>ms host=orders-db.internal order_id=<num>` |
| `spikes` | 1 spike in the minute the events were written (4 errors against a normal 0) |
| `timeline` | The spike start, then when each pattern first appeared |
| `volume` | One bucket per minute for the last hour, mostly zeros |

To see redaction, add a line such as `ERROR login failed for jane@example.com password=hunter2`
to the demo messages and run the block again. In the analysis it appears as
`ERROR login failed for <email> password=<redacted>`, and `redactions` counts both.

## M4. Diagnose

1. Create an API key at https://console.anthropic.com (**API Keys**) and add it to `.env`
   yourself. Never paste it into a chat or commit it:
   ```
   ANTHROPIC_API_KEY=sk-ant-...
   ```
2. Restart the server, write fresh demo events (step 1b), then:
   ```powershell
   $body = @{ log_groups = @("/log-inspector/demo"); question = "Why did checkout fail?" } | ConvertTo-Json
   Invoke-RestMethod -Method Post "$api/connections/$id/diagnose" -ContentType "application/json" -Body $body |
     ConvertTo-Json -Depth 8
   ```

Expect a `high`-confidence root cause about the orders database (connection pool exhaustion
or database timeouts), with the 503 checkout failures described as a symptom. Every
`evidence` item should have `"verified": true`. A call takes about 20 to 90 seconds and costs
a few cents (see `usage`).

If no errors or warnings are in range, the response has `llm_skipped: true` and costs nothing.

## M5. Investigations

```powershell
$body = @{ connection_id = $id; log_groups = @("/log-inspector/demo"); question = "Why did checkout fail?" } | ConvertTo-Json
$inv = Invoke-RestMethod -Method Post "$api/investigations" -ContentType "application/json" -Body $body
$inv.status     # queued: the request returned immediately

do { Start-Sleep 2; $inv = Invoke-RestMethod "$api/investigations/$($inv.id)"; $inv.status } until ($inv.finished)
$inv.result.diagnosis | ConvertTo-Json -Depth 6

Invoke-RestMethod "$api/investigations" | Format-Table status, severity, question, created_at
```

To check history survives a restart: stop the server, start it again, and list investigations.
Stopping the server while one is running marks that one `failed` with "server restarted".

## 7. Clean up (optional)

Keep these resources if you plan to test M2 against the same account.

```powershell
aws iam delete-role-policy --role-name LogInspectorReadOnly --policy-name cloudwatch-logs-read-only
aws iam delete-role --role-name LogInspectorReadOnly
aws iam list-access-keys --user-name log-inspector-service   # note the AccessKeyId
aws iam delete-access-key --user-name log-inspector-service --access-key-id <AccessKeyId>
aws iam delete-user-policy --user-name log-inspector-service --policy-name assume-log-inspector-roles
aws iam delete-user --user-name log-inspector-service
aws logs delete-log-group --log-group-name /log-inspector/demo   # if you created it
Remove-Item trust.json, permissions.json, service-policy.json, events.json -ErrorAction SilentlyContinue
```

The local database is `log_inspector.db` in the project root. Delete it to start fresh.
