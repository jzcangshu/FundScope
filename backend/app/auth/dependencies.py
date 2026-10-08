"""FastAPI dependencies for database sessions and role authorization."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.db.base import UserRole
from app.db.models import User, UserSession

from .service import AuthService

SESSION_COOKIE_NAME = "fund_session"


@dataclass(frozen=True, slots=True)
class AuthContext:
    user: User
    session: UserSession


def get_db(request: Request) -> Iterator[Session]:
    with Session(request.app.state.db_engine) as session:
        yield session


def get_auth_context(
    request: Request,
    session: Session = Depends(get_db),  # noqa: B008
) -> AuthContext:
    raw_token = request.cookies.get(SESSION_COOKIE_NAME)
    if not raw_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated"
        )
    authenticated = AuthService(session).authenticate_session(raw_token)
    if authenticated is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated"
        )
    user, user_session = authenticated
    return AuthContext(user=user, session=user_session)


def require_roles(*roles: UserRole) -> Callable[[AuthContext], AuthContext]:
    allowed_roles = set(roles)

    def dependency(
        context: AuthContext = Depends(get_auth_context),  # noqa: B008
    ) -> AuthContext:
        if context.user.role not in allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden"
            )
        return context

    return dependency
