/* ============================================================================
   delta-scene.js — cinematic real-map scene of the Llobregat Delta
   ----------------------------------------------------------------------------
   Cross-dissolve engine ported from AI4ALL_Decision's MorphLayer ("Story"):
     • every layer lives on the map permanently; views are OPACITY TARGETS
     • year/tab changes tween opacities + an era colour-grade (warm golden
       1956 aerial → cold grey present) with an eased rAF loop
     • modelled fields (temperature / pollution / life / 2050 scenarios)
       cross-fade through two image-overlay slots

   Real layers:
     base        Esri World Imagery (present-day satellite)      — always on
     ign         IGN PNOA histórico 1956-57 aerial (real)        — year 1969
     lc1985/2005 GLC_FCS30D land-cover via TiTiler (real)        — 1985 / 2005
     (2025 = the satellite base itself)

   Requires Leaflet (window.L).
   ========================================================================== */
(function (global) {
  'use strict';
  const L = global.L;

  const ease = t => (t < 0.5 ? 2 * t * t : 1 - Math.pow(-2 * t + 2, 2) / 2);

  // era colour-grades (MorphLayer technique: sepia/sat/brightness/contrast)
  const GRADES = {
    y1969: { sepia: 0.45, sat: 0.92, bri: 1.04, con: 1.00 },
    y1985: { sepia: 0.22, sat: 0.94, bri: 1.00, con: 1.03 },
    y2000: { sepia: 0.14, sat: 0.95, bri: 0.99, con: 1.05 },
    y2005: { sepia: 0.08, sat: 0.96, bri: 0.98, con: 1.06 },
    y2010: { sepia: 0.04, sat: 0.94, bri: 0.97, con: 1.07 },
    y2020: { sepia: 0.00, sat: 0.90, bri: 0.96, con: 1.09 },
    y2025: { sepia: 0.00, sat: 0.88, bri: 0.95, con: 1.10 },
    field: { sepia: 0.00, sat: 0.95, bri: 0.97, con: 1.05 },
    expand:  { sepia: 0.0, sat: 0.62, bri: 0.90, con: 1.16 },
    balance: { sepia: 0.05, sat: 0.95, bri: 0.98, con: 1.04 },
    protect: { sepia: 0.0, sat: 1.18, bri: 1.03, con: 1.00 },
  };
  const yearGrade = y => GRADES['y' + y] || GRADES.y2025;

  class DeltaScene {
    constructor(divId, cfg, opts = {}) {
      this.cfg = cfg;
      this.interactive = !!opts.interactive;
      const map = L.map(divId, {
        center: cfg.center, zoom: cfg.zoom, zoomControl: false, attributionControl: false,
        dragging: this.interactive, scrollWheelZoom: this.interactive, doubleClickZoom: this.interactive,
        touchZoom: this.interactive, boxZoom: false, keyboard: false, zoomSnap: 0.25,
        fadeAnimation: true,
      });
      this.map = map;

      const b = cfg.bbox; // [west, south, east, north]
      this._bounds = [[b[1], b[0]], [b[3], b[2]]];

      // base satellite — always visible
      L.tileLayer(cfg.esri, { maxZoom: 19, zIndex: 100 }).addTo(map);
      this.labels = L.tileLayer(cfg.labels, { maxZoom: 19, opacity: 0.5, zIndex: 700 }).addTo(map);

      // persistent cross-fade layers, all mounted at opacity 0
      this._L = {};

      // Per-year REAL aerial imagery (IGN PNOA histórico WMS), one tile layer
      // per milestone year. Cross-faded in place so the ground genuinely
      // changes through time while the coordinates stay locked. Tile layers
      // (not image overlays) keep the runways pixel-aligned to the engraving.
      if (cfg.aerials && cfg.aerial_wms) {
        for (const yr of Object.keys(cfg.aerials)) {
          // PNG + transparent: PNOA is land-only, so sea / coverage gaps fall
          // through to the Esri satellite base instead of rendering white.
          this._L['aer' + yr] = L.tileLayer.wms(cfg.aerial_wms, {
            layers: cfg.aerials[yr], format: 'image/png', version: '1.1.1',
            transparent: true, maxZoom: 19, opacity: 0, zIndex: 410,
          }).addTo(map);
        }
      }
      if (cfg.landcover) {
        for (const yr of Object.keys(cfg.landcover)) {
          this._L['lc' + yr] = L.tileLayer(cfg.landcover[yr], { maxZoom: 19, opacity: 0, zIndex: 450 }).addTo(map);
        }
      }
      // OpenLandMap land-surface temperature (real, via TiTiler) rendered as a
      // single bbox image per year and CSS-blurred — a seamless heat wash with
      // no tile-grid steps. Cool water/wetland blue, baking sealed ground red.
      if (cfg.lst_images) {
        for (const yr of Object.keys(cfg.lst_images)) {
          this._L['lst' + yr] = L.imageOverlay(cfg.lst_images[yr], this._bounds, {
            opacity: 0, zIndex: 530, interactive: false, className: 'lst-smooth',
          }).addTo(map);
        }
      }
      this._op = {};                 // current opacity per key
      for (const k of Object.keys(this._L)) this._op[k] = 0;
      this._grade = { ...yearGrade(2025) };
      this._raf = 0;
      this._heat = opts.heat || {};
      this._applyGrade(this._grade);
    }

    _applyGrade(g) {
      this.map.getContainer().style.filter =
        `sepia(${g.sepia.toFixed(3)}) saturate(${g.sat.toFixed(3)}) brightness(${g.bri.toFixed(3)}) contrast(${g.con.toFixed(3)})`;
    }

    /* Tween all layer opacities + the grade toward targets. */
    _tween(targets, grade, ms) {
      cancelAnimationFrame(this._raf);
      const keys = Object.keys(this._L);
      const from = {}; keys.forEach(k => from[k] = this._op[k] || 0);
      const to = {}; keys.forEach(k => to[k] = targets[k] != null ? targets[k] : 0);
      const g0 = { ...this._grade }, g1 = grade || this._grade;
      if (!ms) { // instant
        keys.forEach(k => { this._op[k] = to[k]; this._L[k].setOpacity(to[k]); });
        this._grade = { ...g1 }; this._applyGrade(g1); return;
      }
      let start = 0;
      const step = (ts) => {
        if (!start) start = ts;
        const t = Math.min(1, (ts - start) / ms), e = ease(t);
        keys.forEach(k => {
          const v = from[k] + (to[k] - from[k]) * e;
          if (v !== this._op[k]) { this._op[k] = v; this._L[k].setOpacity(v); }
        });
        this._grade = {
          sepia: g0.sepia + (g1.sepia - g0.sepia) * e,
          sat: g0.sat + (g1.sat - g0.sat) * e,
          bri: g0.bri + (g1.bri - g0.bri) * e,
          con: g0.con + (g1.con - g0.con) * e,
        };
        this._applyGrade(this._grade);
        if (t < 1) this._raf = requestAnimationFrame(step);
      };
      this._raf = requestAnimationFrame(step);
    }

    /* Pick the real aerial layer key for a year (exact match, else nearest). */
    _aerialKey(year) {
      if (this._L['aer' + year]) return 'aer' + year;
      let best = null, bestD = 1e9;
      for (const k of Object.keys(this._L)) {
        if (k.slice(0, 3) !== 'aer') continue;
        const d = Math.abs(parseInt(k.slice(3), 10) - year);
        if (d < bestD) { bestD = d; best = k; }
      }
      return best;
    }

    /* The one call the pages use.
       state = {tab, year, scenario, stage} · opts = {animate (default true), ms} */
    setView(state, opts = {}) {
      const animate = opts.animate !== false;
      const ms = animate ? (opts.ms || 1600) : 0;
      const tab = state.tab || 'land';
      const year = state.year || 2025;
      const scenario = state.scenario || null;
      const targets = {};

      // Real aerial base for this year, cross-faded in on every tab.
      const aer = this._aerialKey(year);
      if (aer) targets[aer] = 1;

      if (state.stage === 'result' && scenario) {
        // 2050 futures are hypothetical: keep the latest real aerial and let an
        // era colour-grade carry the mood (bleak grey expand → lush protect).
        this._tween(targets, GRADES[scenario] || GRADES.balance, ms);
        return;
      }

      if (tab === 'land') {
        if (year < 2025 && this._L['lc' + year]) targets['lc' + year] = 0.82;
        this._tween(targets, yearGrade(year), ms);
        return;
      }

      if (tab === 'temperature') {
        // Real OpenLandMap hot-season surface temperature, kept translucent so
        // the aerial (runways, coast, wetland) reads clearly THROUGH the heat
        // tint — you see both the place and how hot each surface runs.
        if (this._L['lst' + year]) targets['lst' + year] = 0.58;
        this._tween(targets, yearGrade(year), ms);
        return;
      }
      // pollution (air traffic), life — year's aerial base; canvas animations carry the story.
      this._tween(targets, GRADES.field, ms);
    }

    invalidate() { this.map.invalidateSize(); }
    fitDelta(pad) { this.map.fitBounds(this._bounds, { padding: [pad || 0, pad || 0], animate: false }); }
    flyHome(ms) { this.map.flyToBounds(this._bounds, { duration: (ms || 1200) / 1000 }); }
  }

  global.DeltaScene = DeltaScene;
})(window);
