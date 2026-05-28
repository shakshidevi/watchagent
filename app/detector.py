"""Event detection logic for WatchAgent — thirteen event types across three categories."""

import logging
import statistics
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy.orm import Session

from app.models import Event, Reading

logger = logging.getLogger(__name__)

# WMO codes we treat as severe — full list at open-meteo.com/en/docs
SEVERE_WMO_CODES = frozenset([
    95, 96, 99,  # thunderstorms
    57, 67,      # freezing drizzle/rain
    73, 75, 77,  # snow
    82, 86,      # violent showers
])

WMO_LABELS = {
    57: "heavy freezing drizzle",
    67: "heavy freezing rain",
    73: "moderate snow",
    75: "heavy snow",
    77: "snow grains",
    82: "violent rain showers",
    86: "heavy snow showers",
    95: "thunderstorm",
    96: "thunderstorm with slight hail",
    99: "thunderstorm with heavy hail",
}

# Environment Canada wind chill warning level
WIND_CHILL_DANGER_C = -20.0
# Environment Canada wind warning threshold
HIGH_WIND_KMH = 70.0
# urban flash-flood threshold
HEAVY_PRECIP_MM = 10.0
# rapid drops signal cold fronts or flash freezes
RAPID_DROP_C = 6.0
# rapid warming is also worth flagging
TEMP_SPIKE_C = 5.0
# extreme localized outlier vs the other two cities (temperature_2m)
CROSS_CITY_DELTA_C = 15.0
# notable cross-city difference in how it actually feels (apparent_temperature)
CROSS_CITY_CONTRAST_C = 8.0
# humidity or wind making felt temperature noticeably different from actual
FEELS_LIKE_GAP_C = 4.0
# one city's wind leads the others by this margin
STRONGEST_WIND_MARGIN_KMH = 20.0

ANOMALY_SIGMA = 2.5
ANOMALY_MIN_READINGS = 6
ANOMALY_LOOKBACK = 24

# sustained trend: all N consecutive readings must move in the same direction
# and the total shift must exceed this threshold to be worth flagging
TREND_MIN_READINGS = 4   # how many prior readings to look back
TREND_WARN_C = 4.0       # °C total shift across the window for a warning
TREND_INFO_C = 2.0       # °C total shift for info (gentler but consistent)

# minimum hours between the same event type firing for the same city
COOLDOWN_HOURS: dict[str, float] = {
    "severe_weather_code":    1.0,
    "dangerous_wind_chill":   2.0,
    "high_wind":              2.0,
    "heavy_precipitation":    1.0,
    "temperature_anomaly":    6.0,
    "rapid_temperature_drop": 3.0,
    "temperature_spike":      3.0,
    "cross_city_outlier":     3.0,
    "cross_city_contrast":    3.0,
    "feels_like_gap":         4.0,
    "strongest_wind_city":        2.0,
    "sustained_warming_trend":    3.0,
    "sustained_cooling_trend":    3.0,
}

# severity escalation is allowed through cooldown — only same-or-lower severity is suppressed
SEVERITY_RANK = {"info": 0, "warning": 1, "critical": 2}


def _recently_fired(db: Session, city: str, event_type: str, new_severity: str) -> bool:
    hours = COOLDOWN_HOURS.get(event_type, 3.0)
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=hours)
    recent = (
        db.query(Event.severity)
        .filter(
            Event.city == city,
            Event.event_type == event_type,
            Event.detected_at >= cutoff,
        )
        .order_by(Event.detected_at.desc())
        .first()
    )
    if recent is None:
        return False
    # suppress if new severity is same or lower — allow if it escalated
    return SEVERITY_RANK.get(new_severity, 0) <= SEVERITY_RANK.get(recent.severity, 0)


