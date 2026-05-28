# WatchAgent

Polls live weather for Ottawa, Toronto, and Vancouver every 10 minutes, figures out when something worth noticing has happened, and makes that data available over a simple HTTP API.

---

## How it works

```
                Open-Meteo API (free, no key needed)
                         |
              polls every 10 min
                         |
                         v
         +-----------------------------+
         |         Poller              |
         |  fetches all 3 cities,      |
         |  skips duplicates,          |
         |  runs event detection       |
         +-----------------------------+
                         |
              writes to SQLite
                         |
                         v
         +-----------------------------+
         |     SQLite database         |
         |  readings  |  events        |
         +-----------------------------+
                         |
              read by FastAPI
                         |
                         v
         +-----------------------------+
         |       REST API :8000        |
         |  /health                    |
         |  /readings                  |
         |  /events                    |
         |  /dashboard  (browser UI)   |
         |  /docs       (API explorer) |
         +-----------------------------+
```

The poller runs in the background as an async task. Open-Meteo updates current conditions roughly every 15 minutes, so each poll typically returns a new timestamp and gets stored. Duplicate timestamps (same city + same time) are silently dropped by a unique constraint on `(city, reading_time)`. When a reading is genuinely new, it goes through event detection before being committed. Events and readings are stored in the same SQLite file, which is volume-mounted in Docker so nothing gets lost between restarts.

---

## Getting started

```bash
cp .env.example .env
docker compose up --build
```

That's it. Once the container is running, open:

| URL | What you get |
|-----|-------------|
| `http://localhost:8000` | Redirects to the dashboard |
| `http://localhost:8000/dashboard` | Visual weather dashboard |
| `http://localhost:8000/docs` | Interactive API explorer (auto-generated) |
| `http://localhost:8000/health` | Service status and row counts |
| `http://localhost:8000/readings` | Stored weather readings |
| `http://localhost:8000/events` | Detected weather events |

The service does one poll on startup so there is data immediately, then keeps polling every 10 minutes.

---

## API

### GET /dashboard

Open in a browser — shows live city cards with current temperature, conditions, trend, and recent events. Auto-refreshes every 60 seconds.

```
http://localhost:8000/dashboard
```

### GET /docs

FastAPI's interactive API explorer. Every endpoint is listed with its parameters and response schema — you can execute real requests directly from the browser without curl.

```
http://localhost:8000/docs
```

### GET /health

```bash
curl http://localhost:8000/health
```

```json
{
  "status": "ok",
  "readings_stored": 42,
  "events_stored": 3
}
```

### GET /readings

Optional query params: `city` (Ottawa, Toronto, or Vancouver) and `limit` (default 50, max 500). Results come back most recent first.

```bash
curl "http://localhost:8000/readings?city=Ottawa&limit=10"
```

```json
{
  "readings": [
    {
      "id": 12,
      "city": "Ottawa",
      "reading_time": "2024-06-01T15:00:00",
      "fetched_at": "2024-06-01T15:03:42",
      "temperature_2m": 22.4,
      "apparent_temperature": 21.1,
      "precipitation": 0.0,
      "wind_speed_10m": 14.5,
      "weather_code": 0
    }
  ]
}
```

### GET /events

Same params as `/readings` — `city` and `limit`, most recent first.

```bash
curl "http://localhost:8000/events?limit=5"
```

```json
{
  "events": [
    {
      "id": 2,
      "city": "Ottawa",
      "event_type": "rapid_temperature_drop",
      "severity": "warning",
      "reading_id": 10,
      "reading_time": "2024-06-01T14:00:00",
      "detected_at": "2024-06-01T14:02:11",
      "description": "Ottawa: rapid temperature drop — 18.0 °C → 10.5 °C (−7.5 °C since previous reading)",
      "metric_name": "temperature_2m",
      "metric_value": 10.5,
      "baseline_value": 18.0
    }
  ]
}
```

---

## Running the tests

```bash
pip install -r requirements-dev.txt
pytest
```

Tests run against an in-memory SQLite database and never hit the real weather API.

---

## Why these tools

**FastAPI** — I needed async from the start since the poller and API share the same process. FastAPI handles that naturally and the automatic docs at `/docs` are a nice bonus. Flask would have needed extra setup to get async working cleanly.

