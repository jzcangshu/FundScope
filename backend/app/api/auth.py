"""Local authentication and administrator account routes."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.auth.dependencies import (
    SESSION_COOKIE_NAME,
    AuthContext,
    get_auth_context,
    get_db,
    require_roles,
)
from app.auth.service import SESSION_TTL, AuthService
from app.db.base import FundAccessMode, UserRole, UserStatus
from app.db.models import Fund, User

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])
user_router = APIRouter(prefix="/api/v1/users", tags=["users"])

DatabaseSession = Annotated[Session, Depends(get_db)]
CurrentContext = Annotated[AuthContext, Depends(get_auth_context)]
AdminContext = Annotated[AuthContext, Depends(require_roles(UserRole.ADMIN))]
AccountPermissionContext = Annotated[
    AuthContext, Depends(require_roles(UserRole.ADMIN, UserRole.BOSS))
]
FundId = Annotated[int, Field(strict=True, gt=0)]

# Hide only these test usernames from the boss overview, regardless of role.
BOSS_OVERVIEW_HIDDEN_USERNAMES = ("admin", "boss", "operator")


class InitializeRequest(BaseModel):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=8, max_length=256)
    display_name: str | None = Field(default=None, max_length=255)


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=256)


class ChangePasswordRequest(BaseModel):
    old_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=8, max_length=256)


class CreateUserRequest(BaseModel):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=8, max_length=256)
    role: UserRole
    display_name: str | None = Field(default=None, max_length=255)
    fund_access_mode: FundAccessMode = FundAccessMode.ALL
    fund_ids: list[FundId] = Field(default_factory=list, max_length=10000)


class ChangeFundAccessRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fund_access_mode: FundAccessMode
    fund_ids: list[FundId] = Field(max_length=10000)
    reason: str = Field(min_length=1, max_length=2000)

    @field_validator("reason")
    @classmethod
    def nonblank_reason(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("A reason is required")
        return value.strip()


class ResetPasswordRequest(BaseModel):
    password: str = Field(min_length=8, max_length=256)
    reason: str | None = Field(default=None, max_length=2000)


class ChangeRoleRequest(BaseModel):
    role: UserRole
    reason: str | None = Field(default=None, max_length=2000)


class StatusChangeRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=2000)


class RevokeSessionsRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=2000)


def _user_data(user: User) -> dict[str, object]:
    navigation = {
        UserRole.ADMIN: ("dashboard", "funds", "imports", "reviews", "users"),
        UserRole.OPERATOR: ("dashboard", "funds", "imports", "reviews"),
        UserRole.VIEWER: ("dashboard", "risk", "funds", "adminRiskRules"),
        UserRole.BOSS: (
            "dashboard",
            "risk",
            "funds",
            "adminRiskRules",
            "accountPermissions",
        ),
    }
    mode = FundAccessMode.ALL if user.role == UserRole.ADMIN else user.fund_access_mode
    return {
        "id": user.id,
        "username": user.username,
        "display_name": user.display_name,
        "role": user.role,
        "status": user.status,
        "last_login_at": user.last_login_at,
        "navigation": navigation.get(UserRole(user.role), ("dashboard", "funds")),
        "fund_access_mode": mode,
        "fund_ids": (
            sorted(grant.fund_id for grant in user.fund_access)
            if mode == FundAccessMode.SELECTED
            else []
        ),
    }


def _set_session_cookie(request: Request, response: Response, raw_token: str) -> None:
    is_production = request.app.state.settings.environment.lower() == "production"
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=raw_token,
        max_age=int(SESSION_TTL.total_seconds()),
        httponly=True,
        secure=is_production,
        # SameSite=None in production so the Tauri desktop client (origin
        # http://tauri.localhost) can send the cookie cross-site; Lax would
        # block it on cross-origin fetch calls and the dashboard would
        # immediately bounce back to the login page.
        samesite="none" if is_production else "lax",
        path="/",
    )


@router.get("/status")
def authentication_status(session: DatabaseSession) -> dict[str, object]:
    initialized = session.scalar(select(User.id).limit(1)) is not None
    return {"data": {"initialized": initialized}}


@router.post("/initialize", status_code=status.HTTP_201_CREATED)
def initialize(
    payload: InitializeRequest,
    request: Request,
    response: Response,
    session: DatabaseSession,
) -> dict[str, object]:
    service = AuthService(session)
    try:
        login = service.initialize_admin(
            payload.username, payload.password, payload.display_name
        )
        session.commit()
    except (AuthService.InitializationClosed, IntegrityError) as exc:
        session.rollback()
        raise HTTPException(
            status_code=409, detail="Initialization already completed"
        ) from exc
    _set_session_cookie(request, response, login.raw_token)
    return {"data": _user_data(login.user)}


@router.post("/login")
def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    session: DatabaseSession,
) -> dict[str, object]:
    service = AuthService(session)
    try:
        login_result = service.authenticate(
            payload.username,
            payload.password,
            device_info=request.headers.get("user-agent"),
        )
        session.commit()
    except AuthService.InvalidCredentials as exc:
        session.commit()
        raise HTTPException(
            status_code=401, detail="Invalid username or password"
        ) from exc
    _set_session_cookie(request, response, login_result.raw_token)
    return {"data": _user_data(login_result.user)}


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(
    request: Request,
    response: Response,
    context: CurrentContext,
    session: DatabaseSession,
) -> Response:
    AuthService(session).logout(context.user, context.session)
    session.commit()
    response.delete_cookie(
        SESSION_COOKIE_NAME,
        path="/",
        httponly=True,
        secure=request.app.state.settings.environment.lower() == "production",
        samesite="none"
        if request.app.state.settings.environment.lower() == "production"
        else "lax",
    )
    response.status_code = status.HTTP_204_NO_CONTENT
    return response


@router.get("/me")
def me(context: CurrentContext) -> dict[str, object]:
    return {"data": _user_data(context.user)}


@router.post("/change-password")
def change_password(
    payload: ChangePasswordRequest,
    context: CurrentContext,
    session: DatabaseSession,
) -> dict[str, object]:
    service = AuthService(session)
    try:
        service.change_password(
            context.user,
            context.session,
            payload.old_password,
            payload.new_password,
        )
        session.commit()
    except AuthService.InvalidOldPassword as exc:
        session.rollback()
        raise HTTPException(
            status_code=400, detail="Current password is incorrect"
        ) from exc
    return {"data": {"changed": True}}


@user_router.post("", status_code=status.HTTP_201_CREATED)
def create_user(
    payload: CreateUserRequest,
    context: AdminContext,
    session: DatabaseSession,
) -> dict[str, object]:
    service = AuthService(session)
    try:
        user = service.create_user(
            context.user,
            payload.username,
            payload.password,
            payload.role,
            payload.display_name,
            fund_access_mode=payload.fund_access_mode,
            fund_ids=payload.fund_ids,
        )
        session.commit()
    except (AuthService.DuplicateUsername, IntegrityError) as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail="Username already exists") from exc
    except AuthService.InvalidFundAccess as exc:
        session.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"data": _user_data(user)}


@user_router.get("")
def list_users(
    _: AdminContext,
    session: DatabaseSession,
    q: str | None = Query(default=None, max_length=100),
    role: UserRole | None = Query(default=None),  # noqa: B008
    user_status: UserStatus | None = Query(default=None, alias="status"),  # noqa: B008
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> dict[str, object]:
    users, meta = _query_users(session, q, role, user_status, page, page_size)
    return {"data": [_user_data(user) for user in users], "meta": meta}


@user_router.get("/permissions")
def list_account_permissions(
    context: AccountPermissionContext,
    session: DatabaseSession,
    q: str | None = Query(default=None, max_length=100),
    role: UserRole | None = Query(default=None),  # noqa: B008
    user_status: UserStatus | None = Query(default=None, alias="status"),  # noqa: B008
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> dict[str, object]:
    """Inspect account grants without exposing account credentials.

    Product names describe grants across all accounts, even when the reader's
    own valuation access is limited. No valuation data is included here.
    """
    is_boss = context.user.role == UserRole.BOSS
    users, meta = _query_users(
        session,
        q,
        role,
        user_status,
        page,
        page_size,
        exclude_usernames=BOSS_OVERVIEW_HIDDEN_USERNAMES if is_boss else (),
        active_only=is_boss,
    )
    fund_ids = {
        grant.fund_id
        for user in users
        if user.role != UserRole.ADMIN
        and user.fund_access_mode == FundAccessMode.SELECTED
        for grant in user.fund_access
    }
    funds = (
        {
            fund.id: _permission_fund_data(fund)
            for fund in session.scalars(select(Fund).where(Fund.id.in_(fund_ids)))
        }
        if fund_ids
        else {}
    )
    return {
        "data": [
            {
                **_user_data(user),
                "funds": [
                    funds[grant.fund_id]
                    for grant in sorted(
                        user.fund_access, key=lambda grant: grant.fund_id
                    )
                    if grant.fund_id in funds
                ]
                if user.role != UserRole.ADMIN
                and user.fund_access_mode == FundAccessMode.SELECTED
                else [],
            }
            for user in users
        ],
        "meta": meta,
    }


def _permission_fund_data(fund: Fund) -> dict[str, object]:
    return {
        "id": fund.id,
        "name": fund.standard_name,
        "product_code": fund.product_code,
        "status": fund.status,
    }


@user_router.get("/permissions/funds")
def list_permission_funds(
    _: AccountPermissionContext,
    session: DatabaseSession,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=100, ge=1, le=100),
) -> dict[str, object]:
    """Product names for assigning grants, including inactive and unassigned products.

    This catalog grants no access to valuations or other product data.
    """
    funds = session.scalars(
        select(Fund)
        .order_by(Fund.standard_name, Fund.id)
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    return {
        "data": [_permission_fund_data(fund) for fund in funds],
        "meta": {
            "page": page,
            "page_size": page_size,
            "total": session.scalar(select(func.count(Fund.id))) or 0,
        },
    }


def _query_users(
    session: Session,
    q: str | None,
    role: UserRole | None,
    user_status: UserStatus | None,
    page: int,
    page_size: int,
    *,
    exclude_usernames: tuple[str, ...] = (),
    active_only: bool = False,
) -> tuple[list[User], dict[str, int]]:
    statement = (
        select(User)
        .options(selectinload(User.fund_access))
        .order_by(User.username, User.id)
    )
    count_statement = select(func.count(User.id))
    if active_only:
        statement = statement.where(User.status == UserStatus.ACTIVE)
        count_statement = count_statement.where(User.status == UserStatus.ACTIVE)
    if exclude_usernames:
        predicate = User.username.not_in(exclude_usernames)
        statement = statement.where(predicate)
        count_statement = count_statement.where(predicate)
    if q:
        predicate = or_(
            User.username.contains(q.strip(), autoescape=True),
            User.display_name.contains(q.strip(), autoescape=True),
        )
        statement = statement.where(predicate)
        count_statement = count_statement.where(predicate)
    if role is not None:
        statement = statement.where(User.role == role)
        count_statement = count_statement.where(User.role == role)
    if user_status is not None:
        statement = statement.where(User.status == user_status)
        count_statement = count_statement.where(User.status == user_status)
    offset = (page - 1) * page_size
    total = session.scalar(count_statement) or 0
    users = list(session.scalars(statement.offset(offset).limit(page_size)))
    return users, {"page": page, "page_size": page_size, "total": total}


def _set_status(
    user_id: int,
    new_status: UserStatus,
    context: AuthContext,
    session: Session,
    *,
    reason: str | None = None,
) -> dict[str, object]:
    try:
        user = AuthService(session).set_user_status(
            context.user, user_id, new_status, reason=reason
        )
        session.commit()
    except AuthService.AccountProtection as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except LookupError as exc:
        session.rollback()
        raise HTTPException(status_code=404, detail="User not found") from exc
    return {"data": _user_data(user)}


@user_router.patch("/{user_id}/fund-access")
def change_fund_access(
    user_id: int,
    payload: ChangeFundAccessRequest,
    context: AccountPermissionContext,
    session: DatabaseSession,
) -> dict[str, object]:
    try:
        user = AuthService(session).set_fund_access(
            context.user,
            user_id,
            payload.fund_access_mode,
            payload.fund_ids,
            reason=payload.reason,
        )
        session.commit()
    except AuthService.FundAccessForbidden as exc:
        session.rollback()
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except AuthService.InvalidFundAccess as exc:
        session.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except LookupError as exc:
        session.rollback()
        raise HTTPException(status_code=404, detail="User not found") from exc
    return {"data": _user_data(user)}


@user_router.post("/{user_id}/disable")
def disable_user(
    user_id: int,
    context: AdminContext,
    session: DatabaseSession,
    payload: StatusChangeRequest | None = None,
) -> dict[str, object]:
    reason = payload.reason if payload else None
    return _set_status(user_id, UserStatus.DISABLED, context, session, reason=reason)


@user_router.post("/{user_id}/enable")
def enable_user(
    user_id: int,
    context: AdminContext,
    session: DatabaseSession,
) -> dict[str, object]:
    return _set_status(user_id, UserStatus.ACTIVE, context, session)


@user_router.post("/{user_id}/reset-password")
def reset_password(
    user_id: int,
    payload: ResetPasswordRequest,
    context: AdminContext,
    session: DatabaseSession,
) -> dict[str, object]:
    try:
        user = AuthService(session).reset_password(
            context.user, user_id, payload.password, reason=payload.reason
        )
        session.commit()
    except LookupError as exc:
        session.rollback()
        raise HTTPException(status_code=404, detail="User not found") from exc
    return {"data": _user_data(user)}


@user_router.patch("/{user_id}/role")
def change_role(
    user_id: int,
    payload: ChangeRoleRequest,
    context: AdminContext,
    session: DatabaseSession,
) -> dict[str, object]:
    try:
        user = AuthService(session).change_role(
            context.user, user_id, payload.role, reason=payload.reason
        )
        session.commit()
    except AuthService.AccountProtection as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except LookupError as exc:
        session.rollback()
        raise HTTPException(status_code=404, detail="User not found") from exc
    return {"data": _user_data(user)}


@user_router.post("/{user_id}/revoke-sessions")
def revoke_sessions(
    user_id: int,
    context: AdminContext,
    session: DatabaseSession,
    payload: RevokeSessionsRequest | None = None,
) -> dict[str, object]:
    try:
        reason = payload.reason if payload else None
        user = AuthService(session).revoke_sessions(
            context.user, user_id, reason=reason
        )
        session.commit()
    except LookupError as exc:
        session.rollback()
        raise HTTPException(status_code=404, detail="User not found") from exc
    return {"data": _user_data(user)}
