"""Raw source and import batch models."""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..base import (
    Base,
    ImportBatchStatus,
    JobStatus,
    SourceType,
    created_at_column,
    enum_column,
)


class SourceMessage(Base):
    __tablename__ = "source_message"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    external_message_id: Mapped[str] = mapped_column(
        String(255), unique=True, nullable=False
    )
    sender: Mapped[str] = mapped_column(String(255), nullable=False)
    subject: Mapped[str | None] = mapped_column(String(500))
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    sync_batch: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = created_at_column()


class SourceFile(Base):
    __tablename__ = "source_file"
    __table_args__ = (
        CheckConstraint("length(file_hash) = 64", name="ck_source_file_hash_length"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    original_filename: Mapped[str] = mapped_column(String(500), nullable=False)
    file_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    file_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    file_extension: Mapped[str] = mapped_column(String(20), nullable=False)
    source_type: Mapped[str] = enum_column(SourceType, nullable=False)
    object_name: Mapped[str] = mapped_column(String(500), unique=True, nullable=False)
    recognized_fund_id: Mapped[int | None] = mapped_column(
        ForeignKey("fund.id", ondelete="SET NULL"), nullable=True, index=True
    )
    retention_expires_on: Mapped[date | None] = mapped_column(Date)
    created_at: Mapped[datetime] = created_at_column()

    batch_links: Mapped[list[ImportBatchFile]] = relationship(
        back_populates="source_file", passive_deletes=True
    )


class ImportBatch(Base):
    __tablename__ = "import_batch"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_type: Mapped[str] = enum_column(SourceType, nullable=False)
    source_message_id: Mapped[int | None] = mapped_column(
        ForeignKey("source_message.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("user_account.id", ondelete="SET NULL"), index=True
    )
    file_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = enum_column(
        ImportBatchStatus, nullable=False, default=ImportBatchStatus.CREATED
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at_column()

    file_links: Mapped[list[ImportBatchFile]] = relationship(
        back_populates="batch", passive_deletes=True
    )


class ImportBatchFile(Base):
    __tablename__ = "import_batch_file"
    __table_args__ = (
        UniqueConstraint(
            "batch_id", "source_file_id", name="uq_import_batch_source_file"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    batch_id: Mapped[int] = mapped_column(
        ForeignKey("import_batch.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    source_file_id: Mapped[int] = mapped_column(
        ForeignKey("source_file.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    duplicate: Mapped[bool] = mapped_column(nullable=False, default=False)
    created_at: Mapped[datetime] = created_at_column()

    batch: Mapped[ImportBatch] = relationship(back_populates="file_links")
    source_file: Mapped[SourceFile] = relationship(back_populates="batch_links")


class BackgroundJob(Base):
    __tablename__ = "background_job"
    __table_args__ = (
        UniqueConstraint("job_type", "resource_id", name="uq_job_type_resource"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_type: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    status: Mapped[str] = enum_column(
        JobStatus, nullable=False, default=JobStatus.PENDING
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    lease_token: Mapped[str | None] = mapped_column(String(64))
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(100))
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_requested: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    resource_id: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[datetime] = created_at_column()
