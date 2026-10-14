import re

# Read-only CloudWatch Logs actions the service needs in the user's account.
REQUIRED_LOGS_ACTIONS = [
    "logs:DescribeLogGroups",
    "logs:DescribeLogStreams",
    "logs:StartQuery",
    "logs:GetQueryResults",
    "logs:StopQuery",
    "logs:FilterLogEvents",
    "logs:GetLogEvents",
]

ROLE_ARN_PATTERN = re.compile(r"^arn:aws[a-z-]*:iam::(\d{12}):role/[\w+=,.@/-]{1,512}$")
_ASSUMED_ROLE_ARN = re.compile(r"^arn:(aws[a-z-]*):sts::(\d{12}):assumed-role/([^/]+)/.+$")


def account_id_from_role_arn(role_arn: str) -> str | None:
    match = ROLE_ARN_PATTERN.match(role_arn)
    return match.group(1) if match else None


def caller_arn_to_principal(caller_arn: str) -> str:
    """Turn a GetCallerIdentity ARN into one usable as a trust policy principal.

    An assumed-role session ARN (arn:aws:sts::123:assumed-role/Name/session) can't be a
    principal, so it is mapped back to its role ARN. Roles with a path (role/path/Name) can't
    be recovered this way; set LOG_INSPECTOR_INSPECTOR_PRINCIPAL_ARN for those.
    """
    match = _ASSUMED_ROLE_ARN.match(caller_arn)
    if match:
        partition, account_id, role_name = match.groups()
        return f"arn:{partition}:iam::{account_id}:role/{role_name}"
    return caller_arn


def trust_policy(inspector_principal_arn: str, external_id: str) -> dict:
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"AWS": inspector_principal_arn},
                "Action": "sts:AssumeRole",
                "Condition": {"StringEquals": {"sts:ExternalId": external_id}},
            }
        ],
    }


def permissions_policy() -> dict:
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "CloudWatchLogsReadOnly",
                "Effect": "Allow",
                "Action": REQUIRED_LOGS_ACTIONS,
                "Resource": "*",
            }
        ],
    }
