from __future__ import annotations

from datetime import date
from decimal import Decimal
from io import BytesIO

from openpyxl import Workbook
from sqlalchemy import event, select
from sqlalchemy.orm import Session

from app.auth.service import AuthService
from app.db.base import SourceType, ValuationStatus
from app.db.models import (
    AccountSubjectDaily,
    AnalysisRun,
    AuditLog,
    BackgroundJob,
    Fund,
    FundAlias,
    FundDailySnapshot,
    PositionDaily,
    ShareClassDailySnapshot,
    SubjectMapping,
    ValuationVersion,
)
from app.imports.processor import (
    _load_product_identity,
    _product_aliases,
    _resolve_fund,
    process_import_batch,
)
from app.imports.service import ImportService
from app.imports.tasks import process_next_job


def _valuation_xlsx(
    *,
    valuation_date: str = "2026-08-25",
    missing_share_assets: bool = False,
) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "估值表"
    sheet.append(["证券投资基金估值表"])
    sheet.append(["千帆一号___专用表"])
    sheet.append([f"估值日期：{valuation_date}"])
    sheet.append(
        [
            "科目代码",
            "科目名称",
            "数量",
            "单位成本",
            "成本",
            "成本占净值%",
            "市价",
            "市值",
            "市值占净值%",
            "估值增值",
            "停牌信息",
        ]
    )
    sheet.append(["1002", "股票投资"])
    sheet.append(["100201", "上交所_信用账户"])
    sheet.append(["10020101", "股票成本_上交所_信用账户"])
    sheet.append(["10020101600001", "测试证券", 10, 10, 100, 100, 11, 110, 110, 10, ""])
    sheet.append(["资产类合计", "", "", "", "", "", "", 100, "", "", ""])
    sheet.append(["负债类合计", "", "", "", "", "", "", 0, "", "", ""])
    sheet.append(["基金资产净值", "", "", "", "", "", "", 100, "", "", ""])
    share_code = "XY0001千帆一号A类"
    sheet.append(
        [
            f"基金资产净值:{share_code}",
            "",
            "",
            "",
            "",
            "",
            "",
            100,
            "",
            "",
            "",
        ]
    )
    sheet.append(
        [
            f"实收资本:{share_code}",
            "",
            "",
            "",
            "",
            "",
            "",
            80,
            "",
            "",
            "",
        ]
    )
    if missing_share_assets:
        missing_share_code = "XY0002千帆一号B类"
        sheet.append(
            [
                f"基金资产净值:{missing_share_code}",
                "",
                "",
                "",
                "",
                "",
                "",
                None,
                "",
                "",
                "",
            ]
        )
        sheet.append(
            [
                f"实收资本:{missing_share_code}",
                "",
                "",
                "",
                "",
                "",
                "",
                20,
                "",
                "",
                "",
            ]
        )
    sheet.append(["基金单位净值", 1])
    sheet.append([f"基金单位净值:{share_code}", 1])
    sheet.append(["累计单位净值", 1])
    sheet.append([f"累计单位净值:{share_code}", 1])
    sheet.append(["昨日单位净值", 0.99])
    sheet.append(["净值日增长率(%)", 1.010101])
    sheet.append([f"净值日增长率:{share_code}(%)", 1.010101])
    output = BytesIO()
    workbook.save(output)
    return output.getvalue()


