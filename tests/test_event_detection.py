"""
Tests for event detection logic.
Each test feeds in a controlled sequence of readings and checks
whether the right events fire (and wrong ones don't).
"""

from datetime import datetime, timedelta

import pytest

from app.detector import (
    ANOMALY_MIN_READINGS,
    ANOMALY_SIGMA,
    CROSS_CITY_DELTA_C,
    CROSS_CITY_CONTRAST_C,
    FEELS_LIKE_GAP_C,
    HIGH_WIND_KMH,
    HEAVY_PRECIP_MM,
    RAPID_DROP_C,
    STRONGEST_WIND_MARGIN_KMH,
    TEMP_SPIKE_C,
    TREND_MIN_READINGS,
    TREND_INFO_C,
    TREND_WARN_C,
    WIND_CHILL_DANGER_C,
    detect_events,
)
from app.models import Event, Reading
from tests.conftest import make_reading


def _add(db, reading: Reading) -> Reading:
    db.add(reading)
    db.flush()
    return reading


class TestSevereWeatherCode:
    def test_thunderstorm_fires_critical(self, db_session):
        r = _add(db_session, make_reading(weather_code=95))
        events = detect_events(r, db_session)
        types = [e.event_type for e in events]
        assert "severe_weather_code" in types
        sev = next(e for e in events if e.event_type == "severe_weather_code")
        assert sev.severity == "critical"

    def test_heavy_snow_fires_warning(self, db_session):
        r = _add(db_session, make_reading(weather_code=75))
        events = detect_events(r, db_session)
        types = [e.event_type for e in events]
        assert "severe_weather_code" in types
        sev = next(e for e in events if e.event_type == "severe_weather_code")
        assert sev.severity == "warning"

    def test_clear_sky_no_event(self, db_session):
        r = _add(db_session, make_reading(weather_code=0))
        events = detect_events(r, db_session)
        assert not any(e.event_type == "severe_weather_code" for e in events)

    def test_moderate_rain_no_event(self, db_session):
        # WMO 63 = moderate rain — not in the severe set
        r = _add(db_session, make_reading(weather_code=63))
        events = detect_events(r, db_session)
        assert not any(e.event_type == "severe_weather_code" for e in events)


class TestDangerousWindChill:
    def test_below_threshold_fires(self, db_session):
        r = _add(db_session, make_reading(
            temperature_2m=-10.0,
            apparent_temperature=WIND_CHILL_DANGER_C - 5,
        ))
        events = detect_events(r, db_session)
        assert any(e.event_type == "dangerous_wind_chill" for e in events)

    def test_above_threshold_no_event(self, db_session):
        r = _add(db_session, make_reading(
            temperature_2m=0.0,
            apparent_temperature=WIND_CHILL_DANGER_C + 1,
        ))
        events = detect_events(r, db_session)
        assert not any(e.event_type == "dangerous_wind_chill" for e in events)

    def test_exactly_at_threshold_no_event(self, db_session):
        # boundary check: condition is < not <=
        r = _add(db_session, make_reading(
            temperature_2m=-15.0,
            apparent_temperature=WIND_CHILL_DANGER_C,
        ))
        events = detect_events(r, db_session)
        assert not any(e.event_type == "dangerous_wind_chill" for e in events)


class TestHighWind:
    def test_above_threshold_fires(self, db_session):
        r = _add(db_session, make_reading(wind_speed_10m=HIGH_WIND_KMH + 1))
        events = detect_events(r, db_session)
        assert any(e.event_type == "high_wind" for e in events)

    def test_below_threshold_no_event(self, db_session):
        r = _add(db_session, make_reading(wind_speed_10m=HIGH_WIND_KMH - 1))
        events = detect_events(r, db_session)
        assert not any(e.event_type == "high_wind" for e in events)


