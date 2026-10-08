"""CLI entry point for manual benchmark sync.

Usage:
    python -m app.benchmark.sync          # incremental sync
    python -m app.benchmark.sync --full   # full historical sync
"""

from __future__ import annotations

import argparse
import logging
import sys

from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.session import create_engine

from .sync import sync_benchmark


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stdout,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser(description="Benchmark index sync tool")
    parser.add_argument(
        "--full",
        action="store_true",
        help="Fetch full history from 2005-04-08 to today",
    )
    args = parser.parse_args()

    settings = get_settings()
    engine = create_engine(settings.database_url)
    try:
        with Session(engine) as session:
            result = sync_benchmark(session, settings, full=args.full)
            print(f"Sync result: {result}")
            if result.error:
                print(f"Error: {result.error}", file=sys.stderr)
                sys.exit(1)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
