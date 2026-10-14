from datetime import UTC, datetime, timedelta

import boto3
import pytest
from botocore.exceptions import NoCredentialsError
from botocore.stub import Stubber

from log_inspector.aws.errors import RoleAssumptionFailed, ServiceCredentialsMissing
from log_inspector.aws.role_assumer import RoleAssumer, TemporaryCredentials

ROLE_ARN = "arn:aws:iam::123456789012:role/LogInspectorReadOnly"


def _creds(expires_in: timedelta) -> TemporaryCredentials:
    return TemporaryCredentials("AKIA", "secret", "token", datetime.now(UTC) + expires_in)


def test_assumes_role_and_returns_session(aws, customer_role_arn):
    assumer = RoleAssumer(region="us-east-1")

    session = assumer.session_for(customer_role_arn, "ext-id", "test-session", "eu-west-1")

    assert session.region_name == "eu-west-1"
    identity = session.client("sts").get_caller_identity()
    assert ":assumed-role/LogInspectorReadOnly/test-session" in identity["Arn"]


def test_caches_credentials_until_near_expiry(monkeypatch):
    assumer = RoleAssumer(region="us-east-1")
    calls = []

    def fake_assume(role_arn, external_id, session_name):
        calls.append(role_arn)
        return _creds(timedelta(hours=1))

    monkeypatch.setattr(assumer, "_assume_role", fake_assume)

    assumer.session_for(ROLE_ARN, "ext", "session", "us-east-1")
    assumer.session_for(ROLE_ARN, "ext", "session", "us-east-1")
    assert len(calls) == 1

    # A different external ID is a different cache entry.
    assumer.session_for(ROLE_ARN, "other-ext", "session", "us-east-1")
    assert len(calls) == 2


def test_refreshes_credentials_close_to_expiry(monkeypatch):
    assumer = RoleAssumer(region="us-east-1")
    lifetimes = iter([timedelta(minutes=2), timedelta(hours=1)])
    calls = []

    def fake_assume(role_arn, external_id, session_name):
        calls.append(role_arn)
        return _creds(next(lifetimes))

    monkeypatch.setattr(assumer, "_assume_role", fake_assume)

    assumer.session_for(ROLE_ARN, "ext", "session", "us-east-1")
    assumer.session_for(ROLE_ARN, "ext", "session", "us-east-1")
    assert len(calls) == 2


def test_invalidate_forces_reassume(monkeypatch):
    assumer = RoleAssumer(region="us-east-1")
    calls = []
    monkeypatch.setattr(
        assumer, "_assume_role", lambda *a: calls.append(a) or _creds(timedelta(hours=1))
    )

    assumer.session_for(ROLE_ARN, "ext", "session", "us-east-1")
    assumer.invalidate(ROLE_ARN, "ext")
    assumer.session_for(ROLE_ARN, "ext", "session", "us-east-1")
    assert len(calls) == 2


def _stubbed_sts(monkeypatch, assumer: RoleAssumer) -> Stubber:
    client = boto3.client("sts", region_name="us-east-1")
    stubber = Stubber(client)
    stubber.activate()
    monkeypatch.setattr(assumer, "_sts", lambda: client)
    return stubber


def test_access_denied_explains_trust_policy(monkeypatch):
    assumer = RoleAssumer(region="us-east-1")
    stubber = _stubbed_sts(monkeypatch, assumer)
    stubber.add_client_error("assume_role", service_error_code="AccessDenied", http_status_code=403)

    with pytest.raises(RoleAssumptionFailed, match="trust policy"):
        assumer.session_for(ROLE_ARN, "ext", "session", "us-east-1")


def test_missing_service_credentials(monkeypatch):
    assumer = RoleAssumer(region="us-east-1")

    class NoCredsSts:
        def assume_role(self, **_):
            raise NoCredentialsError()

        def get_caller_identity(self):
            raise NoCredentialsError()

    monkeypatch.setattr(assumer, "_sts", NoCredsSts)

    with pytest.raises(ServiceCredentialsMissing):
        assumer.session_for(ROLE_ARN, "ext", "session", "us-east-1")
    with pytest.raises(ServiceCredentialsMissing):
        assumer.inspector_principal_arn()


def test_principal_override_skips_sts(monkeypatch):
    assumer = RoleAssumer(region="us-east-1", principal_arn_override="arn:aws:iam::1:role/X")
    monkeypatch.setattr(assumer, "_sts", lambda: pytest.fail("STS should not be called"))

    assert assumer.inspector_principal_arn() == "arn:aws:iam::1:role/X"


def test_principal_from_caller_identity(aws):
    # Moto reports the caller as arn:aws:sts::<account>:user/moto.
    assert (
        RoleAssumer(region="us-east-1")
        .inspector_principal_arn()
        .endswith("::123456789012:user/moto")
    )


def test_unknown_profile_is_reported(aws):
    assumer = RoleAssumer(region="us-east-1", profile_name="does-not-exist")

    with pytest.raises(ServiceCredentialsMissing, match="does-not-exist"):
        assumer.inspector_principal_arn()


def test_uses_configured_profile(aws, customer_role_arn, monkeypatch, tmp_path):
    credentials_file = tmp_path / "aws_credentials"
    credentials_file.write_text(
        "[log-inspector]\naws_access_key_id = profilekey\naws_secret_access_key = profilesecret\n"
    )
    # Without the profile there would be no credentials at all.
    monkeypatch.delenv("AWS_ACCESS_KEY_ID")
    monkeypatch.delenv("AWS_SECRET_ACCESS_KEY")

    assumer = RoleAssumer(region="us-east-1", profile_name="log-inspector")

    assert assumer.inspector_principal_arn().endswith("::123456789012:user/moto")
    assumer.session_for(customer_role_arn, "ext", "session", "us-east-1")
