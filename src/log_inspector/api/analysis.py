from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
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
from log_inspector.presenters import analysis_out
from log_inspector.schemas import AnalysisOut, AnalyzeRequest
from log_inspector.services import analysis as service
from log_inspector.services.logs import ConnectionNotReady, resolve_time_range

router = APIRouter(tags=["analysis"])


@router.post("/connections/{connection_id}/analyze", dependencies=[Depends(expensive_rate_limit)])
def analyze(
    connection_id: str,
    user: CurrentUser,
    body: AnalyzeRequest,
    db: Annotated[Session, Depends(get_db)],
    assumer: Annotated[RoleAssumer, Depends(get_role_assumer)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> AnalysisOut:
    """Cluster error and warning lines, detect error spikes, and build a timeline."""
    connection = get_connection_or_404(db, connection_id, user)
    time_range = resolve_time_range(
        body.start_time, body.end_time, timedelta(days=settings.max_query_range_days)
    )
    try:
        report = service.analyze_logs(
            connection,
            assumer,
            log_groups=body.log_groups,
            time_range=time_range,
            max_events=body.max_events or settings.analysis_max_events,
            settings=settings,
        )
    except ConnectionNotReady as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None
    return analysis_out(report)
