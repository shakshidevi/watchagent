"""
Fetches current weather from Open-Meteo for Ottawa, Toronto, and Vancouver.
Open-Meteo updates current conditions roughly every 15 minutes, so most of our
10-minute polls return a timestamp we've already seen — those get silently dropped
by the unique constraint.
"""

import logging
import os
from datetime import datetime, timezone

import httpx
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.detector import detect_events
from app.models import Event, Reading

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "600"))

CITIES = [
    {"name": "Ottawa",    "lat": 45.42,  "lon": -75.69},
    {"name": "Toronto",   "lat": 43.70,  "lon": -79.42},
    {"name": "Vancouver", "lat": 49.25,  "lon": -123.12},
]

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"

OPEN_METEO_PARAMS = {
    "current": "temperature_2m,apparent_temperature,precipitation,wind_speed_10m,weather_code",
    "wind_speed_unit": "kmh",
    "timezone": "auto",
}


async def poll_all_cities() -> None:
    logger.info("Poll cycle starting")
    async with httpx.AsyncClient(timeout=15.0) as client:
        for city in CITIES:
            try:
                await _poll_city(client, city)
            except Exception:
                logger.error(
                    "Unexpected error polling %s — skipping this city for this cycle",
                    city["name"],
                    exc_info=True,
                )
    logger.info("Poll cycle complete")


async def _poll_city(client: httpx.AsyncClient, city: dict) -> None:
    city_name = city["name"]
    params = {**OPEN_METEO_PARAMS, "latitude": city["lat"], "longitude": city["lon"]}

    try:
        response = await client.get(OPEN_METEO_URL, params=params)
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        logger.warning(
            "HTTP error fetching %s: status=%s url=%s",
            city_name,
            exc.response.status_code,
            exc.request.url,
        )
        return
    except httpx.RequestError as exc:
        logger.warning("Network error fetching %s: %s", city_name, exc)
        return

    data = response.json()
    current = data.get("current", {})

    reading_time_str = current.get("time")
    if not reading_time_str:
        logger.warning("No 'time' field in response for %s — skipping", city_name)
        return

    reading = Reading(
        city=city_name,
        reading_time=datetime.fromisoformat(reading_time_str),
        fetched_at=datetime.now(timezone.utc).replace(tzinfo=None),
        temperature_2m=current["temperature_2m"],
        apparent_temperature=current["apparent_temperature"],
        precipitation=current["precipitation"],
        wind_speed_10m=current["wind_speed_10m"],
        weather_code=int(current["weather_code"]),
    )

    db: Session = SessionLocal()
    try:
        _store_reading(db, reading)
    finally:
        db.close()


def _store_reading(db: Session, reading: Reading) -> None:
    try:
        db.add(reading)
        db.flush()  # get reading.id before committing
    except IntegrityError:
        db.rollback()
        logger.debug(
            "Duplicate reading for %s at %s — skipping",
            reading.city,
            reading.reading_time,
        )
        return

    try:
        events: list[Event] = detect_events(reading, db)
    except Exception:
        logger.error(
            "Event detection failed for %s at %s — storing reading without events",
            reading.city,
            reading.reading_time,
            exc_info=True,
        )
        events = []

    for event in events:
        db.add(event)

    db.commit()
    logger.info(
        "Stored reading for %s at %s (id=%s), %d event(s) detected",
        reading.city,
        reading.reading_time,
        reading.id,
        len(events),
    )
