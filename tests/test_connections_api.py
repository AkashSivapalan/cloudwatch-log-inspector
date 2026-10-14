import boto3
import pytest
from botocore.exceptions import NoCredentialsError
from botocore.stub import Stubber
from fastapi.testclient import TestClient

from log_inspector.aws.errors import LogsAccessDenied
from log_inspector.main import create_app
from log_inspector.services.connections import _sample_log_groups
from tests.conftest import MOTO_ACCOUNT_ID, login, make_settings, use_key


def _create(client, name="production", region="us-east-1") -> dict:
    response = client.post("/connections", json={"name": name, "region": region})
    assert response.status_code == 201, response.text
    return response.json()


def test_create_returns_setup_instructions(client):
    body = _create(client)
    connection, setup = body["connection"], body["setup"]

    assert connection["status"] == "awaiting_role"
    assert connection["role_arn"] is None
    assert len(connection["external_id"]) == 32
    assert setup["external_id"] == connection["external_id"]
    assert f"::{MOTO_ACCOUNT_ID}:" in setup["inspector_principal_arn"]

    statement = setup["trust_policy"]["Statement"][0]
    assert statement["Principal"]["AWS"] == setup["inspector_principal_arn"]
    assert statement["Condition"]["StringEquals"]["sts:ExternalId"] == connection["external_id"]
    assert "logs:StartQuery" in setup["permissions_policy"]["Statement"][0]["Action"]


def test_each_connection_gets_unique_external_id(client):
    a = _create(client, "a")["connection"]["external_id"]
    b = _create(client, "b")["connection"]["external_id"]
    assert a != b


@pytest.mark.parametrize(
    "payload",
    [{"name": ""}, {"name": "x", "region": "mars-1"}, {"name": "x" * 101}],
)
def test_create_validates_input(client, payload):
    assert client.post("/connections", json=payload).status_code == 422


def test_list_get_setup_and_404(client):
    created = _create(client)["connection"]

    assert [c["id"] for c in client.get("/connections").json()] == [created["id"]]
    assert client.get(f"/connections/{created['id']}").json()["name"] == "production"
    setup = client.get(f"/connections/{created['id']}/setup").json()
    assert setup["external_id"] == created["external_id"]
    assert client.get("/connections/does-not-exist").status_code == 404


@pytest.mark.parametrize(
    "role_arn",
    [
        "arn:aws:iam::123456789012:user/not-a-role",
        "arn:aws:iam::12345:role/ShortAccount",
        "LogInspectorReadOnly",
    ],
)
def test_set_role_rejects_invalid_arn(client, role_arn):
    conn_id = _create(client)["connection"]["id"]
    response = client.put(f"/connections/{conn_id}/role", json={"role_arn": role_arn})
    assert response.status_code == 422


def test_verify_requires_role(client):
    conn_id = _create(client)["connection"]["id"]
    assert client.post(f"/connections/{conn_id}/verify").status_code == 409


def test_full_flow_verifies_access(client, customer_role_arn, log_groups):
    conn_id = _create(client)["connection"]["id"]

    response = client.put(f"/connections/{conn_id}/role", json={"role_arn": customer_role_arn})
    assert response.status_code == 200
    assert response.json()["status"] == "pending_verification"

    result = client.post(f"/connections/{conn_id}/verify").json()
    assert result["verified"] is True
    assert result["status"] == "verified"
    assert result["account_id"] == MOTO_ACCOUNT_ID
    assert sorted(result["sample_log_groups"]) == sorted(log_groups)
    assert result["error"] is None

    connection = client.get(f"/connections/{conn_id}").json()
    assert connection["status"] == "verified"
    assert connection["last_verified_at"] is not None


def test_verify_reports_trust_policy_problem(app, client):
    conn_id = _create(client)["connection"]["id"]
    client.put(
        f"/connections/{conn_id}/role",
        json={"role_arn": "arn:aws:iam::999999999999:role/WrongTrust"},
    )

    sts = boto3.client("sts", region_name="us-east-1")
    stubber = Stubber(sts)
    stubber.add_client_error("assume_role", service_error_code="AccessDenied", http_status_code=403)
    stubber.activate()
    app.state.role_assumer._sts = lambda: sts

    result = client.post(f"/connections/{conn_id}/verify").json()
    assert result["verified"] is False
    assert result["status"] == "failed"
    assert "trust policy" in result["error"]
    assert client.get(f"/connections/{conn_id}").json()["last_error"] == result["error"]


def test_changing_role_resets_verification(client, customer_role_arn, log_groups):
    conn_id = _create(client)["connection"]["id"]
    client.put(f"/connections/{conn_id}/role", json={"role_arn": customer_role_arn})
    client.post(f"/connections/{conn_id}/verify")

    other_role = "arn:aws:iam::123456789012:role/Other"
    response = client.put(f"/connections/{conn_id}/role", json={"role_arn": other_role}).json()
    assert response["status"] == "pending_verification"
    assert response["role_arn"] == other_role


def test_delete_connection(client):
    conn_id = _create(client)["connection"]["id"]

    assert client.delete(f"/connections/{conn_id}").status_code == 204
    assert client.get(f"/connections/{conn_id}").status_code == 404
    assert client.delete(f"/connections/{conn_id}").status_code == 404


def test_missing_service_credentials_returns_503(app, client):
    class NoCredsSts:
        def get_caller_identity(self):
            raise NoCredentialsError()

    app.state.role_assumer._sts = NoCredsSts

    response = client.post("/connections", json={"name": "production"})
    assert response.status_code == 503
    assert "no AWS credentials" in response.json()["detail"]
    assert client.get("/connections").json() == []


def test_connections_persist_across_restarts(aws, tmp_path):
    settings = make_settings(database_url=f"sqlite:///{tmp_path / 'test.db'}")

    with TestClient(create_app(settings)) as first:
        key = login(first)
        conn_id = _create(first)["connection"]["id"]
    with TestClient(create_app(settings)) as second:
        use_key(second, key)
        assert second.get(f"/connections/{conn_id}").json()["id"] == conn_id


class _FakeSession:
    def __init__(self, logs_client):
        self._logs = logs_client

    def client(self, name):
        assert name == "logs"
        return self._logs


def test_logs_access_denied_is_explained():
    logs = boto3.client("logs", region_name="us-east-1")
    stubber = Stubber(logs)
    stubber.add_client_error(
        "describe_log_groups", service_error_code="AccessDeniedException", http_status_code=400
    )
    stubber.activate()

    with pytest.raises(LogsAccessDenied, match="permissions policy"):
        _sample_log_groups(_FakeSession(logs))


def test_timestamps_are_utc(client):
    created_at = _create(client)["connection"]["created_at"]
    assert created_at.endswith(("Z", "+00:00"))
