"""
The Ground Beneath Growth — Tablet + Projection Interface
=========================================================
A single server that drives two synced surfaces for the physical installation:

  • TABLET  (the iPad in the table edge)  →  http://localhost:8000/
        The controller. Visitor walks the Hook → Explore → Decide flow.

  • PROJECTION (ceiling projector onto the engraved table map) → http://localhost:8000/projection
        A pure mirror. Whatever the tablet shows, this projects onto the model.

The tablet POSTs its state to /api/state; the server broadcasts it over SSE
(/api/stream) so the projection updates in real time. Votes are tallied
server-side so multiple visitors build a living public record.

Run:
    pip install -r requirements.txt
    python app.py
"""
from __future__ import annotations

import asyncio
import csv
import io
import json
import math
import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote_plus, urljoin

import requests
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

BASE = Path(__file__).parent
STATIC = BASE / "static"
DATA = BASE / "data"
VOTES_FILE = DATA / "votes.json"

app = FastAPI(title="The Ground Beneath Growth — Installation")


@app.middleware("http")
async def _no_cache_assets(request: Request, call_next):
    """Never let the browser cache the page/JS/CSS — this is a live-edited
    kiosk, and stale cached scripts cause hard-to-debug errors. Data API
    responses cache server-side, so HTTP no-store here is harmless."""
    resp = await call_next(request)
    path = request.url.path
    if path == "/" or path == "/projection" or path.startswith("/static"):
        resp.headers["Cache-Control"] = "no-store, must-revalidate"
    return resp


# ─────────────────────────────────────────────────────────────
# Shared live state (the single source of truth the tablet drives)
# ─────────────────────────────────────────────────────────────
STATE: dict = {
    "stage": "hook",        # hook | explore | decide | result
    "tab": "land",          # land | temperature | pollution | life
    "year": 2025,           # 1969 | 1985 | 2005 | 2025
    "scenario": None,       # expand | balance | protect (after decision)
    "rev": 0,               # bumped on every change so clients can dedupe
    "ts": 0.0,
}

# Each connected client (projection screens, other tablets) gets a queue.
_subscribers: set[asyncio.Queue] = set()


# ─────────────────────────────────────────────────────────────
# Participatory record — every decision is stored as one row in a SQLite
# database, so the responses gathered here are a real, queryable dataset that
# can feed back into the city's urban-planning conversation (not just a tally).
#   data/responses.db · table `responses`
#       id          autoincrement
#       choice      expand | balance | protect
#       created_at  ISO-8601 UTC timestamp of the vote
#       session     anonymous per-visit token (no personal data)
#       user_agent  device string (helps distinguish kiosk vs remote)
# ─────────────────────────────────────────────────────────────
DB_FILE = DATA / "responses.db"
CHOICES = ("expand", "balance", "protect")
_db_lock = threading.Lock()


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _legacy_votes() -> dict:
    if VOTES_FILE.exists():
        try:
            return json.loads(VOTES_FILE.read_text())
        except Exception:
            pass
    return {}