class TestHeavyPrecipitation:
    def test_above_threshold_fires(self, db_session):
        r = _add(db_session, make_reading(precipitation=HEAVY_PRECIP_MM + 1))
        events = detect_events(r, db_session)
        assert any(e.event_type == "heavy_precipitation" for e in events)

    def test_below_threshold_no_event(self, db_session):
        r = _add(db_session, make_reading(precipitation=HEAVY_PRECIP_MM - 1))
        events = detect_events(r, db_session)
        assert not any(e.event_type == "heavy_precipitation" for e in events)

    def test_zero_precipitation_no_event(self, db_session):
        r = _add(db_session, make_reading(precipitation=0.0))
        events = detect_events(r, db_session)
        assert not any(e.event_type == "heavy_precipitation" for e in events)


class TestTemperatureAnomaly:
    def _seed_baseline(self, db_session, city: str, base_temp: float, n: int):
        # alternate ±0.5 so stdev > 0 (zero stdev skips anomaly detection)
        base_time = datetime(2024, 1, 1, 0, 0, 0)
        for i in range(n):
            variation = 0.5 if i % 2 == 0 else -0.5
            r = make_reading(
                city=city,
                reading_time=base_time + timedelta(hours=i),
                temperature_2m=base_temp + variation,
                apparent_temperature=base_temp + variation - 2,
            )
            db_session.add(r)
        db_session.flush()

    def test_anomaly_fires_when_enough_history(self, db_session):
        self._seed_baseline(db_session, "Ottawa", base_temp=10.0, n=ANOMALY_MIN_READINGS)
        extreme = _add(db_session, make_reading(
            city="Ottawa",
            reading_time=datetime(2024, 1, 1, 0, 0, 0) + timedelta(hours=ANOMALY_MIN_READINGS),
            temperature_2m=35.0,
            apparent_temperature=33.0,
        ))
        events = detect_events(extreme, db_session)
        assert any(e.event_type == "temperature_anomaly" for e in events)

    def test_anomaly_does_not_fire_with_insufficient_history(self, db_session):
        self._seed_baseline(db_session, "Ottawa", base_temp=10.0, n=ANOMALY_MIN_READINGS - 1)
        extreme = _add(db_session, make_reading(
            city="Ottawa",
            reading_time=datetime(2024, 1, 1, 0, 0, 0) + timedelta(hours=ANOMALY_MIN_READINGS),
            temperature_2m=40.0,
            apparent_temperature=38.0,
        ))
        events = detect_events(extreme, db_session)
        assert not any(e.event_type == "temperature_anomaly" for e in events)

    def test_normal_reading_no_anomaly(self, db_session):
        self._seed_baseline(db_session, "Toronto", base_temp=20.0, n=ANOMALY_MIN_READINGS)
        normal = _add(db_session, make_reading(
            city="Toronto",
            reading_time=datetime(2024, 1, 1, 0, 0, 0) + timedelta(hours=ANOMALY_MIN_READINGS),
            temperature_2m=21.0,
            apparent_temperature=19.0,
        ))
        events = detect_events(normal, db_session)
        assert not any(e.event_type == "temperature_anomaly" for e in events)

    def test_anomaly_isolated_to_city(self, db_session):
        # events should only be for the city that was checked
        self._seed_baseline(db_session, "Ottawa", base_temp=10.0, n=ANOMALY_MIN_READINGS)
        extreme = _add(db_session, make_reading(
            city="Ottawa",
            reading_time=datetime(2024, 1, 1, 0, 0, 0) + timedelta(hours=ANOMALY_MIN_READINGS),
            temperature_2m=40.0,
            apparent_temperature=38.0,
        ))
        events = detect_events(extreme, db_session)
        for e in events:
            assert e.city == "Ottawa"


