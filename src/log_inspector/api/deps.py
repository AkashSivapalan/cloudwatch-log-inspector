from math import ceil
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from log_inspector.aws.role_assumer import RoleAssumer
from log_inspector.config import Settings, get_settings
from log_inspector.db import get_db
from log_inspector.models import Connection, User
from log_inspector.ratelimit import RateLimiter
from log_inspector.services import auth as auth_service
from log_inspector.services import connections as connection_service
from log_inspector.services.cognito import InvalidToken

_bearer = HTTPBearer(
    auto_error=False, description="An API key (li_<prefix>_<secret>) or a Cognito access token"
)


def _authenticate(request: Request, db: Session, token: str) -> User | None:
    if token.startswith(f"{auth_service.KEY_SCHEME}_"):
        return auth_service.authenticate(db, token)
    verifier = request.app.state.cognito_verifier
    if verifier is None:
        return None
    try:
        claims = verifier.verify(token)
    except InvalidToken:
        return None
    return auth_service.get_or_create_cognito_user(db, claims)


def get_current_user(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    db: Annotated[Session, Depends(get_db)],
) -> User:
    user = _authenticate(request, db, credentials.credentials) if credentials else None
    if user is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Missing or invalid credentials. Send an API key or a login token as "
            "'Authorization: Bearer <token>'.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    # For the request log (see observability.py).
    request.state.user_id = user.id
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def get_rate_limiter(request: Request) -> RateLimiter:
    return request.app.state.rate_limiter


def _enforce(limiter: RateLimiter, key: str, limit: int, window: int, what: str) -> None:
    retry_after = limiter.hit(key, limit, window)
    if retry_after is not None:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            f"Rate limit exceeded: {limit} {what}. Try again in {ceil(retry_after)} seconds.",
            headers={"Retry-After": str(ceil(retry_after))},
        )


def rate_limit(
    user: CurrentUser,
    limiter: Annotated[RateLimiter, Depends(get_rate_limiter)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> None:
    """Applies to every authenticated request."""
    if settings.rate_limits_enabled:
        _enforce(
            limiter, f"all:{user.id}", settings.rate_limit_per_minute, 60, "requests per minute"
        )


def expensive_rate_limit(
    user: CurrentUser,
    limiter: Annotated[RateLimiter, Depends(get_rate_limiter)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> None:
    """For endpoints that run Insights queries or call Claude, which cost money."""
    if settings.rate_limits_enabled:
        _enforce(
            limiter,
            f"expensive:{user.id}",
            settings.expensive_rate_limit_per_hour,
            3600,
            "log queries and diagnoses per hour",
        )


def get_role_assumer(request: Request) -> RoleAssumer:
    return request.app.state.role_assumer


def get_connection_or_404(db: Session, connection_id: str, user: User) -> Connection:
    # Other users' connections are reported as not found, so IDs can't be probed.
    try:
        return connection_service.get_connection(db, connection_id, owner=user)
    except connection_service.ConnectionNotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Connection not found") from None
