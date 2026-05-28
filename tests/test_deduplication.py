"""
Tests for the deduplication invariant:
(city, reading_time) must be stored at most once.
"""

from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from app.poller import _store_reading
from tests.conftest import make_reading


def test_same_reading_stored_once(db_session):
    r1 = make_reading(city="Ottawa", reading_time=datetime(2024, 6, 1, 12, 0, 0))
    r2 = make_reading(city="Ottawa", reading_time=datetime(2024, 6, 1, 12, 0, 0))

    _store_reading(db_session, r1)
    _store_reading(db_session, r2)  # duplicate, should be ignored

    from app.models import Reading
    count = db_session.query(Reading).filter_by(city="Ottawa").count()
    assert count == 1


def test_different_timestamp_stored_separately(db_session):
    r1 = make_reading(city="Ottawa", reading_time=datetime(2024, 6, 1, 12, 0, 0))
    r2 = make_reading(city="Ottawa", reading_time=datetime(2024, 6, 1, 13, 0, 0))

    _store_reading(db_session, r1)
    _store_reading(db_session, r2)

    from app.models import Reading
    count = db_session.query(Reading).filter_by(city="Ottawa").count()
    assert count == 2


def test_same_time_different_city_stored_separately(db_session):
    ts = datetime(2024, 6, 1, 12, 0, 0)
    r1 = make_reading(city="Ottawa",    reading_time=ts)
    r2 = make_reading(city="Vancouver", reading_time=ts)

    _store_reading(db_session, r1)
    _store_reading(db_session, r2)

    from app.models import Reading
    count = db_session.query(Reading).count()
    assert count == 2


@pytest.mark.asyncio
async def test_poll_deduplication_via_mock():
    """Simulate the API returning the same timestamp twice — only one row stored."""
    api_payload = {
        "current": {
            "time": "2024-06-01T12:00",
            "temperature_2m": 15.0,
            "apparent_temperature": 13.0,
            "precipitation": 0.0,
            "wind_speed_10m": 10.0,
            "weather_code": 0,
        }
    }

    import httpx
    from app.poller import _poll_city, CITIES

    ottawa = next(c for c in CITIES if c["name"] == "Ottawa")

    mock_response = MagicMock()
    mock_response.json.return_value = api_payload
    mock_response.raise_for_status.return_value = None

    with patch("app.poller.SessionLocal") as mock_session_local:
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from app.models import Base

        engine = create_engine(
            "sqlite:///:memory:", connect_args={"check_same_thread": False}
        )
        Base.metadata.create_all(engine)
        TestSession = sessionmaker(bind=engine)

        mock_session_local.side_effect = TestSession

        async with httpx.AsyncClient() as client:
            with patch.object(client, "get", return_value=mock_response):
                await _poll_city(client, ottawa)
                await _poll_city(client, ottawa)  # same response, should be deduped

        from app.models import Reading
        session = TestSession()
        count = session.query(Reading).filter_by(city="Ottawa").count()
        session.close()
        assert count == 1
