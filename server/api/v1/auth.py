from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from server.config import get_settings
from server.deps import get_current_user, get_db
from server.schemas.auth import LoginIn, RegisterIn, TokenOut, UserOut
from server.services.auth_service import AuthService

router = APIRouter(prefix="/auth", tags=["auth"])
_service = AuthService()


@router.post("/register", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def register(body: RegisterIn, db: Session = Depends(get_db)) -> UserOut:
    settings = get_settings()
    if not settings.allow_register:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="register disabled")
    try:
        user = _service.register(db, username=body.username, password=body.password)
    except ValueError as exc:
        code = str(exc)
        if code == "username_taken":
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="username taken") from exc
        if code == "password_too_short":
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="password too short") from exc
        raise
    return UserOut.model_validate(user)


@router.post("/login", response_model=TokenOut)
def login(body: LoginIn, db: Session = Depends(get_db)) -> TokenOut:
    try:
        return _service.login(db, username=body.username, password=body.password)
    except ValueError as exc:
        if str(exc) == "invalid_credentials":
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid credentials") from exc
        raise


@router.get("/me", response_model=UserOut)
def me(current_user=Depends(get_current_user)) -> UserOut:
    return UserOut.model_validate(current_user)
