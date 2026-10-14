from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from log_inspector.api.deps import CurrentUser
from log_inspector.db import get_db
from log_inspector.schemas import ApiKeyCreate, ApiKeyCreated, ApiKeyOut, UserOut
from log_inspector.services import auth as service

router = APIRouter(prefix="/me", tags=["account"])

DbSession = Annotated[Session, Depends(get_db)]


@router.get("")
def get_me(user: CurrentUser) -> UserOut:
    """The signed-in user (API key or login token)."""
    return UserOut.model_validate(user)


@router.get("/api-keys")
def list_api_keys(user: CurrentUser, db: DbSession) -> list[ApiKeyOut]:
    return [ApiKeyOut.model_validate(k) for k in service.list_api_keys(db, user)]


@router.post("/api-keys", status_code=status.HTTP_201_CREATED)
def create_api_key(body: ApiKeyCreate, user: CurrentUser, db: DbSession) -> ApiKeyCreated:
    """Create another key, e.g. one per tool, so each can be revoked on its own."""
    api_key, plaintext = service.create_api_key(db, user, body.name)
    return ApiKeyCreated(**ApiKeyOut.model_validate(api_key).model_dump(), key=plaintext)


@router.delete("/api-keys/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
def revoke_api_key(key_id: str, user: CurrentUser, db: DbSession) -> None:
    """Revoke a key. It stops working immediately, including if it's the one in use."""
    try:
        service.revoke_api_key(db, user, key_id)
    except service.ApiKeyNotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "API key not found") from None
