#!/usr/bin/env python3
"""
Replays stored readings through all thirteen event detectors and compares
the simulated output against what is actually in the database.
Useful for checking what would change if you adjust a threshold.

Note: cooldown suppression is NOT applied here — the simulation shows what
would fire on each reading in isolation. Divergences between simulated and
stored events are expected when cooldown suppressed a real event.

Usage:
    python .cursor/skills/replay_events.py --city Ottawa --hours 48
    python .cursor/skills/replay_events.py --all-cities --hours 24 --output text
"""

import argparse
import json
import os
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker

# import thresholds directly so this stays in sync with the real detector
from app.detector import (
    ANOMALY_LOOKBACK,
    ANOMALY_MIN_READINGS,
    ANOMALY_SIGMA,
    CROSS_CITY_CONTRAST_C,
    CROSS_CITY_DELTA_C,
    FEELS_LIKE_GAP_C,
    HEAVY_PRECIP_MM,
    HIGH_WIND_KMH,
    RAPID_DROP_C,
    SEVERE_WMO_CODES,
    STRONGEST_WIND_MARGIN_KMH,
    TEMP_SPIKE_C,
    TREND_INFO_C,
    TREND_MIN_READINGS,
    TREND_WARN_C,
    WMO_LABELS,
    WIND_CHILL_DANGER_C,
)

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./data/weather.db")
ALL_CITIES = ["Ottawa", "Toronto", "Vancouver"]


def get_session():
    engine = create_engine(
        DATABASE_URL,
        connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {},
    )
    return sessionmaker(bind=engine)()