class TestRapidTemperatureDrop:
    def test_drop_above_threshold_fires(self, db_session):
        base_time = datetime(2024, 3, 1, 12, 0, 0)
        _add(db_session, make_reading(
            city="Ottawa",
            reading_time=base_time,
            temperature_2m=15.0,
            apparent_temperature=13.0,
        ))
        current = _add(db_session, make_reading(
            city="Ottawa",
            reading_time=base_time + timedelta(hours=1),
            temperature_2m=15.0 - RAPID_DROP_C,  # exactly at threshold, should fire (>=)
            apparent_temperature=7.0,
        ))
        events = detect_events(current, db_session)
        assert any(e.event_type == "rapid_temperature_drop" for e in events)

    def test_small_drop_no_event(self, db_session):
        base_time = datetime(2024, 3, 1, 12, 0, 0)
        _add(db_session, make_reading(city="Ottawa", reading_time=base_time, temperature_2m=15.0))
        current = _add(db_session, make_reading(
            city="Ottawa",
            reading_time=base_time + timedelta(hours=1),
            temperature_2m=12.0,  # 3 °C drop, below threshold
        ))
        events = detect_events(current, db_session)
        assert not any(e.event_type == "rapid_temperature_drop" for e in events)

    def test_rapid_rise_does_not_fire(self, db_session):
        base_time = datetime(2024, 3, 1, 12, 0, 0)
        _add(db_session, make_reading(city="Vancouver", reading_time=base_time, temperature_2m=5.0))
        current = _add(db_session, make_reading(
            city="Vancouver",
            reading_time=base_time + timedelta(hours=1),
            temperature_2m=5.0 + RAPID_DROP_C + 2,  # big rise, not a drop
        ))
        events = detect_events(current, db_session)
        assert not any(e.event_type == "rapid_temperature_drop" for e in events)

    def test_no_previous_reading_no_event(self, db_session):
        r = _add(db_session, make_reading(city="Vancouver", temperature_2m=5.0))
        events = detect_events(r, db_session)
        assert not any(e.event_type == "rapid_temperature_drop" for e in events)


class TestCrossCityOutlier:
    def _seed_all_cities(self, db_session, temps: dict[str, float], base_time: datetime):
        readings = {}
        for city, temp in temps.items():
            r = _add(db_session, make_reading(
                city=city,
                reading_time=base_time,
                temperature_2m=temp,
                apparent_temperature=temp - 2,
            ))
            readings[city] = r
        return readings

    def test_outlier_fires_for_divergent_city(self, db_session):
        base_time = datetime(2024, 1, 15, 12, 0, 0)
        readings = self._seed_all_cities(
            db_session,
            {"Toronto": 5.0, "Vancouver": 5.0, "Ottawa": -20.0},
            base_time,
        )
        events = detect_events(readings["Ottawa"], db_session)
        assert any(e.event_type == "cross_city_outlier" and e.city == "Ottawa" for e in events)

    def test_no_outlier_when_cities_similar(self, db_session):
        base_time = datetime(2024, 6, 1, 12, 0, 0)
        readings = self._seed_all_cities(
            db_session,
            {"Toronto": 22.0, "Vancouver": 20.0, "Ottawa": 23.0},
            base_time,
        )
        events = detect_events(readings["Ottawa"], db_session)
        assert not any(e.event_type == "cross_city_outlier" for e in events)

    def test_outlier_requires_recent_other_cities(self, db_session):
        # other cities' readings are 4h old — should not trigger cross-city check
        old_time = datetime(2024, 1, 15, 8, 0, 0)
        new_time = datetime(2024, 1, 15, 12, 0, 0)

        _add(db_session, make_reading(city="Toronto",   reading_time=old_time, temperature_2m=5.0))
        _add(db_session, make_reading(city="Vancouver", reading_time=old_time, temperature_2m=5.0))
        ottawa = _add(db_session, make_reading(
            city="Ottawa",
            reading_time=new_time,
            temperature_2m=-20.0,
            apparent_temperature=-25.0,
        ))
        events = detect_events(ottawa, db_session)
        assert not any(e.event_type == "cross_city_outlier" for e in events)


