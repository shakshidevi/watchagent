"""Tests for API shape: correct structure, filtering, ordering, and limits."""

from datetime import datetime, timedelta, timezone

import pytest

from app.models import Event, Reading
from tests.conftest import make_reading


def _seed_reading(db, city="Ottawa", reading_time=None, **kwargs) -> Reading:
    r = make_reading(city=city, reading_time=reading_time or datetime(2024, 6, 1, 12, 0, 0), **kwargs)
    db.add(r)
    db.flush()
    return r


def _seed_event(db, reading: Reading, event_type="high_wind") -> Event:
    e = Event(
        city=reading.city,
        event_type=event_type,
        severity="warning",
        reading_id=reading.id,
        reading_time=reading.reading_time,
        detected_at=datetime.now(timezone.utc).replace(tzinfo=None),
        description="Test event",
        metric_name="wind_speed_10m",
        metric_value=80.0,
        baseline_value=70.0,
    )
    db.add(e)
    db.flush()
    return e


class TestHealth:
    def test_health_returns_ok(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200

    def test_health_structure(self, client):
        body = client.get("/health").json()
        assert "status" in body
        assert "readings_stored" in body
        assert "events_stored" in body
        assert body["status"] == "ok"

    def test_health_counts_are_integers(self, client):
        body = client.get("/health").json()
        assert isinstance(body["readings_stored"], int)
        assert isinstance(body["events_stored"], int)

    def test_health_counts_increment(self, client, db_session):
        _seed_reading(db_session)
        db_session.commit()
        body = client.get("/health").json()
        assert body["readings_stored"] >= 1


class TestReadings:
    def test_empty_returns_list(self, client):
        body = client.get("/readings").json()
        assert "readings" in body
        assert isinstance(body["readings"], list)

    def test_reading_has_expected_fields(self, client, db_session):
        _seed_reading(db_session)
        db_session.commit()
        body = client.get("/readings").json()
        r = body["readings"][0]
        for field in [
            "id", "city", "reading_time", "fetched_at",
            "temperature_2m", "apparent_temperature",
            "precipitation", "wind_speed_10m", "weather_code",
        ]:
            assert field in r, f"Missing field: {field}"

    def test_city_filter(self, client, db_session):
        _seed_reading(db_session, city="Ottawa")
        _seed_reading(db_session, city="Toronto", reading_time=datetime(2024, 6, 1, 13, 0, 0))
        db_session.commit()

        body = client.get("/readings?city=Ottawa").json()
        assert all(r["city"] == "Ottawa" for r in body["readings"])

    def test_limit_respected(self, client, db_session):
        base = datetime(2024, 6, 1, 0, 0, 0)
        for i in range(10):
            r = make_reading(city="Ottawa", reading_time=base + timedelta(hours=i))
            db_session.add(r)
        db_session.commit()

        body = client.get("/readings?limit=3").json()
        assert len(body["readings"]) <= 3

    def test_default_limit_is_50(self, client, db_session):
        base = datetime(2024, 6, 1, 0, 0, 0)
        for i in range(60):
            r = make_reading(city="Ottawa", reading_time=base + timedelta(hours=i))
            db_session.add(r)
        db_session.commit()

        body = client.get("/readings").json()
        assert len(body["readings"]) == 50

    def test_results_ordered_most_recent_first(self, client, db_session):
        base = datetime(2024, 6, 1, 0, 0, 0)
        for i in range(5):
            r = make_reading(city="Ottawa", reading_time=base + timedelta(hours=i))
            db_session.add(r)
        db_session.commit()

        body = client.get("/readings").json()
        times = [r["reading_time"] for r in body["readings"]]
        assert times == sorted(times, reverse=True)


class TestEvents:
    def test_empty_returns_list(self, client):
        body = client.get("/events").json()
        assert "events" in body
        assert isinstance(body["events"], list)

    def test_event_has_expected_fields(self, client, db_session):
        r = _seed_reading(db_session)
        _seed_event(db_session, r)
        db_session.commit()

        body = client.get("/events").json()
        e = body["events"][0]
        for field in [
            "id", "city", "event_type", "severity",
            "reading_id", "reading_time", "detected_at",
            "description", "metric_value", "metric_name", "baseline_value",
        ]:
            assert field in e, f"Missing field: {field}"

    def test_city_filter(self, client, db_session):
        r_ott = _seed_reading(db_session, city="Ottawa")
        r_van = _seed_reading(db_session, city="Vancouver",
                              reading_time=datetime(2024, 6, 1, 13, 0, 0))
        _seed_event(db_session, r_ott)
        _seed_event(db_session, r_van)
        db_session.commit()

        body = client.get("/events?city=Ottawa").json()
        assert all(e["city"] == "Ottawa" for e in body["events"])

    def test_limit_respected(self, client, db_session):
        base = datetime(2024, 6, 1, 0, 0, 0)
        for i in range(10):
            r = _seed_reading(db_session, city="Ottawa", reading_time=base + timedelta(hours=i))
            _seed_event(db_session, r)
        db_session.commit()

        body = client.get("/events?limit=3").json()
        assert len(body["events"]) <= 3