def detect_events(new_reading: Reading, db: Session) -> list[Event]:
    """Run all checks on a new reading and return any events to store."""
    candidates: list[Event] = []

    _check_severe_weather_code(new_reading, candidates)
    _check_dangerous_wind_chill(new_reading, candidates)
    _check_high_wind(new_reading, candidates)
    _check_heavy_precipitation(new_reading, candidates)
    _check_feels_like_gap(new_reading, candidates)
    _check_temperature_anomaly(new_reading, db, candidates)
    _check_rapid_temperature_drop(new_reading, db, candidates)
    _check_temperature_spike(new_reading, db, candidates)
    _check_sustained_trend(new_reading, db, candidates)
    _check_cross_city_outlier(new_reading, db, candidates)
    _check_cross_city_contrast(new_reading, db, candidates)
    _check_strongest_wind_city(new_reading, db, candidates)

    # apply cooldown — drop anything that fired too recently at same or higher severity
    events = [e for e in candidates if not _recently_fired(db, e.city, e.event_type, e.severity)]

    if events:
        logger.info(
            "%d event(s) for %s at %s: %s",
            len(events),
            new_reading.city,
            new_reading.reading_time,
            [e.event_type for e in events],
        )

    return events


def _make_event(
    reading: Reading,
    event_type: str,
    severity: str,
    description: str,
    metric_name: Optional[str] = None,
    metric_value: Optional[float] = None,
    baseline_value: Optional[float] = None,
) -> Event:
    return Event(
        city=reading.city,
        event_type=event_type,
        severity=severity,
        reading_id=reading.id,
        reading_time=reading.reading_time,
        detected_at=datetime.now(timezone.utc).replace(tzinfo=None),
        description=description,
        metric_name=metric_name,
        metric_value=metric_value,
        baseline_value=baseline_value,
    )


def _check_severe_weather_code(reading: Reading, events: list[Event]) -> None:
    code = reading.weather_code
    if code in SEVERE_WMO_CODES:
        label = WMO_LABELS.get(code, f"WMO code {code}")
        severity = "critical" if code in {95, 96, 99, 67} else "warning"
        events.append(
            _make_event(
                reading,
                event_type="severe_weather_code",
                severity=severity,
                description=f"{reading.city}: severe weather — {label} (WMO {code})",
                metric_name="weather_code",
                metric_value=float(code),
            )
        )


def _check_dangerous_wind_chill(reading: Reading, events: list[Event]) -> None:
    apparent = reading.apparent_temperature
    if apparent < WIND_CHILL_DANGER_C:
        gap = reading.temperature_2m - apparent
        events.append(
            _make_event(
                reading,
                event_type="dangerous_wind_chill",
                severity="critical",
                description=(
                    f"{reading.city}: dangerous wind chill — apparent temperature "
                    f"{apparent:.1f} °C (actual {reading.temperature_2m:.1f} °C, "
                    f"chill factor {gap:.1f} °C)"
                ),
                metric_name="apparent_temperature",
                metric_value=apparent,
                baseline_value=WIND_CHILL_DANGER_C,
            )
        )


def _check_high_wind(reading: Reading, events: list[Event]) -> None:
    wind = reading.wind_speed_10m
    if wind > HIGH_WIND_KMH:
        events.append(
            _make_event(
                reading,
                event_type="high_wind",
                severity="warning",
                description=(
                    f"{reading.city}: high wind — {wind:.1f} km/h "
                    f"(threshold {HIGH_WIND_KMH:.0f} km/h)"
                ),
                metric_name="wind_speed_10m",
                metric_value=wind,
                baseline_value=HIGH_WIND_KMH,
            )
        )


def _check_heavy_precipitation(reading: Reading, events: list[Event]) -> None:
    precip = reading.precipitation
    if precip > HEAVY_PRECIP_MM:
        events.append(
            _make_event(
                reading,
                event_type="heavy_precipitation",
                severity="warning",
                description=(
                    f"{reading.city}: heavy precipitation — {precip:.1f} mm/h "
                    f"(threshold {HEAVY_PRECIP_MM:.0f} mm/h)"
                ),
                metric_name="precipitation",
                metric_value=precip,
                baseline_value=HEAVY_PRECIP_MM,
            )
        )