def _init_db() -> None:
    DATA.mkdir(exist_ok=True)
    with _db() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS responses(
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   choice TEXT NOT NULL,
                   created_at TEXT NOT NULL,
                   session TEXT,
                   user_agent TEXT)"""
        )
        # One-time migration: fold any existing votes.json counts into the DB so
        # the running public tally carries over, then the DB is the source of truth.
        if conn.execute("SELECT COUNT(*) FROM responses").fetchone()[0] == 0:
            ts = datetime.now(timezone.utc).isoformat()
            for choice, n in _legacy_votes().items():
                if choice in CHOICES:
                    conn.executemany(
                        "INSERT INTO responses(choice,created_at,session,user_agent) VALUES(?,?,?,?)",
                        [(choice, ts, "legacy", "migrated-from-votes.json")] * int(n or 0),
                    )
        conn.commit()


def _counts() -> dict:
    out = {c: 0 for c in CHOICES}
    try:
        with _db() as conn:
            for row in conn.execute("SELECT choice, COUNT(*) n FROM responses GROUP BY choice"):
                if row["choice"] in out:
                    out[row["choice"]] = row["n"]
    except Exception as exc:
        print(f"  [responses] count failed: {exc}")
    return out


def _record_vote(choice: str, session: str | None, user_agent: str | None) -> None:
    ts = datetime.now(timezone.utc).isoformat()
    with _db_lock, _db() as conn:
        conn.execute(
            "INSERT INTO responses(choice,created_at,session,user_agent) VALUES(?,?,?,?)",
            (choice, ts, session, user_agent),
        )
        conn.commit()


async def _broadcast(payload: dict) -> None:
    """Push an event to every connected SSE client."""
    dead = []
    for q in _subscribers:
        try:
            q.put_nowait(payload)
        except Exception:
            dead.append(q)
    for q in dead:
        _subscribers.discard(q)


# ─────────────────────────────────────────────────────────────
# Real-map configuration
#   • Esri World Imagery   → present-day satellite base (recognizable delta)
#   • IGN PNOA histórico    → real 1956-57 aerial for the 1969 milestone
#   • GLC_FCS30D (TiTiler)  → real land-cover tiles for 1985 & 2005
# Land-cover assets are discovered from STAC once and cached.
# ─────────────────────────────────────────────────────────────
STAC_COLLECTION = "https://s3.eu-central-1.wasabisys.com/stac/openlandmap/lc_glc.fcs30d/collection.json"
STAC_ASSET_KEY = "lc_glc.fcs30d_c_30m_s"
# Registered to the laser-engraved projection board (0.80 m × 0.60 m → 4:3).
# The bbox real-world aspect (Δlng·cos(lat) : Δlat) is held at 4:3 so the
# projection fills the board edge-to-edge with no letterboxing, and the
# satellite runways land on the engraved runways. Centred on LEBL.
MAP_CENTER = [41.2930, 2.0830]
MAP_BBOX = [2.0250, 41.2603, 2.1410, 41.3257]  # west, south, east, north — 4:3 board

# GLC_FCS30D fine classes → simplified, readable groups (id, label, hex, codes)
LANDCOVER_GROUPS = [
    ("built",    "Built-up",      "#6f757e", [190]),
    ("cropland", "Cropland",      "#e3c46b", [10, 11, 12, 20]),
    ("forest",   "Forest",        "#2e7d46", [51, 52, 61, 62, 71, 72, 81, 82, 91, 92]),
    ("grass",    "Grass & shrub", "#9fcf6a", [120, 121, 122, 130, 140, 150, 151, 152, 153]),
    ("wetland",  "Wetland",       "#34a0a4", [180, 181, 182, 183, 184, 185, 186, 187]),
    ("water",    "Water",         "#3c7fd0", [210]),
    ("bare",     "Bare ground",   "#dccfb6", [200, 201, 202]),
    ("ice",      "Snow & ice",    "#eef3f7", [220]),
]


def _landcover_colormap() -> dict:
    cmap = {}
    for _id, _label, color, codes in LANDCOVER_GROUPS:
        for code in codes:
            cmap[str(code)] = color
    return cmap


def _titiler_url(asset_href: str) -> str:
    cmap = quote_plus(json.dumps(_landcover_colormap(), separators=(",", ":")))
    return (f"https://titiler.xyz/cog/tiles/WebMercatorQuad/{{z}}/{{x}}/{{y}}.png"
            f"?url={quote_plus(asset_href)}&colormap={cmap}")


@lru_cache(maxsize=1)
def _resolve_landcover(years_key: str) -> dict:
    """Discover GLC_FCS30D COG assets for the requested years and build tile URLs.
    Returns {year: tile_url_template}. Empty/partial on network failure."""
    years = {int(y) for y in years_key.split(",")}
    out: dict[str, str] = {}
    try:
        coll = requests.get(STAC_COLLECTION, timeout=30).json()
        for link in coll.get("links", []):
            if link.get("rel") != "item":
                continue
            href = urljoin(STAC_COLLECTION, link.get("href", ""))
            m = re.search(r"(19|20)\d{2}", href)
            if not m:
                continue
            yr = int(m.group(0))
            if yr not in years:
                continue
            item = requests.get(href, timeout=30).json()
            asset = item.get("assets", {}).get(STAC_ASSET_KEY, {}).get("href")
            if asset:
                out[str(yr)] = _titiler_url(asset)
            years.discard(yr)
            if not years:
                break
    except Exception as exc:  # network/STAC down → frontend falls back to satellite only
        print(f"  [map-config] land-cover discovery failed: {exc}")
    return out


# ─────────────────────────────────────────────────────────────
# Real heat & air data  (2005 → 2024, divided into 4 milestones)
#
#   HEAT  — Open-Meteo ERA5 July mean 2 m temperature + MODIS Terra LST Day
#   AIR   — Open-Meteo CAMS PM2.5 / NO2 (real from 2013; fallback for 2005/2011)
#           + MODIS Terra Aerosol Optical Depth tiles
#
#   Milestone years: 2005 · 2011 · 2017 · 2024
# ─────────────────────────────────────────────────────────────
_MILESTONE_YEARS = [2005, 2011, 2017, 2024]

HEAT_FALLBACK: dict[str, float] = {
    "2005": 25.1, "2011": 25.8, "2017": 26.3, "2024": 27.2,
}

MODIS_LST_TILES: dict[str, str] = {
    "2005": ("https://gibs.earthdata.nasa.gov/wmts/epsg3857/best/"
             "MODIS_Terra_Land_Surface_Temp_Day/default/2005-07-15/"
             "GoogleMapsCompatible_Level7/{z}/{y}/{x}.png"),
    "2011": ("https://gibs.earthdata.nasa.gov/wmts/epsg3857/best/"
             "MODIS_Terra_Land_Surface_Temp_Day/default/2011-07-15/"
             "GoogleMapsCompatible_Level7/{z}/{y}/{x}.png"),
    "2017": ("https://gibs.earthdata.nasa.gov/wmts/epsg3857/best/"
             "MODIS_Terra_Land_Surface_Temp_Day/default/2017-07-15/"
             "GoogleMapsCompatible_Level7/{z}/{y}/{x}.png"),
    "2024": ("https://gibs.earthdata.nasa.gov/wmts/epsg3857/best/"
             "MODIS_Terra_Land_Surface_Temp_Day/default/2024-07-15/"
             "GoogleMapsCompatible_Level7/{z}/{y}/{x}.png"),
}

AIR_FALLBACK: dict[str, dict] = {
    "2005": {"pm25": 22.0, "no2": 41.0},
    "2011": {"pm25": 19.0, "no2": 37.0},
    "2017": {"pm25": 15.0, "no2": 32.0},
    "2024": {"pm25": 11.0, "no2": 26.0},
}

# MODIS Terra Aerosol Optical Depth — proxy for particle pollution (August peak)
AOD_TILES: dict[str, str] = {
    "2005": ("https://gibs.earthdata.nasa.gov/wmts/epsg3857/best/"
             "MODIS_Terra_Aerosol_Optical_Depth_Land_Ocean/default/2005-08-01/"
             "GoogleMapsCompatible_Level7/{z}/{y}/{x}.png"),
    "2011": ("https://gibs.earthdata.nasa.gov/wmts/epsg3857/best/"
             "MODIS_Terra_Aerosol_Optical_Depth_Land_Ocean/default/2011-08-01/"
             "GoogleMapsCompatible_Level7/{z}/{y}/{x}.png"),
    "2017": ("https://gibs.earthdata.nasa.gov/wmts/epsg3857/best/"
             "MODIS_Terra_Aerosol_Optical_Depth_Land_Ocean/default/2017-08-01/"
             "GoogleMapsCompatible_Level7/{z}/{y}/{x}.png"),
    "2024": ("https://gibs.earthdata.nasa.gov/wmts/epsg3857/best/"
             "MODIS_Terra_Aerosol_Optical_Depth_Land_Ocean/default/2024-08-01/"
             "GoogleMapsCompatible_Level7/{z}/{y}/{x}.png"),
}

# ─────────────────────────────────────────────────────────────
# OpenLandMap land-surface temperature (real satellite thermal data)
#   Collection : lst_mod11a2.daytime.annual  (MOD11A2, 1 km, 2000-2021)
#   Asset      : p95 = hot-season daytime surface temperature — the band
#                where sealed runways/tarmac diverge sharply from wetland.
#   Encoding   : uint16, °C = DN·0.02 − 273.15.  Rendered through TiTiler with
#                a fixed blue→yellow→red ramp (rdylbu_r) so the summer urban
#                heat island reads instantly: cool water/wetland blue, baking
#                sealed ground red. Rescale 29 °C → 44 °C (DN 15108 → 15858).
#   Year map   : the heat milestones 2005·2011·2017·2024 map to the nearest
#                available LST years (2024 → 2021, the latest in the record).
# ─────────────────────────────────────────────────────────────
_LST_ASSET = "lst_mod11a2.daytime_p95_1km_s"
_LST_YEAR_MAP = {"2005": 2005, "2011": 2011, "2017": 2017, "2024": 2021}
# °C = DN·0.02 − 273.15  →  29 °C = 15108 DN, 44 °C = 15858 DN
_LST_RESCALE = "15108,15858"
_LST_CMAP = "rdylbu_r"


def _lst_cog_href(year: int) -> str:
    return (f"https://s3.openlandmap.org/arco/{_LST_ASSET}"
            f"_{year}0101_{year}1231_go_epsg.4326_v1.2.tif")


def _lst_images() -> dict:
    """One TiTiler bbox-image URL per heat milestone (single image, bilinear-
    resampled). The frontend lays it over the bounds and CSS-blurs it, so the
    coarse 1 km data reads as a seamless heat wash with no tile-grid steps."""
    w, s, e, n = MAP_BBOX
    out: dict[str, str] = {}
    for ui_year, data_year in _LST_YEAR_MAP.items():
        href = quote_plus(_lst_cog_href(data_year))
        out[ui_year] = (
            f"https://titiler.xyz/cog/bbox/{w},{s},{e},{n}/640x480.png"
            f"?url={href}&rescale={_LST_RESCALE}"
            f"&colormap_name={_LST_CMAP}&resampling=bilinear"
        )
    return out


# ─────────────────────────────────────────────────────────────
# Per-year REAL aerial imagery (IGN PNOA histórico WMS).
#   Each milestone year maps to the nearest IGN layer that actually covers
#   El Prat (PNOA reflies Barcelona only every ~2-3 years, so not every year
#   exists). Cross-faded in place on every tab → the ground changes through
#   time while coordinates stay fixed. Verified to render at the app's zoom.
# ─────────────────────────────────────────────────────────────
AERIAL_WMS = "https://www.ign.es/wms/pnoa-historico?"
AERIALS = {
    "1969": "AMS_1956-1957",        # earliest aerial — the pre-airport delta
    "1985": "AMS_1956-1957",        # no BCN coverage 1957-2002; 1956 stands in
    "2000": "SIGPAC",               # ~2002 orthophoto
    "2005": "PNOA2004",
    "2010": "PNOA2009",
    "2011": "PNOA2012",
    "2017": "PNOA2015",
    "2020": "PNOA2018",
    "2024": "PNOA2021",             # latest BCN coverage
}


def _fetch_era5_temp(year: int) -> float:
    yr = min(year, 2023)  # archive API lags ~3 months
    url = (
        "https://archive-api.open-meteo.com/v1/archive"
        f"?latitude=41.294&longitude=2.0775"
        f"&start_date={yr}-07-01&end_date={yr}-07-31"
        "&hourly=temperature_2m&timezone=Europe/Madrid"
    )
    data = requests.get(url, timeout=20).json()
    vals = [v for v in data.get("hourly", {}).get("temperature_2m", []) if v is not None]
    if not vals:
        raise ValueError("empty response")
    return round(sum(vals) / len(vals), 1)


@lru_cache(maxsize=1)
def _heat_data() -> dict:
    out: dict[str, dict] = {}
    baseline: float | None = None
    for year in [2005, 2011, 2017, 2024]:
        try:
            mean_c = _fetch_era5_temp(year)
            print(f"  [heat] {year}: {mean_c}°C (ERA5)")
        except Exception as exc:
            mean_c = HEAT_FALLBACK[str(year)]
            print(f"  [heat] {year}: {mean_c}°C (fallback — {exc})")
        if baseline is None:
            baseline = mean_c
        out[str(year)] = {"mean_c": mean_c, "anomaly": round(mean_c - baseline, 1)}
    return out


@app.get("/api/map-config")
def map_config():
    return JSONResponse({
        "center": MAP_CENTER,
        "zoom": 12,
        "bbox": MAP_BBOX,
        "esri": "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
        "labels": "https://{s}.basemaps.cartocdn.com/light_only_labels/{z}/{x}/{y}{r}.png",
        "ign1956": {"url": AERIAL_WMS, "layers": "AMS_1956-1957"},
        "aerial_wms": AERIAL_WMS,           # per-year real aerial imagery (IGN PNOA)
        "aerials": AERIALS,
        "landcover": _resolve_landcover("1985,2000,2010,2020"),
        "lst_images": _lst_images(),        # OpenLandMap real surface-temperature
        "legend": [{"id": g, "label": l, "color": c} for g, l, c, _ in LANDCOVER_GROUPS],
    })


# ─────────────────────────────────────────────────────────────
# Data API
# ─────────────────────────────────────────────────────────────
@app.get("/api/indicators")
def indicators():
    return JSONResponse(json.loads((DATA / "indicators.json").read_text()))


@app.get("/api/state")
def get_state():
    return JSONResponse(STATE)


class StatePatch(BaseModel):
    stage: str | None = None
    tab: str | None = None
    year: int | None = None
    scenario: str | None = None


@app.post("/api/state")
async def set_state(patch: StatePatch):
    changed = False
    for field in ("stage", "tab", "year", "scenario"):
        val = getattr(patch, field)
        if val is not None and STATE.get(field) != val:
            STATE[field] = val
            changed = True
    if changed:
        STATE["rev"] += 1
        STATE["ts"] = time.time()
        await _broadcast({"type": "state", "state": STATE})
    return JSONResponse(STATE)


class Vote(BaseModel):
    choice: str                      # expand | balance | protect
    session: str | None = None       # anonymous per-visit token from the tablet


@app.post("/api/vote")
async def vote(v: Vote, request: Request):
    if v.choice in CHOICES:
        ua = request.headers.get("user-agent", "")
        await asyncio.get_running_loop().run_in_executor(None, _record_vote, v.choice, v.session, ua)
        await _broadcast({"type": "tally", "tally": _counts()})
    return JSONResponse(_tally_payload())


def _tally_payload() -> dict:
    counts = _counts()
    total = sum(counts.values()) or 1
    return {
        "counts": counts,
        "total": sum(counts.values()),
        "pct": {k: round(counts[k] / total * 100) for k in counts},
    }


@app.get("/api/tally")
def tally():
    return JSONResponse(_tally_payload())


@app.get("/api/responses")
def responses_summary():
    """The full participatory picture: totals, share, and a daily breakdown —
    enough to actually inform a planning decision, not just a live count."""
    payload = _tally_payload()
    daily: dict[str, dict] = {}
    recent: list[dict] = []
    try:
        with _db() as conn:
            for row in conn.execute(
                "SELECT substr(created_at,1,10) d, choice, COUNT(*) n "
                "FROM responses WHERE session IS NOT 'legacy' GROUP BY d, choice ORDER BY d"
            ):
                daily.setdefault(row["d"], {c: 0 for c in CHOICES})[row["choice"]] = row["n"]
            for row in conn.execute(
                "SELECT choice, created_at FROM responses ORDER BY id DESC LIMIT 25"
            ):
                recent.append({"choice": row["choice"], "at": row["created_at"]})
    except Exception as exc:
        payload["error"] = str(exc)
    payload["by_day"] = daily
    payload["recent"] = recent
    return JSONResponse(payload)


@app.get("/api/responses.csv")
def responses_csv():
    """Download every recorded response as CSV — the dataset for analysis."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["id", "choice", "created_at_utc", "session", "user_agent"])
    try:
        with _db() as conn:
            for row in conn.execute(
                "SELECT id, choice, created_at, session, user_agent FROM responses ORDER BY id"
            ):
                w.writerow([row["id"], row["choice"], row["created_at"], row["session"], row["user_agent"]])
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="delta_responses.csv"'},
    )


