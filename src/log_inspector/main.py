import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.orm import sessionmaker

from log_inspector import __version__
from log_inspector.api import (
    analysis,
    connections,
    diagnosis,
    health,
    investigations,
    logs,
    me,
)
from log_inspector.api.deps import rate_limit
from log_inspector.aws.errors import AwsError
from log_inspector.aws.role_assumer import RoleAssumer
from log_inspector.config import Settings, get_settings
from log_inspector.db import create_db_engine, init_db
from log_inspector.diagnosis.diagnoser import Diagnoser, DiagnosisError
from log_inspector.observability import add_request_logging, configure_logging
from log_inspector.ratelimit import RateLimiter
from log_inspector.services.cognito import CognitoVerifier
from log_inspector.services.investigations import (
    InvestigationRunner,
    fail_interrupted,
    fail_stale,
)

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.log_format)

    engine = create_db_engine(settings.database_url)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if settings.run_migrations_on_startup:
            init_db(engine)
        with session_factory() as db:
            if settings.recover_interrupted_on_startup:
                failed = fail_interrupted(db)
            else:
                failed = fail_stale(db, timedelta(seconds=settings.investigation_timeout_seconds))
        if failed:
            logger.warning("Marked %d interrupted investigations as failed", failed)
        yield
        app.state.investigation_runner.shutdown()
        engine.dispose()

    app = FastAPI(title=settings.app_name, version=__version__, lifespan=lifespan)
    app.state.session_factory = session_factory
    app.state.rate_limiter = RateLimiter()
    app.state.role_assumer = RoleAssumer(
        region=settings.aws_region,
        duration_seconds=settings.assume_role_duration_seconds,
        principal_arn_override=settings.inspector_principal_arn,
        profile_name=settings.aws_profile,
    )
    app.state.cognito_verifier = (
        CognitoVerifier(
            settings.aws_region, settings.cognito_user_pool_id, settings.cognito_client_id
        )
        if settings.cognito_user_pool_id and settings.cognito_client_id
        else None
    )
    app.state.diagnoser = Diagnoser(settings)
    app.state.investigation_runner = InvestigationRunner(
        session_factory, app.state.role_assumer, app.state.diagnoser, settings
    )
    app.dependency_overrides[get_settings] = lambda: settings

    @app.exception_handler(AwsError)
    async def _aws_error(_: Request, exc: AwsError):
        return JSONResponse(status_code=exc.status_code, content={"detail": str(exc)})

    @app.exception_handler(DiagnosisError)
    async def _diagnosis_error(_: Request, exc: DiagnosisError):
        return JSONResponse(status_code=exc.status_code, content={"detail": str(exc)})

    add_request_logging(app)
    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_methods=["*"],
            allow_headers=["Authorization", "Content-Type"],
        )

    app.include_router(health.router)
    # Everything else needs an API key (rate_limit depends on the current user), applied
    # here so a new endpoint can't forget it.
    authenticated = [Depends(rate_limit)]
    for module in (me, connections, logs, analysis, diagnosis, investigations):
        app.include_router(module.router, dependencies=authenticated)
    return app


app = create_app()
