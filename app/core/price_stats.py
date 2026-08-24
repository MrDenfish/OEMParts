"""Price statistics and deal signals.

Two deliberately narrow signals (spec §4.1):
  - recent_drop: a listing's current price vs its own peak within a lookback
    window. High confidence — a listing compared only against itself.
  - is_low_in_search: price at/below the 25th percentile of the search's
    active listings. Advisory only — searches legitimately mix part types
    ($25 brackets next to $1,189 compressors), so this is presented as
    "among the cheapest in this search", never "below market value".
"""

import uuid
from dataclasses import dataclass
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy.orm import Session

from app.config import settings
from app.db.models import Listing, PriceHistory, SearchListing, utcnow

MIN_LISTINGS_FOR_STATS = 5


@dataclass
class PriceDrop:
    old_price: Decimal
    new_price: Decimal
    pct: Decimal  # whole-percent drop, e.g. Decimal("38")


@dataclass
class SearchPriceStats:
    median: Decimal
    minimum: Decimal
    p25: Decimal
    count: int


def recent_drop(
    db: Session, listing_id: uuid.UUID, lookback_days: int
) -> PriceDrop | None:
    """Return the drop from the window's peak price to the latest price.

    Prices are snapshotted on every fetch, so the comparison is latest
    snapshot vs the highest earlier snapshot within the window — "last two
    rows" would miss a drop followed by stable prices.
    """
    cutoff = utcnow() - timedelta(days=lookback_days)
    rows = (
        db.query(PriceHistory)
        .filter(
            PriceHistory.listing_id == listing_id,
            PriceHistory.recorded_at >= cutoff,
        )
        .order_by(PriceHistory.recorded_at.desc())
        .all()
    )
    if len(rows) < 2:
        return None

    current = rows[0].price
    assert isinstance(current, Decimal), "PriceHistory.price must be Decimal"
    peak = max(row.price for row in rows[1:])
    assert isinstance(peak, Decimal), "PriceHistory.price must be Decimal"
    if peak <= 0 or current >= peak:
        return None

    # Compare the raw (unrounded) ratio against the threshold first — rounding
    # before comparing would let e.g. a 9.5% drop round up to 10% and pass a
    # 10% bar it didn't actually clear. Only the *displayed* pct is quantized.
    raw_pct = (peak - current) / peak * 100
    if raw_pct < settings.digest_price_drop_pct:
        return None
    pct = raw_pct.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return PriceDrop(old_price=peak, new_price=current, pct=pct)


def _nearest_rank(sorted_prices: list[Decimal], percentile: int) -> Decimal:
    """Nearest-rank percentile: value at index ceil(P/100 * n) - 1.

    Exact integer arithmetic (ceil division via negated floor division)
    instead of float math — avoids float/percentile edge cases entirely.
    """
    n = len(sorted_prices)
    index = -(-percentile * n // 100) - 1
    return sorted_prices[max(index, 0)]


def search_price_stats(db: Session, search_id: uuid.UUID) -> SearchPriceStats | None:
    """Stats over a search's active listings; None below the minimum count."""
    prices: list[Decimal] = []
    for row in (
        db.query(Listing.price)
        .join(SearchListing, SearchListing.listing_id == Listing.id)
        .filter(
            SearchListing.search_id == search_id,
            Listing.is_active.is_(True),
        )
        .all()
    ):
        price = row[0]
        assert isinstance(price, Decimal), "Listing.price must be Decimal"
        prices.append(price)
    if len(prices) < MIN_LISTINGS_FOR_STATS:
        return None
    prices.sort()
    return SearchPriceStats(
        median=_nearest_rank(prices, 50),
        minimum=prices[0],
        p25=_nearest_rank(prices, settings.deal_percentile),
        count=len(prices),
    )


def is_low_in_search(price: Decimal, stats: SearchPriceStats | None) -> bool:
    """True when price sits in the cheapest quartile of its search."""
    if stats is None:
        return False
    return price <= stats.p25
