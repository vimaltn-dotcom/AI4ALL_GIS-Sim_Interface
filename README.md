# The Ground Beneath Growth

An interactive installation about the **Llobregat Delta** beside Barcelona–El Prat
airport (LEBL/BCN) — Catalonia's last great coastal wetland, and the proposed
third runway that would reach into it. Visitors watch 40 years of change unfold
across four lenses, then cast a vote on the delta's future.

The piece runs as **two synced surfaces** driven by one server:

- **Tablet** (`/`) — the controller embedded in the table edge. A visitor walks
  the *Hook → Explore → Decide* flow.
- **Projection** (`/projection`) — a ceiling projector casts the same scene down
  onto a **laser-engraved board (0.80 m × 0.60 m)** of the airport. Whatever the
  tablet shows, the projection mirrors in real time.

The tablet POSTs its state to the server, which broadcasts it over Server-Sent
Events so the projection updates instantly. Votes are tallied server-side, so
multiple visitors build a living public record.

---

## The four lenses

Each tab steps through milestone years, cross-dissolving real imagery in place
(coordinates stay locked so the airport never moves):

| Tab | Years | What it shows | Real data |
|-----|-------|---------------|-----------|
| **Land** | 1985 · 2000 · 2010 · 2020 | Wetland & farmland sealed under runway and city | GLC_FCS30D land cover (via TiTiler) over IGN aerials |
| **Heat** | 2005 · 2011 · 2017 · 2024 | Summer surface heat — sealed runways bake ~20 °C hotter than the wetland | OpenLandMap MOD11A2 land-surface temperature |
| **Traffic** | 2005 · 2011 · 2017 · 2024 | Flight corridors thickening over the lagoons; live aircraft | Simulated routes from OpenFlights + live OpenSky positions |
| **Life** | 1985 · 2000 · 2010 · 2020 | Bird species fading as habitat is lost | GBIF occurrence records; AI cinematic video backgrounds |

Beneath every tab the **base map itself changes with the year**: real
**IGN PNOA histórico** aerial orthophotos of El Prat, cross-faded from the 1956
delta through to the modern airport. (Barcelona is only re-flown every few years,
so each milestone maps to the nearest year with real coverage.)

After exploring, the visitor decides 2050:

1. **Yes, expand the airport** — build the third runway; accept the wetland is lost.
2. **Allow a limited expansion** — grow under strict limits; some delta restored.
3. **No, don't expand it** — protect what's left and let the wetland recover.

---

## Real data sources

Nothing here is invented imagery — every layer is a real public dataset:

- **IGN PNOA histórico** (Instituto Geográfico Nacional de España) — historical
  aerial orthophotos, 1956 → present, via WMS.
- **OpenLandMap** (`lst_mod11a2.daytime.annual`) — MODIS land-surface temperature
  COGs, rendered through **TiTiler** with a blue→red ramp.
- **GLC_FCS30D** — 30 m global land cover, discovered via STAC and tiled by TiTiler.
- **OpenSky Network** — live aircraft positions around El Prat (`/api/flights`).
- **OpenFlights** — real BCN routes, grouped by bearing sector to label flights.
- **GBIF** — bird species / occurrence counts for the delta bounding box.
- **Open-Meteo ERA5** — July temperature reference.
- **Esri World Imagery** — present-day satellite base.

---

## Architecture

```
delta-interface/
├── app.py                  FastAPI server: state, SSE sync, votes, data proxies
├── requirements.txt
├── render.yaml             Render.com deploy config
├── data/
│   ├── indicators.json     curated land/life indicators + 2050 scenarios
│   └── votes.json          persisted public tally
└── static/
    ├── tablet.html         the controller UI (Hook → Explore → Decide)
    ├── projection.html     the mirrored ceiling projection + board calibration
    ├── delta-scene.js      the cross-dissolve map engine (Leaflet layer tweening)
    └── video/              AI cinematic backgrounds (per tab+year)
```

- **Server** — FastAPI + Uvicorn. Holds the single source of truth (`STATE`),
  fans it out over SSE (`/api/stream`), tallies votes, and proxies/ caches the
  external data feeds. Static assets are served `no-store` so live edits always
  refresh.
- **Map engine** (`delta-scene.js`) — every layer lives on a Leaflet map at
  opacity 0; year/tab changes tween opacities plus an era colour-grade. Tile
  layers keep imagery pixel-aligned to the engraving.
- **Canvas overlays** (in `tablet.html`) — aircraft and bird animations drawn on
  2D canvases above the map.

### Tech stack
Python · FastAPI · Uvicorn · Leaflet 1.9 · Server-Sent Events · Canvas 2D ·
TiTiler · vanilla JS (no build step).

---

## Running locally

```bash
pip install -r requirements.txt
python app.py
```

Then open:

- Tablet (controller): <http://localhost:8000/>
- Projection (mirror):  <http://localhost:8000/projection>
- Live flights JSON:    <http://localhost:8000/api/flights>
- Reset the day's votes: <http://localhost:8000/api/reset>

The server listens on `$PORT` (default `8000`).

---

## API

| Endpoint | Purpose |
|----------|---------|
| `GET /` · `GET /projection` | the two UI pages |
| `GET /api/map-config` | tile/WMS URLs, bbox, per-year aerials & LST images |
| `GET /api/indicators` | land/life indicators + 2050 scenarios |
| `GET/POST /api/state` | read / drive the shared installation state |
| `GET /api/stream` | SSE feed of state + tally (projection subscribes here) |
| `POST /api/vote` · `GET /api/tally` | cast and read the public vote |
| `GET /api/flights` | live OpenSky aircraft around El Prat (30 s cache) |
| `GET /api/bcn-routes` | OpenFlights BCN destinations by sector (24 h cache) |
| `GET /api/heat-data` · `GET /api/life-data` | ERA5 heat · GBIF birds |
| `GET /api/reset` | clear votes and return to the hook |

---

## Projection calibration

The projected image must register onto the physical engraved board. On the
**projection** screen:

| Key | Action |
|-----|--------|
| `+` / `-` | zoom the map in / out |
| arrow keys | nudge position |
| `Shift` + above | larger steps |
| `R` | reset to default |
| `G` | toggle the registration grid |

The fit is saved to `localStorage`, so it survives reloads. The default starts
slightly zoomed in to fill the board.

---

## Adding cinematic video backgrounds

AI-generated videos can replace the map background for a specific tab+year.
Drop the file in `static/video/` and add one line to the `VIDEO_BG` map in
**both** `tablet.html` and `projection.html`:

```js
const VIDEO_BG = {
  'life_1985': '/static/video/Birds_swarm_coast_and_airport_202606141800.mp4',
  // 'pollution_2024': '/static/video/...mp4',   // tab_year : path
};
```

The video fades in (muted, looped, cover-fit) when that view is active and fades
out otherwise; on Life it also suppresses the canvas-bird animation so they don't
overlap. On the projection the video sits inside the calibrated board area.

---

## Deployment

Configured for **Render.com** (`render.yaml`): Python web service, installs
`requirements.txt`, starts Uvicorn on `$PORT`, health-checks `/api/tally`.
External data is fetched live and cached server-side, so the only state that
persists is the vote tally in `data/votes.json`.
