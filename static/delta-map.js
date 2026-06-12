/* ============================================================================
   delta-map.js — shared renderer for the Llobregat Delta
   ----------------------------------------------------------------------------
   One canvas engine used by BOTH the tablet preview and the full projection.
   Geography is calibrated to the real BBOX (west 1.95 / east 2.15 / south
   41.26 / north 41.38), ported from the original "Ground Beneath Growth"
   classifier. On top of the land-cover base we derive four readable layers:

       land         classified land cover (the base)
       temperature  urban-heat surface — hot where sealed, cool where wet
       pollution    a plume that grows around the airport / city
       life         where wildlife habitat survives

   Year → t mapping (history):  t = (year - 1985) / 40
       1969 → -0.40 (near-pristine)   1985 → 0.0
       2005 →  0.50                   2025 → 1.0
   ========================================================================== */
(function (global) {
  'use strict';

  // Land-cover palette (satellite-calibrated)
  const COL = {
    0: '#3f7d88',  // sea / background
    1: '#5e93a6',  // open water (river / coastal)
    2: '#7aa86a',  // marsh / delta wetland
    3: '#cdb97a',  // farmland
    4: '#3f6b41',  // natural habitat (pine forest)
    5: '#b07a5c',  // urban / built-up
    6: '#d8cd92',  // airport hard surface
  };

  function yearToT(year) { return (year - 1985) / 40; }

  function cellSeed(i, j) {
    let h = ((i * 73856093) ^ (j * 19349663)) >>> 0;
    h = (((h >> 16) ^ h) * 0x45d9f3b) >>> 0;
    h = (((h >> 16) ^ h) * 0x45d9f3b) >>> 0;
    return (((h >> 16) ^ h) >>> 0) / 0xffffffff;
  }

  // ── Base land types (calibrated to satellite anchors) ──
  function base(nx, ny) {
    const coast = 0.928 + 0.014 * Math.sin(nx * Math.PI * 3.2) - 0.006 * nx;
    if (ny > coast) return 'sea';
    if (ny > coast - 0.036) return 'shore';
    const rx = 0.775 + 0.012 * Math.sin(ny * Math.PI * 2.1);
    if (Math.abs(nx - rx) < 0.018 + 0.010 * ny) return 'river';
    const dw1 = Math.sqrt((nx - 0.13) * (nx - 0.13) * 1.4 + (ny - 0.82) * (ny - 0.82));
    const dw2 = Math.sqrt((nx - 0.24) * (nx - 0.24) + (ny - 0.76) * (ny - 0.76) * 1.3);
    const dw3 = Math.sqrt((nx - 0.07) * (nx - 0.07) * 0.9 + (ny - 0.72) * (ny - 0.72));
    if (dw1 < 0.13 || dw2 < 0.11 || dw3 < 0.07) return 'wetland';
    if (nx > 0.25 && nx < 0.88 && ny > 0.53 && ny < 0.93) {
      if (nx < 0.38 && ny < 0.67) return 'farm';
      return 'airport';
    }
    return 'farm';
  }

  // ── Land class (0-6) for a cell at history-time p (or a future scenario) ──
  function cellType(c, phase, t, scenario) {
    const b = c.base;
    if (b === 'sea' || b === 'shore') return 0;
    if (phase === 'history') {
      const p = t;
      if (b === 'airport') {
        const l = (c.nx - 0.25) / 0.63;
        if (l > 0.55) return 6;
        if (l > 0.30 && l <= 0.55) return p > 0.3 ? 6 : 3;
        return p > 0.6 ? 6 : p > 0.25 ? 5 : 3;
      }
      if (b === 'wetland') {
        const r = Math.sqrt((c.nx - 0.13) * (c.nx - 0.13) * 1.4 + (c.ny - 0.82) * (c.ny - 0.82));
        const dr = 0.13 * (1 - 0.55 * p);
        if (r > dr) return p > 0.5 ? 5 : 3;
        if (c.seed > 0.82 && p < 0.5) return 4;
        return 2;
      }
      if (b === 'river') return 1;
      if (b === 'farm') {
        if (c.ny < 0.35 && c.seed < p * 0.55) return 5;
        if (c.ny < 0.5 && c.nx > 0.6 && c.seed < p * 0.3) return 5;
        if (c.seed < p * 0.08) return 5;
        return 3;
      }
      return 3;
    }
    // future scenarios
    const p = t;
    if (b === 'airport') {
      if (scenario === 'protect') { const l = (c.nx - 0.25) / 0.63; return (l < 0.35 && c.seed > 0.5) ? 2 : 6; }
      if (scenario === 'balance') { const l = (c.nx - 0.25) / 0.63; return l < 0.30 + 0.15 * p ? 5 : 6; }
      return 6;
    }
    if (b === 'wetland') {
      const r = Math.sqrt((c.nx - 0.13) * (c.nx - 0.13) * 1.4 + (c.ny - 0.82) * (c.ny - 0.82));
      if (scenario === 'expand') { if (c.nx > 0.22 && c.ny > 0.68) return p > 0.4 ? 6 : 5; if (r > 0.065) return p > 0.5 ? 5 : 3; return c.seed > 0.9 ? 5 : 2; }
      if (scenario === 'balance') { if (r < 0.07) return 2; if (r < 0.10 && c.seed > 0.6) return 4; return 3; }
      if (r < 0.065 * 2.2) return c.seed > 0.78 ? 4 : 2; return 2;
    }
    if (b === 'river') return 1;
    if (b === 'farm') {
      if (scenario === 'expand') return c.seed < 0.52 ? 5 : 3;
      if (scenario === 'protect') return (c.ny > 0.5 && c.seed > 0.7) ? 2 : 3;
      return c.seed < 0.27 ? 5 : 3;
    }
    return 3;
  }

  // colour helpers
  function hexToRgb(h) { const c = h.replace('#', ''); return [parseInt(c.substr(0, 2), 16), parseInt(c.substr(2, 2), 16), parseInt(c.substr(4, 2), 16)]; }
  function mix(a, b, f) { return [a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f, a[2] + (b[2] - a[2]) * f]; }
  function rgb(a) { return `rgb(${a[0] | 0},${a[1] | 0},${a[2] | 0})`; }

  // temperature ramp: cool blue → amber → deep red-crimson
  const T_COOL = hexToRgb('#1a6faa'), T_MID = hexToRgb('#e85d00'), T_HOT = hexToRgb('#8b0000');
  function tempColor(v) { return v < 0.5 ? rgb(mix(T_COOL, T_MID, v / 0.5)) : rgb(mix(T_MID, T_HOT, (v - 0.5) / 0.5)); }

  // pollution ramp: clear → amber → violet-brown
  const P_LOW = hexToRgb('#1a9850'), P_MID = hexToRgb('#fdae61'), P_HI = hexToRgb('#7b3294');
  function polColor(v) { return v < 0.5 ? mix(P_LOW, P_MID, v / 0.5) : mix(P_MID, P_HI, (v - 0.5) / 0.5); }

  class DeltaMap {
    constructor(canvas, opts = {}) {
      this.canvas = canvas;
      this.ctx = canvas.getContext('2d');
      this.G = opts.grid || 120;
      this.glow = opts.glow !== false;      // soft bloom for projection
      this.cells = [];
      this._build();
    }

    _build() {
      const G = this.G;
      this.cells = [];
      for (let j = 0; j < G; j++) for (let i = 0; i < G; i++) {
        const nx = (i + 0.5) / G, ny = (j + 0.5) / G;
        this.cells.push({ i, j, nx, ny, base: base(nx, ny), seed: cellSeed(i, j) });
      }
    }

    /* state = {layer, year, phase, scenario} */
    render(state) {
      const { ctx, canvas, G } = this;
      const w = canvas.width, h = canvas.height;
      const cw = w / G, ch = h / G;
      const layer = state.layer || 'land';
      const phase = state.phase || 'history';
      const t = state.t != null ? state.t : yearToT(state.year || 2025);
      const anomaly = state.anomaly != null ? state.anomaly : null;
      const scenario = state.scenario || null;

      ctx.clearRect(0, 0, w, h);
      // dark sea base
      ctx.fillStyle = '#0c1418';
      ctx.fillRect(0, 0, w, h);

      for (const c of this.cells) {
        const cls = cellType(c, phase, t, scenario);
        let color = COL[cls];

        if (layer === 'temperature') {
          // sealed/dark surfaces store heat; water & wetland stay cool
          let heat = 0.18;
          if (cls === 6) heat = 0.97;        // tarmac
          else if (cls === 5) heat = 0.86;   // urban
          else if (cls === 3) heat = 0.52;   // bare-ish farmland
          else if (cls === 4) heat = 0.28;   // forest
          else if (cls === 2) heat = 0.12;   // marsh
          else if (cls === 1) heat = 0.05;   // water
          else if (cls === 0) { color = '#0c1418'; ctx.fillStyle = color; ctx.fillRect(c.i * cw, c.j * ch, cw + 1, ch + 1); continue; }
          // real ERA5 anomaly (°C) calibrates the baseline; falls back to time-based proxy
          const warming = anomaly != null ? Math.max(0, anomaly) / 6 : 0.18 * Math.max(0, t);
          heat = Math.min(1, heat + warming);
          color = tempColor(heat);
        } else if (layer === 'pollution') {
          if (cls === 0) { ctx.fillStyle = '#0c1418'; ctx.fillRect(c.i * cw, c.j * ch, cw + 1, ch + 1); continue; }
          // plume centred on the airport core (nx .62, ny .72), drifting NE with the city
          const ax = 0.62, ay = 0.72;
          const d = Math.sqrt((c.nx - ax) * (c.nx - ax) + (c.ny - ay) * (c.ny - ay));
          const era = Math.min(1, Math.max(0, (t + 0.4) / 1.4));   // 0 at 1969 → 1 at 2025
          let conc = Math.max(0, 1 - d / (0.30 + 0.45 * era)) * (0.35 + 0.65 * era);
          if (cls === 6 || cls === 5) conc = Math.min(1, conc + 0.25); // sources
          conc = Math.min(1, conc);
          const land = hexToRgb(COL[cls]);
          color = rgb(mix(land, polColor(conc), 0.35 + 0.5 * conc));
        } else if (layer === 'life') {
          // where life persists: wetland, habitat, water = alive; everything else dim
          if (cls === 2 || cls === 4) color = '#54d07a';
          else if (cls === 1) color = '#3a8fa6';
          else if (cls === 3) color = '#5d5a40';
          else color = '#1c2226';
        }

        ctx.fillStyle = color;
        ctx.fillRect(c.i * cw, c.j * ch, cw + 1, ch + 1);
      }

      // soft bloom pass for the projection (reads better as light on the model)
      if (this.glow) {
        ctx.save();
        ctx.globalCompositeOperation = 'lighter';
        ctx.globalAlpha = 0.06;
        ctx.drawImage(canvas, -1, -1, w + 2, h + 2);
        ctx.restore();
      }
      this._labels(ctx, w, h, layer);
    }

    /* Transparent data field for laying OVER a real satellite map.
       Returns a data URL (PNG with alpha). state = {layer, year, phase, scenario} */
    renderField(state) {
      const { ctx, canvas, G } = this;
      const w = canvas.width, h = canvas.height;
      const cw = w / G, ch = h / G;
      const layer = state.layer || 'temperature';
      const phase = state.phase || 'history';
      const t = state.t != null ? state.t : yearToT(state.year || 2025);
      const anomaly = state.anomaly != null ? state.anomaly : null;
      const scenario = state.scenario || null;
      ctx.clearRect(0, 0, w, h);

      for (const c of this.cells) {
        const cls = cellType(c, phase, t, scenario);
        let col = null, alpha = 0;

        if (layer === 'scenario' || layer === 'land') {
          if (cls === 0) continue;                 // leave sea transparent
          col = hexToRgb(COL[cls]); alpha = 0.72;
        } else if (layer === 'temperature') {
          if (cls === 0) continue;
          let heat = cls === 6 ? 0.97 : cls === 5 ? 0.86 : cls === 3 ? 0.52 : cls === 4 ? 0.28 : cls === 2 ? 0.12 : 0.05;
          const warming = anomaly != null ? Math.max(0, anomaly) / 6 : 0.18 * Math.max(0, t);
          heat = Math.min(1, heat + warming);
          col = (heat < 0.5) ? mix(T_COOL, T_MID, heat / 0.5) : mix(T_MID, T_HOT, (heat - 0.5) / 0.5);
          alpha = 0.06 + 0.90 * heat;               // cool≈transparent, hot≈opaque
        } else if (layer === 'pollution') {
          if (cls === 0) continue;
          const ax = 0.62, ay = 0.72;
          const d = Math.sqrt((c.nx - ax) * (c.nx - ax) + (c.ny - ay) * (c.ny - ay));
          const era = Math.min(1, Math.max(0, (t + 0.4) / 1.4));
          let conc = Math.max(0, 1 - d / (0.30 + 0.45 * era)) * (0.35 + 0.65 * era);
          if (cls === 6 || cls === 5) conc = Math.min(1, conc + 0.25);
          conc = Math.min(1, conc);
          if (conc < 0.06) continue;
          col = polColor(conc); alpha = 0.15 + 0.6 * conc;
        } else if (layer === 'life') {
          if (cls === 2 || cls === 4) { col = hexToRgb('#54d07a'); alpha = 0.7; }
          else if (cls === 1) { col = hexToRgb('#39c0e0'); alpha = 0.5; }
          else continue;                            // only highlight living cells
        }

        ctx.fillStyle = `rgba(${col[0] | 0},${col[1] | 0},${col[2] | 0},${alpha})`;
        ctx.fillRect(c.i * cw, c.j * ch, cw + 1, ch + 1);
      }
      return canvas.toDataURL('image/png');
    }

    _labels(ctx, w, h, layer) {
      // airport tag, north of runways
      ctx.save();
      ctx.fillStyle = 'rgba(255,255,255,.55)';
      ctx.font = `${Math.max(10, w * 0.018)}px "Space Mono",monospace`;
      ctx.fillText('BCN · LEBL', w * 0.46, h * 0.50);
      ctx.fillStyle = 'rgba(255,255,255,.32)';
      ctx.fillText('Llobregat', w * 0.80, h * 0.42);
      ctx.restore();
    }
  }

  global.DeltaMap = DeltaMap;
  global.deltaYearToT = yearToT;
  global.DELTA_COL = COL;
})(window);
