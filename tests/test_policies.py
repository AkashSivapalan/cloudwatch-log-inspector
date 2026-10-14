import pytest

from log_inspector.aws.policies import (
    REQUIRED_LOGS_ACTIONS,
    account_id_from_role_arn,
    caller_arn_to_principal,
    permissions_policy,
    trust_policy,
)


@pytest.mark.parametrize(
    ("caller_arn", "expected"),
    [
        ("arn:aws:iam::111122223333:user/inspector", "arn:aws:iam::111122223333:user/inspector"),
        (
            "arn:aws:sts::111122223333:assumed-role/InspectorTask/i-0abc",
            "arn:aws:iam::111122223333:role/InspectorTask",
        ),
        (
            "arn:aws-us-gov:sts::111122223333:assumed-role/InspectorTask/session",
            "arn:aws-us-gov:iam::111122223333:role/InspectorTask",
        ),
    ],
)
def test_caller_arn_to_principal(caller_arn, expected):
    assert caller_arn_to_principal(caller_arn) == expected


@pytest.mark.parametrize(
    ("role_arn", "account_id"),
    [
        ("arn:aws:iam::111122223333:role/LogInspector", "111122223333"),
        ("arn:aws:iam::111122223333:role/team/LogInspector", "111122223333"),
        ("arn:aws:iam::111122223333:user/someone", None),
        ("not-an-arn", None),
    ],
)
def test_account_id_from_role_arn(role_arn, account_id):
    assert account_id_from_role_arn(role_arn) == account_id


def test_trust_policy_requires_external_id():
    policy = trust_policy("arn:aws:iam::111122223333:user/inspector", "ext-123")
    statement = policy["Statement"][0]

    assert statement["Principal"] == {"AWS": "arn:aws:iam::111122223333:user/inspector"}
    assert statement["Action"] == "sts:AssumeRole"
    assert statement["Condition"] == {"StringEquals": {"sts:ExternalId": "ext-123"}}


def test_permissions_policy_is_read_only_logs():
    actions = permissions_policy()["Statement"][0]["Action"]

    assert actions == REQUIRED_LOGS_ACTIONS
    assert all(a.startswith("logs:") for a in actions)
    assert not any(verb in a for a in actions for verb in ("Put", "Delete", "Create"))