def test_processor_persists_version_snapshot_positions_and_validation(
    app_and_engine: tuple[object, object],
) -> None:
    app, engine = app_and_engine
    with Session(engine) as session:
        actor = AuthService(session).initialize_admin("admin", "correct horse").user
        fund = Fund(standard_name="千帆一号")
        session.add(fund)
        session.add(
            SubjectMapping(
                subject_code_or_prefix="1002",
                standard_category="股票",
                include_in_holdings=True,
                rule_version="test-v1",
            )
        )
        session.flush()
        service = ImportService.from_settings(session, app.state.settings)
        batch = service.create_batch(SourceType.UPLOAD, actor.id)
        service.receive_upload(
            batch.id, "千帆一号 08月25日.xlsx", BytesIO(_valuation_xlsx()), actor.id
        )
        service.complete_batch(batch.id, actor.id)
        session.commit()

        result = process_import_batch(session, batch.id, app.state.settings)
        session.commit()

        assert result.processed_files == 1
        assert result.created_versions
        version = session.get(ValuationVersion, result.created_versions[0])
        assert version.status == ValuationStatus.PUBLISHED
        assert result.published_files == 1
        assert len(result.analysis_run_ids) == 1
        snapshot = session.scalar(
            select(FundDailySnapshot).where(
                FundDailySnapshot.valuation_version_id == version.id
            )
        )
        position = session.scalar(
            select(PositionDaily).where(
                PositionDaily.valuation_version_id == version.id
            )
        )
        subject = session.scalar(
            select(AccountSubjectDaily).where(
                AccountSubjectDaily.valuation_version_id == version.id,
                AccountSubjectDaily.raw_subject_code == "10020101600001",
            )
        )
        share_snapshot = session.scalar(
            select(ShareClassDailySnapshot).where(
                ShareClassDailySnapshot.valuation_version_id == version.id
            )
        )
        assert snapshot.net_asset_value == 100
        assert position.market_value == 110
        assert position.market == "上交所"
        assert position.account == "信用账户"
        assert position.original_subject_code == "10020101600001"
        assert position.source_worksheet == "估值表"
        assert position.source_row == 8
        assert subject.standard_category == "股票"
        assert subject.include_in_holdings is True
        assert subject.source_worksheet == "估值表"
        assert subject.source_row == 8
        assert share_snapshot.paid_in_capital == 80
        assert share_snapshot.daily_return == Decimal("0.01010101")


def test_processor_keeps_critical_version_for_manual_review(
    app_and_engine: tuple[object, object],
) -> None:
    app, engine = app_and_engine
    with Session(engine) as session:
        actor = AuthService(session).initialize_admin("admin", "correct horse").user
        fund = Fund(standard_name="千帆一号")
        session.add(fund)
        session.flush()
        service = ImportService.from_settings(session, app.state.settings)
        batch = service.create_batch(SourceType.UPLOAD, actor.id)
        service.receive_upload(
            batch.id,
            "千帆一号 08月25日.xlsx",
            BytesIO(_valuation_xlsx(missing_share_assets=True)),
            actor.id,
        )
        service.complete_batch(batch.id, actor.id)
        session.commit()

        result = process_import_batch(session, batch.id, app.state.settings)
        session.commit()

        version = session.get(ValuationVersion, result.created_versions[0])
        assert version is not None
        assert version.status == ValuationStatus.PENDING_REVIEW
        assert result.published_files == 0
        assert result.analysis_run_ids == ()


def test_processor_coalesces_clean_publications_by_fund(
    app_and_engine: tuple[object, object],
) -> None:
    app, engine = app_and_engine
    with Session(engine) as session:
        actor = AuthService(session).initialize_admin("admin", "correct horse").user
        fund = Fund(standard_name="千帆一号")
        session.add(fund)
        session.flush()
        service = ImportService.from_settings(session, app.state.settings)
        batch = service.create_batch(SourceType.UPLOAD, actor.id)
        for day in ("2026-08-25", "2026-08-26"):
            service.receive_upload(
                batch.id,
                f"千帆一号 {day}.xlsx",
                BytesIO(_valuation_xlsx(valuation_date=day)),
                actor.id,
            )
        service.complete_batch(batch.id, actor.id)
        session.commit()

        result = process_import_batch(session, batch.id, app.state.settings)
        session.commit()

        versions = session.scalars(
            select(ValuationVersion).order_by(ValuationVersion.valuation_date)
        ).all()
        runs = session.scalars(select(AnalysisRun)).all()
        jobs = session.scalars(
            select(BackgroundJob).where(
                BackgroundJob.job_type == "process_analysis_run"
            )
        ).all()
        publication_audits = session.scalars(
            select(AuditLog).where(AuditLog.action == "valuation.published")
        ).all()

        assert result.published_files == 2
        assert [version.status for version in versions] == [
            ValuationStatus.PUBLISHED,
            ValuationStatus.PUBLISHED,
        ]
        assert [version.published_by for version in versions] == [
            "system:auto-import",
            "system:auto-import",
        ]
        assert len(runs) == len(jobs) == len(result.analysis_run_ids) == 1
        assert runs[0].input_start_date == date(2026, 8, 25)
        assert runs[0].input_end_date is None
        assert runs[0].input_version_range == (
            f"fund:{fund.id};dates:2026-08-25..latest"
        )
        assert len(publication_audits) == 2
        assert all(
            audit.summary["analysis_scheduled"] is False for audit in publication_audits
        )