@app.get("/api/heat-data")
def heat_data_api():
    return JSONResponse(_heat_data())


def _fetch_cams_air(year: int) -> dict:
    """July mean PM2.5 + NO2 from Open-Meteo CAMS historical for the delta."""
    url = (
        "https://air-quality-api.open-meteo.com/v1/air-quality"
        f"?latitude=41.294&longitude=2.0775"
        f"&start_date={year}-07-01&end_date={year}-07-31"
        "&hourly=pm2_5,nitrogen_dioxide"
    )
    data = requests.get(url, timeout=20).json()
    hourly = data.get("hourly", {})

    def _mean(key: str) -> float | None:
        vals = [v for v in (hourly.get(key) or []) if v is not None]
        return round(sum(vals) / len(vals), 1) if vals else None

    pm25 = _mean("pm2_5")
    no2 = _mean("nitrogen_dioxide")
    if pm25 is None or no2 is None:
        raise ValueError("incomplete response")
    return {"pm25": pm25, "no2": no2}


@lru_cache(maxsize=1)
def _air_data() -> dict:
    out: dict[str, dict] = {}
    for year in [2005, 2011, 2017, 2024]:
        if year < 2013:
            d = AIR_FALLBACK.get(str(year), {"pm25": 20.0, "no2": 35.0})
            print(f"  [air] {year}: {d} (pre-CAMS fallback)")
        else:
            try:
                d = _fetch_cams_air(year)
                print(f"  [air] {year}: {d} (CAMS)")
            except Exception as exc:
                d = AIR_FALLBACK.get(str(year), {"pm25": 20.0, "no2": 35.0})
                print(f"  [air] {year}: {d} (fallback — {exc})")
        out[str(year)] = d
    return out