def _check_feels_like_gap(reading: Reading, events: list[Event]) -> None:
    """Fires when apparent temperature diverges noticeably from actual — humidity or wind effect."""
    gap = reading.apparent_temperature - reading.temperature_2m
    if abs(gap) >= FEELS_LIKE_GAP_C:
        if gap > 0:
            reason = f"humid conditions making it feel {gap:.1f} °C warmer than actual"
        else:
            reason = f"wind chill making it feel {abs(gap):.1f} °C colder than actual"
        severity = "critical" if abs(gap) >= 10 else "warning" if abs(gap) >= 7 else "info"
        events.append(
            _make_event(
                reading,
                event_type="feels_like_gap",
                severity=severity,
                description=(
                    f"{reading.city}: {reason} "
                    f"(actual {reading.temperature_2m:.1f} °C, "
                    f"feels like {reading.apparent_temperature:.1f} °C)"
                ),
                metric_name="apparent_temperature",
                metric_value=reading.apparent_temperature,
                baseline_value=reading.temperature_2m,
            )
        )


def _check_temperature_anomaly(reading: Reading, db: Session, events: list[Event]) -> None:
    # use readings before this one so we don't evaluate against ourselves
    history = (
        db.query(Reading.temperature_2m)
        .filter(
            Reading.city == reading.city,
            Reading.id != reading.id,
        )
        .order_by(Reading.reading_time.desc())
        .limit(ANOMALY_LOOKBACK)
        .all()
    )

    temps = [row.temperature_2m for row in history]
    if len(temps) < ANOMALY_MIN_READINGS:
        return

    mean = statistics.mean(temps)
    raw_std = statistics.stdev(temps)
    # floor at 1 °C: a rock-stable baseline can't inflate z-scores from trivial shifts
    effective_std = max(raw_std, 1.0)

    z = (reading.temperature_2m - mean) / effective_std
    if abs(z) > ANOMALY_SIGMA:
        delta = reading.temperature_2m - mean
        delta_str = f"+{delta:.1f}" if delta > 0 else f"{delta:.1f}"
        direction = "warmer" if z > 0 else "colder"
        baseline_desc = "stable" if raw_std < 1.0 else "variable"
        severity = "critical" if abs(z) > 5.0 else "warning" if abs(z) > 3.0 else "info"
        events.append(
            _make_event(
                reading,
                event_type="temperature_anomaly",
                severity=severity,
                description=(
                    f"{reading.city}: temperature anomaly — {reading.temperature_2m:.1f} °C is "
                    f"{direction} than its recent trend "
                    f"({delta_str} °C from a {baseline_desc} average of {mean:.1f} °C; "
                    f"{abs(z):.1f}σ deviation over last {len(temps)} readings)"
                ),
                metric_name="temperature_2m",
                metric_value=reading.temperature_2m,
                baseline_value=mean,
            )
        )


def _check_rapid_temperature_drop(reading: Reading, db: Session, events: list[Event]) -> None:
    # only flag drops — a rapid warm-up isn't dangerous, a sudden drop can mean a cold front
    prev = (
        db.query(Reading)
        .filter(
            Reading.city == reading.city,
            Reading.id != reading.id,
        )
        .order_by(Reading.reading_time.desc())
        .first()
    )

    if prev is None:
        return

    drop = prev.temperature_2m - reading.temperature_2m
    if drop >= RAPID_DROP_C:
        severity = "critical" if drop >= 10 else "warning"
        events.append(
            _make_event(
                reading,
                event_type="rapid_temperature_drop",
                severity=severity,
                description=(
                    f"{reading.city}: rapid temperature drop — "
                    f"{prev.temperature_2m:.1f} °C → {reading.temperature_2m:.1f} °C "
                    f"(−{drop:.1f} °C since previous reading)"
                ),
                metric_name="temperature_2m",
                metric_value=reading.temperature_2m,
                baseline_value=prev.temperature_2m,
            )
        )


