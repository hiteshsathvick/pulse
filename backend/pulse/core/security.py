import uuid
from datetime import UTC, datetime, timedelta

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from pulse.core.config import get_settings
from pulse.models import User
from pulse.repositories.postgres import session_scope

_JWT_ALGORITHM = "HS256"
_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except VerifyMismatchError:
        return False


class InvalidAccessToken(Exception):
    pass


def create_access_token(user_id: uuid.UUID, *, expires_delta: timedelta | None = None) -> str:
    settings = get_settings()
    delta = expires_delta or timedelta(minutes=settings.jwt_access_token_ttl_minutes)
    now = datetime.now(UTC)
    payload = {"sub": str(user_id), "type": "access", "iat": now, "exp": now + delta}
    return jwt.encode(payload, settings.jwt_secret, algorithm=_JWT_ALGORITHM)


def decode_access_token(token: str) -> uuid.UUID:
    settings = get_settings()
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[_JWT_ALGORITHM])
    except jwt.PyJWTError as exc:
        raise InvalidAccessToken(str(exc)) from exc
    if payload.get("type") != "access":
        raise InvalidAccessToken("not an access token")
    try:
        return uuid.UUID(payload["sub"])
    except (KeyError, ValueError) as exc:
        raise InvalidAccessToken("malformed subject claim") from exc


_bearer_scheme = HTTPBearer(auto_error=False)


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> User:
    """`auto_error=False` on the scheme, so a missing Authorization header
    reaches here and gets the same 401 as an invalid one -- FastAPI's
    HTTPBearer otherwise raises 403 for "missing" vs 401 for "invalid",
    which is an inconsistency callers shouldn't have to handle."""
    unauthorized = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token"
    )
    if credentials is None:
        raise unauthorized

    try:
        user_id = decode_access_token(credentials.credentials)
    except InvalidAccessToken as exc:
        raise unauthorized from exc

    async with session_scope() as session:
        user = await session.get(User, user_id)
    if user is None or not user.is_active:
        raise unauthorized
    return user