@app.get("/api/air-data")
def air_data_api():
    return JSONResponse(_air_data())


# ─────────────────────────────────────────────────────────────
# Live flight data — OpenSky Network (anonymous, no auth needed)
# Bounding box covers the BCN/LEBL approach + departure sectors.
# Server-side 30-second cache to stay within rate limits.
# ─────────────────────────────────────────────────────────────
_OPENSKY_URL = (
    "https://opensky-network.org/api/states/all"
    "?lamin=40.90&lomin=1.50&lamax=41.70&lomax=2.70"
)
_FLIGHTS_TTL = 30  # seconds
_flights_cache: dict = {"data": None, "ts": 0.0}


@app.get("/api/flights")
def flights_api():
    """Proxy live aircraft positions from OpenSky Network around El Prat (LEBL)."""
    global _flights_cache
    now = time.time()
    if _flights_cache["data"] is not None and now - _flights_cache["ts"] < _FLIGHTS_TTL:
        return JSONResponse(_flights_cache["data"])
    try:
        resp = requests.get(_OPENSKY_URL, timeout=12)
        raw = resp.json()
        planes = []
        for s in (raw.get("states") or []):
            lon, lat = s[5], s[6]
            if lon is None or lat is None:
                continue
            on_ground = s[8] is True
            alt = s[7] or 0
            if on_ground or alt < 300:   # skip taxiing / parked aircraft
                continue
            planes.append({
                "icao": s[0],
                "call": (s[1] or "").strip(),
                "lng":  round(lon, 5),
                "lat":  round(lat, 5),
                "alt":  round(alt),
                "vel":  round(s[9] if s[9] is not None else 150),  # m/s; explicit None check — 0 is valid
                "hdg":  round(s[10] if s[10] is not None else 0),  # true track degrees
            })
        result = {
            "ok": True,
            "ts": raw.get("time", int(now)),
            "count": len(planes),
            "planes": planes,
        }
        _flights_cache = {"data": result, "ts": now}
        return JSONResponse(result)
    except Exception as exc:
        # Copy to avoid mutating the cached good payload with an error key.
        cached = _flights_cache["data"]
        fallback = dict(cached) if cached else {"ok": False, "planes": [], "count": 0}
        fallback["error"] = str(exc)
        return JSONResponse(fallback)


