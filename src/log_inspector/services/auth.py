"""Users and API keys.

A key looks like "li_<prefix>_<secret>". The prefix is stored as-is to find the key; only a
SHA-256 hash of the whole key is stored. Keys are long random strings, so a fast hash is
safe here (slow hashes like bcrypt matter for guessable passwords).
"""

import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from log_inspector.models import ApiKey, Connection, User

KEY_SCHEME = "li"
# Don't write last_used_at on every request; once a minute is precise enough.
_LAST_USED_RESOLUTION = timedelta(minutes=1)


class ApiKeyNotFound(Exception):
    pass


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def create_user(db: Session, name: str) -> User:
    user = User(name=name)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def get_or_create_cognito_user(db: Session, claims: dict) -> User:
    """The user for a verified Cognito token; created on their first request."""
    sub = claims["sub"]
    user = db.scalar(select(User).where(User.cognito_sub == sub))
    if user is None:
        # Access tokens carry the username, not the email or display name.
        user = User(name=str(claims.get("username") or sub)[:100], cognito_sub=sub)
        db.add(user)
        db.commit()
        db.refresh(user)
    return user


def create_api_key(db: Session, user: User, name: str) -> tuple[ApiKey, str]:
    """Create a key and return it with its plaintext, which can't be recovered later."""
    prefix = secrets.token_hex(6)
    plaintext = f"{KEY_SCHEME}_{prefix}_{secrets.token_urlsafe(32)}"
    api_key = ApiKey(user_id=user.id, name=name, prefix=prefix, key_hash=hash_key(plaintext))
    db.add(api_key)
    db.commit()
    db.refresh(api_key)
    return api_key, plaintext


def authenticate(db: Session, key: str) -> User | None:
    scheme, _, rest = key.partition("_")
    prefix, _, secret = rest.partition("_")
    if scheme != KEY_SCHEME or not prefix or not secret:
        return None
    api_key = db.scalar(select(ApiKey).where(ApiKey.prefix == prefix))
    if api_key is None or api_key.revoked_at is not None:
        return None
    if not hmac.compare_digest(api_key.key_hash, hash_key(key)):
        return None

    now = datetime.now(UTC)
    if api_key.last_used_at is None or now - api_key.last_used_at > _LAST_USED_RESOLUTION:
        api_key.last_used_at = now
        db.commit()
    return api_key.user


def list_api_keys(db: Session, user: User) -> list[ApiKey]:
    return list(
        db.scalars(select(ApiKey).where(ApiKey.user_id == user.id).order_by(ApiKey.created_at))
    )


def revoke_api_key(db: Session, user: User, key_id: str) -> ApiKey:
    api_key = db.get(ApiKey, key_id)
    if api_key is None or api_key.user_id != user.id:
        raise ApiKeyNotFound(key_id)
    if api_key.revoked_at is None:
        api_key.revoked_at = datetime.now(UTC)
        db.commit()
    return api_key


def claim_unowned_connections(db: Session, user: User) -> int:
    """Give connections created before users existed to this user."""
    result = db.execute(
        update(Connection).where(Connection.owner_id.is_(None)).values(owner_id=user.id)
    )
    db.commit()
    return result.rowcount
