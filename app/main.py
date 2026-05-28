"""
FastAPI app for WatchAgent.
Chose FastAPI over Flask mainly for native async and built-in query param validation.
"""

import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import Depends, FastAPI, Query
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import get_db, init_db
from app.models import Event, Reading
from app.poller import POLL_INTERVAL_SECONDS, poll_all_cities
from app.schemas import CityName, HealthResponse, ReadingsResponse, EventsResponse

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler()


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    logger.info("Database initialised")

    # do one poll immediately so there's data right away
    await poll_all_cities()

    scheduler.add_job(
        poll_all_cities,
        trigger="interval",
        seconds=POLL_INTERVAL_SECONDS,
        id="weather_poll",
        replace_existing=True,
    )
    scheduler.start()
    logger.info("Scheduler started — polling every %s seconds", POLL_INTERVAL_SECONDS)

    yield

    scheduler.shutdown(wait=False)


app = FastAPI(
    title="WatchAgent",
    version="1.0.0",
    description="""
Weather monitor for **Ottawa**, **Toronto**, and **Vancouver**.

Polls [Open-Meteo](https://open-meteo.com) every 10 minutes and stores readings in SQLite.
Thirteen event types are detected — from absolute thresholds (high wind, wind chill) to
statistical anomalies and cross-city comparisons.

Endpoints: `/readings`, `/events`, `/health`, `/dashboard` (browser UI).
""",
    lifespan=lifespan,
)


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse(url="/dashboard")


@app.get(
    "/health",
    response_model=HealthResponse,
    summary="Service health",
    tags=["Status"],
    description="Returns `ok` plus a count of how many readings and events are stored. Useful for Docker health checks and monitoring.",
)
def health(db: Session = Depends(get_db)) -> dict:
    readings_stored: int = db.query(func.count(Reading.id)).scalar() or 0
    events_stored: int = db.query(func.count(Event.id)).scalar() or 0
    return {
        "status": "ok",
        "readings_stored": readings_stored,
        "events_stored": events_stored,
    }


@app.get(
    "/readings",
    response_model=ReadingsResponse,
    summary="Weather readings",
    tags=["Data"],
    description="Returns stored weather readings, most recent first. Filter by city using the `city` param — accepts Ottawa, Toronto, or Vancouver.",
)
def get_readings(
    city: CityName | None = Query(default=None, description="Filter by city name"),
    limit: int = Query(default=50, ge=1, le=500, description="Max results"),
    db: Session = Depends(get_db),
) -> dict:
    q = db.query(Reading)
    if city:
        q = q.filter(Reading.city == city.value)
    rows = q.order_by(Reading.reading_time.desc()).limit(limit).all()
    return {"readings": [r.to_dict() for r in rows]}


@app.get(
    "/events",
    response_model=EventsResponse,
    summary="Detected weather events",
    tags=["Data"],
    description="Returns weather events detected by the poller. Each event has a severity (`critical`, `warning`, or `info`) and a plain-English description of what triggered it.",
)
def get_events(
    city: CityName | None = Query(default=None, description="Filter by city name"),
    limit: int = Query(default=50, ge=1, le=500, description="Max results"),
    db: Session = Depends(get_db),
) -> dict:
    q = db.query(Event)
    if city:
        q = q.filter(Event.city == city.value)
    rows = q.order_by(Event.reading_time.desc()).limit(limit).all()
    return {"events": [e.to_dict() for e in rows]}