class TestFeelsLikeGap:
    def test_humid_gap_fires(self, db_session):
        r = _add(db_session, make_reading(
            temperature_2m=25.0,
            apparent_temperature=25.0 + FEELS_LIKE_GAP_C,
        ))
        events = detect_events(r, db_session)
        assert any(e.event_type == "feels_like_gap" for e in events)

    def test_wind_chill_gap_fires(self, db_session):
        r = _add(db_session, make_reading(
            temperature_2m=5.0,
            apparent_temperature=5.0 - FEELS_LIKE_GAP_C,
        ))
        events = detect_events(r, db_session)
        assert any(e.event_type == "feels_like_gap" for e in events)

    def test_small_gap_no_event(self, db_session):
        r = _add(db_session, make_reading(
            temperature_2m=20.0,
            apparent_temperature=21.5,  # only 1.5°C gap
        ))
        events = detect_events(r, db_session)
        assert not any(e.event_type == "feels_like_gap" for e in events)

    def test_description_mentions_direction(self, db_session):
        r = _add(db_session, make_reading(
            temperature_2m=28.0,
            apparent_temperature=33.0,  # +5°C humid gap
        ))
        events = detect_events(r, db_session)
        gap_event = next(e for e in events if e.event_type == "feels_like_gap")
        assert "warmer" in gap_event.description


class TestTemperatureSpike:
    def test_spike_fires(self, db_session):
        base_time = datetime(2024, 3, 1, 6, 0, 0)
        _add(db_session, make_reading(city="Ottawa", reading_time=base_time, temperature_2m=5.0))
        current = _add(db_session, make_reading(
            city="Ottawa",
            reading_time=base_time + timedelta(hours=1),
            temperature_2m=5.0 + TEMP_SPIKE_C,
        ))
        events = detect_events(current, db_session)
        assert any(e.event_type == "temperature_spike" for e in events)

    def test_small_rise_no_event(self, db_session):
        base_time = datetime(2024, 3, 1, 6, 0, 0)
        _add(db_session, make_reading(city="Ottawa", reading_time=base_time, temperature_2m=10.0))
        current = _add(db_session, make_reading(
            city="Ottawa",
            reading_time=base_time + timedelta(hours=1),
            temperature_2m=12.0,  # only 2°C rise
        ))
        events = detect_events(current, db_session)
        assert not any(e.event_type == "temperature_spike" for e in events)

    def test_drop_does_not_fire_spike(self, db_session):
        base_time = datetime(2024, 3, 1, 6, 0, 0)
        _add(db_session, make_reading(city="Ottawa", reading_time=base_time, temperature_2m=20.0))
        current = _add(db_session, make_reading(
            city="Ottawa",
            reading_time=base_time + timedelta(hours=1),
            temperature_2m=20.0 - TEMP_SPIKE_C - 2,
        ))
        events = detect_events(current, db_session)
        assert not any(e.event_type == "temperature_spike" for e in events)

    def test_no_previous_reading_no_event(self, db_session):
        r = _add(db_session, make_reading(city="Toronto", temperature_2m=25.0))
        events = detect_events(r, db_session)
        assert not any(e.event_type == "temperature_spike" for e in events)


