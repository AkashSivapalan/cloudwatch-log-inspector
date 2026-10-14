from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.orm import Session

from log_inspector.api.deps import CurrentUser, get_connection_or_404, get_role_assumer
from log_inspector.aws import policies
from log_inspector.aws.role_assumer import RoleAssumer
from log_inspector.db import get_db
from log_inspector.models import Connection
from log_inspector.schemas import (
    ConnectionCreate,
    ConnectionCreated,
    ConnectionOut,
    RoleUpdate,
    SetupInstructions,
    VerificationOut,
)
from log_inspector.services import connections as service

router = APIRouter(prefix="/connections", tags=["connections"])

DbSession = Annotated[Session, Depends(get_db)]
Assumer = Annotated[RoleAssumer, Depends(get_role_assumer)]


def _setup_instructions(connection: Connection, assumer: RoleAssumer) -> SetupInstructions:
    principal = assumer.inspector_principal_arn()
    return SetupInstructions(
        inspector_principal_arn=principal,
        external_id=connection.external_id,
        trust_policy=policies.trust_policy(principal, connection.external_id),
        permissions_policy=policies.permissions_policy(),
        steps=[
            "In your AWS account, create an IAM role using trust_policy as its trust relationship.",
            "Attach permissions_policy to the role (read-only CloudWatch Logs access).",
            f"Submit the role ARN with PUT /connections/{connection.id}/role.",
            f"Call POST /connections/{connection.id}/verify to confirm access.",
        ],
    )


@router.post("", status_code=status.HTTP_201_CREATED)
def create_connection(
    body: ConnectionCreate, db: DbSession, assumer: Assumer, user: CurrentUser
) -> ConnectionCreated:
    # Resolve the principal first so a misconfigured service fails before creating anything.
    assumer.inspector_principal_arn()
    connection = service.create_connection(db, user, body.name, body.region)
    return ConnectionCreated(
        connection=ConnectionOut.model_validate(connection),
        setup=_setup_instructions(connection, assumer),
    )


@router.get("")
def list_connections(db: DbSession, user: CurrentUser) -> list[ConnectionOut]:
    return [ConnectionOut.model_validate(c) for c in service.list_connections(db, user)]


@router.get("/{connection_id}")
def get_connection(connection_id: str, db: DbSession, user: CurrentUser) -> ConnectionOut:
    return ConnectionOut.model_validate(get_connection_or_404(db, connection_id, user))


@router.get("/{connection_id}/setup")
def get_setup(
    connection_id: str, db: DbSession, assumer: Assumer, user: CurrentUser
) -> SetupInstructions:
    return _setup_instructions(get_connection_or_404(db, connection_id, user), assumer)


@router.put("/{connection_id}/role")
def set_role(
    connection_id: str, body: RoleUpdate, db: DbSession, assumer: Assumer, user: CurrentUser
) -> ConnectionOut:
    connection = service.set_role(
        db, get_connection_or_404(db, connection_id, user), body.role_arn, assumer
    )
    return ConnectionOut.model_validate(connection)


@router.post("/{connection_id}/verify")
def verify_connection(
    connection_id: str, db: DbSession, assumer: Assumer, user: CurrentUser
) -> VerificationOut:
    connection = get_connection_or_404(db, connection_id, user)
    try:
        result = service.verify_connection(db, connection, assumer)
    except service.RoleNotSet:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "Set a role ARN with PUT /connections/{id}/role first"
        ) from None
    return VerificationOut(
        verified=result.verified,
        status=connection.status,
        account_id=result.account_id,
        sample_log_groups=result.sample_log_groups,
        error=result.error,
    )


@router.delete("/{connection_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_connection(
    connection_id: str, db: DbSession, assumer: Assumer, user: CurrentUser
) -> Response:
    service.delete_connection(db, get_connection_or_404(db, connection_id, user), assumer)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
