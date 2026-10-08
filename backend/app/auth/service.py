"""Password, lockout, session, account administration, and audit logic."""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, VerifyMismatchError
from sqlalchemy import func, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.base import AuditResult, FundAccessMode, UserRole, UserStatus
from app.db.models import AuditLog, Fund, SystemState, User, UserFundAccess, UserSession

SESSION_TTL = timedelta(hours=12)
LOCKOUT_THRESHOLD = 5
LOCKOUT_DURATION = timedelta(minutes=15)
DUMMY_PASSWORD_HASH = (
    "$argon2id$v=19$m=65536,t=3,p=4$f/2/WPIFPDH2/p/i7cvVDw$"
    "3lSl2CF5KH6MNK9uqN/Gormtr/cCA1h9Ec4JShzlIUI"
)


class InvalidCredentialsError(Exception):
    """Raised for every externally indistinguishable login failure."""


class InitializationClosedError(Exception):
    """Raised when the first-admin initialization has already completed."""


class DuplicateUsernameError(Exception):
    """Raised when an administrator creates an existing username."""


class InvalidOldPasswordError(Exception):
    """Raised when a password change cannot verify the current password."""


class AccountProtectionError(LookupError):
    """Raised when an account change would remove the last usable admin."""


class InvalidFundAccessError(ValueError):
    """Raised for an inconsistent product scope or nonexistent product grant."""


class FundAccessForbiddenError(PermissionError):
    """Raised when an actor cannot change the target account's product grants."""


@dataclass(frozen=True, slots=True)
class LoginResult:
    user: User
    session: UserSession
    raw_token: str


