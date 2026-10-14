from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from log_inspector.api.deps import (
    CurrentUser,
    expensive_rate_limit,
    get_connection_or_404,
    get_role_assumer,
)
from log_inspector.aws.canned_queries import CANNED_QUERIES
from log_inspector.aws.role_assumer import RoleAssumer
from log_inspector.config import Settings, get_settings
from log_inspector.db import get_db
from log_inspector.schemas import (
    CannedQueryOut,
    LogGroupPageOut,
    QueryRequest,
    QueryResponse,
    QueryStatistics,
)
from log_inspector.services import logs as service

router = APIRouter(tags=["logs"])

DbSession = Annotated[Session, Depends(get_db)]
Assumer = Annotated[RoleAssumer, Depends(get_role_assumer)]
AppSettings = Annotated[Settings, Depends(get_settings)]


def _not_ready(exc: service.ConnectionNotReady) -> HTTPException:
    return HTTPException(status.HTTP_409_CONFLICT, str(exc))


@router.get("/canned-queries")
def list_canned_queries() -> list[CannedQueryOut]:
    return [CannedQueryOut.model_validate(q) for q in CANNED_QUERIES.values()]


@router.get("/connections/{connection_id}/log-groups")
def list_log_groups(
    connection_id: str,
    user: CurrentUser,
    db: DbSession,
    assumer: Assumer,
    prefix: Annotated[str | None, Query(min_length=1, max_length=512)] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 50,
    next_token: str | None = None,
) -> LogGroupPageOut:
    connection = get_connection_or_404(db, connection_id, user)
    try:
        page = service.list_log_groups(connection, assumer, prefix, limit, next_token)
    except service.ConnectionNotReady as exc:
        raise _not_ready(exc) from None
    return LogGroupPageOut.model_validate(page)


@router.post("/connections/{connection_id}/query", dependencies=[Depends(expensive_rate_limit)])
def run_query(
    connection_id: str,
    user: CurrentUser,
    body: QueryRequest,
    db: DbSession,
    assumer: Assumer,
    settings: AppSettings,
) -> QueryResponse:
    connection = get_connection_or_404(db, connection_id, user)
    time_range = service.resolve_time_range(
        body.start_time, body.end_time, timedelta(days=settings.max_query_range_days)
    )
    query = service.query_string_for(body.query, body.canned_query)
    try:
        result = service.run_insights_query(
            connection,
            assumer,
            log_groups=body.log_groups,
            query=query,
            time_range=time_range,
            limit=body.limit,
            timeout_seconds=settings.query_timeout_seconds,
        )
    except service.ConnectionNotReady as exc:
        raise _not_ready(exc) from None
    return QueryResponse(
        query_id=result.query_id,
        query=query,
        log_groups=body.log_groups,
        start_time=time_range.start,
        end_time=time_range.end,
        row_count=len(result.rows),
        rows=result.rows,
        statistics=QueryStatistics(
            records_matched=result.records_matched,
            records_scanned=result.records_scanned,
            bytes_scanned=result.bytes_scanned,
        ),
    )