class TestCrossCityContrast:
    def _seed_cities(self, db_session, apparent_temps: dict[str, float], base_time: datetime):
        readings = {}
        for city, apparent in apparent_temps.items():
            r = _add(db_session, make_reading(
                city=city,
                reading_time=base_time,
                temperature_2m=apparent - 2,
                apparent_temperature=apparent,
            ))
            readings[city] = r
        return readings

    def test_contrast_fires_for_outlier_city(self, db_session):
        base_time = datetime(2024, 7, 1, 14, 0, 0)
        readings = self._seed_cities(db_session, {
            "Ottawa": 20.0,
            "Vancouver": 19.0,
            "Toronto": 20.0 + CROSS_CITY_CONTRAST_C + 1,  # clearly warmer
        }, base_time)
        events = detect_events(readings["Toronto"], db_session)
        assert any(e.event_type == "cross_city_contrast" and e.city == "Toronto" for e in events)

    def test_no_contrast_when_cities_similar(self, db_session):
        base_time = datetime(2024, 7, 1, 14, 0, 0)
        readings = self._seed_cities(db_session, {
            "Ottawa": 22.0, "Toronto": 23.0, "Vancouver": 21.0,
        }, base_time)
        events = detect_events(readings["Toronto"], db_session)
        assert not any(e.event_type == "cross_city_contrast" for e in events)

    def test_contrast_requires_recent_data(self, db_session):
        old_time = datetime(2024, 7, 1, 10, 0, 0)
        new_time = datetime(2024, 7, 1, 14, 0, 0)
        _add(db_session, make_reading(city="Ottawa",    reading_time=old_time, apparent_temperature=20.0))
        _add(db_session, make_reading(city="Vancouver", reading_time=old_time, apparent_temperature=19.0))
        toronto = _add(db_session, make_reading(
            city="Toronto",
            reading_time=new_time,
            apparent_temperature=35.0,
        ))
        events = detect_events(toronto, db_session)
        assert not any(e.event_type == "cross_city_contrast" for e in events)


class TestStrongestWindCity:
    def _seed_wind(self, db_session, winds: dict[str, float], base_time: datetime):
        readings = {}
        for city, wind in winds.items():
            r = _add(db_session, make_reading(
                city=city,
                reading_time=base_time,
                wind_speed_10m=wind,
            ))
            readings[city] = r
        return readings

    def test_dominant_wind_fires(self, db_session):
        base_time = datetime(2024, 4, 1, 12, 0, 0)
        readings = self._seed_wind(db_session, {
            "Ottawa": 5.0, "Vancouver": 5.0,
            "Toronto": 5.0 + STRONGEST_WIND_MARGIN_KMH + 5,
        }, base_time)
        events = detect_events(readings["Toronto"], db_session)
        assert any(e.event_type == "strongest_wind_city" and e.city == "Toronto" for e in events)

    def test_small_margin_no_event(self, db_session):
        base_time = datetime(2024, 4, 1, 12, 0, 0)
        readings = self._seed_wind(db_session, {
            "Ottawa": 20.0, "Toronto": 25.0, "Vancouver": 22.0,
        }, base_time)
        events = detect_events(readings["Toronto"], db_session)
        assert not any(e.event_type == "strongest_wind_city" for e in events)

    def test_requires_both_other_cities(self, db_session):
        # only one other city present — should not fire
        base_time = datetime(2024, 4, 1, 12, 0, 0)
        _add(db_session, make_reading(city="Ottawa", reading_time=base_time, wind_speed_10m=5.0))
        toronto = _add(db_session, make_reading(
            city="Toronto",
            reading_time=base_time,
            wind_speed_10m=5.0 + STRONGEST_WIND_MARGIN_KMH + 10,
        ))
        events = detect_events(toronto, db_session)
        assert not any(e.event_type == "strongest_wind_city" for e in events)


