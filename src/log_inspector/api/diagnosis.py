from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from log_inspector.api.deps import (
    CurrentUser,
    expensive_rate_limit,
    get_connection_or_404,
    get_role_assumer,
)
from log_inspector.aws.role_assumer import RoleAssumer
from log_inspector.config import Settings, get_settings
from log_inspector.db import get_db
from log_inspector.diagnosis.diagnoser import Diagnoser
from log_inspector.presenters import diagnose_response
from log_inspector.schemas import DiagnoseRequest, DiagnoseResponse
from log_inspector.services import diagnosis as service
from log_inspector.services.logs import ConnectionNotReady, resolve_time_range

router = APIRouter(tags=["diagnosis"])


def get_diagnoser(request: Request) -> Diagnoser:
    return request.app.state.diagnoser


@router.post("/connections/{connection_id}/diagnose", dependencies=[Depends(expensive_rate_limit)])
def diagnose(
    connection_id: str,
    user: CurrentUser,
    body: DiagnoseRequest,
    db: Annotated[Session, Depends(get_db)],
    assumer: Annotated[RoleAssumer, Depends(get_role_assumer)],
    diagnoser: Annotated[Diagnoser, Depends(get_diagnoser)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> DiagnoseResponse:
    """Analyze the logs, then ask Claude for the likely root causes, evidence, and fixes."""
    connection = get_connection_or_404(db, connection_id, user)
    time_range = resolve_time_range(
        body.start_time, body.end_time, timedelta(days=settings.max_query_range_days)
    )
    try:
        report = service.diagnose_logs(
            connection,
            assumer,
            diagnoser,
            question=body.question,
            log_groups=body.log_groups,
            time_range=time_range,
            max_events=body.max_events or settings.analysis_max_events,
            settings=settings,
        )
    except ConnectionNotReady as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None

    return diagnose_response(body.question, report, body.include_analysis)
