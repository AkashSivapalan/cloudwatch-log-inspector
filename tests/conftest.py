import json

import boto3
import pytest
from fastapi.testclient import TestClient
from moto import mock_aws

from log_inspector.aws.policies import permissions_policy
from log_inspector.config import Settings
from log_inspector.main import create_app
from log_inspector.services import auth

MOTO_ACCOUNT_ID = "123456789012"


@pytest.fixture(autouse=True)
def fake_aws_env(monkeypatch, tmp_path):
    """Make sure tests can never reach a real AWS account."""
    for var in ("AWS_PROFILE", "AWS_DEFAULT_PROFILE", "AWS_SESSION_TOKEN", "AWS_SECURITY_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "aws_config"))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "aws_credentials"))


@pytest.fixture(autouse=True)
def no_anthropic_credentials(monkeypatch, tmp_path):
    """Make sure tests can never call the real Anthropic API (or spend money)."""
    for var in (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_PROFILE",
        "LOG_INSPECTOR_ANTHROPIC_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("ANTHROPIC_CONFIG_DIR", str(tmp_path / "anthropic"))


@pytest.fixture
def aws():
    with mock_aws():
        yield


def login(client: TestClient, name: str = "tester") -> str:
    """Create a user in the client's app, send their API key on every request, return it."""
    with client.app.state.session_factory() as db:
        user = auth.create_user(db, name)
        _, key = auth.create_api_key(db, user, "test")
    use_key(client, key)
    return key


def use_key(client: TestClient, key: str) -> None:
    client.headers["Authorization"] = f"Bearer {key}"


def make_settings(**overrides) -> Settings:
    values = {"database_url": "sqlite://", "environment": "local"}
    values.update(overrides)
    return Settings(_env_file=None, **values)


@pytest.fixture
def app(aws):
    return create_app(make_settings())


@pytest.fixture
def client(app):
    with TestClient(app) as test_client:
        login(test_client)
        yield test_client


@pytest.fixture
def customer_role_arn(aws) -> str:
    """An IAM role in the (mocked) customer account, set up as the setup instructions describe."""
    iam = boto3.client("iam", region_name="us-east-1")
    role = iam.create_role(
        RoleName="LogInspectorReadOnly",
        AssumeRolePolicyDocument=json.dumps({"Version": "2012-10-17", "Statement": []}),
    )
    iam.put_role_policy(
        RoleName="LogInspectorReadOnly",
        PolicyName="logs-read-only",
        PolicyDocument=json.dumps(permissions_policy()),
    )
    return role["Role"]["Arn"]


@pytest.fixture
def log_groups(aws) -> list[str]:
    logs = boto3.client("logs", region_name="us-east-1")
    names = ["/aws/lambda/checkout", "/aws/lambda/payments", "/ecs/api"]
    for name in names:
        logs.create_log_group(logGroupName=name)
    return names
