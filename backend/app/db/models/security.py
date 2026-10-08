"""Local account, server-side session, and audit models."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..base import (
    AuditResult,
    Base,
    FundAccessMode,
    UserRole,
    UserStatus,
    created_at_column,
    enum_column,
    updated_at_column,
)


class User(Base):
    __tablename__ = "user_account"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(255))
    password_hash: Mapped[str] = mapped_column(String(500), nullable=False)
    role: Mapped[str] = enum_column(UserRole, nullable=False)
    fund_access_mode: Mapped[str] = enum_column(
        FundAccessMode,
        nullable=False,
        default=FundAccessMode.ALL,
        server_default=FundAccessMode.ALL.value,
    )
    status: Mapped[str] = enum_column(
        UserStatus, nullable=False, default=UserStatus.ACTIVE
    )
    failed_login_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = updated_at_column()

    sessions: Mapped[list[UserSession]] = relationship(
        back_populates="user", passive_deletes=True
    )
    fund_access: Mapped[list[UserFundAccess]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class UserFundAccess(Base):
    """Explicit product grants for an account in selected-product mode."""

    __tablename__ = "user_fund_access"

    user_id: Mapped[int] = mapped_column(
        ForeignKey("user_account.id", ondelete="RESTRICT"), primary_key=True
    )
    fund_id: Mapped[int] = mapped_column(
        ForeignKey("fund.id", ondelete="RESTRICT"), primary_key=True, index=True
    )
    created_at: Mapped[datetime] = created_at_column()

    user: Mapped[User] = relationship(back_populates="fund_access")


class SystemState(Base):
    """Singleton state used for database-serialized one-time operations."""

    __tablename__ = "system_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    initialized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    settings: Mapped[dict[str, object] | None] = mapped_column(JSON)


class UserSession(Base):
    __tablename__ = "user_session"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("user_account.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    device_info: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = created_at_column()

    user: Mapped[User] = relationship(back_populates="sessions")


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    actor_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("user_account.id", ondelete="SET NULL"), index=True
    )
    action: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    resource_type: Mapped[str] = mapped_column(String(100), nullable=False)
    resource_id: Mapped[str | None] = mapped_column(String(100))
    summary: Mapped[dict[str, object] | None] = mapped_column(JSON)
    reason: Mapped[str | None] = mapped_column(Text)
    result: Mapped[str] = enum_column(AuditResult, nullable=False)
    created_at: Mapped[datetime] = created_at_column()