@app.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
def dashboard(db: Session = Depends(get_db)):
    readings = db.query(Reading).order_by(Reading.reading_time.desc()).limit(60).all()
    events = db.query(Event).order_by(Event.reading_time.desc()).limit(25).all()
    total_readings = db.query(func.count(Reading.id)).scalar() or 0
    total_events = db.query(func.count(Event.id)).scalar() or 0

    city_list = ["Ottawa", "Toronto", "Vancouver"]
    city_color = {"Ottawa": "#60a5fa", "Toronto": "#fbbf24", "Vancouver": "#34d399"}

    current: dict[str, Reading] = {}
    previous: dict[str, Reading] = {}
    for r in readings:
        if r.city not in current:
            current[r.city] = r
        elif r.city not in previous:
            previous[r.city] = r

    city_event_count: dict[str, int] = {}
    city_last_event: dict = {}
    city_temp_range: dict = {}
    for c in city_list:
        city_event_count[c] = db.query(func.count(Event.id)).filter(Event.city == c).scalar() or 0
        city_last_event[c] = (
            db.query(Event).filter(Event.city == c).order_by(Event.reading_time.desc()).first()
        )
        cr = [r.temperature_2m for r in readings if r.city == c]
        city_temp_range[c] = (round(min(cr), 1), round(max(cr), 1)) if cr else None

    weather_emoji = {
        0: "☀️", 1: "🌤️", 2: "⛅", 3: "☁️",
        45: "🌫️", 48: "🌫️",
        51: "🌦️", 53: "🌦️", 55: "🌧️",
        61: "🌧️", 63: "🌧️", 65: "🌧️",
        71: "🌨️", 73: "❄️", 75: "❄️", 77: "❄️",
        80: "🌦️", 81: "🌦️", 82: "⛈️",
        85: "🌨️", 86: "🌨️",
        95: "⛈️", 96: "⛈️", 99: "⛈️",
    }
    weather_label = {
        0: "Clear", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
        45: "Fog", 48: "Freezing fog",
        51: "Light drizzle", 53: "Drizzle", 55: "Heavy drizzle",
        61: "Light rain", 63: "Rain", 65: "Heavy rain",
        71: "Light snow", 73: "Snow", 75: "Heavy snow", 77: "Snow grains",
        80: "Showers", 81: "Showers", 82: "Violent showers",
        85: "Snow showers", 86: "Heavy snow showers",
        95: "Thunderstorm", 96: "Thunderstorm + hail", 99: "Thunderstorm + heavy hail",
    }

    def get_emoji(code: int) -> str:
        return weather_emoji.get(code, "🌡️")

    def get_label(code: int) -> str:
        return weather_label.get(code, f"WMO {code}")

    def temp_color_panel(t: float) -> str:
        if t < 0:   return "#0369a1"
        if t < 10:  return "#0ea5e9"
        if t < 20:  return "#10b981"
        if t < 28:  return "#f59e0b"
        return "#ef4444"

    panel_city_colors = {"Ottawa": "#0369a1", "Toronto": "#d97706", "Vancouver": "#059669"}

    severity_border = {"critical": "#ef4444", "warning": "#f59e0b", "info": "#0ea5e9"}
    badge_style = {
        "critical": "background:#fef2f2;color:#b91c1c;border:1px solid #fecaca",
        "warning":  "background:#fffbeb;color:#b45309;border:1px solid #fde68a",
        "info":     "background:#e0f2fe;color:#0369a1;border:1px solid #7dd3fc",
    }

    # city cards
    city_cards = ""
    for city_name in city_list:
        c_color = city_color[city_name]
        r = current.get(city_name)
        if not r:
            city_cards += f"""
      <div class="city-card">
        <div class="city-label" style="color:{c_color}">{city_name}</div>
        <div style="color:rgba(255,255,255,0.3);margin-top:24px;font-size:0.9em">No data yet</div>
      </div>"""
            continue

        emoji = get_emoji(r.weather_code)
        label = get_label(r.weather_code)

        if city_name in previous:
            delta = r.temperature_2m - previous[city_name].temperature_2m
            if delta > 0.2:
                trend_html = f'<span style="color:#fbbf24;font-weight:700">↑ +{delta:.1f}°</span>'
            elif delta < -0.2:
                trend_html = f'<span style="color:#93c5fd;font-weight:700">↓ {delta:.1f}°</span>'
            else:
                trend_html = '<span style="color:rgba(255,255,255,0.4)">→ steady</span>'
        else:
            trend_html = '<span style="color:rgba(255,255,255,0.4)">first reading</span>'

        evt_count = city_event_count[city_name]
        evt_badge = (
            f'<span style="background:rgba(255,255,255,0.15);color:white;font-size:0.68em;'
            f'font-weight:700;padding:3px 9px;border-radius:20px;border:1px solid rgba(255,255,255,0.2)">'
            f'{evt_count} alert{"s" if evt_count != 1 else ""}</span>'
            if evt_count > 0 else ""
        )

        tr = city_temp_range[city_name]
        range_line = (
            f'<div class="city-stat"><div class="city-stat-val">{tr[0]}° – {tr[1]}°</div>'
            f'<div class="city-stat-lbl">range</div></div>'
            if tr and tr[0] != tr[1] else ""
        )

        last_evt = city_last_event[city_name]
        last_evt_strip = ""
        if last_evt:
            sev_col = {"critical": "#ef4444", "warning": "#fbbf24", "info": "#38bdf8"}.get(last_evt.severity, "#fff")
            type_lbl = last_evt.event_type.replace("_", " ")
            last_evt_strip = (
                f'<div class="last-evt-strip">'
                f'<span style="display:inline-block;width:6px;height:6px;border-radius:50%;'
                f'background:{sev_col};flex-shrink:0"></span>'
                f'<span style="font-size:0.7em;color:rgba(255,255,255,0.45);text-transform:uppercase;'
                f'letter-spacing:0.8px">{last_evt.severity}</span>'
                f'<span style="font-size:0.78em;color:rgba(255,255,255,0.75)">{type_lbl}</span>'
                f'</div>'
            )

        precip_val = f"{r.precipitation:.1f}" if r.precipitation > 0 else "—"
        precip_lbl = "mm/h" if r.precipitation > 0 else "no rain"

        city_cards += f"""
      <div class="city-card">
        <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:12px">
          <div class="city-label" style="color:{c_color}">{city_name}</div>
          {evt_badge}
        </div>
        <div class="city-weather-icon">{emoji}</div>
        <div class="city-temp">{r.temperature_2m:.1f}°</div>
        <div style="font-size:0.82em;margin:4px 0 6px">{trend_html} <span style="color:rgba(255,255,255,0.4)">from last poll</span></div>
        <div class="city-condition">{label}</div>
        <div class="city-stats">
          <div class="city-stat">
            <div class="city-stat-val">{r.apparent_temperature:.0f}°C</div>
            <div class="city-stat-lbl">feels like</div>
          </div>
          <div class="city-stat">
            <div class="city-stat-val">{r.wind_speed_10m:.0f}</div>
            <div class="city-stat-lbl">km/h wind</div>
          </div>
          <div class="city-stat">
            <div class="city-stat-val">{precip_val}</div>
            <div class="city-stat-lbl">{precip_lbl}</div>
          </div>
          {range_line}
        </div>
        {last_evt_strip}
        <div class="city-time">reading at {r.reading_time.strftime("%H:%M")}</div>
      </div>"""

    # event cards
    event_cards_html = ""
    if events:
        for e in events:
            bdr = severity_border.get(e.severity, "#6b7280")
            bstyle = badge_style.get(e.severity, "background:#f5f5f5;color:#555")
            panel_city_color = panel_city_colors.get(e.city, "#6b7280")
            type_display = e.event_type.replace("_", " ")
            event_cards_html += f"""
      <div class="event-card" style="border-left:4px solid {bdr}">
        <div class="event-card-header">
          <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
            <span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:{panel_city_color};flex-shrink:0"></span>
            <span style="font-size:0.76em;font-weight:800;text-transform:uppercase;letter-spacing:1px;color:{panel_city_color}">{e.city}</span>
            <span style="font-size:0.9em;font-weight:600;color:#1a1a2e">{type_display}</span>
          </div>
          <div style="display:flex;align-items:center;gap:8px;flex-shrink:0">
            <span class="badge" style="{bstyle}">{e.severity}</span>
            <span style="font-size:0.74em;color:#9ca3af;white-space:nowrap">{e.reading_time.strftime("%b %d, %H:%M")}</span>
          </div>
        </div>
        <div class="event-desc">{e.description}</div>
      </div>"""
    else:
        event_cards_html = '<div class="empty-state">No notable weather anomalies detected across monitored cities</div>'

    # reading history table
    reading_rows = ""
    for r in readings[:20]:
        t_color = temp_color_panel(r.temperature_2m)
        precip_display = f"{r.precipitation:.1f} mm" if r.precipitation > 0 else "—"
        p_color = panel_city_colors.get(r.city, "#6b7280")
        reading_rows += f"""
      <tr>
        <td><span style="display:inline-block;width:7px;height:7px;border-radius:50%;background:{p_color};margin-right:7px;vertical-align:middle"></span><span style="font-weight:600;color:#1a1a2e">{r.city}</span></td>
        <td style="color:#9ca3af;font-size:0.83em">{r.reading_time.strftime("%b %d, %H:%M")}</td>
        <td style="color:{t_color};font-weight:700">{r.temperature_2m:.1f}°C</td>
        <td style="color:#9ca3af">{r.apparent_temperature:.1f}°C</td>
        <td style="color:#6b7280">{r.wind_speed_10m:.0f} km/h</td>
        <td style="color:#6b7280">{precip_display}</td>
        <td style="color:#6b7280">{get_emoji(r.weather_code)} {get_label(r.weather_code)}</td>
      </tr>"""

    now_str = datetime.now().strftime("%b %d at %I:%M %p")

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta http-equiv="refresh" content="60">
  <title>WatchAgent</title>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}

    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: linear-gradient(145deg, #0369a1 0%, #0ea5e9 45%, #38bdf8 80%, #7dd3fc 100%);
      min-height: 100vh;
      color: white;
    }}

    /* header */
    header {{
      padding: 18px 32px;
      display: flex;
      justify-content: space-between;
      align-items: center;
      border-bottom: 1px solid rgba(255,255,255,0.07);
    }}
    header h1 {{
      font-size: 1.1em; font-weight: 700;
      display: flex; align-items: center; gap: 10px;
    }}
    .live-dot {{
      display: inline-block; width: 8px; height: 8px;
      background: #22c55e; border-radius: 50%;
      box-shadow: 0 0 0 3px rgba(34,197,94,0.25);
      animation: pulse 2.5s infinite;
    }}
    @keyframes pulse {{
      0%, 100% {{ box-shadow: 0 0 0 3px rgba(34,197,94,0.25); }}
      50%       {{ box-shadow: 0 0 0 7px rgba(34,197,94,0.08); }}
    }}
    .sub {{ font-size: 0.74em; color: rgba(255,255,255,0.4); margin-top: 3px; }}
    .header-right {{ display: flex; align-items: center; gap: 18px; }}
    .updated {{ font-size: 0.74em; color: rgba(255,255,255,0.4); }}
    .api-link {{
      font-size: 0.74em; color: rgba(255,255,255,0.55);
      border: 1px solid rgba(255,255,255,0.15);
      border-radius: 6px; padding: 5px 12px; text-decoration: none;
      transition: color 0.15s, border-color 0.15s;
    }}
    .api-link:hover {{ color: white; border-color: rgba(255,255,255,0.35); }}

    /* city cards */
    .city-grid {{
      display: grid;
      grid-template-columns: repeat(3, 1fr);
      gap: 16px;
      padding: 26px 28px 0;
    }}
    .city-card {{
      background: rgba(255,255,255,0.1);
      backdrop-filter: blur(20px);
      -webkit-backdrop-filter: blur(20px);
      border: 1px solid rgba(255,255,255,0.16);
      border-radius: 22px;
      padding: 22px 22px 16px;
    }}
    .city-label {{
      font-size: 0.63em; font-weight: 800;
      text-transform: uppercase; letter-spacing: 2.5px;
    }}
    .city-weather-icon {{
      font-size: 3em; line-height: 1;
      margin: 14px 0 8px;
    }}
    .city-temp {{
      font-size: 4.4em; font-weight: 800;
      letter-spacing: -3px; line-height: 1;
      color: white; margin-bottom: 4px;
    }}
    .city-condition {{
      font-size: 0.87em;
      color: rgba(255,255,255,0.6);
      margin: 8px 0 16px;
    }}
    .city-stats {{
      display: flex;
      border-top: 1px solid rgba(255,255,255,0.1);
      padding-top: 14px;
      margin-bottom: 14px;
    }}
    .city-stat {{
      flex: 1; text-align: center;
      border-right: 1px solid rgba(255,255,255,0.1);
    }}
    .city-stat:last-child {{ border-right: none; }}
    .city-stat-val {{
      font-size: 0.95em; font-weight: 700; color: white;
    }}
    .city-stat-lbl {{
      font-size: 0.6em; color: rgba(255,255,255,0.4);
      text-transform: uppercase; letter-spacing: 0.5px; margin-top: 3px;
    }}
    .last-evt-strip {{
      display: flex; align-items: center; gap: 7px;
      background: rgba(255,255,255,0.07);
      border-radius: 9px; padding: 9px 12px;
      margin-bottom: 12px;
    }}
    .city-time {{
      font-size: 0.64em; color: rgba(255,255,255,0.28); text-align: right;
    }}

    /* summary pills */
    .summary-bar {{
      display: flex; gap: 10px;
      padding: 18px 28px 0; flex-wrap: wrap;
    }}
    .pill {{
      background: rgba(255,255,255,0.09);
      border: 1px solid rgba(255,255,255,0.12);
      border-radius: 8px; padding: 6px 14px;
      font-size: 0.79em; color: rgba(255,255,255,0.65);
    }}
    .pill strong {{ color: white; }}

    /* content panel */
    .content-panel {{
      background: #f0f9ff;
      border-radius: 28px 28px 0 0;
      margin-top: 26px;
      padding: 0;
      color: #075985;
    }}

    section {{ margin-bottom: 0; }}
    .section-header-bar {{
      background: #075985;
      color: white;
      padding: 12px 28px;
      font-size: 0.62em;
      font-weight: 800;
      text-transform: uppercase;
      letter-spacing: 2.5px;
    }}
    .section-body {{ padding: 20px 28px 28px; }}
    .section-title {{
      font-size: 0.62em; font-weight: 800;
      text-transform: uppercase; letter-spacing: 2.5px;
      color: #075985; margin-bottom: 12px;
    }}

    /* event cards */
    .event-list {{ display: flex; flex-direction: column; gap: 10px; }}
    .event-card {{
      background: white;
      border-radius: 14px;
      padding: 14px 18px;
      border: 1px solid #bae6fd;
      box-shadow: 0 2px 8px rgba(3,105,161,0.07);
    }}
    .event-card-header {{
      display: flex; justify-content: space-between;
      align-items: center; margin-bottom: 7px; gap: 12px;
    }}
    .event-desc {{
      font-size: 0.83em; color: #374151; line-height: 1.55;
    }}
    .empty-state {{
      background: white; border-radius: 14px;
      padding: 40px; text-align: center;
      color: #7dd3fc; font-size: 0.9em;
      border: 1px solid #bae6fd;
    }}

    /* reading table */
    table {{
      width: 100%; border-collapse: collapse;
      background: white; border-radius: 14px;
      overflow: hidden; border: 1px solid #bae6fd;
      box-shadow: 0 2px 8px rgba(3,105,161,0.07);
    }}
    th {{
      background: #075985; text-align: left;
      padding: 11px 14px; font-size: 0.61em;
      text-transform: uppercase; letter-spacing: 1.2px;
      color: rgba(255,255,255,0.85); font-weight: 700;
      border-bottom: none;
    }}
    td {{
      padding: 10px 14px; border-top: 1px solid #e0f2fe;
      font-size: 0.86em; vertical-align: middle; color: #374151;
    }}
    tr:hover td {{ background: #f0f9ff; }}

    .badge {{
      display: inline-block; padding: 2px 9px;
      border-radius: 20px; font-size: 0.71em; font-weight: 700;
    }}

    /* detection tiles */
    .detection-grid {{
      display: grid; grid-template-columns: repeat(3, 1fr);
      gap: 12px; margin-bottom: 12px;
    }}
    .detection-tile {{
      background: white; border-radius: 14px;
      padding: 18px;
      border: 1px solid #bae6fd;
      border-top: 4px solid #0ea5e9;
      box-shadow: 0 2px 8px rgba(3,105,161,0.07);
    }}
    .detection-icon {{ font-size: 1.6em; margin-bottom: 10px; }}
    .detection-category {{
      font-size: 0.7em; font-weight: 800;
      text-transform: uppercase; letter-spacing: 1.5px;
      color: #075985; margin-bottom: 6px;
    }}
    .detection-events {{
      font-size: 0.77em; color: #0ea5e9;
      margin-bottom: 10px; line-height: 1.45;
    }}
    .detection-desc {{
      font-size: 0.79em; color: #374151; line-height: 1.55;
      border-top: 1px solid #e0f2fe; padding-top: 10px;
    }}
    .detection-note {{
      font-size: 0.79em; color: #0369a1; line-height: 1.55;
      padding: 14px 18px; background: #e0f2fe;
      border-radius: 12px; border: 1px solid #7dd3fc;
    }}

    footer {{
      text-align: center; padding: 28px;
      color: #0ea5e9; font-size: 0.74em;
      background: #e0f2fe;
    }}
    footer a {{ color: #0284c7; text-decoration: none; }}
  </style>
</head>
<body>

  <header>
    <div>
      <h1>WatchAgent <span class="live-dot"></span></h1>
      <div class="sub">Ottawa · Toronto · Vancouver · polls every {POLL_INTERVAL_SECONDS // 60} min</div>
    </div>
    <div class="header-right">
      <div class="updated">Updated {now_str}</div>
      <a class="api-link" href="/docs">API docs ↗</a>
    </div>
  </header>

  <div class="city-grid">
    {city_cards}
  </div>

  <div class="summary-bar">
    <div class="pill"><strong>{total_readings}</strong> readings stored</div>
    <div class="pill"><strong>{total_events}</strong> events detected</div>
    <div class="pill">auto-refreshes every <strong>60 s</strong></div>
  </div>

  <div class="content-panel">

    <section>
      <div class="section-header-bar">Recent Events</div>
      <div class="section-body">
        <div class="event-list">{event_cards_html}</div>
      </div>
    </section>

    <section>
      <div class="section-header-bar">Reading History</div>
      <div class="section-body">
        <table>
          <thead>
            <tr>
              <th>City</th><th>Time</th><th>Temp</th><th>Feels like</th>
              <th>Wind</th><th>Precip</th><th>Conditions</th>
            </tr>
          </thead>
          <tbody>{reading_rows}</tbody>
        </table>
      </div>
    </section>

    <section>
      <div class="section-header-bar">How Detection Works</div>
      <div class="section-body">
        <div class="detection-grid">
          <div class="detection-tile">
            <div class="detection-icon">📏</div>
            <div class="detection-category">Absolute Threshold</div>
            <div class="detection-events">severe weather · wind chill · high wind · heavy rain · feels-like gap</div>
            <div class="detection-desc">A single reading crosses a fixed limit anchored to Environment Canada advisory levels. No history needed.</div>
          </div>
          <div class="detection-tile">
            <div class="detection-icon">📈</div>
            <div class="detection-category">History-Dependent</div>
            <div class="detection-events">temperature anomaly · rapid drop · spike · sustained warming/cooling trend</div>
            <div class="detection-desc">Compares against each city's own rolling 24-reading baseline — adapts to Ottawa's winters and Vancouver's mild norms equally.</div>
          </div>
          <div class="detection-tile">
            <div class="detection-icon">🗺️</div>
            <div class="detection-category">Cross-City Comparative</div>
            <div class="detection-events">cross-city outlier · cross-city contrast · strongest wind city</div>
            <div class="detection-desc">One city diverges significantly from the other two — catches localised extremes that per-city checks miss entirely.</div>
          </div>
        </div>
        <div class="detection-note" style="margin-top:12px">
          Severity scales with magnitude: <strong>info</strong> flags notable deviations,
          <strong>warning</strong> indicates potentially hazardous conditions,
          <strong>critical</strong> requires immediate attention.
          Cooldown windows (1–6 h) suppress repeat alerts; a higher-severity reading bypasses the cooldown.
        </div>
      </div>
    </section>

    <footer>
      WatchAgent · data from <a href="https://open-meteo.com">Open-Meteo</a>
    </footer>

  </div>

</body>
</html>"""
    return html
