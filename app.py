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
import json
import re
import time
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote_plus, urljoin

import requests
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

BASE = Path(__file__).parent
STATIC = BASE / "static"
DATA = BASE / "data"
VOTES_FILE = DATA / "votes.json"

app = FastAPI(title="The Ground Beneath Growth — Installation")

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


def _load_votes() -> dict:
    if VOTES_FILE.exists():
        try:
            return json.loads(VOTES_FILE.read_text())
        except Exception:
            pass
    return {"expand": 0, "balance": 0, "protect": 0}


def _save_votes(v: dict) -> None:
    VOTES_FILE.write_text(json.dumps(v))


VOTES = _load_votes()


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
MAP_CENTER = [41.2940, 2.0775]
MAP_BBOX = [2.0255, 41.272020, 2.1295, 41.315973]  # west, south, east, north

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
        "ign1956": {"url": "https://www.ign.es/wms/pnoa-historico?", "layers": "AMS_1956-1957"},
        "landcover": _resolve_landcover("1985,2000,2010,2020"),
        "modis_lst": MODIS_LST_TILES,
        "aod_tiles": AOD_TILES,
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
    choice: str  # expand | balance | protect


@app.post("/api/vote")
async def vote(v: Vote):
    if v.choice in VOTES:
        VOTES[v.choice] += 1
        _save_votes(VOTES)
        await _broadcast({"type": "tally", "tally": VOTES})
    return JSONResponse(_tally_payload())


def _tally_payload() -> dict:
    total = sum(VOTES.values()) or 1
    return {
        "counts": VOTES,
        "total": sum(VOTES.values()),
        "pct": {k: round(VOTES[k] / total * 100) for k in VOTES},
    }


@app.get("/api/tally")
def tally():
    return JSONResponse(_tally_payload())


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
    loop = asyncio.get_running_loop()
    loop.run_in_executor(None, _heat_data)
    loop.run_in_executor(None, _air_data)
    loop.run_in_executor(None, _life_data)
    loop.run_in_executor(None, _resolve_landcover, "1985,2000,2010,2020")


@app.get("/api/reset")
async def reset():
    """Admin: clear the day's votes and return the installation to the hook."""
    for k in VOTES:
        VOTES[k] = 0
    _save_votes(VOTES)
    STATE.update(stage="hook", tab="land", year=2025, scenario=None)
    STATE["rev"] += 1
    await _broadcast({"type": "tally", "tally": VOTES})
    await _broadcast({"type": "state", "state": STATE})
    return JSONResponse({"ok": True})


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
        yield _sse({"type": "tally", "tally": VOTES})
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
@app.get("/")
def tablet():
    return FileResponse(STATIC / "tablet.html")


@app.get("/projection")
def projection():
    return FileResponse(STATIC / "projection.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")


if __name__ == "__main__":
    print("\n  THE GROUND BENEATH GROWTH — installation server")
    print("  ────────────────────────────────────────────────")
    print("  Tablet     →  http://localhost:8000/")
    print("  Projection →  http://localhost:8000/projection")
    print("  Reset day  →  http://localhost:8000/api/reset\n")
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="warning")
