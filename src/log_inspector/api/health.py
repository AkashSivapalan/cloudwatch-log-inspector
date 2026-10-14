from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from log_inspector import __version__
from log_inspector.config import Settings, get_settings

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    status: str
    version: str
    environment: str


@router.get("/health")
def health(settings: Annotated[Settings, Depends(get_settings)]) -> HealthResponse:
    return HealthResponse(status="ok", version=__version__, environment=settings.environment)
