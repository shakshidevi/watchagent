"""SQLAlchemy models for readings and events."""

from datetime import datetime, timezone

from sqlalchemy import (
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import declarative_base

Base = declarative_base()


class Reading(Base):
    __tablename__ = "readings"

    id = Column(Integer, primary_key=True, autoincrement=True)
    city = Column(String, nullable=False, index=True)
    reading_time = Column(DateTime, nullable=False)       # timestamp from API
    fetched_at = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None))

    temperature_2m = Column(Float, nullable=False)        # °C
    apparent_temperature = Column(Float, nullable=False)  # °C (wind chill / heat index)
    precipitation = Column(Float, nullable=False)         # mm in the last hour
    wind_speed_10m = Column(Float, nullable=False)        # km/h
    weather_code = Column(Integer, nullable=False)        # WMO code

    # deduplication — same city + timestamp only stored once
    __table_args__ = (
        UniqueConstraint("city", "reading_time", name="uq_city_reading_time"),
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "city": self.city,
            "reading_time": self.reading_time.isoformat(),
            "fetched_at": self.fetched_at.isoformat(),
            "temperature_2m": self.temperature_2m,
            "apparent_temperature": self.apparent_temperature,
            "precipitation": self.precipitation,
            "wind_speed_10m": self.wind_speed_10m,
            "weather_code": self.weather_code,
        }


class Event(Base):
    __tablename__ = "events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    city = Column(String, nullable=False, index=True)
    event_type = Column(String, nullable=False, index=True)
    severity = Column(String, nullable=False)  # info / warning / critical

    reading_id = Column(Integer, ForeignKey("readings.id"), nullable=False)
    reading_time = Column(DateTime, nullable=False)   # denormalized so queries don't need a join
    detected_at = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None))

    description = Column(String, nullable=False)
    metric_value = Column(Float, nullable=True)
    metric_name = Column(String, nullable=True)
    baseline_value = Column(Float, nullable=True)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "city": self.city,
            "event_type": self.event_type,
            "severity": self.severity,
            "reading_id": self.reading_id,
            "reading_time": self.reading_time.isoformat(),
            "detected_at": self.detected_at.isoformat(),
            "description": self.description,
            "metric_value": self.metric_value,
            "metric_name": self.metric_name,
            "baseline_value": self.baseline_value,
        }