**SQLite** — Open-Meteo updates every 15 minutes, so three cities gives around 288 rows a day at most. There's no reason to run a separate Postgres container for that volume. SQLite with a Docker volume gives full persistence without the overhead, and swapping to Postgres later is just a `DATABASE_URL` change.

**httpx** — Async HTTP client that fits naturally with FastAPI's event loop. The API is simple and httpx handles it well.

**APScheduler** — Solid interval scheduler that plays nicely with asyncio. Keeps the polling logic separate from the API without needing a second process.

**pytest** — Standard choice. `pytest-asyncio` handles the one async test case without any ceremony.

---

## Event detection

The two failure modes I wanted to avoid were alert fatigue (fires on everything, you stop reading it) and the opposite — fires so rarely it becomes useless. Every threshold is either anchored to an Environment Canada advisory level or has a specific reason I can point to.

**Why a cooldown period?** Without one, a city stuck at −25 °C for three hours fires `dangerous_wind_chill` on every poll. The cooldown suppresses repeats when conditions persist. It's severity-aware though — if a `warning` fired an hour ago and conditions worsen to `critical`, the critical event still goes through. A deteriorating situation shouldn't get silently swallowed just because we already fired something.

**Why 24 readings for the anomaly baseline?** Ottawa and Vancouver have completely different normal temperature ranges. A fixed threshold calibrated for one city would be deafening or useless for the other. 24 readings is roughly 6 hours at Open-Meteo's 15-minute cadence — enough for a stable mean without needing days of warm-up. I also floor the effective standard deviation at 1 °C; without this, a run of unusually stable weather makes the detector hypersensitive to trivial shifts.

**Why cross-city comparison?** Per-city checks only flag things that are extreme *for that city*. Ottawa at −15 °C in January isn't remarkable. But if Toronto and Vancouver are both at +5 °C at the same time, that 20 °C gap tells you something real is happening locally. It's the kind of signal you'd miss with only absolute thresholds.

**Why sustained trend detection?** The single-reading checks catch a sudden jump. A slow but consistent decline across four or five readings is a different signal — an approaching front, not a measurement spike. I require strict monotonicity (every step in the same direction) so it doesn't fire on ordinary oscillation that happens to trend slightly one way.

### severe_weather_code

Fires when the WMO weather code lands in a set I defined as severe: thunderstorms (95, 96, 99), heavy freezing rain (57, 67), heavy snow (73, 75, 77), violent showers (82), heavy snow showers (86). These are the API's own classifications — when the code is in that set, something real is already happening. Thunderstorms and freezing rain are `critical`; heavy snow is `warning` since it's more common and less immediately dangerous. Cooldown: 1 hour.

### dangerous_wind_chill

Fires when apparent temperature drops below −20 °C — the Environment Canada wind chill warning level where exposed skin can get frostbite in under 30 minutes. The description includes both actual and apparent temperature so you can see how big the gap is. Cooldown: 2 hours.

### high_wind

Fires above 70 km/h (Environment Canada wind warning threshold for most regions). Below that, gusty days are normal and not worth paging on. Cooldown: 2 hours.

### heavy_precipitation

Fires above 10 mm/hour — the standard urban flash-flood threshold and low end of what Environment Canada calls heavy rain. Cooldown: 1 hour.

### feels_like_gap

Fires when apparent temperature differs from actual by 4 °C or more in either direction — humidity making it feel warmer, or wind cutting into the warmth. Doesn't need extreme conditions to trigger, so cooldown is longer at 4 hours to avoid noise on ordinary humid or breezy days. Severity scales with the gap size (info → warning → critical at 4/7/10 °C).

### temperature_anomaly

Uses a z-score instead of a fixed threshold because Ottawa and Vancouver have completely different normal temperature ranges — a single number calibrated for one city would be useless or deafening for the other. I keep a rolling baseline of the last 24 readings per city and flag anything more than 2.5 standard deviations out.

One thing I had to handle: if recent weather has been very stable (raw σ = 0.2 °C), even a 1–2 °C shift produces a huge z-score — technically correct but not actually anomalous. I floor the effective sigma at 1.0 °C, which anchors the z-score to a physically meaningful minimum. Without this, a stretch of calm weather makes the detector hypersensitive.

Severity scales with the z-score: 2.5–3.0σ is `info`, 3.0–5.0σ is `warning`, above 5.0σ is `critical`. Won't fire until there are at least 6 prior readings. Cooldown: 6 hours.

