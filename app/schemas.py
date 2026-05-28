"""Pydantic response models — give FastAPI enough info to generate proper docs."""

from enum import Enum
from typing import Optional
from pydantic import BaseModel


class CityName(str, Enum):
    ottawa = "Ottawa"
    toronto = "Toronto"
    vancouver = "Vancouver"


class ReadingOut(BaseModel):
    id: int
    city: str
    reading_time: str
    fetched_at: str
    temperature_2m: float
    apparent_temperature: float
    precipitation: float
    wind_speed_10m: float
    weather_code: int


class EventOut(BaseModel):
    id: int
    city: str
    event_type: str
    severity: str
    reading_id: int
    reading_time: str
    detected_at: str
    description: str
    metric_name: Optional[str] = None
    metric_value: Optional[float] = None
    baseline_value: Optional[float] = None


class HealthResponse(BaseModel):
    status: str
    readings_stored: int
    events_stored: int


class ReadingsResponse(BaseModel):
    readings: list[ReadingOut]


class EventsResponse(BaseModel):
    events: list[EventOut]