class AuthService:
    """Deep module for local account and server-side session behavior."""

    InvalidCredentials = InvalidCredentialsError
    InitializationClosed = InitializationClosedError
    DuplicateUsername = DuplicateUsernameError
    InvalidOldPassword = InvalidOldPasswordError
    AccountProtection = AccountProtectionError
    InvalidFundAccess = InvalidFundAccessError
    FundAccessForbidden = FundAccessForbiddenError

    def __init__(self, session: Session) -> None:
        self.session = session
        self.password_hasher = PasswordHasher()

    def initialize_admin(
        self,
        username: str,
        password: str,
        display_name: str | None = None,
    ) -> LoginResult:
        if self.session.scalar(select(func.count(User.id))) != 0:
            raise InitializationClosedError

        self._claim_initialization()

        user = User(
            username=self._normalize_username(username),
            display_name=display_name,
            password_hash=self.password_hasher.hash(password),
            role=UserRole.ADMIN,
            status=UserStatus.ACTIVE,
        )
        self.session.add(user)
        self.session.flush()
        self.record_audit(
            action="auth.initialize",
            resource_type="user",
            resource_id=str(user.id),
            actor_user_id=user.id,
        )
        return self._create_session(user)

    def _claim_initialization(self) -> None:
        state = self.session.get(SystemState, 1)
        if state is None:
            try:
                with self.session.begin_nested():
                    self.session.add(SystemState(id=1))
                    self.session.flush()
            except IntegrityError:
                state = self.session.get(SystemState, 1)
                if state is None:
                    raise

        result = cast(
            CursorResult[Any],
            self.session.execute(
                update(SystemState)
                .where(SystemState.id == 1, SystemState.initialized_at.is_(None))
                .values(initialized_at=datetime.now(UTC))
            ),
        )
        if result.rowcount != 1:
            raise InitializationClosedError
        self.session.flush()

    def authenticate(
        self,
        username: str,
        password: str,
        *,
        device_info: str | None = None,
        now: datetime | None = None,
    ) -> LoginResult:
        current_time = now or datetime.now(UTC)
        normalized_username = self._normalize_username(username)
        user = self._load_user(normalized_username)

        if user is None:
            self._verify_dummy_password(password)
            self._audit_login_failure(normalized_username, "invalid_credentials")
            self.session.flush()
            raise InvalidCredentialsError

        if user.status != UserStatus.ACTIVE:
            self._verify_dummy_password(password)
            self._audit_login_failure(normalized_username, "invalid_credentials")
            self.session.flush()
            raise InvalidCredentialsError

        if user.locked_until and current_time < self._as_utc(user.locked_until):
            self._verify_dummy_password(password)
            self._audit_login_failure(normalized_username, "locked")
            self.session.flush()
            raise InvalidCredentialsError

        try:
            self.password_hasher.verify(user.password_hash, password)
        except (VerifyMismatchError, VerificationError):
            user.failed_login_count += 1
            if user.failed_login_count >= LOCKOUT_THRESHOLD:
                user.locked_until = current_time + LOCKOUT_DURATION
            self._audit_login_failure(normalized_username, "invalid_credentials")
            self.session.flush()
            raise InvalidCredentialsError from None

        if self.password_hasher.check_needs_rehash(user.password_hash):
            user.password_hash = self.password_hasher.hash(password)
        user.failed_login_count = 0
        user.locked_until = None
        user.last_login_at = current_time
        login = self._create_session(user, device_info=device_info, now=current_time)
        self.record_audit(
            action="auth.login",
            resource_type="user",
            resource_id=str(user.id),
            actor_user_id=user.id,
        )
        self.session.flush()
        return login

    def authenticate_session(
        self, raw_token: str, *, now: datetime | None = None
    ) -> tuple[User, UserSession] | None:
        current_time = now or datetime.now(UTC)
        token_hash = self.hash_token(raw_token)
        user_session = self.session.scalar(
            select(UserSession).where(UserSession.token_hash == token_hash)
        )
        if user_session is None or user_session.revoked_at is not None:
            return None
        if self._as_utc(user_session.expires_at) <= current_time:
            return None
        user = user_session.user
        if user.status != UserStatus.ACTIVE:
            return None
        return user, user_session

    def logout(self, user: User, user_session: UserSession) -> None:
        if user_session.revoked_at is None:
            user_session.revoked_at = datetime.now(UTC)
        self.record_audit(
            action="auth.logout",
            resource_type="session",
            resource_id=str(user_session.id),
            actor_user_id=user.id,
        )
        self.session.flush()

    def change_password(
        self,
        user: User,
        current_session: UserSession,
        old_password: str,
        new_password: str,
    ) -> None:
        try:
            self.password_hasher.verify(user.password_hash, old_password)
        except (VerifyMismatchError, VerificationError):
            raise InvalidOldPasswordError from None

        user.password_hash = self.password_hasher.hash(new_password)
        user.failed_login_count = 0
        user.locked_until = None
        now = datetime.now(UTC)
        self.session.execute(
            update(UserSession)
            .where(
                UserSession.user_id == user.id,
                UserSession.id != current_session.id,
                UserSession.revoked_at.is_(None),
            )
            .values(revoked_at=now)
        )
        self.record_audit(
            action="auth.change_password",
            resource_type="user",
            resource_id=str(user.id),
            actor_user_id=user.id,
        )
        self.session.flush()

    def create_user(
        self,
        actor: User,
        username: str,
        password: str,
        role: UserRole,
        display_name: str | None = None,
        *,
        fund_access_mode: FundAccessMode = FundAccessMode.ALL,
        fund_ids: list[int] | None = None,
    ) -> User:
        normalized_username = self._normalize_username(username)
        if self.session.scalar(
            select(User.id).where(User.username == normalized_username)
        ):
            raise DuplicateUsernameError
        grants = self._validate_fund_access(role, fund_access_mode, fund_ids or [])
        user = User(
            username=normalized_username,
            display_name=display_name,
            password_hash=self.password_hasher.hash(password),
            role=role,
            fund_access_mode=fund_access_mode,
            fund_access=[UserFundAccess(fund_id=fund_id) for fund_id in grants],
            status=UserStatus.ACTIVE,
        )
        self.session.add(user)
        self.session.flush()
        self.record_audit(
            action="user.create",
            resource_type="user",
            resource_id=str(user.id),
            actor_user_id=actor.id,
            summary={
                "role": role.value,
                "fund_access_mode": fund_access_mode.value,
                "fund_ids": grants,
            },
        )
        return user

    def set_user_status(
        self,
        actor: User,
        user_id: int,
        status: UserStatus,
        *,
        reason: str | None = None,
    ) -> User:
        user = self._load_user_by_id(user_id)
        if user is None:
            raise LookupError(user_id)
        if user.id == actor.id and status == UserStatus.DISABLED:
            raise AccountProtectionError("admin_cannot_disable_self")
        if (
            status == UserStatus.DISABLED
            and user.role == UserRole.ADMIN
            and user.status == UserStatus.ACTIVE
            and self._active_admin_count() <= 1
        ):
            raise AccountProtectionError("last_active_admin_cannot_be_disabled")
        user.status = status
        if status == UserStatus.DISABLED:
            self._revoke_user_sessions(user.id)
        self.record_audit(
            action=f"user.{status.value}",
            resource_type="user",
            resource_id=str(user.id),
            actor_user_id=actor.id,
            reason=reason,
        )
        self.session.flush()
        return user

    def reset_password(
        self,
        actor: User,
        user_id: int,
        password: str,
        *,
        reason: str | None = None,
    ) -> User:
        user = self.session.get(User, user_id)
        if user is None:
            raise LookupError(user_id)
        user.password_hash = self.password_hasher.hash(password)
        user.failed_login_count = 0
        user.locked_until = None
        self._revoke_user_sessions(user.id)
        self.record_audit(
            action="user.reset_password",
            resource_type="user",
            resource_id=str(user.id),
            actor_user_id=actor.id,
            reason=reason,
        )
        self.session.flush()
        return user

    def change_role(
        self,
        actor: User,
        user_id: int,
        role: UserRole,
        *,
        reason: str | None = None,
    ) -> User:
        user = self._load_user_by_id(user_id)
        if user is None:
            raise LookupError(user_id)
        if user.id == actor.id and role != UserRole.ADMIN:
            raise AccountProtectionError("admin_cannot_downgrade_self")
        if (
            role != UserRole.ADMIN
            and user.role == UserRole.ADMIN
            and user.status == UserStatus.ACTIVE
            and self._active_admin_count() <= 1
        ):
            raise AccountProtectionError("last_active_admin_cannot_be_downgraded")
        previous_role = user.role
        previous_scope = self._fund_access_summary(user)
        user.role = role
        if role == UserRole.ADMIN:
            user.fund_access_mode = FundAccessMode.ALL
            user.fund_access.clear()
        self.record_audit(
            action="user.change_role",
            resource_type="user",
            resource_id=str(user.id),
            actor_user_id=actor.id,
            summary={
                "role": role.value,
                "previous_role": previous_role,
                "previous_fund_access": previous_scope,
                "fund_access": self._fund_access_summary(user),
            },
            reason=reason,
        )
        self.session.flush()
        return user

    def set_fund_access(
        self,
        actor: User,
        user_id: int,
        fund_access_mode: FundAccessMode,
        fund_ids: list[int],
        *,
        reason: str,
    ) -> User:
        if actor.status != UserStatus.ACTIVE or actor.role not in (
            UserRole.ADMIN,
            UserRole.BOSS,
        ):
            raise FundAccessForbiddenError("Cannot manage account product access")
        if actor.role == UserRole.BOSS and actor.id == user_id:
            raise FundAccessForbiddenError(
                "Boss cannot change their own product access"
            )
        user = self._load_user_by_id(user_id)
        if user is None:
            raise LookupError(user_id)
        if actor.role == UserRole.BOSS and user.role == UserRole.ADMIN:
            raise FundAccessForbiddenError(
                "Boss cannot change administrator product access"
            )
        if not reason.strip():
            raise InvalidFundAccessError("A reason is required")
        grants = self._validate_fund_access(user.role, fund_access_mode, fund_ids)
        previous_scope = self._fund_access_summary(user)
        user.fund_access_mode = fund_access_mode
        existing = {grant.fund_id: grant for grant in user.fund_access}
        user.fund_access = [
            existing[fund_id]
            if fund_id in existing
            else UserFundAccess(fund_id=fund_id)
            for fund_id in grants
        ]
        self.record_audit(
            action="user.change_fund_access",
            resource_type="user",
            resource_id=str(user.id),
            actor_user_id=actor.id,
            summary={
                "before": previous_scope,
                "after": self._fund_access_summary(user),
            },
            reason=reason.strip(),
        )
        self.session.flush()
        return user

    def _validate_fund_access(
        self, role: str, mode: FundAccessMode, fund_ids: list[int]
    ) -> list[int]:
        if mode not in (FundAccessMode.ALL, FundAccessMode.SELECTED):
            raise InvalidFundAccessError("Invalid product access mode")
        if role == UserRole.ADMIN and mode != FundAccessMode.ALL:
            raise InvalidFundAccessError("Administrators must have all product access")
        if any(type(fund_id) is not int or fund_id <= 0 for fund_id in fund_ids):
            raise InvalidFundAccessError("Product IDs must be positive integers")
        grants = sorted(set(fund_ids))
        if mode == FundAccessMode.ALL and grants:
            raise InvalidFundAccessError(
                "All-product access cannot include product IDs"
            )
        if grants:
            existing = set(
                self.session.scalars(select(Fund.id).where(Fund.id.in_(grants)))
            )
            if existing != set(grants):
                raise InvalidFundAccessError("One or more products do not exist")
        return grants

    @staticmethod
    def _fund_access_summary(user: User) -> dict[str, object]:
        return {
            "fund_access_mode": user.fund_access_mode,
            "fund_ids": sorted(grant.fund_id for grant in user.fund_access),
        }

    def revoke_sessions(
        self,
        actor: User,
        user_id: int,
        *,
        reason: str | None = None,
    ) -> User:
        user = self.session.get(User, user_id)
        if user is None:
            raise LookupError(user_id)
        self._revoke_user_sessions(user.id)
        self.record_audit(
            action="user.revoke_sessions",
            resource_type="user",
            resource_id=str(user.id),
            actor_user_id=actor.id,
            reason=reason,
        )
        self.session.flush()
        return user

    def record_audit(
        self,
        *,
        action: str,
        resource_type: str,
        resource_id: str | None = None,
        actor_user_id: int | None = None,
        summary: dict[str, object] | None = None,
        reason: str | None = None,
        result: AuditResult = AuditResult.SUCCESS,
    ) -> AuditLog:
        audit = AuditLog(
            actor_user_id=actor_user_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            summary=summary,
            reason=reason,
            result=result,
        )
        self.session.add(audit)
        return audit

    @staticmethod
    def hash_token(raw_token: str) -> str:
        return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()

    @staticmethod
    def _normalize_username(username: str) -> str:
        return username.strip().lower()

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    def _create_session(
        self,
        user: User,
        *,
        device_info: str | None = None,
        now: datetime | None = None,
    ) -> LoginResult:
        current_time = now or datetime.now(UTC)
        raw_token = secrets.token_urlsafe(32)
        user_session = UserSession(
            user_id=user.id,
            token_hash=self.hash_token(raw_token),
            expires_at=current_time + SESSION_TTL,
            device_info=device_info,
        )
        self.session.add(user_session)
        self.session.flush()
        return LoginResult(user=user, session=user_session, raw_token=raw_token)

    def _load_user(self, username: str) -> User | None:
        statement = select(User).where(User.username == username)
        if self._supports_row_locks:
            statement = statement.with_for_update()
        return self.session.scalar(statement)

    def _load_user_by_id(self, user_id: int) -> User | None:
        statement = select(User).where(User.id == user_id)
        if self._supports_row_locks:
            statement = statement.with_for_update()
        return self.session.scalar(statement)

    def _active_admin_count(self) -> int:
        statement = select(User.id).where(
            User.role == UserRole.ADMIN,
            User.status == UserStatus.ACTIVE,
        )
        if self._supports_row_locks:
            statement = statement.with_for_update()
        return len(self.session.scalars(statement).all())

    def _verify_dummy_password(self, password: str) -> None:
        try:
            self.password_hasher.verify(DUMMY_PASSWORD_HASH, password)
        except (VerifyMismatchError, VerificationError):
            pass

    def _audit_login_failure(self, username: str, reason: str) -> None:
        username_digest = hashlib.sha256(username.encode("utf-8")).hexdigest()[:16]
        self.record_audit(
            action="auth.login_failed",
            resource_type="authentication",
            summary={"username_hash": username_digest, "reason": reason},
            result=AuditResult.FAILURE,
        )

    def _revoke_user_sessions(self, user_id: int) -> None:
        self.session.execute(
            update(UserSession)
            .where(UserSession.user_id == user_id, UserSession.revoked_at.is_(None))
            .values(revoked_at=datetime.now(UTC))
        )

    @property
    def _supports_row_locks(self) -> bool:
        return (
            self.session.bind is not None
            and self.session.bind.dialect.name == "postgresql"
        )
