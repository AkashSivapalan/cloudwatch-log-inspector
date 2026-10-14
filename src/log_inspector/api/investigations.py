from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.orm import Session

from log_inspector.api.deps import CurrentUser, expensive_rate_limit, get_connection_or_404
from log_inspector.config import Settings, get_settings
from log_inspector.db import get_db
from log_inspector.models import Investigation, InvestigationStatus, User
from log_inspector.schemas import (
    InvestigationCreate,
    InvestigationOut,
    InvestigationSummaryOut,
)
from log_inspector.services import investigations as service
from log_inspector.services.logs import ConnectionNotReady, resolve_time_range

router = APIRouter(prefix="/investigations", tags=["investigations"])

DbSession = Annotated[Session, Depends(get_db)]


def get_runner(request: Request) -> service.InvestigationRunner:
    return request.app.state.investigation_runner


def _timeout(settings: Settings) -> timedelta:
    return timedelta(seconds=settings.investigation_timeout_seconds)


def _get_or_404(db: Session, investigation_id: str, user: User) -> Investigation:
    try:
        return service.get_investigation(db, investigation_id, user)
    except service.InvestigationNotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Investigation not found") from None


def _out(investigation: Investigation, include_analysis: bool) -> InvestigationOut:
    result = investigation.result
    if result is not None and not include_analysis:
        result = {**result, "analysis": None}
    duration = None
    if investigation.started_at and investigation.finished_at:
        duration = (investigation.finished_at - investigation.started_at).total_seconds()
    return InvestigationOut(
        id=investigation.id,
        connection_id=investigation.connection_id,
        status=investigation.status,
        finished=investigation.status.finished,
        question=investigation.question,
        log_groups=investigation.log_groups,
        start_time=investigation.start_time,
        end_time=investigation.end_time,
        created_at=investigation.created_at,
        started_at=investigation.started_at,
        finished_at=investigation.finished_at,
        duration_seconds=duration,
        error=investigation.error,
        error_status=investigation.error_status,
        result=result,
    )


@router.post("", status_code=status.HTTP_202_ACCEPTED, dependencies=[Depends(expensive_rate_limit)])
def start_investigation(
    body: InvestigationCreate,
    db: DbSession,
    user: CurrentUser,
    runner: Annotated[service.InvestigationRunner, Depends(get_runner)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> InvestigationOut:
    """Start a diagnosis in the background. Poll GET /investigations/{id} for the result."""
    connection = get_connection_or_404(db, body.connection_id, user)
    # Runs whose worker died never recorded a failure. Settle them first, so they don't count
    # against the limit on active investigations.
    service.fail_stale(db, _timeout(settings), owner=user)
    time_range = resolve_time_range(
        body.start_time, body.end_time, timedelta(days=settings.max_query_range_days)
    )
    try:
        investigation = service.create_investigation(
            db,
            connection,
            question=body.question,
            log_groups=body.log_groups,
            time_range=time_range,
            max_events=body.max_events or settings.analysis_max_events,
            max_active=settings.max_active_investigations,
        )
    except ConnectionNotReady as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None
    except service.TooManyActiveInvestigations as exc:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, str(exc)) from None
    response = _out(investigation, include_analysis=False)
    runner.submit(investigation.id)
    return response


@router.get("")
def list_investigations(
    db: DbSession,
    user: CurrentUser,
    settings: Annotated[Settings, Depends(get_settings)],
    connection_id: str | None = None,
    status_filter: Annotated[InvestigationStatus | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> list[InvestigationSummaryOut]:
    """Past and running investigations, newest first."""
    rows = service.list_investigations(db, user, connection_id, status_filter, limit)
    if any(service.is_stale(i, _timeout(settings)) for i in rows):
        # Some worker died without recording a failure. Record it, then read again.
        service.fail_stale(db, _timeout(settings), owner=user)
        db.expire_all()
        rows = service.list_investigations(db, user, connection_id, status_filter, limit)
    return [
        InvestigationSummaryOut(
            id=i.id,
            connection_id=i.connection_id,
            status=i.status,
            question=i.question,
            log_groups=i.log_groups,
            created_at=i.created_at,
            finished_at=i.finished_at,
            severity=i.result["diagnosis"]["severity"] if i.result else None,
            summary=i.result["diagnosis"]["summary"] if i.result else None,
            error=i.error,
        )
        for i in rows
    ]


@router.get("/{investigation_id}")
def get_investigation(
    investigation_id: str,
    db: DbSession,
    user: CurrentUser,
    settings: Annotated[Settings, Depends(get_settings)],
    include_analysis: bool = False,
) -> InvestigationOut:
    investigation = _get_or_404(db, investigation_id, user)
    if service.is_stale(investigation, _timeout(settings)):
        # Its worker died without recording a failure, so say so now rather than leaving it
        # "in progress" until something else happens to clean up.
        service.fail_stale(db, _timeout(settings), owner=user)
        db.refresh(investigation)
    return _out(investigation, include_analysis)
