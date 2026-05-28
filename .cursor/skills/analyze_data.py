#!/usr/bin/env python3
"""
Query the WatchAgent database and return structured analysis as JSON.

Usage:
    python .cursor/skills/analyze_data.py --mode summary
    python .cursor/skills/analyze_data.py --mode city_comparison
    python .cursor/skills/analyze_data.py --mode event_breakdown
    python .cursor/skills/analyze_data.py --mode temperature_trend --city Ottawa --hours 48
    python .cursor/skills/analyze_data.py --mode recent_events --limit 10
    python .cursor/skills/analyze_data.py --mode anomaly_replay --city Toronto --hours 24
"""

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from sqlalchemy import create_engine, func
from sqlalchemy.orm import sessionmaker

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./data/weather.db")


def get_session():
    engine = create_engine(
        DATABASE_URL,
        connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {},
    )
    return sessionmaker(bind=engine)()


def _err(msg: str) -> dict:
    return {"error": msg}


def mode_summary(db) -> dict:
    from app.models import Reading, Event

    total_readings = db.query(func.count(Reading.id)).scalar()
    total_events = db.query(func.count(Event.id)).scalar()

    cities = ["Ottawa", "Toronto", "Vancouver"]
    city_stats = {}

    for city in cities:
        count = db.query(func.count(Reading.id)).filter(Reading.city == city).scalar()
        latest = (
            db.query(Reading)
            .filter(Reading.city == city)
            .order_by(Reading.reading_time.desc())
            .first()
        )
        event_count = db.query(func.count(Event.id)).filter(Event.city == city).scalar()

        city_stats[city] = {
            "reading_count": count,
            "event_count": event_count,
            "latest_reading_time": latest.reading_time.isoformat() if latest else None,
            "latest_temp_c": latest.temperature_2m if latest else None,
            "latest_wind_kmh": latest.wind_speed_10m if latest else None,
            "latest_weather_code": latest.weather_code if latest else None,
        }

    earliest = db.query(func.min(Reading.reading_time)).scalar()
    latest_ts = db.query(func.max(Reading.reading_time)).scalar()

    return {
        "mode": "summary",
        "total_readings": total_readings,
        "total_events": total_events,
        "data_from": earliest.isoformat() if earliest else None,
        "data_to": latest_ts.isoformat() if latest_ts else None,
        "cities": city_stats,
    }


def mode_city_comparison(db) -> dict:
    from app.models import Reading

    cities = ["Ottawa", "Toronto", "Vancouver"]
    result = {}

    for city in cities:
        stats = db.query(
            func.min(Reading.temperature_2m).label("min_temp"),
            func.max(Reading.temperature_2m).label("max_temp"),
            func.avg(Reading.temperature_2m).label("avg_temp"),
            func.avg(Reading.wind_speed_10m).label("avg_wind"),
            func.max(Reading.wind_speed_10m).label("max_wind"),
            func.avg(Reading.precipitation).label("avg_precip"),
            func.max(Reading.precipitation).label("max_precip"),
        ).filter(Reading.city == city).first()

        latest = (
            db.query(Reading)
            .filter(Reading.city == city)
            .order_by(Reading.reading_time.desc())
            .first()
        )

        result[city] = {
            "min_temp_c": round(stats.min_temp, 1) if stats.min_temp is not None else None,
            "max_temp_c": round(stats.max_temp, 1) if stats.max_temp is not None else None,
            "avg_temp_c": round(stats.avg_temp, 1) if stats.avg_temp is not None else None,
            "avg_wind_kmh": round(stats.avg_wind, 1) if stats.avg_wind is not None else None,
            "max_wind_kmh": round(stats.max_wind, 1) if stats.max_wind is not None else None,
            "avg_precip_mm": round(stats.avg_precip, 2) if stats.avg_precip is not None else None,
            "max_precip_mm": round(stats.max_precip, 2) if stats.max_precip is not None else None,
            "current_temp_c": latest.temperature_2m if latest else None,
            "current_wind_kmh": latest.wind_speed_10m if latest else None,
        }

    current_temps = {
        city: result[city]["current_temp_c"]
        for city in cities
        if result[city]["current_temp_c"] is not None
    }
    if len(current_temps) >= 2:
        spread = max(current_temps.values()) - min(current_temps.values())
        result["cross_city"] = {
            "current_temp_spread_c": round(spread, 1),
            "warmest_city": max(current_temps, key=current_temps.get),
            "coldest_city": min(current_temps, key=current_temps.get),
        }

    return {"mode": "city_comparison", "cities": result}


def mode_event_breakdown(db) -> dict:
    from app.models import Event

    rows = (
        db.query(Event.event_type, Event.city, func.count(Event.id).label("cnt"))
        .group_by(Event.event_type, Event.city)
        .all()
    )

    breakdown: dict[str, dict] = {}
    for row in rows:
        if row.event_type not in breakdown:
            breakdown[row.event_type] = {"total": 0, "by_city": {}}
        breakdown[row.event_type]["by_city"][row.city] = row.cnt
        breakdown[row.event_type]["total"] += row.cnt

    for event_type in breakdown:
        latest = (
            db.query(Event)
            .filter(Event.event_type == event_type)
            .order_by(Event.reading_time.desc())
            .first()
        )
        if latest:
            breakdown[event_type]["most_recent"] = {
                "city": latest.city,
                "reading_time": latest.reading_time.isoformat(),
                "description": latest.description,
            }

    return {
        "mode": "event_breakdown",
        "event_types": breakdown,
        "total_events": sum(v["total"] for v in breakdown.values()),
    }