def _check_temperature_spike(reading: Reading, db: Session, events: list[Event]) -> None:
    """Rapid warming — a cold morning flipping warm can be just as notable as a drop."""
    prev = (
        db.query(Reading)
        .filter(
            Reading.city == reading.city,
            Reading.id != reading.id,
        )
        .order_by(Reading.reading_time.desc())
        .first()
    )

    if prev is None:
        return

    rise = reading.temperature_2m - prev.temperature_2m
    if rise >= TEMP_SPIKE_C:
        severity = "warning" if rise >= 8 else "info"
        events.append(
            _make_event(
                reading,
                event_type="temperature_spike",
                severity=severity,
                description=(
                    f"{reading.city}: rapid temperature rise — "
                    f"{prev.temperature_2m:.1f} °C → {reading.temperature_2m:.1f} °C "
                    f"(+{rise:.1f} °C since previous reading)"
                ),
                metric_name="temperature_2m",
                metric_value=reading.temperature_2m,
                baseline_value=prev.temperature_2m,
            )
        )


def _check_sustained_trend(reading: Reading, db: Session, events: list[Event]) -> None:
    # catches gradual directional shifts — different from the single-jump drop/spike checks
    history = (
        db.query(Reading.temperature_2m)
        .filter(
            Reading.city == reading.city,
            Reading.id != reading.id,
        )
        .order_by(Reading.reading_time.desc())
        .limit(TREND_MIN_READINGS)
        .all()
    )

    if len(history) < TREND_MIN_READINGS:
        return

    # oldest → newest → current
    temps = [row.temperature_2m for row in reversed(history)] + [reading.temperature_2m]
    deltas = [temps[i + 1] - temps[i] for i in range(len(temps) - 1)]
    total = temps[-1] - temps[0]

    if all(d > 0 for d in deltas) and total >= TREND_INFO_C:
        severity = "warning" if total >= TREND_WARN_C else "info"
        rate = total / len(deltas)
        events.append(
            _make_event(
                reading,
                event_type="sustained_warming_trend",
                severity=severity,
                description=(
                    f"{reading.city}: sustained warming trend — temperature has risen "
                    f"+{total:.1f} °C across {len(deltas)} consecutive readings "
                    f"(~{rate:.1f} °C per reading, from {temps[0]:.1f} °C to {reading.temperature_2m:.1f} °C)"
                ),
                metric_name="temperature_2m",
                metric_value=reading.temperature_2m,
                baseline_value=temps[0],
            )
        )
    elif all(d < 0 for d in deltas) and abs(total) >= TREND_INFO_C:
        severity = "warning" if abs(total) >= TREND_WARN_C else "info"
        rate = abs(total) / len(deltas)
        events.append(
            _make_event(
                reading,
                event_type="sustained_cooling_trend",
                severity=severity,
                description=(
                    f"{reading.city}: sustained cooling trend — temperature has dropped "
                    f"{total:.1f} °C across {len(deltas)} consecutive readings "
                    f"(~{rate:.1f} °C per reading, from {temps[0]:.1f} °C to {reading.temperature_2m:.1f} °C)"
                ),
                metric_name="temperature_2m",
                metric_value=reading.temperature_2m,
                baseline_value=temps[0],
            )
        )


def _check_cross_city_outlier(reading: Reading, db: Session, events: list[Event]) -> None:
    # only compare against recent readings (within 2h) to avoid stale data
    all_cities = {"Ottawa", "Toronto", "Vancouver"}
    other_cities = all_cities - {reading.city}

    cutoff = reading.reading_time - timedelta(hours=2)
    other_readings: list[tuple[str, float]] = []

    for city in other_cities:
        row = (
            db.query(Reading.city, Reading.temperature_2m)
            .filter(Reading.city == city, Reading.reading_time >= cutoff)
            .order_by(Reading.reading_time.desc())
            .first()
        )
        if row:
            other_readings.append((row.city, row.temperature_2m))

    if len(other_readings) < 2:
        return

    other_temps = [t for _, t in other_readings]
    other_avg = statistics.mean(other_temps)
    delta = reading.temperature_2m - other_avg

    if abs(delta) >= CROSS_CITY_DELTA_C:
        direction = "warmer" if delta > 0 else "colder"
        other_summary = ", ".join(f"{city} {temp:.1f} °C" for city, temp in other_readings)
        severity = "critical" if abs(delta) >= 25 else "warning" if abs(delta) >= 20 else "info"
        events.append(
            _make_event(
                reading,
                event_type="cross_city_outlier",
                severity=severity,
                description=(
                    f"{reading.city}: cross-city outlier — {reading.temperature_2m:.1f} °C "
                    f"is {abs(delta):.1f} °C {direction} than the other cities "
                    f"({other_summary})"
                ),
                metric_name="temperature_2m",
                metric_value=reading.temperature_2m,
                baseline_value=other_avg,
            )
        )