# ─────────────────────────────────────────────────────────────
# OpenFlights route data — real BCN destination/sector map
# ─────────────────────────────────────────────────────────────
_BCN_LAT = 41.2971
_BCN_LON = 2.07846

_OF_AIRPORTS = "https://raw.githubusercontent.com/jpatokal/openflights/master/data/airports.dat"
_OF_ROUTES   = "https://raw.githubusercontent.com/jpatokal/openflights/master/data/routes.dat"
_OF_TTL      = 86400  # 24-hour cache
_of_cache: dict = {"data": None, "ts": 0.0}


def _bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """True bearing in degrees from (lat1,lon1) to (lat2,lon2)."""
    r = math.pi / 180
    dlon = (lon2 - lon1) * r
    lat1r, lat2r = lat1 * r, lat2 * r
    x = math.sin(dlon) * math.cos(lat2r)
    y = math.cos(lat1r) * math.sin(lat2r) - math.sin(lat1r) * math.cos(lat2r) * math.cos(dlon)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def _sector(bearing: float) -> str:
    """Group bearing into named traffic sectors relative to BCN."""
    # ILS 25R arrivals come from ENE (bearing ~45-135 from BCN = NE/E/SE quadrant)
    if 22.5 <= bearing < 112.5:   return "NE"    # N.Europe / UK (dominant ILS 25R traffic)
    if 112.5 <= bearing < 157.5:  return "SE"    # Med East / Middle East
    if 157.5 <= bearing < 202.5:  return "S"     # Africa / Sub-Saharan
    if 202.5 <= bearing < 247.5:  return "SW"    # Canary Islands / W.Africa
    if 247.5 <= bearing < 292.5:  return "W"     # Americas / long-haul (east-wind 07R ops)
    if 292.5 <= bearing < 337.5:  return "NW"    # N.Spain / Portugal
    return "N"                                    # Scandinavia / Scotland


