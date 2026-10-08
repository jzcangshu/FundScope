from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.auth.dependencies import get_db
from app.db.base import (
    Base,
    FundStatus,
    UserRole,
    UserStatus,
    ValidationLevel,
    ValuationStatus,
)
from app.db.models import (
    Fund,
    FundDailySnapshot,
    PositionDaily,
    User,
    ValidationResult,
    ValuationVersion,
)
from app.db.session import create_engine
from app.main import create_app
from app.publishing import PublishingService


@pytest.fixture()
def app_and_engine(tmp_path) -> Iterator[tuple[FastAPI, object]]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    app = create_app()
    app.state.settings = replace(
        app.state.settings,
        environment="test",
        upload_temp_dir=str(tmp_path / "temp"),
        source_storage_dir=str(tmp_path / "source"),
        max_upload_bytes=1024 * 1024,
    )

    def override_get_db() -> Iterator[Session]:
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    yield app, engine
    app.dependency_overrides.clear()
    engine.dispose()


@pytest.fixture()
def admin_client(app_and_engine: tuple[FastAPI, object]) -> Iterator[TestClient]:
    app, _ = app_and_engine
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/auth/initialize",
            json={"username": "admin", "password": "correct horse"},
        )
        assert response.status_code == 201
        yield client


def seed_published_fund(
    engine: object,
    *,
    name: str = "梦溪一号",
    valuation_date: date = date(2026, 8, 25),
    unit_nav: Decimal = Decimal("1.25"),
    daily_return: Decimal = Decimal("0.01"),
    fund_status: FundStatus = FundStatus.ACTIVE,
    position_count: int = 1,
) -> tuple[int, int]:
    with Session(engine) as session:
        from sqlalchemy import select

        actor = session.scalar(select(User).where(User.username == "admin"))
        assert actor is not None
        fund = Fund(standard_name=name, status=fund_status)
        session.add(fund)
        session.flush()
        version = ValuationVersion(
            fund_id=fund.id,
            valuation_date=valuation_date,
            version_no=1,
            status=ValuationStatus.PUBLISHABLE,
        )
        session.add(version)
        session.flush()
        session.add(
            FundDailySnapshot(
                valuation_version_id=version.id,
                total_assets=Decimal(100000),
                total_liabilities=Decimal(10000),
                net_asset_value=Decimal(90000),
                unit_nav=unit_nav,
                cumulative_unit_nav=unit_nav,
                previous_unit_nav=unit_nav - Decimal("0.01"),
                daily_return=daily_return,
            )
        )
        for index in range(position_count):
            session.add(
                PositionDaily(
                    valuation_version_id=version.id,
                    security_code=f"{index + 1:06d}",
                    security_name="测试证券",
                    market_value=Decimal(1000 * (index + 1)),
                    nav_weight=Decimal("0.011111"),
                )
            )
        session.add(
            ValidationResult(
                valuation_version_id=version.id,
                rule_code="test_rule",
                level=ValidationLevel.INFO,
                message="校验通过",
            )
        )
        session.flush()
        session.commit()
        PublishingService(session).publish_version(
            version.id,
            actor_user_id=actor.id,
            actor_label=actor.username,
            reason="接口测试发布",
        )
        session.commit()
        return fund.id, version.id


def seed_pending_version(
    engine: object,
    *,
    fund_name: str = "待复核产品",
    version_no: int = 1,
) -> tuple[int, int]:
    with Session(engine) as session:
        from sqlalchemy import select

        actor = session.scalar(select(User).where(User.username == "admin"))
        if actor is None:
            actor = User(
                username="admin",
                password_hash="not-used",
                role=UserRole.ADMIN,
                status=UserStatus.ACTIVE,
            )
            session.add(actor)
            session.flush()
        fund = Fund(standard_name=fund_name)
        session.add(fund)
        session.flush()
        version = ValuationVersion(
            fund_id=fund.id,
            valuation_date=date(2026, 8, 25),
            version_no=version_no,
            status=ValuationStatus.PENDING_REVIEW,
        )
        session.add(version)
        session.flush()
        session.add(
            ValidationResult(
                valuation_version_id=version.id,
                rule_code="identity",
                level=ValidationLevel.CRITICAL,
                message="需要复核",
            )
        )
        session.commit()
        return fund.id, version.id
