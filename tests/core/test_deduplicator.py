"""Tests for the deduplication logic."""

from datetime import timedelta

from app.core.deduplicator import should_skip_search
from app.db.models import Search, utcnow


def test_skip_recently_fetched(test_search: Search) -> None:
    """A search fetched within TTL should be skipped."""
    test_search.last_fetched_at = utcnow()
    assert should_skip_search(test_search) is True


def test_do_not_skip_stale(test_search: Search) -> None:
    """A search fetched outside TTL should not be skipped."""
    test_search.last_fetched_at = utcnow() - timedelta(hours=5)
    assert should_skip_search(test_search) is False


def test_do_not_skip_never_fetched(test_search: Search) -> None:
    """A search that was never fetched should not be skipped."""
    test_search.last_fetched_at = None
    assert should_skip_search(test_search) is False
