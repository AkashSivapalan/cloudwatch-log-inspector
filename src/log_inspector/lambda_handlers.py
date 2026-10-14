"""AWS Lambda entry points. One image, three functions:

- api:    the FastAPI app behind API Gateway (via Mangum).
- worker: runs investigations from the SQS queue.
- admin:  runs `log-inspector` commands, e.g. {"args": ["create-user", "akash"]}. RDS is only
          reachable inside the VPC, so this replaces running the CLI locally.

Secrets are fetched once per cold start and never come from the function's configuration:
the database password from the RDS-managed secret in Secrets Manager, and the Anthropic key
from an SSM SecureString parameter.
"""

import contextlib
import io
import json
import logging
import os
import time
from datetime import timedelta
from typing import Any
from urllib.parse import quote

import boto3

logger = logging.getLogger(__name__)

_api_handler = None

# Stop the Claude step this long before Lambda would kill the worker, leaving time to save
# the failure. (A killed worker records nothing, and the investigation would look stuck.)
WORKER_SAFETY_MARGIN_SECONDS = 30


def load_secrets() -> None:
    """Fill in the database URL and Anthropic key from AWS secret stores, if configured."""
    secret_arn = os.environ.get("LOG_INSPECTOR_DATABASE_SECRET_ARN")
    if secret_arn and not os.environ.get("LOG_INSPECTOR_DATABASE_URL"):
        secret = json.loads(
            boto3.client("secretsmanager").get_secret_value(SecretId=secret_arn)["SecretString"]
        )
        host = os.environ["LOG_INSPECTOR_DATABASE_HOST"]
        name = os.environ["LOG_INSPECTOR_DATABASE_NAME"]
        user, password = quote(secret["username"], safe=""), quote(secret["password"], safe="")
        os.environ["LOG_INSPECTOR_DATABASE_URL"] = (
            f"postgresql+psycopg://{user}:{password}@{host}/{name}?sslmode=require"
        )

    parameter = os.environ.get("LOG_INSPECTOR_ANTHROPIC_API_KEY_PARAMETER")
    if parameter and not os.environ.get("ANTHROPIC_API_KEY"):
        response = boto3.client("ssm").get_parameter(Name=parameter, WithDecryption=True)
        os.environ["ANTHROPIC_API_KEY"] = response["Parameter"]["Value"]


def _app():
    # Imported late: creating the app reads settings, which need the secrets above.
    load_secrets()
    from log_inspector import main

    return main.app


def api(event: dict, context: Any) -> dict:
    global _api_handler
    if _api_handler is None:
        from mangum import Mangum

        _api_handler = Mangum(_app(), lifespan="auto")
    return _api_handler(event, context)


def worker(event: dict, context: Any) -> dict:
    from log_inspector.config import get_settings
    from log_inspector.services.investigations import fail_stale

    app = _app()
    settings = get_settings()
    with app.state.session_factory() as db:
        fail_stale(db, timedelta(seconds=settings.investigation_timeout_seconds))

    runner = app.state.investigation_runner
    for record in event.get("Records", []):
        investigation_id = json.loads(record["body"])["investigation_id"]
        logger.info("Running investigation %s", investigation_id)
        # run() records success or failure itself, and skips investigations that are no
        # longer queued, so SQS redelivering a message is harmless.
        # Lambda gives each invocation a fixed time; pass on what is left of it.
        remaining = context.get_remaining_time_in_millis() / 1000
        runner.run(
            investigation_id, deadline=time.monotonic() + remaining - WORKER_SAFETY_MARGIN_SECONDS
        )
    return {"processed": len(event.get("Records", []))}


def admin(event: dict, context: Any) -> dict:
    load_secrets()
    from log_inspector import cli

    args = event.get("args")
    if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
        return {"exit_code": 2, "output": 'Expected {"args": ["<command>", ...]}'}

    # Capture the output (it may contain a new API key) so it goes back to the caller only,
    # never into the function's CloudWatch logs.
    output = io.StringIO()
    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
        try:
            exit_code = cli.main(args)
        except SystemExit as exc:  # argparse errors and --help
            exit_code = exc.code if isinstance(exc.code, int) else 1
    return {"exit_code": exit_code, "output": output.getvalue()}
