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
MAP_CENTER = [41.2974, 2.0833]
MAP_BBOX = [1.95, 41.26, 2.15, 41.38]  # west, south, east, north

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


@app.get("/api/map-config")
def map_config():
    return JSONResponse({
        "center": MAP_CENTER,
        "zoom": 12,
        "bbox": MAP_BBOX,
        "esri": "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
        "labels": "https://{s}.basemaps.cartocdn.com/light_only_labels/{z}/{x}/{y}{r}.png",
        "ign1956": {"url": "https://www.ign.es/wms/pnoa-historico?", "layers": "AMS_1956-1957"},
        "landcover": _resolve_landcover("1985,2005"),
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
