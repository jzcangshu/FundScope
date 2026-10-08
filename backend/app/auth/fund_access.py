"""Product visibility shared by every API that reads or changes product data."""

from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.base import FundAccessMode, UserRole
from app.db.models import User, UserFundAccess


def visible_fund_ids(session: Session, user: User) -> frozenset[int] | None:
    """Return current grants; None means all products, an empty set means none.

    Grants are queried from the database rather than saved in login sessions so
    changes take effect on an account's next request without signing in again.
    Unknown modes fail closed. Administrators always retain unrestricted access.
    """

    if user.role == UserRole.ADMIN or user.fund_access_mode == FundAccessMode.ALL:
        return None
    if user.fund_access_mode != FundAccessMode.SELECTED:
        return frozenset()
    return frozenset(
        session.scalars(
            select(UserFundAccess.fund_id).where(UserFundAccess.user_id == user.id)
        )
    )


def require_fund_access(session: Session, user: User, fund_id: int) -> None:
    allowed_ids = visible_fund_ids(session, user)
    if allowed_ids is not None and fund_id not in allowed_ids:
        # Do not reveal whether an inaccessible product exists.
        raise HTTPException(status_code=404, detail="Fund not found")


def require_unrestricted_access(session: Session, user: User) -> None:
    """Guard shared resources that cannot be safely attributed to one product."""

    if visible_fund_ids(session, user) is not None:
        raise HTTPException(
            status_code=403, detail="Unrestricted product access required"
        )