def _check_cross_city_contrast(reading: Reading, db: Session, events: list[Event]) -> None:
    # like cross_city_outlier but uses apparent temp and a lower bar — catches humidity/wind contrasts
    all_cities = {"Ottawa", "Toronto", "Vancouver"}
    other_cities = all_cities - {reading.city}

    cutoff = reading.reading_time - timedelta(hours=2)
    other_readings: list[tuple[str, float]] = []

    for city in other_cities:
        row = (
            db.query(Reading.city, Reading.apparent_temperature)
            .filter(Reading.city == city, Reading.reading_time >= cutoff)
            .order_by(Reading.reading_time.desc())
            .first()
        )
        if row:
            other_readings.append((row.city, row.apparent_temperature))

    if len(other_readings) < 2:
        return

    other_apparent = [t for _, t in other_readings]
    other_avg = statistics.mean(other_apparent)
    delta = reading.apparent_temperature - other_avg

    if abs(delta) >= CROSS_CITY_CONTRAST_C:
        direction = "warmer" if delta > 0 else "colder"
        other_summary = ", ".join(f"{city} feels {temp:.1f} °C" for city, temp in other_readings)
        severity = "critical" if abs(delta) >= 16 else "warning" if abs(delta) >= 12 else "info"
        events.append(
            _make_event(
                reading,
                event_type="cross_city_contrast",
                severity=severity,
                description=(
                    f"{reading.city} feels {abs(delta):.1f} °C {direction} than the other cities "
                    f"({reading.city} feels {reading.apparent_temperature:.1f} °C vs {other_summary})"
                ),
                metric_name="apparent_temperature",
                metric_value=reading.apparent_temperature,
                baseline_value=other_avg,
            )
        )


def _check_strongest_wind_city(reading: Reading, db: Session, events: list[Event]) -> None:
    # localized wind events that wouldn't cross the absolute high-wind threshold
    all_cities = {"Ottawa", "Toronto", "Vancouver"}
    other_cities = all_cities - {reading.city}

    cutoff = reading.reading_time - timedelta(hours=2)
    other_readings: list[tuple[str, float]] = []

    for city in other_cities:
        row = (
            db.query(Reading.city, Reading.wind_speed_10m)
            .filter(Reading.city == city, Reading.reading_time >= cutoff)
            .order_by(Reading.reading_time.desc())
            .first()
        )
        if row:
            other_readings.append((row.city, row.wind_speed_10m))

    if len(other_readings) < 2:
        return

    other_winds = [w for _, w in other_readings]
    other_avg = statistics.mean(other_winds)
    margin = reading.wind_speed_10m - other_avg

    if margin >= STRONGEST_WIND_MARGIN_KMH:
        other_summary = ", ".join(f"{city} {wind:.0f} km/h" for city, wind in other_readings)
        severity = "warning" if margin >= 35 else "info"
        events.append(
            _make_event(
                reading,
                event_type="strongest_wind_city",
                severity=severity,
                description=(
                    f"{reading.city} has the strongest winds among monitored cities — "
                    f"{reading.wind_speed_10m:.0f} km/h "
                    f"({margin:.0f} km/h above the others: {other_summary})"
                ),
                metric_name="wind_speed_10m",
                metric_value=reading.wind_speed_10m,
                baseline_value=other_avg,
            )
        )