class TestCooldown:
    def _seed_event(self, db_session, city: str, event_type: str, base_time: datetime, severity: str = "warning"):
        from datetime import timezone as tz
        from app.models import Event as EventModel

        anchor = _add(db_session, make_reading(city=city, reading_time=base_time))
        recent_event = EventModel(
            city=city,
            event_type=event_type,
            severity=severity,
            reading_id=anchor.id,
            reading_time=base_time,
            detected_at=datetime.now(tz.utc).replace(tzinfo=None),
            description="test event",
        )
        db_session.add(recent_event)
        db_session.flush()

    def test_same_event_suppressed_within_cooldown(self, db_session):
        base_time = datetime(2024, 6, 1, 12, 0, 0)
        self._seed_event(db_session, "Ottawa", "high_wind", base_time, severity="warning")

        r = _add(db_session, make_reading(
            city="Ottawa",
            reading_time=base_time + timedelta(minutes=15),
            wind_speed_10m=HIGH_WIND_KMH + 10,
        ))
        events = detect_events(r, db_session)
        assert not any(e.event_type == "high_wind" for e in events)

    def test_different_city_not_suppressed(self, db_session):
        base_time = datetime(2024, 6, 1, 12, 0, 0)
        self._seed_event(db_session, "Ottawa", "high_wind", base_time, severity="warning")

        r = _add(db_session, make_reading(
            city="Toronto",
            reading_time=base_time + timedelta(minutes=15),
            wind_speed_10m=HIGH_WIND_KMH + 10,
        ))
        events = detect_events(r, db_session)
        assert any(e.event_type == "high_wind" and e.city == "Toronto" for e in events)

    def test_severity_escalation_bypasses_cooldown(self, db_session):
        # warning fired recently — critical (10°C drop) should still go through
        base_time = datetime(2024, 6, 1, 12, 0, 0)
        # seed the stored event at base_time-15min so prev/current can be the real readings
        self._seed_event(db_session, "Ottawa", "rapid_temperature_drop", base_time - timedelta(minutes=15), severity="warning")

        prev = _add(db_session, make_reading(city="Ottawa", reading_time=base_time, temperature_2m=20.0))
        current = _add(db_session, make_reading(
            city="Ottawa",
            reading_time=base_time + timedelta(minutes=15),
            temperature_2m=10.0,  # 10°C drop → critical
        ))
        events = detect_events(current, db_session)
        assert any(e.event_type == "rapid_temperature_drop" and e.severity == "critical" for e in events)

    def test_same_severity_suppressed_within_cooldown(self, db_session):
        # warning fired recently — another warning should be suppressed
        base_time = datetime(2024, 6, 1, 12, 0, 0)
        # seed the stored event at base_time-15min so prev/current can be the real readings
        self._seed_event(db_session, "Ottawa", "rapid_temperature_drop", base_time - timedelta(minutes=15), severity="warning")

        prev = _add(db_session, make_reading(city="Ottawa", reading_time=base_time, temperature_2m=20.0))
        current = _add(db_session, make_reading(
            city="Ottawa",
            reading_time=base_time + timedelta(minutes=15),
            temperature_2m=13.0,  # 7°C drop → warning (same severity, should suppress)
        ))
        events = detect_events(current, db_session)
        assert not any(e.event_type == "rapid_temperature_drop" for e in events)