def _fetch_openflights_bcn() -> dict:
    """Return {sector: [IATA, ...]} for top BCN destinations, cached for 24h."""
    global _of_cache
    now = time.time()
    if _of_cache["data"] is not None and now - _of_cache["ts"] < _OF_TTL:
        return _of_cache["data"]

    # Fetch airport coordinates
    apt_raw = requests.get(_OF_AIRPORTS, timeout=20).text.splitlines()
    airports: dict[str, tuple[float, float]] = {}
    for line in apt_raw:
        parts = line.split(",")
        if len(parts) < 8:
            continue
        iata = parts[4].strip('"')
        if not iata or iata == "\\N":
            continue
        try:
            lat, lon = float(parts[6]), float(parts[7])
            airports[iata] = (lat, lon)
        except ValueError:
            pass

    # Fetch routes and count BCN connections
    rte_raw = requests.get(_OF_ROUTES, timeout=20).text.splitlines()
    counts: dict[str, int] = {}
    for line in rte_raw:
        parts = line.split(",")
        if len(parts) < 5:
            continue
        src, dst = parts[2].strip('"'), parts[4].strip('"')
        if src == "BCN" and dst in airports:
            counts[dst] = counts.get(dst, 0) + 1
        elif dst == "BCN" and src in airports:
            counts[src] = counts.get(src, 0) + 1

    # Group top destinations by sector
    sectors: dict[str, list[str]] = {"NE": [], "SE": [], "S": [], "SW": [], "W": [], "NW": [], "N": []}
    sorted_dests = sorted(counts.keys(), key=lambda k: counts[k], reverse=True)
    for iata in sorted_dests:
        if iata == "BCN":
            continue
        lat, lon = airports[iata]
        brg = _bearing(_BCN_LAT, _BCN_LON, lat, lon)
        sec = _sector(brg)
        if len(sectors[sec]) < 12:
            sectors[sec].append(iata)

    _of_cache = {"data": sectors, "ts": now}
    return sectors


