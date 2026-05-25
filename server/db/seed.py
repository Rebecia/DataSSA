"""开发用种子数据（步骤 4 可选）。用法：python -m server.db.seed"""

from __future__ import annotations

import os
import sys

import bcrypt
from sqlalchemy import select

from server.db.models import User
from server.db.session import SessionLocal


def _hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def seed_admin(*, username: str = "admin", password: str = "admin12345") -> None:
    if len(password) < 8:
        raise ValueError("password must be at least 8 characters")

    db = SessionLocal()
    try:
        existing = db.scalar(select(User).where(User.username == username))
        if existing:
            print(f"skip: user {username!r} already exists (role={existing.role})")
            return
        user = User(
            username=username,
            password_hash=_hash_password(password),
            role="admin",
        )
        db.add(user)
        db.commit()
        print(f"created admin user: {username!r}")
    finally:
        db.close()


def main() -> None:
    username = (os.getenv("SEED_ADMIN_USER") or "admin").strip()
    password = (os.getenv("SEED_ADMIN_PASSWORD") or "admin12345").strip()
    seed_admin(username=username, password=password)


if __name__ == "__main__":
    main()