def _simulate_events(reading, preceding: list, latest_other_cities: dict) -> list[str]:
    """Return event types that would fire given the reading and its history context.

    Cooldown is not applied — callers should expect divergences where a real
    event was suppressed by cooldown but simulation would re-fire it.
    """
    fired = []
    temp = reading.temperature_2m
    apparent = reading.apparent_temperature
    wind = reading.wind_speed_10m
    precip = reading.precipitation
    code = reading.weather_code

    # --- absolute threshold events ---

    if code in SEVERE_WMO_CODES:
        label = WMO_LABELS.get(code, f"WMO {code}")
        fired.append(f"severe_weather_code ({label})")

    if apparent < WIND_CHILL_DANGER_C:
        fired.append(f"dangerous_wind_chill (apparent {apparent:.1f} °C < {WIND_CHILL_DANGER_C} °C)")

    if wind > HIGH_WIND_KMH:
        fired.append(f"high_wind ({wind:.1f} km/h > {HIGH_WIND_KMH} km/h)")

    if precip > HEAVY_PRECIP_MM:
        fired.append(f"heavy_precipitation ({precip:.1f} mm/h > {HEAVY_PRECIP_MM} mm/h)")

    gap = apparent - temp
    if abs(gap) >= FEELS_LIKE_GAP_C:
        fired.append(f"feels_like_gap (apparent {apparent:.1f} °C vs actual {temp:.1f} °C, gap {gap:+.1f} °C)")

    # --- history-dependent events ---

    preceding_temps = [r.temperature_2m for r in preceding[-ANOMALY_LOOKBACK:]]
    if len(preceding_temps) >= ANOMALY_MIN_READINGS:
        mean = statistics.mean(preceding_temps)
        raw_std = statistics.stdev(preceding_temps)
        effective_std = max(raw_std, 1.0)
        z = (temp - mean) / effective_std
        if abs(z) > ANOMALY_SIGMA:
            fired.append(f"temperature_anomaly ({temp:.1f} °C, z={z:.2f}, mean={mean:.1f} °C)")

    if preceding:
        prev_temp = preceding[-1].temperature_2m
        drop = prev_temp - temp
        rise = temp - prev_temp
        if drop >= RAPID_DROP_C:
            fired.append(f"rapid_temperature_drop ({prev_temp:.1f} → {temp:.1f} °C, drop={drop:.1f} °C)")
        if rise >= TEMP_SPIKE_C:
            fired.append(f"temperature_spike ({prev_temp:.1f} → {temp:.1f} °C, rise={rise:.1f} °C)")

    if len(preceding) >= TREND_MIN_READINGS:
        recent = [r.temperature_2m for r in preceding[-TREND_MIN_READINGS:]] + [temp]
        deltas = [recent[i + 1] - recent[i] for i in range(len(recent) - 1)]
        total = recent[-1] - recent[0]
        if all(d > 0 for d in deltas) and total >= TREND_INFO_C:
            fired.append(f"sustained_warming_trend (+{total:.1f} °C over {len(deltas)} readings)")
        elif all(d < 0 for d in deltas) and abs(total) >= TREND_INFO_C:
            fired.append(f"sustained_cooling_trend ({total:.1f} °C over {len(deltas)} readings)")

    # --- cross-city comparative events ---

    other_readings = [(city, r) for city, r in latest_other_cities.items() if r is not None]
    if len(other_readings) == 2:
        other_temps = [(city, r.temperature_2m) for city, r in other_readings]
        other_apparent = [(city, r.apparent_temperature) for city, r in other_readings]
        other_winds = [(city, r.wind_speed_10m) for city, r in other_readings]

        other_temp_avg = statistics.mean(t for _, t in other_temps)
        delta_temp = temp - other_temp_avg
        if abs(delta_temp) >= CROSS_CITY_DELTA_C:
            direction = "warmer" if delta_temp > 0 else "colder"
            other_str = ", ".join(f"{c} {t:.1f} °C" for c, t in other_temps)
            fired.append(
                f"cross_city_outlier ({temp:.1f} °C is {abs(delta_temp):.1f} °C {direction} than others: {other_str})"
            )

        other_apparent_avg = statistics.mean(t for _, t in other_apparent)
        delta_apparent = apparent - other_apparent_avg
        if abs(delta_apparent) >= CROSS_CITY_CONTRAST_C:
            direction = "warmer" if delta_apparent > 0 else "colder"
            fired.append(
                f"cross_city_contrast (apparent {apparent:.1f} °C, {abs(delta_apparent):.1f} °C {direction} than others)"
            )

        other_wind_avg = statistics.mean(w for _, w in other_winds)
        wind_margin = wind - other_wind_avg
        if wind_margin >= STRONGEST_WIND_MARGIN_KMH:
            fired.append(
                f"strongest_wind_city ({wind:.1f} km/h, {wind_margin:.1f} km/h above others' avg)"
            )

    return fired