def mode_temperature_trend(db, city: str, hours: int) -> dict:
    from app.models import Reading

    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=hours)
    readings = (
        db.query(Reading)
        .filter(Reading.city == city, Reading.reading_time >= cutoff)
        .order_by(Reading.reading_time.asc())
        .all()
    )

    if not readings:
        return {"mode": "temperature_trend", "city": city, "hours": hours, "readings": []}

    temps = [r.temperature_2m for r in readings]
    series = [
        {
            "time": r.reading_time.isoformat(),
            "temp_c": r.temperature_2m,
            "apparent_c": r.apparent_temperature,
            "wind_kmh": r.wind_speed_10m,
            "precip_mm": r.precipitation,
        }
        for r in readings
    ]

    return {
        "mode": "temperature_trend",
        "city": city,
        "hours": hours,
        "reading_count": len(readings),
        "min_temp_c": round(min(temps), 1),
        "max_temp_c": round(max(temps), 1),
        "temp_range_c": round(max(temps) - min(temps), 1),
        "series": series,
    }


def mode_recent_events(db, limit: int) -> dict:
    from app.models import Event

    events = (
        db.query(Event)
        .order_by(Event.reading_time.desc())
        .limit(limit)
        .all()
    )

    return {
        "mode": "recent_events",
        "limit": limit,
        "count": len(events),
        "events": [e.to_dict() for e in events],
    }


def mode_anomaly_replay(db, city: str, hours: int) -> dict:
    """
    Replay readings through detection logic without touching the DB.
    Useful for calibrating thresholds after you have real data.
    """
    import statistics
    from app.models import Reading
    from app.detector import (
        ANOMALY_SIGMA, ANOMALY_MIN_READINGS, ANOMALY_LOOKBACK,
        HIGH_WIND_KMH, HEAVY_PRECIP_MM, RAPID_DROP_C, WIND_CHILL_DANGER_C,
        SEVERE_WMO_CODES,
    )

    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=hours)
    readings = (
        db.query(Reading)
        .filter(Reading.city == city, Reading.reading_time >= cutoff)
        .order_by(Reading.reading_time.asc())
        .all()
    )

    all_prior = (
        db.query(Reading.reading_time, Reading.temperature_2m)
        .filter(Reading.city == city, Reading.reading_time < cutoff)
        .order_by(Reading.reading_time.desc())
        .limit(ANOMALY_LOOKBACK)
        .all()
    )
    prior_temps = [r.temperature_2m for r in all_prior]

    replay_log = []
    prev_temp = None

    for r in readings:
        fired = []

        if r.weather_code in SEVERE_WMO_CODES:
            fired.append(f"severe_weather_code (WMO {r.weather_code})")
        if r.apparent_temperature < WIND_CHILL_DANGER_C:
            fired.append(f"dangerous_wind_chill ({r.apparent_temperature:.1f} °C)")
        if r.wind_speed_10m > HIGH_WIND_KMH:
            fired.append(f"high_wind ({r.wind_speed_10m:.1f} km/h)")
        if r.precipitation > HEAVY_PRECIP_MM:
            fired.append(f"heavy_precipitation ({r.precipitation:.1f} mm)")

        window = prior_temps[-ANOMALY_LOOKBACK:]
        if len(window) >= ANOMALY_MIN_READINGS:
            mean = statistics.mean(window)
            effective_std = max(statistics.stdev(window), 1.0)
            z = (r.temperature_2m - mean) / effective_std
            if abs(z) > ANOMALY_SIGMA:
                fired.append(f"temperature_anomaly (z={z:.2f})")

        if prev_temp is not None and (prev_temp - r.temperature_2m) >= RAPID_DROP_C:
            fired.append(f"rapid_temperature_drop ({prev_temp:.1f} → {r.temperature_2m:.1f} °C)")

        prior_temps.append(r.temperature_2m)
        prev_temp = r.temperature_2m

        replay_log.append({
            "time": r.reading_time.isoformat(),
            "temp_c": r.temperature_2m,
            "would_fire": fired,
        })

    total_fired = sum(len(e["would_fire"]) for e in replay_log)

    return {
        "mode": "anomaly_replay",
        "city": city,
        "hours": hours,
        "readings_replayed": len(readings),
        "total_events_would_fire": total_fired,
        "log": replay_log,
    }


def main():
    parser = argparse.ArgumentParser(description="WatchAgent data analysis skill")
    parser.add_argument(
        "--mode",
        choices=[
            "summary", "city_comparison", "event_breakdown",
            "temperature_trend", "recent_events", "anomaly_replay",
        ],
        required=True,
    )
    parser.add_argument("--city", default="Ottawa")
    parser.add_argument("--hours", type=int, default=24)
    parser.add_argument("--limit", type=int, default=10)
    args = parser.parse_args()

    try:
        db = get_session()
    except Exception as e:
        print(json.dumps(_err(f"Could not connect to database: {e}")))
        sys.exit(1)

    try:
        if args.mode == "summary":
            result = mode_summary(db)
        elif args.mode == "city_comparison":
            result = mode_city_comparison(db)
        elif args.mode == "event_breakdown":
            result = mode_event_breakdown(db)
        elif args.mode == "temperature_trend":
            result = mode_temperature_trend(db, args.city, args.hours)
        elif args.mode == "recent_events":
            result = mode_recent_events(db, args.limit)
        elif args.mode == "anomaly_replay":
            result = mode_anomaly_replay(db, args.city, args.hours)
        else:
            result = _err(f"Unknown mode: {args.mode}")

        print(json.dumps(result, indent=2, default=str))
    except Exception as e:
        print(json.dumps(_err(str(e))))
        sys.exit(1)
    finally:
        db.close()


if __name__ == "__main__":
    main()