### rapid_temperature_drop

Fires when temperature drops 6 °C or more from the previous reading. A drop that fast usually means a cold front or a flash freeze — it's different in character from a gradual overnight cooling. Cooldown: 3 hours.

### temperature_spike

Same as rapid_temperature_drop but for warming — fires on a 5 °C or more rise. A spring morning that flips 8 °C warmer than an hour ago is worth noting even if neither absolute temperature is alarming. Cooldown: 3 hours.

### cross_city_outlier

Fires when one city's temperature is more than 15 °C from the average of the other two, as long as all three have readings within the last 2 hours. Ottawa at −15 °C in January is unremarkable — but if Toronto and Vancouver are both at +10 °C at the same time, that 25 °C gap is genuinely unusual and worth surfacing. Cooldown: 3 hours.

### cross_city_contrast

Same idea but uses apparent temperature with a lower 8 °C threshold. Humidity and wind can push how-it-feels temperatures apart even when actual temps look similar — Toronto feeling 10 °C warmer than Vancouver on a humid afternoon is interesting even if the raw numbers are close. Cooldown: 3 hours.

### strongest_wind_city

Fires when one city leads the others' average by 20 km/h or more. Catches localized wind events that wouldn't cross the absolute 70 km/h threshold on their own — Ottawa at 45 km/h while the others are calm at 5 km/h is a meaningful local difference worth flagging. Cooldown: 2 hours.

### sustained_warming_trend / sustained_cooling_trend

Catches gradual shifts that no single-reading check would flag. Fires when temperature moves in the same direction across every one of the last four consecutive readings and the total shift is at least 2 °C. Total shift ≥ 4 °C upgrades it to `warning`. Cooldown: 3 hours each.

The computation is straightforward: take the last four stored readings plus the current one, compute the delta between each adjacent pair, and check that all four deltas share the same sign. I chose consecutive diffs over a linear regression slope because I want to catch real directional momentum — a slope can be positive even if the last reading reversed. One dip or one spike disqualifies the window entirely, which is intentional: if the temperature bounced even once, it isn't a sustained trend. "Sustained" means every single step went the same way.

---

## Cursor setup

### Rules

**error_handling.mdc** — Locks down how the poller handles failures: HTTP errors log city/status/URL at WARNING and return (never raise), duplicates log at DEBUG and bail before event detection runs, unexpected errors log at ERROR with a full traceback. Also pins `%s`-style log formatting so log aggregators can group by format string rather than seeing each f-string variant as a different message.

**event_schema.mdc** — Defines what every `Event` needs: all required fields, what the description string should look like, when to use each severity level, and a checklist for adding new event types. Useful for keeping things consistent when I come back to add another detector later.

### Agent

**event_detection_reviewer.md** — Reviews event detection logic. Has context on all thirteen event types, the Environment Canada thresholds, and typical temperature ranges for each city. I use it when adding a new event type to get a second opinion on whether the threshold is calibrated well, or after collecting real data to ask whether anything is over-firing.

### Skills

**analyze_data.py** — Queries the database and returns structured JSON. Six modes:

| Mode | What you get |
|------|-------------|
| `summary` | counts, data freshness, latest reading per city |
| `city_comparison` | min/max/avg temp, wind, precip across all three cities |
| `event_breakdown` | event counts by type and city, most recent per type |
| `temperature_trend` | time series for a city over the last N hours |
| `recent_events` | last N events across all cities |
| `anomaly_replay` | what would have fired in the last N hours |

```bash
python .cursor/skills/analyze_data.py --mode city_comparison
python .cursor/skills/analyze_data.py --mode anomaly_replay --city Toronto --hours 48
```

**replay_events.py** — More thorough than the mode in `analyze_data.py`. Runs all thirteen detectors against stored readings using only the data that existed at the time of each reading, then diffs against what's actually in the database. Useful before changing a threshold — run it over a week of data to see what would be affected.

```bash
python .cursor/skills/replay_events.py --city Ottawa --hours 48
python .cursor/skills/replay_events.py --all-cities --hours 24 --output text
```

---

## Environment variables

| Variable | Default | What it does |
|----------|---------|--------------|
| `DATABASE_URL` | `sqlite:////app/data/weather.db` | database connection string |
| `POLL_INTERVAL_SECONDS` | `600` | seconds between polls |
| `LOG_LEVEL` | `INFO` | logging level |