class TestDynamicSeverity:
    def test_rapid_drop_warning_below_10(self, db_session):
        base_time = datetime(2024, 3, 1, 12, 0, 0)
        _add(db_session, make_reading(city="Ottawa", reading_time=base_time, temperature_2m=20.0))
        r = _add(db_session, make_reading(
            city="Ottawa",
            reading_time=base_time + timedelta(hours=1),
            temperature_2m=13.0,  # 7°C drop → warning
        ))
        events = detect_events(r, db_session)
        drop = next(e for e in events if e.event_type == "rapid_temperature_drop")
        assert drop.severity == "warning"

    def test_rapid_drop_critical_at_10(self, db_session):
        base_time = datetime(2024, 3, 1, 12, 0, 0)
        _add(db_session, make_reading(city="Ottawa", reading_time=base_time, temperature_2m=20.0))
        r = _add(db_session, make_reading(
            city="Ottawa",
            reading_time=base_time + timedelta(hours=1),
            temperature_2m=10.0,  # 10°C drop → critical
        ))
        events = detect_events(r, db_session)
        drop = next(e for e in events if e.event_type == "rapid_temperature_drop")
        assert drop.severity == "critical"

    def test_feels_like_gap_info_at_4(self, db_session):
        r = _add(db_session, make_reading(temperature_2m=25.0, apparent_temperature=29.0))  # 4°C gap
        events = detect_events(r, db_session)
        gap = next(e for e in events if e.event_type == "feels_like_gap")
        assert gap.severity == "info"

    def test_feels_like_gap_warning_at_7(self, db_session):
        r = _add(db_session, make_reading(temperature_2m=25.0, apparent_temperature=32.0))  # 7°C gap
        events = detect_events(r, db_session)
        gap = next(e for e in events if e.event_type == "feels_like_gap")
        assert gap.severity == "warning"

    def test_feels_like_gap_critical_at_10(self, db_session):
        r = _add(db_session, make_reading(temperature_2m=25.0, apparent_temperature=35.0))  # 10°C gap
        events = detect_events(r, db_session)
        gap = next(e for e in events if e.event_type == "feels_like_gap")
        assert gap.severity == "critical"

    def _seed_anomaly_baseline(self, db_session, base_temp: float = 10.0):
        base_time = datetime(2024, 2, 1, 0, 0, 0)
        for i in range(ANOMALY_MIN_READINGS):
            variation = 0.5 if i % 2 == 0 else -0.5
            db_session.add(make_reading(
                city="Ottawa",
                reading_time=base_time + timedelta(hours=i),
                temperature_2m=base_temp + variation,
            ))
        db_session.flush()
        return base_time + timedelta(hours=ANOMALY_MIN_READINGS)

    def test_anomaly_info_just_above_threshold(self, db_session):
        # effective_std=1.0 (floor applied), mean=10.0; z=2.6 → info
        next_time = self._seed_anomaly_baseline(db_session)
        r = _add(db_session, make_reading(city="Ottawa", reading_time=next_time, temperature_2m=12.6))
        events = detect_events(r, db_session)
        ev = next(e for e in events if e.event_type == "temperature_anomaly")
        assert ev.severity == "info"

    def test_anomaly_warning_mid_range(self, db_session):
        # z=4.0 → warning
        next_time = self._seed_anomaly_baseline(db_session)
        r = _add(db_session, make_reading(city="Ottawa", reading_time=next_time, temperature_2m=14.0))
        events = detect_events(r, db_session)
        ev = next(e for e in events if e.event_type == "temperature_anomaly")
        assert ev.severity == "warning"

    def test_anomaly_critical_extreme(self, db_session):
        # z=6.0 → critical
        next_time = self._seed_anomaly_baseline(db_session)
        r = _add(db_session, make_reading(city="Ottawa", reading_time=next_time, temperature_2m=16.0))
        events = detect_events(r, db_session)
        ev = next(e for e in events if e.event_type == "temperature_anomaly")
        assert ev.severity == "critical"


