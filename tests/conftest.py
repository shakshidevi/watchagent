"""
Shared fixtures for all tests.
Uses in-memory SQLite with StaticPool so every session shares the same
connection and sees the same tables.
"""
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import get_db
from app.main import app
from app.models import Base, Reading
from datetime import datetime, timezone


def _make_engine():
    return create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )


@pytest.fixture(scope="function")
def db_engine():
    engine = _make_engine()
    Base.metadata.create_all(bind=engine)
    yield engine
    Base.metadata.drop_all(bind=engine)
    engine.dispose()


@pytest.fixture(scope="function")
def db_session(db_engine):
    Session = sessionmaker(bind=db_engine)
    session = Session()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture(scope="function")
def client(db_engine):
    Session = sessionmaker(bind=db_engine)

    def override_get_db():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    def fake_init_db():
        Base.metadata.create_all(bind=db_engine)

    app.dependency_overrides[get_db] = override_get_db

    with patch("app.main.poll_all_cities", new_callable=AsyncMock), \
         patch("app.main.init_db", side_effect=fake_init_db), \
         patch("app.main.scheduler") as mock_sched:
        mock_sched.add_job = MagicMock()
        mock_sched.start = MagicMock()
        mock_sched.shutdown = MagicMock()
        with TestClient(app, raise_server_exceptions=True) as c:
            yield c

    app.dependency_overrides.clear()


def make_reading(
    city: str = "Ottawa",
    reading_time: datetime = None,
    temperature_2m: float = 10.0,
    apparent_temperature: float = 8.0,
    precipitation: float = 0.0,
    wind_speed_10m: float = 15.0,
    weather_code: int = 0,
    **kwargs,
) -> Reading:
    return Reading(
        city=city,
        reading_time=reading_time or datetime(2024, 6, 1, 12, 0, 0),
        fetched_at=datetime.now(timezone.utc).replace(tzinfo=None),
        temperature_2m=temperature_2m,
        apparent_temperature=apparent_temperature,
        precipitation=precipitation,
        wind_speed_10m=wind_speed_10m,
        weather_code=weather_code,
        **kwargs,
    )