def test_processor_treats_non_valuation_workbook_as_ignored(
    app_and_engine: tuple[object, object],
) -> None:
    app, engine = app_and_engine
    with Session(engine) as session:
        actor = AuthService(session).initialize_admin("admin", "correct horse").user
        service = ImportService.from_settings(session, app.state.settings)
        batch = service.create_batch(SourceType.UPLOAD, actor.id)
        content = BytesIO()
        workbook = Workbook()
        workbook.active.append(["交易记录"])
        workbook.save(content)
        service.receive_upload(
            batch.id, "交易记录.xlsx", BytesIO(content.getvalue()), actor.id
        )
        service.complete_batch(batch.id, actor.id)
        session.commit()

        result = process_import_batch(session, batch.id, app.state.settings)

        assert result.non_valuation_files == 1
        assert result.created_versions == ()


def test_processor_is_idempotent_for_a_completed_source_file(
    app_and_engine: tuple[object, object],
) -> None:
    app, engine = app_and_engine
    with Session(engine) as session:
        actor = AuthService(session).initialize_admin("admin", "correct horse").user
        fund = Fund(standard_name="千帆一号")
        session.add(fund)
        session.flush()
        service = ImportService.from_settings(session, app.state.settings)
        batch = service.create_batch(SourceType.UPLOAD, actor.id)
        service.receive_upload(
            batch.id, "千帆一号.xlsx", BytesIO(_valuation_xlsx()), actor.id
        )
        service.complete_batch(batch.id, actor.id)
        session.commit()

        first = process_next_job(session, app.state.settings)
        assert first is not None
        second = process_import_batch(session, batch.id, app.state.settings)

        assert first[1] is not None
        assert second.duplicate_files == 1
        assert second.created_versions == ()


def test_product_alias_queries_are_constant_for_multiple_funds(
    app_and_engine: tuple[object, object],
) -> None:
    _, engine = app_and_engine
    with Session(engine) as session:
        funds = [Fund(standard_name=f"产品{i}") for i in range(4)]
        session.add_all(funds)
        session.flush()
        session.add_all(
            [
                FundAlias(fund_id=funds[0].id, alias="甲产品"),
                FundAlias(fund_id=funds[0].id, alias="  A-Product  "),
                FundAlias(fund_id=funds[1].id, alias="乙产品"),
                FundAlias(fund_id=funds[2].id, alias="丙产品"),
            ]
        )
        session.flush()

        select_statements: list[str] = []

        def count_selects(
            _conn: object,
            _cursor: object,
            statement: str,
            _parameters: object,
            _context: object,
            _executemany: bool,
        ) -> None:
            if statement.lstrip().upper().startswith("SELECT"):
                select_statements.append(statement)

        event.listen(engine, "before_cursor_execute", count_selects)
        try:
            aliases = _product_aliases(session)
            assert aliases == {
                "产品0": ("甲产品", "  A-Product  "),
                "产品1": ("乙产品",),
                "产品2": ("丙产品",),
                "产品3": (),
            }
            assert len(select_statements) == 1

            select_statements.clear()
            assert _resolve_fund(session, " a-product ") is funds[0]
            assert len(select_statements) == 1
        finally:
            event.remove(engine, "before_cursor_execute", count_selects)


def test_product_identity_uses_alias_priority_and_reuses_one_query(
    app_and_engine: tuple[object, object],
) -> None:
    _, engine = app_and_engine
    with Session(engine) as session:
        low = Fund(standard_name="低优先级产品")
        high = Fund(standard_name="高优先级产品")
        session.add_all([low, high])
        session.flush()
        session.add_all(
            [
                FundAlias(fund_id=low.id, alias="共同别名", match_priority=1),
                FundAlias(fund_id=high.id, alias="共同别名", match_priority=20),
            ]
        )
        session.flush()

        select_count = 0

        def count_selects(
            _conn: object,
            _cursor: object,
            statement: str,
            _parameters: object,
            _context: object,
            _executemany: bool,
        ) -> None:
            nonlocal select_count
            if statement.lstrip().upper().startswith("SELECT"):
                select_count += 1

        event.listen(engine, "before_cursor_execute", count_selects)
        try:
            aliases, lookup = _load_product_identity(session)
            assert _resolve_fund(session, "共同别名", lookup=lookup) is high
            assert aliases["高优先级产品"] == ("共同别名",)
            assert select_count == 1
        finally:
            event.remove(engine, "before_cursor_execute", count_selects)
