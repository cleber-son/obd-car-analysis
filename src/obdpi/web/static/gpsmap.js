/* window.obdMap — mapa do GPS sobre o Leaflet 1.9.4 (copiado em /static, sem CDN e sem as imagens padrão: só
   circleMarker e divIcon). Ladrilhos do OpenStreetMap carregados pelo navegador; sem internet o traçado aparece sobre
   fundo liso. docs/GPS-ARQUITETURA.md, seções 8.1 e 9.4. */
(() => {
  "use strict";
  const TILE = "https://tile.openstreetmap.org/{z}/{x}/{y}.png";
  const SCALE5 = ["#2c7bff", "#21c7d9", "#36d67a", "#ffb020", "#ff4d5e"];          // baixo → alto (quantis do trajeto)
  const GEAR = { 0: "#8592a6", 1: "#ff4fa3", 2: "#ff6b3d", 3: "#ffb020", 4: "#36d67a", 5: "#2ec8ff" };
  const OK = "#36d67a", WARN = "#ffb020", BAD = "#ff4d5e", COLD = "#2c7bff", GAP = "#8592a6";
  // sinais com faixa conhecida: cor fixa (o resto usa os quantis do próprio trajeto)
  const BANDS = {
    stft1: (v) => Math.abs(v) <= 10 ? OK : Math.abs(v) <= 25 ? WARN : BAD,
    lam: (v) => v >= 0.97 && v <= 1.03 ? OK : v >= 0.93 && v <= 1.07 ? WARN : BAD,
    ect: (v) => v < 70 ? COLD : v <= 100 ? OK : v <= 105 ? WARN : BAD,
    gear: (v) => GEAR[v] || GEAR[0],
  };
  const BAND_LEGEND = {
    stft1: [[OK, "±10 %"], [WARN, "até ±25 %"], [BAD, "além"]],
    lam: [[OK, "0,97–1,03"], [WARN, "0,93–1,07"], [BAD, "fora"]],
    ect: [[COLD, "< 70 °C"], [OK, "70–100"], [WARN, "100–105"], [BAD, "> 105"]],
    gear: [1, 2, 3, 4, 5].map((g) => [GEAR[g], g + "ª"]).concat([[GEAR[0], "N"]]),
  };

  const median = (xs) => { const s = xs.slice().sort((a, b) => a - b); const n = s.length; return n ? (n % 2 ? s[(n - 1) / 2] : (s[n / 2 - 1] + s[n / 2]) / 2) : null; };
  function quantiles(vals) {
    const s = vals.filter((v) => v != null && Number.isFinite(v)).sort((a, b) => a - b);
    if (!s.length) return null;
    const q = (p) => s[Math.min(s.length - 1, Math.floor(p * s.length))];
    return { cuts: [q(0.2), q(0.4), q(0.6), q(0.8)], min: s[0], max: s[s.length - 1], med: median(s) };
  }
  function colorFn(colorBy, vals) {
    if (BANDS[colorBy]) return { fn: BANDS[colorBy], legend: BAND_LEGEND[colorBy], stats: quantiles(vals) };
    const qs = quantiles(vals);
    if (!qs) return { fn: () => GAP, legend: [], stats: null };
    return { fn: (v) => { let i = 0; while (i < 4 && v > qs.cuts[i]) i++; return SCALE5[i]; }, legend: null, stats: qs };
  }

  function create(el, opts = {}) {
    if (!window.L) { el.innerHTML = `<div class="empty">Mapa indisponível (leaflet.js não carregou).</div>`; return null; }
    const map = L.map(el, { preferCanvas: true, zoomControl: true, attributionControl: true, ...opts });
    map.attributionControl.setPrefix(false);
    const tiles = L.tileLayer(TILE, { maxZoom: 19, attribution: "© OpenStreetMap" });
    let errs = 0;
    const note = L.control({ position: "bottomleft" });
    note.onAdd = () => { const d = L.DomUtil.create("div", "gm-note"); d.textContent = "sem internet: só o traçado"; d.hidden = true; return d; };
    note.addTo(map);
    tiles.on("tileerror", () => { if (++errs >= 2) { const d = note.getContainer(); if (d) d.hidden = false; el.classList.add("gm-notiles"); } });
    tiles.on("tileload", () => { errs = 0; const d = note.getContainer(); if (d) d.hidden = true; el.classList.remove("gm-notiles"); });
    tiles.addTo(map);
    L.control.scale({ imperial: false, position: "bottomright" }).addTo(map);
    map.setView(opts.center || [-15.78, -47.93], opts.zoom || 4);
    // o mapa pode nascer dentro de um bloco oculto ou que muda de tamanho: corrige o tamanho quando aparece
    let ro = null;
    try { ro = new ResizeObserver(() => { if (!map._gone) map.invalidateSize(); }); ro.observe(el); } catch { }
    map.on("unload", () => { map._gone = true; try { ro && ro.disconnect(); } catch { } });
    return map;
  }

  // Polilinhas por trecho com a cor do valor; trechos dentro de um buraco do GPS (túnel) tracejados.
  // Devolve {layer, legend, stats, colors} (colors[i] = cor do trecho i→i+1) para o replay destacar o percorrido.
  function track(map, trk, colorBy = "speed", layer = null) {
    if (layer) layer.clearLayers(); else layer = L.layerGroup().addTo(map);
    if (!trk || !trk.lat || trk.lat.length < 2) return { layer, legend: [], stats: null, colors: [] };
    const vals = colorBy === "alt" ? trk.alt : (trk.cols?.[colorBy] || []);
    const c = colorFn(colorBy, vals);
    const gaps = trk.gaps || [];
    const inGap = (t0, t1) => gaps.some(([a, b]) => t0 >= a - 0.01 && t1 <= b + 0.01);
    const colors = [];
    let run = null;
    const flush = () => { if (run && run.pts.length > 1) L.polyline(run.pts, { color: run.color, weight: run.gap ? 3 : 5, opacity: run.gap ? 0.7 : 0.95, dashArray: run.gap ? "6 8" : null, interactive: false }).addTo(layer); };
    for (let i = 0; i < trk.lat.length - 1; i++) {
      const gap = inGap(trk.t[i], trk.t[i + 1]);
      const v = vals[i] ?? vals[i + 1];
      const color = gap ? GAP : v == null ? GAP : c.fn(v);
      colors.push(color);
      const a = [trk.lat[i], trk.lon[i]], b = [trk.lat[i + 1], trk.lon[i + 1]];
      if (run && run.color === color && run.gap === gap) run.pts.push(b);
      else { flush(); run = { color, gap, pts: [a, b] }; }
    }
    flush();
    // faixa larga e transparente por cima: o toque no traçado é fácil mesmo no celular
    const hit = L.polyline(trk.lat.map((la, i) => [la, trk.lon[i]]), { color: "#000", opacity: 0, weight: 22 }).addTo(layer);
    layer._hit = hit;
    return { layer, legend: c.legend, stats: c.stats, colors };
  }

  // destaque do pedaço já percorrido (replay)
  function progress(map, trk, idx, colors, layer = null) {
    if (layer) layer.clearLayers(); else layer = L.layerGroup().addTo(map);
    if (!trk || idx < 1) return layer;
    let run = null;
    const flush = () => { if (run && run.pts.length > 1) L.polyline(run.pts, { color: run.color, weight: 9, opacity: 1, interactive: false }).addTo(layer); };
    for (let i = 0; i < Math.min(idx, trk.lat.length - 1); i++) {
      const color = colors[i] || GAP, a = [trk.lat[i], trk.lon[i]], b = [trk.lat[i + 1], trk.lon[i + 1]];
      if (run && run.color === color) run.pts.push(b); else { flush(); run = { color, pts: [a, b] }; }
    }
    flush();
    return layer;
  }

  // items: [{lat, lon, color, r, html, label, onclick}] → L.circleMarker (ou divIcon com 'label')
  function pins(map, items, layer = null) {
    if (layer) layer.clearLayers(); else layer = L.layerGroup().addTo(map);
    for (const it of items || []) {
      if (it.lat == null || it.lon == null) continue;
      const m = it.label
        ? L.marker([it.lat, it.lon], { icon: L.divIcon({ className: "gm-pin", html: it.label, iconSize: [28, 28], iconAnchor: [14, 14] }), keyboard: false })
        : L.circleMarker([it.lat, it.lon], { radius: it.r || 7, color: it.ring || "#fff", weight: it.ring ? 3 : 2, fillColor: it.color || BAD, fillOpacity: 0.95 });
      if (it.html) m.bindPopup(it.html, { maxWidth: 260 });
      if (it.onclick) m.on("click", () => it.onclick(it));
      m.addTo(layer);
    }
    return layer;
  }

  // marcador do replay e da posição ao vivo: seta com o rumo (sem rumo: ponto)
  function cursor(map, lat, lon, hdg, prev = null) {
    const html = hdg == null ? `<i class="gm-dot"></i>` : `<i class="gm-arrow" style="transform:rotate(${Math.round(hdg)}deg)"></i>`;
    const icon = L.divIcon({ className: "gm-cur", html, iconSize: [26, 26], iconAnchor: [13, 13] });
    if (lat == null || lon == null) { if (prev) prev.remove(); return null; }
    if (prev) { prev.setLatLng([lat, lon]); prev.setIcon(icon); return prev; }
    return L.marker([lat, lon], { icon, interactive: false, keyboard: false, zIndexOffset: 1000 }).addTo(map);
  }

  // toque no mapa devolve lat/lon (uma vez); devolve a função que cancela
  function pick(map, cb) {
    const el = map.getContainer();
    el.classList.add("gm-picking");
    const h = (e) => { done(); cb(e.latlng.lat, e.latlng.lng); };
    const done = () => { map.off("click", h); el.classList.remove("gm-picking"); };
    map.on("click", h);
    return done;
  }

  // índice do ponto do trajeto mais próximo (toque no traçado → instante)
  function nearest(trk, lat, lon) {
    if (!trk || !trk.lat) return -1;
    const k = Math.cos(lat * Math.PI / 180);
    let best = -1, bd = Infinity;
    for (let i = 0; i < trk.lat.length; i++) {
      const dy = trk.lat[i] - lat, dx = (trk.lon[i] - lon) * k, d = dx * dx + dy * dy;
      if (d < bd) { bd = d; best = i; }
    }
    return best;
  }

  // posição interpolada no instante ts (ms) e rumo do trecho; null fora do trajeto
  function at(trk, ts) {
    if (!trk || !trk.t || !trk.t.length) return null;
    const s = (ts - trk.t0) / 1000, t = trk.t;
    if (s < t[0] - 5 || s > t[t.length - 1] + 5) return null;
    let lo = 0, hi = t.length - 1;
    while (lo < hi) { const m = (lo + hi + 1) >> 1; if (t[m] <= s) lo = m; else hi = m - 1; }
    const j = Math.min(lo + 1, t.length - 1), f = t[j] > t[lo] ? Math.max(0, Math.min(1, (s - t[lo]) / (t[j] - t[lo]))) : 0;
    const lat = trk.lat[lo] + (trk.lat[j] - trk.lat[lo]) * f, lon = trk.lon[lo] + (trk.lon[j] - trk.lon[lo]) * f;
    let hdg = null;
    if (j > lo && (trk.cols?.speed?.[lo] ?? 0) >= 3) {
      const y = Math.sin((trk.lon[j] - trk.lon[lo]) * Math.PI / 180) * Math.cos(trk.lat[j] * Math.PI / 180);
      const x = Math.cos(trk.lat[lo] * Math.PI / 180) * Math.sin(trk.lat[j] * Math.PI / 180) - Math.sin(trk.lat[lo] * Math.PI / 180) * Math.cos(trk.lat[j] * Math.PI / 180) * Math.cos((trk.lon[j] - trk.lon[lo]) * Math.PI / 180);
      hdg = (Math.atan2(y, x) * 180 / Math.PI + 360) % 360;
    }
    return { lat, lon, hdg, idx: lo };
  }

  // cor de um valor numa escala min–max (mapa de calor agregado)
  function heatColor(v, min, max, signal) {
    if (BANDS[signal]) return BANDS[signal](v);
    if (min == null || max == null || max <= min) return SCALE5[2];
    const i = Math.max(0, Math.min(4, Math.floor((v - min) / (max - min) * 5)));
    return SCALE5[i];
  }

  function fit(map, bounds, maxZoom = 16) {
    if (map._gone) return;
    try { if (bounds && bounds.isValid && bounds.isValid()) map.fitBounds(bounds, { padding: [24, 24], maxZoom }); } catch { }
  }

  window.obdMap = { create, track, progress, pins, cursor, pick, nearest, at, heatColor, fit, colorFn, quantiles, SCALE5, BANDS, BAND_LEGEND };
})();
