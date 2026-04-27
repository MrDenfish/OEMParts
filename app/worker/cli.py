"""CLI entry point for OEMParts worker commands.

Uses argparse (stdlib) with subparsers for each command.
Commands: fetch, cleanup, health.
"""

import argparse
import json
import logging
import sys

from app.config import settings
from app.db.session import get_session
from app.worker.cleanup import run_cleanup
from app.worker.fetcher import run_fetch_cycle


def setup_logging() -> None:
    """Configure logging based on LOG_LEVEL from settings."""
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def cmd_fetch(args: argparse.Namespace) -> None:
    """Run a fetch cycle."""
    setup_logging()
    with get_session() as db:
        run_fetch_cycle(
            db,
            cycle_type=args.cycle,
            search_id=args.search_id,
        )


def cmd_cleanup(args: argparse.Namespace) -> None:
    """Run the cleanup job."""
    setup_logging()
    with get_session() as db:
        run_cleanup(db)


def cmd_health(args: argparse.Namespace) -> None:
    """Print system health as JSON."""
    setup_logging()
    from app.db import queries

    with get_session() as db:
        latest = queries.get_latest_fetch_run(db)
        health = {
            "database": "ok",
            "last_fetch": None,
        }
        if latest:
            health["last_fetch"] = {
                "status": latest.status,
                "started_at": latest.started_at.isoformat(),
                "searches_processed": latest.searches_processed,
                "listings_fetched": latest.listings_fetched,
                "listings_new": latest.listings_new,
                "errors": latest.errors,
            }
        # print() is acceptable here — CLI user-facing output (per CLAUDE.md)
        print(json.dumps(health, indent=2))


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser."""
    parser = argparse.ArgumentParser(
        prog="oemparts",
        description="OEMPartsAgent CLI — parts tracking worker commands",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # fetch
    fetch_parser = subparsers.add_parser("fetch", help="Run a fetch cycle")
    fetch_parser.add_argument(
        "--cycle",
        required=True,
        choices=["nightly", "intraday", "manual"],
        help="Cycle type: nightly (all), intraday (high-priority), manual",
    )
    fetch_parser.add_argument(
        "--search-id",
        default=None,
        help="Specific search UUID (for manual cycle)",
    )
    fetch_parser.set_defaults(func=cmd_fetch)

    # cleanup
    cleanup_parser = subparsers.add_parser(
        "cleanup", help="Mark stale listings inactive and archive old data"
    )
    cleanup_parser.set_defaults(func=cmd_cleanup)

    # health
    health_parser = subparsers.add_parser("health", help="Print system health JSON")
    health_parser.set_defaults(func=cmd_health)

    return parser


def main() -> None:
    """CLI entry point."""
    parser = build_parser()
    args = parser.parse_args()
    try:
        args.func(args)
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as exc:
        logging.getLogger(__name__).exception("CLI command failed: %s", exc)
        sys.exit(1)
