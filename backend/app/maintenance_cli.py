"""Command-line entry point for one-shot maintenance operations."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence

from sqlalchemy.orm import Session

from .config import get_settings
from .db.session import create_engine
from .system.maintenance import MAINTENANCE_COMMANDS, MaintenanceService


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Fund dashboard maintenance")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in MAINTENANCE_COMMANDS:
        subparser = subparsers.add_parser(command)
        if command == "source-retention":
            subparser.add_argument(
                "--apply",
                action="store_true",
                help="delete only files approved by the retention safety checks",
            )
        if command == "database-backup":
            subparser.add_argument("--output-name", default=None)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = get_settings()
    engine = create_engine(settings.database_url)
    try:
        with Session(engine) as session:
            result = MaintenanceService(session, settings).run(
                args.command,
                dry_run=not getattr(args, "apply", False),
                output_name=getattr(args, "output_name", None),
            )
        print(json.dumps(result.as_dict(), ensure_ascii=False, default=str))
        return 0 if result.status == "succeeded" else 1
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