def replay_city(db, city: str, hours: int) -> dict:
    from app.models import Event, Reading

    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=hours)

    window_readings = (
        db.query(Reading)
        .filter(Reading.city == city, Reading.reading_time >= cutoff)
        .order_by(Reading.reading_time.asc())
        .all()
    )

    if not window_readings:
        return {
            "city": city,
            "hours": hours,
            "readings_in_window": 0,
            "message": "No readings found for this city in the specified window.",
            "log": [],
        }

    # grab readings before the window for anomaly baseline context
    pre_window = (
        db.query(Reading)
        .filter(Reading.city == city, Reading.reading_time < cutoff)
        .order_by(Reading.reading_time.desc())
        .limit(ANOMALY_LOOKBACK)
        .all()
    )
    preceding = list(reversed(pre_window))

    log = []

    for reading in window_readings:
        stored_events = (
            db.query(Event.event_type)
            .filter(Event.reading_id == reading.id)
            .all()
        )
        stored_types = sorted({row.event_type for row in stored_events})

        # only compare against other-city readings within 2h of this reading
        cutoff_2h = reading.reading_time - timedelta(hours=2)
        latest_other: dict = {}
        for other_city in ALL_CITIES:
            if other_city == city:
                continue
            other_row = (
                db.query(Reading)
                .filter(
                    Reading.city == other_city,
                    Reading.reading_time <= reading.reading_time,
                    Reading.reading_time >= cutoff_2h,
                )
                .order_by(Reading.reading_time.desc())
                .first()
            )
            latest_other[other_city] = other_row

        simulated = _simulate_events(reading, preceding, latest_other)
        simulated_types = sorted({s.split(" (")[0] for s in simulated})

        divergence = {
            "simulated_not_stored": [t for t in simulated_types if t not in stored_types],
            "stored_not_simulated": [t for t in stored_types if t not in simulated_types],
        }
        has_divergence = any(divergence[k] for k in divergence)

        log.append({
            "reading_time": reading.reading_time.isoformat(),
            "temp_c": reading.temperature_2m,
            "apparent_c": reading.apparent_temperature,
            "wind_kmh": reading.wind_speed_10m,
            "precip_mm": reading.precipitation,
            "weather_code": reading.weather_code,
            "simulated_events": simulated,
            "stored_event_types": stored_types,
            "divergence": divergence if has_divergence else None,
        })

        preceding.append(reading)

    total_simulated = sum(len(e["simulated_events"]) for e in log)
    total_stored = sum(len(e["stored_event_types"]) for e in log)
    divergent_readings = [e for e in log if e["divergence"]]

    return {
        "city": city,
        "hours": hours,
        "readings_in_window": len(window_readings),
        "total_simulated_events": total_simulated,
        "total_stored_events": total_stored,
        "readings_with_divergence": len(divergent_readings),
        "log": log,
    }


def format_text(result: dict) -> str:
    lines = [
        f"=== Replay: {result['city']} (last {result['hours']}h) ===",
        f"Readings: {result['readings_in_window']}  "
        f"Simulated: {result.get('total_simulated_events', 0)}  "
        f"Stored: {result.get('total_stored_events', 0)}  "
        f"Divergent: {result.get('readings_with_divergence', 0)}",
        "",
    ]
    for entry in result.get("log", []):
        fired = entry["simulated_events"]
        stored = entry["stored_event_types"]
        divergence = entry.get("divergence")
        if not fired and not stored and not divergence:
            continue
        lines.append(
            f"  {entry['reading_time']}  "
            f"T={entry['temp_c']:.1f}°C  "
            f"W={entry['wind_kmh']:.0f}km/h  "
            f"P={entry['precip_mm']:.1f}mm"
        )
        for e in fired:
            lines.append(f"    + {e}")
        if divergence:
            for t in divergence["simulated_not_stored"]:
                lines.append(f"    ! SIMULATED NOT STORED: {t}")
            for t in divergence["stored_not_simulated"]:
                lines.append(f"    ! STORED NOT SIMULATED: {t}")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Replay stored readings through event detection")
    parser.add_argument("--city", choices=ALL_CITIES)
    parser.add_argument("--all-cities", action="store_true")
    parser.add_argument("--hours", type=int, default=24)
    parser.add_argument("--output", choices=["json", "text"], default="json")
    args = parser.parse_args()

    if not args.city and not args.all_cities:
        parser.error("Provide --city <name> or --all-cities")

    cities = ALL_CITIES if args.all_cities else [args.city]

    try:
        db = get_session()
    except Exception as exc:
        print(json.dumps({"error": f"Could not connect to database: {exc}"}))
        sys.exit(1)

    try:
        inspector = inspect(db.bind)
        if not inspector.has_table("readings"):
            print(json.dumps({
                "error": "Database schema not found.",
                "hint": "Start the service first: docker compose up --build",
            }))
            sys.exit(1)

        results = [replay_city(db, city, args.hours) for city in cities]

        if args.output == "text":
            for r in results:
                print(format_text(r))
                print()
        else:
            payload = results[0] if len(results) == 1 else {"cities": results}
            print(json.dumps(payload, indent=2, default=str))

    except Exception as exc:
        print(json.dumps({"error": str(exc)}))
        sys.exit(1)
    finally:
        db.close()


if __name__ == "__main__":
    main()
