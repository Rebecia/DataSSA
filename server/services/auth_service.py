from __future__ import annotations

from datetime import UTC, datetime, timedelta

import bcrypt
from jose import jwt
from sqlalchemy import select
from sqlalchemy.orm import Session

from server.config import get_settings
from server.db.models import User
from server.schemas.auth import TokenOut



def _now_utc() -> datetime:
    return datetime.now(UTC)


class AuthService:
    def hash_password(self, *, password: str) -> str:
        return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")

    def register(self, db: Session, *, username: str, password: str, role: str = "user") -> User:
        if len(password) < 8:
            raise ValueError("password_too_short")

        existing = db.execute(select(User).where(User.username == username)).scalar_one_or_none()
        if existing is not None:
            raise ValueError("username_taken")

        user = User(
            username=username,
            password_hash=self.hash_password(password=password),
            role=role,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        return user

    def verify_password(self, *, password: str, password_hash: str) -> bool:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))

    def authenticate(self, db: Session, *, username: str, password: str) -> User | None:
        user = db.execute(select(User).where(User.username == username)).scalar_one_or_none()
        if user is None:
            return None
        if not self.verify_password(password=password, password_hash=user.password_hash):
            return None
        return user

    def create_access_token(self, *, username: str, role: str, expires_minutes: int | None = None) -> str:
        settings = get_settings()
        expire_minutes = expires_minutes if expires_minutes is not None else settings.jwt_expire_minutes
        expire_at = _now_utc() + timedelta(minutes=expire_minutes)
        payload = {"sub": username, "role": role, "exp": expire_at}
        return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)

    def login(self, db: Session, *, username: str, password: str) -> TokenOut:
        user = self.authenticate(db, username=username, password=password)
        if user is None:
            raise ValueError("invalid_credentials")
        token = self.create_access_token(username=user.username, role=user.role)
        return TokenOut(access_token=token)
