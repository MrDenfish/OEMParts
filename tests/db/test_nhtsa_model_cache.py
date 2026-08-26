"""Schema tests for nhtsa_model_cache table."""

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.db.models import NhtsaModelCache, utcnow


def test_nhtsa_model_cache_insert_roundtrip(db_session: Session) -> None:
    """Verify nhtsa_model_cache can be inserted and retrieved correctly."""
    models_json = '["LR4", "Range Rover"]'
    cache = NhtsaModelCache(make="Land Rover", year=2012, models_json=models_json)
    db_session.add(cache)
    db_session.commit()
    db_session.refresh(cache)

    assert cache.make == "Land Rover"
    assert cache.year == 2012
    assert cache.models_json == models_json
    assert cache.cached_at is not None
    assert isinstance(cache.cached_at, datetime)


def test_nhtsa_model_cache_cached_at_timezone_aware(db_session: Session) -> None:
    """Verify cached_at maintains UTC timezone awareness."""
    cache_time = utcnow()
    cache = NhtsaModelCache(
        make="Toyota",
        year=2015,
        models_json='["Tacoma", "Tundra"]',
        cached_at=cache_time,
    )
    db_session.add(cache)
    db_session.commit()
    db_session.refresh(cache)

    assert cache.cached_at is not None
    assert cache.cached_at.tzinfo is not None
    # Verify it's UTC
    assert cache.cached_at.tzinfo == timezone.utc


def test_nhtsa_model_cache_composite_pk_overwrites(db_session: Session) -> None:
    """Verify composite PK (make, year) enforces one row per pair via merge."""
    # Insert initial entry
    cache1 = NhtsaModelCache(make="Honda", year=2020, models_json='["Civic", "Accord"]')
    db_session.add(cache1)
    db_session.commit()

    # Query to verify it was inserted
    result1 = (
        db_session.query(NhtsaModelCache).filter_by(make="Honda", year=2020).first()
    )
    assert result1 is not None
    assert result1.models_json == '["Civic", "Accord"]'

    # Insert another entry for same (make, year) - should overwrite
    cache2 = NhtsaModelCache(
        make="Honda", year=2020, models_json='["Civic", "Accord", "CR-V"]'
    )
    db_session.merge(cache2)
    db_session.commit()

    # Verify there's still only one row for this (make, year)
    results = db_session.query(NhtsaModelCache).filter_by(make="Honda", year=2020).all()
    assert len(results) == 1
    assert results[0].models_json == '["Civic", "Accord", "CR-V"]'


def test_nhtsa_model_cache_multiple_makes_years(db_session: Session) -> None:
    """Verify multiple (make, year) combinations can coexist."""
    cache1 = NhtsaModelCache(make="Ford", year=2018, models_json='["Mustang", "F-150"]')
    cache2 = NhtsaModelCache(
        make="Ford", year=2019, models_json='["Mustang", "F-150", "Escape"]'
    )
    cache3 = NhtsaModelCache(
        make="Chevrolet", year=2018, models_json='["Silverado", "Tahoe"]'
    )
    db_session.add_all([cache1, cache2, cache3])
    db_session.commit()

    # Verify all three rows exist
    all_rows = db_session.query(NhtsaModelCache).all()
    assert len(all_rows) == 3

    # Verify specific rows have correct values
    ford_2018 = (
        db_session.query(NhtsaModelCache).filter_by(make="Ford", year=2018).first()
    )
    assert ford_2018 is not None
    assert ford_2018.models_json == '["Mustang", "F-150"]'

    ford_2019 = (
        db_session.query(NhtsaModelCache).filter_by(make="Ford", year=2019).first()
    )
    assert ford_2019 is not None
    assert ford_2019.models_json == '["Mustang", "F-150", "Escape"]'

    chevy_2018 = (
        db_session.query(NhtsaModelCache).filter_by(make="Chevrolet", year=2018).first()
    )
    assert chevy_2018 is not None
    assert chevy_2018.models_json == '["Silverado", "Tahoe"]'
