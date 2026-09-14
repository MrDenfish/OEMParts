"""Tests for fetch-run status semantics.

An intraday cycle with zero high-priority searches is a healthy no-op, not
a failure — but `errors < searches_processed` is False for 0/0, so every
empty cycle was stamped "failed" (observed daily since 2026-08-24).
"""

from sqlalchemy.orm import Session

from app.db import queries
from app.worker import fetcher


def test_empty_cycle_completes_not_fails(db_session: Session) -> None:
    """0 searches + 0 errors is a no-op, recorded as completed."""
    run = queries.create_fetch_run(db_session, "intraday")
    queries.complete_fetch_run(
        db_session,
        run,
        searches_processed=0,
        listings_fetched=0,
        listings_new=0,
        listings_updated=0,
        api_calls_made=0,
        errors=0,
    )
    assert run.status == "completed"


def test_all_searches_erroring_is_failed(db_session: Session) -> None:
    """Errors on every search (processed counts only successes) → failed."""
    run = queries.create_fetch_run(db_session, "nightly")
    queries.complete_fetch_run(
        db_session,
        run,
        searches_processed=0,
        listings_fetched=0,
        listings_new=0,
        listings_updated=0,
        api_calls_made=3,
        errors=3,
    )
    assert run.status == "failed"


def test_partial_errors_still_completed(db_session: Session) -> None:
    run = queries.create_fetch_run(db_session, "nightly")
    queries.complete_fetch_run(
        db_session,
        run,
        searches_processed=2,
        listings_fetched=50,
        listings_new=5,
        listings_updated=45,
        api_calls_made=3,
        errors=1,
    )
    assert run.status == "completed"


def test_intraday_cycle_with_no_high_priority_searches_completes(
    db_session: Session,
) -> None:
    """End-to-end through run_fetch_cycle: an empty intraday run completes."""
    fetcher.run_fetch_cycle(db_session, cycle_type="intraday")
    run = queries.get_latest_fetch_run(db_session)
    assert run is not None
    assert run.cycle_type == "intraday"
    assert run.searches_processed == 0
    assert run.status == "completed"