class TestSustainedTrend:
    def _seed_trend(self, db_session, city: str, start_temp: float, step: float, n: int):
        """Insert n readings each `step` °C apart, oldest first."""
        base_time = datetime(2024, 5, 1, 8, 0, 0)
        for i in range(n):
            db_session.add(make_reading(
                city=city,
                reading_time=base_time + timedelta(minutes=15 * i),
                temperature_2m=round(start_temp + step * i, 2),
            ))
        db_session.flush()
        return base_time + timedelta(minutes=15 * n)

    def test_warming_trend_fires(self, db_session):
        # TREND_MIN_READINGS prior readings each rising 1.5°C → total 6°C ≥ TREND_WARN_C
        next_time = self._seed_trend(db_session, "Ottawa", start_temp=10.0, step=1.5, n=TREND_MIN_READINGS)
        r = _add(db_session, make_reading(
            city="Ottawa",
            reading_time=next_time,
            temperature_2m=10.0 + 1.5 * TREND_MIN_READINGS,
        ))
        events = detect_events(r, db_session)
        assert any(e.event_type == "sustained_warming_trend" for e in events)

    def test_warming_trend_warning_severity(self, db_session):
        next_time = self._seed_trend(db_session, "Ottawa", start_temp=10.0, step=1.5, n=TREND_MIN_READINGS)
        r = _add(db_session, make_reading(
            city="Ottawa",
            reading_time=next_time,
            temperature_2m=10.0 + 1.5 * TREND_MIN_READINGS,
        ))
        events = detect_events(r, db_session)
        ev = next(e for e in events if e.event_type == "sustained_warming_trend")
        assert ev.severity == "warning"

    def test_warming_trend_info_severity(self, db_session):
        # step=0.6°C × 4 readings = 2.4°C total ≥ TREND_INFO_C but < TREND_WARN_C
        next_time = self._seed_trend(db_session, "Ottawa", start_temp=10.0, step=0.6, n=TREND_MIN_READINGS)
        r = _add(db_session, make_reading(
            city="Ottawa",
            reading_time=next_time,
            temperature_2m=10.0 + 0.6 * TREND_MIN_READINGS,
        ))
        events = detect_events(r, db_session)
        ev = next(e for e in events if e.event_type == "sustained_warming_trend")
        assert ev.severity == "info"

    def test_cooling_trend_fires(self, db_session):
        # negative step → sustained cooling
        next_time = self._seed_trend(db_session, "Toronto", start_temp=20.0, step=-1.5, n=TREND_MIN_READINGS)
        r = _add(db_session, make_reading(
            city="Toronto",
            reading_time=next_time,
            temperature_2m=20.0 - 1.5 * TREND_MIN_READINGS,
        ))
        events = detect_events(r, db_session)
        assert any(e.event_type == "sustained_cooling_trend" for e in events)

    def test_no_trend_when_direction_reverses(self, db_session):
        # temperatures go up then down — not a consistent trend
        base_time = datetime(2024, 5, 2, 8, 0, 0)
        for i, temp in enumerate([10.0, 11.5, 10.8, 12.0]):
            db_session.add(make_reading(
                city="Ottawa",
                reading_time=base_time + timedelta(minutes=15 * i),
                temperature_2m=temp,
            ))
        db_session.flush()
        r = _add(db_session, make_reading(
            city="Ottawa",
            reading_time=base_time + timedelta(hours=1),
            temperature_2m=13.0,
        ))
        events = detect_events(r, db_session)
        assert not any(e.event_type in {"sustained_warming_trend", "sustained_cooling_trend"} for e in events)

    def test_no_trend_with_insufficient_history(self, db_session):
        # fewer than TREND_MIN_READINGS prior readings
        next_time = self._seed_trend(db_session, "Vancouver", start_temp=15.0, step=1.5, n=TREND_MIN_READINGS - 1)
        r = _add(db_session, make_reading(
            city="Vancouver",
            reading_time=next_time,
            temperature_2m=15.0 + 1.5 * TREND_MIN_READINGS,
        ))
        events = detect_events(r, db_session)
        assert not any(e.event_type in {"sustained_warming_trend", "sustained_cooling_trend"} for e in events)

    def test_no_trend_below_info_threshold(self, db_session):
        # consistent direction but total shift < TREND_INFO_C
        next_time = self._seed_trend(db_session, "Ottawa", start_temp=10.0, step=0.3, n=TREND_MIN_READINGS)
        r = _add(db_session, make_reading(
            city="Ottawa",
            reading_time=next_time,
            temperature_2m=10.0 + 0.3 * TREND_MIN_READINGS,
        ))
        events = detect_events(r, db_session)
        assert not any(e.event_type in {"sustained_warming_trend", "sustained_cooling_trend"} for e in events)


class TestMultipleEvents:
    def test_thunderstorm_plus_high_wind_both_fire(self, db_session):
        r = _add(db_session, make_reading(
            weather_code=95,
            wind_speed_10m=HIGH_WIND_KMH + 10,
        ))
        events = detect_events(r, db_session)
        types = {e.event_type for e in events}
        assert "severe_weather_code" in types
        assert "high_wind" in types
