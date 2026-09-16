import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pulse.core.config import get_settings
from pulse.core.security import create_access_token, hash_password, verify_password
from pulse.models import RefreshToken, User
from pulse.repositories.postgres import session_scope


class EmailAlreadyRegistered(Exception):
    pass


class InvalidCredentials(Exception):
    """Deliberately the same error for "no such user" and "wrong password" --
    login must not let a caller distinguish the two."""


class InvalidRefreshToken(Exception):
    pass


@dataclass(frozen=True)
class TokenPair:
    access_token: str
    refresh_token: str


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def _issue_token_pair(user_id: uuid.UUID, session: AsyncSession) -> TokenPair:
    settings = get_settings()
    refresh_token = secrets.token_urlsafe(48)
    session.add(
        RefreshToken(
            user_id=user_id,
            token_hash=_hash_token(refresh_token),
            expires_at=datetime.now(UTC) + timedelta(days=settings.jwt_refresh_token_ttl_days),
        )
    )
    return TokenPair(access_token=create_access_token(user_id), refresh_token=refresh_token)


async def register(email: str, password: str, name: str) -> User:
    async with session_scope() as session:
        existing = await session.scalar(select(User).where(User.email == email))
        if existing is not None:
            raise EmailAlreadyRegistered()

        user = User(email=email, password_hash=hash_password(password), name=name)
        session.add(user)
        await session.commit()
        return user


async def login(email: str, password: str) -> TokenPair:
    async with session_scope() as session:
        user = await session.scalar(select(User).where(User.email == email))
        if user is None or not user.is_active or not verify_password(password, user.password_hash):
            raise InvalidCredentials()

        pair = await _issue_token_pair(user.id, session)
        await session.commit()
        return pair


async def refresh(refresh_token: str) -> TokenPair:
    token_hash = _hash_token(refresh_token)
    async with session_scope() as session:
        existing = await session.scalar(
            select(RefreshToken).where(RefreshToken.token_hash == token_hash)
        )
        now = datetime.now(UTC)
        if existing is None or existing.revoked_at is not None or existing.expires_at < now:
            raise InvalidRefreshToken()

        # Rotation: this token dies the instant it's used, whether or not the
        # caller ever sees the new pair.
        existing.revoked_at = now
        pair = await _issue_token_pair(existing.user_id, session)
        await session.commit()
        return pair


async def logout(refresh_token: str) -> None:
    """Idempotent by design: an already-revoked or unknown token is not an
    error -- logging out twice, or with a stale token, should just succeed."""
    token_hash = _hash_token(refresh_token)
    async with session_scope() as session:
        existing = await session.scalar(
            select(RefreshToken).where(RefreshToken.token_hash == token_hash)
        )
        if existing is not None and existing.revoked_at is None:
            existing.revoked_at = datetime.now(UTC)
            await session.commit()
