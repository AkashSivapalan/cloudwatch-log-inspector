import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy import select
from sqlalchemy.orm import Session

from log_inspector.aws.errors import LogsAccessDenied, RoleAssumptionFailed
from log_inspector.aws.policies import account_id_from_role_arn
from log_inspector.aws.role_assumer import RoleAssumer
from log_inspector.models import Connection, ConnectionStatus, User

logger = logging.getLogger(__name__)

_ACCESS_DENIED_CODES = {"AccessDenied", "AccessDeniedException", "UnauthorizedOperation"}


class ConnectionNotFound(Exception):
    pass


class RoleNotSet(Exception):
    pass


@dataclass
class VerificationResult:
    verified: bool
    account_id: str | None
    sample_log_groups: list[str] = field(default_factory=list)
    error: str | None = None


def create_connection(db: Session, owner: User, name: str, region: str) -> Connection:
    connection = Connection(owner_id=owner.id, name=name, region=region)
    db.add(connection)
    db.commit()
    db.refresh(connection)
    return connection


def list_connections(db: Session, owner: User) -> list[Connection]:
    query = select(Connection).where(Connection.owner_id == owner.id)
    return list(db.scalars(query.order_by(Connection.created_at)))


def get_connection(db: Session, connection_id: str, owner: User) -> Connection:
    connection = db.get(Connection, connection_id)
    if connection is None or connection.owner_id != owner.id:
        raise ConnectionNotFound(connection_id)
    return connection


def set_role(
    db: Session, connection: Connection, role_arn: str, assumer: RoleAssumer
) -> Connection:
    if connection.role_arn and connection.role_arn != role_arn:
        assumer.invalidate(connection.role_arn, connection.external_id)
    connection.role_arn = role_arn
    connection.status = ConnectionStatus.PENDING_VERIFICATION
    connection.last_error = None
    connection.last_verified_at = None
    db.commit()
    db.refresh(connection)
    return connection


def delete_connection(db: Session, connection: Connection, assumer: RoleAssumer) -> None:
    if connection.role_arn:
        assumer.invalidate(connection.role_arn, connection.external_id)
    db.delete(connection)
    db.commit()


def session_for_connection(connection: Connection, assumer: RoleAssumer) -> boto3.Session:
    if not connection.role_arn:
        raise RoleNotSet(connection.id)
    return assumer.session_for(
        role_arn=connection.role_arn,
        external_id=connection.external_id,
        session_name=f"log-inspector-{connection.id[:8]}",
        region=connection.region,
    )


def verify_connection(
    db: Session, connection: Connection, assumer: RoleAssumer
) -> VerificationResult:
    """Assume the role fresh and make a read-only CloudWatch Logs call to prove access works."""
    if not connection.role_arn:
        raise RoleNotSet(connection.id)

    # Always re-assume, so fixes to the user's trust policy take effect immediately.
    assumer.invalidate(connection.role_arn, connection.external_id)
    account_id = account_id_from_role_arn(connection.role_arn)

    try:
        session = session_for_connection(connection, assumer)
        sample = _sample_log_groups(session)
    except (RoleAssumptionFailed, LogsAccessDenied) as exc:
        connection.status = ConnectionStatus.FAILED
        connection.last_error = str(exc)
        db.commit()
        return VerificationResult(verified=False, account_id=account_id, error=str(exc))

    connection.status = ConnectionStatus.VERIFIED
    connection.last_error = None
    connection.last_verified_at = datetime.now(UTC)
    db.commit()
    return VerificationResult(verified=True, account_id=account_id, sample_log_groups=sample)


def _sample_log_groups(session: boto3.Session, limit: int = 5) -> list[str]:
    try:
        response = session.client("logs").describe_log_groups(limit=limit)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in _ACCESS_DENIED_CODES:
            raise LogsAccessDenied(
                "The role was assumed, but it is not allowed to call logs:DescribeLogGroups. "
                "Attach the permissions policy from the setup instructions to the role."
            ) from exc
        raise LogsAccessDenied(f"CloudWatch Logs call failed ({code}).") from exc
    except BotoCoreError as exc:
        raise LogsAccessDenied(f"CloudWatch Logs call failed: {exc}") from exc
    return [group["logGroupName"] for group in response.get("logGroups", [])]