@app.get("/api/bcn-routes")
def bcn_routes_api():
    """Destination IATA codes grouped by bearing sector from BCN (OpenFlights)."""
    try:
        sectors = _fetch_openflights_bcn()
        return JSONResponse({"ok": True, "sectors": sectors})
    except Exception as exc:
        # Return a safe fallback so the frontend never breaks
        return JSONResponse({"ok": False, "error": str(exc), "sectors": {
            "NE": ["LHR","CDG","AMS","FRA","MUC","BRU","LGW","ORY"],
            "SE": ["FCO","ATH","IST","TLV","CAI","NAP"],
            "S":  ["CMN","TUN","ALG","RAK"],
            "SW": ["LPA","TFS","TFN","ACE"],
            "W":  ["MAD","LIS","JFK","EWR","MIA"],
            "NW": ["BIO","VGO","SCQ","PMI"],
            "N":  ["CDG","ORY","GVA","GEN","NCE"],
        }})


def _fetch_gbif_birds(year: int) -> dict:
    """Unique bird species observed in the delta via GBIF for a given year."""
    url = (
        "https://api.gbif.org/v1/occurrence/search"
        "?classKey=212"                 # Aves
        "&decimalLongitude=1.95,2.15"
        "&decimalLatitude=41.26,41.38"
        f"&year={year}"
        "&limit=0&facet=speciesKey&facetLimit=600"
    )
    resp = requests.get(url, timeout=20).json()
    facets = resp.get("facets") or []
    species = len(facets[0].get("counts", [])) if facets else 0
    return {"species": species, "occurrences": resp.get("count", 0)}


