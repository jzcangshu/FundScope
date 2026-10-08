"""One-shot import of the sample product NAV history workbooks.

Run from the ``backend`` directory against the local dev database::

    python -m tools.nav_import <directory-with-xls/xlsx-files>

Creates the sample funds (with aliases) when missing, then feeds every
workbook through the standard import batch chain so validation, publishing,
audit logs, and analysis runs behave exactly like production uploads.
All product names are fictional sample data.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.config import get_settings
from app.db.base import SourceType
from app.db.models import Fund, FundAlias
from app.db.session import create_engine
from app.imports.processor import process_import_batch
from app.imports.service import ImportService

# standard name -> (product code, aliases used for matching)
FUND_CATALOG: dict[str, tuple[str, tuple[str, ...]]] = {
    "示例云川一号私募证券投资基金": ("FS0001", ()),
    "示例云川二号私募证券投资基金": ("FS0002", ()),
    "示例云川三号私募证券投资基金": ("FS0003", ()),
    "示例明曜三号私募证券投资基金": (
        "FS0004",
        ("示例明曜三号", "明曜三号"),
    ),
    "示例紫微私募证券投资基金": (
        "FS0005",
        ("示例紫微", "紫微"),
    ),
    "示例青岩稳健一号私募证券投资基金": (
        "FS0006",
        ("示例青岩稳健一号", "青岩稳健一号"),
    ),
    "示例磐石二号私募证券投资基金": (
        "FS0007",
        ("示例磐石二号", "磐石二号"),
    ),
    "示例启航企业二号私募证券投资基金": (
        "FS0008",
        ("示例启航二号", "启航二号"),
    ),
    "示例星辰私募证券投资基金": ("FS0009", ("示例星辰", "星辰")),
    "示例慧达一号私募证券投资基金": (
        "FS0010",
        ("示例慧达一号", "慧达一号"),
    ),
    "示例慧达二号私募证券投资基金": (
        "FS0011",
        ("示例慧达二号", "慧达二号"),
    ),
    "示例明曜一号私募证券投资基金": (
        "FS0012",
        ("示例明曜一号", "明曜一号"),
    ),
}


def ensure_funds(session: Session) -> None:
    for standard_name, (code, aliases) in FUND_CATALOG.items():
        fund = session.scalar(
            select(Fund).where(Fund.standard_name == standard_name)
        )
        if fund is None:
            fund = Fund(standard_name=standard_name, product_code=code)
            session.add(fund)
            session.flush()
            print(f"created fund {fund.id}: {standard_name} ({code})")
        elif fund.product_code is None:
            fund.product_code = code
        existing = set(
            session.scalars(
                select(FundAlias.alias).where(FundAlias.fund_id == fund.id)
            )
        )
        for alias in aliases:
            if alias not in existing:
                session.add(
                    FundAlias(
                        fund_id=fund.id,
                        alias=alias,
                        source_location="nav-import",
                    )
                )
    session.commit()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Import NAV history workbooks")
    parser.add_argument("directory", type=Path)
    args = parser.parse_args(argv)
    directory = args.directory
    if not directory.is_dir():
        raise SystemExit(f"not a directory: {directory}")

    settings = get_settings()
    engine = create_engine(settings.database_url)
    try:
        with Session(engine) as session:
            ensure_funds(session)
            service = ImportService.from_settings(session, settings)
            batch = service.create_batch(SourceType.UPLOAD, actor_user_id=None)
            for path in sorted(directory.iterdir()):
                if path.suffix.lower() not in {".xls", ".xlsx"}:
                    continue
                with path.open("rb") as stream:
                    result = service.receive_upload(
                        batch.id, path.name, stream, actor_user_id=None
                    )
                print(
                    f"uploaded {path.name}: file_id={result.source_file.id}"
                    f" duplicate={result.duplicate}"
                )
            service.complete_batch(batch.id, actor_user_id=None)
            session.commit()

            summary = process_import_batch(session, batch.id, settings)
            session.commit()
            print(summary)
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