@lru_cache(maxsize=1)
def _life_data() -> dict:
    out: dict[str, dict] = {}
    for year in [1985, 2000, 2010, 2020]:
        try:
            d = _fetch_gbif_birds(year)
            print(f"  [life] {year}: {d} (GBIF)")
        except Exception as exc:
            print(f"  [life] {year}: fallback ({exc})")
            d = {"species": None, "occurrences": None}
        out[str(year)] = d
    return out


@app.get("/api/life-data")
def life_data_api():
    return JSONResponse(_life_data())


@app.on_event("startup")
async def _prefetch():
    _init_db()                                                # participatory responses DB (+ migrate legacy votes)
    loop = asyncio.get_running_loop()
    loop.run_in_executor(None, _heat_data)                    # temperature tab → ERA5 July means
    loop.run_in_executor(None, _life_data)                    # life tab → GBIF bird species
    loop.run_in_executor(None, _resolve_landcover, "1985,2000,2010,2020")  # land tab → GLC tiles
    loop.run_in_executor(None, _fetch_openflights_bcn)        # pollution tab → BCN route sectors


@app.get("/api/reset")
async def reset():
    """Admin: return the installation to the hook for the next visitor.
    Recorded responses are PRESERVED — this is real participatory data and the
    public tally is cumulative. (To export, see /api/responses.csv.)"""
    STATE.update(stage="hook", tab="land", year=2025, scenario=None)
    STATE["rev"] += 1
    await _broadcast({"type": "state", "state": STATE})
    await _broadcast({"type": "tally", "tally": _counts()})
    return JSONResponse({"ok": True, "preserved_responses": sum(_counts().values())})


# ─────────────────────────────────────────────────────────────
# SSE stream — projection + kiosks subscribe here
# ─────────────────────────────────────────────────────────────
@app.get("/api/stream")
async def stream(request: Request):
    q: asyncio.Queue = asyncio.Queue()
    _subscribers.add(q)

    async def gen():
        # Prime the new client with current state + tally.
        yield _sse({"type": "state", "state": STATE})
        yield _sse({"type": "tally", "tally": _counts()})
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    payload = await asyncio.wait_for(q.get(), timeout=15)
                    yield _sse(payload)
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"  # comment frame keeps the pipe open
        finally:
            _subscribers.discard(q)

    return StreamingResponse(gen(), media_type="text/event-stream")


def _sse(obj: dict) -> str:
    return f"data: {json.dumps(obj)}\n\n"


# ─────────────────────────────────────────────────────────────
# Pages
# ─────────────────────────────────────────────────────────────
@app.head("/")
def tablet_head():
    return Response()


@app.get("/")
def tablet():
    return FileResponse(STATIC / "tablet.html")


@app.get("/projection")
def projection():
    return FileResponse(STATIC / "projection.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    print("\n  THE GROUND BENEATH GROWTH -- installation server")
    print("  ------------------------------------------------")
    print(f"  Tablet      ->  http://localhost:{port}/")
    print(f"  Projection  ->  http://localhost:{port}/projection")
    print(f"  Live flights->  http://localhost:{port}/api/flights")
    print(f"  Responses   ->  http://localhost:{port}/api/responses   (CSV: /api/responses.csv)")
    print(f"  Reset day   ->  http://localhost:{port}/api/reset\n")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
