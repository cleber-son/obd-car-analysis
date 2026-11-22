/* window.obdGps — aba Mapa (Diário, Mapa, Lugares, Rotas, Trechos, Desempenho, Cidade × estrada, Trânsito), trajeto no detalhe da viagem e replay sincronizado.
   Usa os utilitários do app.js (window.OBD) e o window.obdMap (gpsmap.js). docs/GPS-ARQUITETURA.md, seções 8.1, 9.4,
   9.15, 9.22, 9.24, 9.3 e 9.5. Cada fase acrescenta o seu bloco (// ---- F4: …). Sem GPS: "GPS não conectado" e telas vazias. */
(() => {
  "use strict";
  const $ = (s, r = document) => r.querySelector(s);
  const U = () => window.OBD || {};
  const esc = (s) => (U().esc || String)(s);
  const nf = (v, d = 0) => U().nf ? U().nf(v, d) : String(v ?? "—");
  const dt = (ts) => U().dt ? U().dt(ts) : String(ts);
  const tm = (ts) => U().tm ? U().tm(ts) : String(ts);
  const dur = (s) => U().dur ? U().dur(s) : String(s);
  const M = () => window.obdMap;
  const cloud = () => (U().cloud ? U().cloud() : null);
  const SEV = { crit: ["#ff4d5e", "Crítico"], warn: ["#ffb020", "Atenção"], info: ["#2ec8ff", "Info"] };
  const NO_GPS = "GPS não conectado. Quando o receptor for ligado na USB do Pi, o mapa começa a ser preenchido sozinho.";
  const COLOR_BY = [["speed", "Velocidade", "km/h", 0], ["rpm", "Rotação", "rpm", 0], ["gear", "Marcha", "", 0], ["load", "Carga", "%", 0],
    ["stft1", "Ajuste curto", "%", 1], ["lam", "Sonda λ", "", 3], ["kml", "Consumo", "km/L", 1], ["ect", "Temperatura", "°C", 0],
    ["timing", "Avanço", "°", 1], ["map", "MAP", "kPa", 0], ["tps", "Borboleta", "%", 1], ["slope", "Inclinação", "%", 1], ["limit", "Limite", "km/h", 0],
    ["alt", "Altitude", "m", 0]];
  const HEAT = [["stft1", "Ajuste curto"], ["load", "Carga"], ["kml", "Consumo (km/L)"], ["speed", "Velocidade"], ["ect", "Temperatura"], ["map", "MAP"]];
  const KIND = { casa: "🏠 Casa", trabalho: "🏢 Trabalho", oficina: "🔧 Oficina", posto: "⛽ Posto", outro: "📍 Outro", auto: "❔ Frequente" };

  // ------------------------------------------------------------ dados
  class Locked extends Error { constructor(info) { super("Localização protegida"); this.info = info || {}; } }
  async function get(p) {
    const r = await fetch(p, { cache: "no-store" });
    if (r.status === 401) { const j = await r.json().catch(() => ({})); if (j.locked) { const c = cloud(); if (c) c.loc = j.configured ? "trancada" : "desativada"; throw new Locked(j); } }
    if (!r.ok) { const e = new Error((await r.json().catch(() => ({}))).error || r.status); e.status = r.status; throw e; }
    return r.json();
  }
  async function post(p, body) {
    const r = await fetch(p, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
    const j = await r.json().catch(() => ({}));
    if (r.status === 401 && j.locked) throw new Locked(j);
    if (!r.ok) throw new Error(j.error || j.detail || r.status);
    return j;
  }
  const locOpen = () => { const c = cloud(); return !c || c.loc === "aberta"; };
  let lbCache = null;
  async function logbook(force = false) {
    if (!force && lbCache && Date.now() - lbCache.at < 60e3) return lbCache.data;
    const data = await get("/api/gps/logbook?limit=1000");
    lbCache = { at: Date.now(), data };
    return data;
  }
  const trkCache = new Map();
  async function trackOf(id) {
    if (trkCache.has(id)) return trkCache.get(id);
    const t = await get(`/api/gps/trips/${id}/track`);
    if (trkCache.size > 40) trkCache.delete(trkCache.keys().next().value);
    trkCache.set(id, t);
    return t;
  }

  // ------------------------------------------------------------ painel trancado (nuvem)
  function lockedHtml() {
    const c = cloud() || {};
    if (c.loc === "desativada")
      return `<div class="card"><h2>🔒 Localização desativada no painel online</h2><div class="muted" style="margin-top:8px">Mapa, trajetos, lugares e onde o carro está ficam só no painel do Pi (rede local).
        Para vê-los aqui, o dono define a senha de localização (<code class="k">LOC_PASSWORD</code>) no painel online. Sem ela, nenhuma posição sai do Pi.</div></div>`;
    return `<div class="card"><h2>🔒 Localização protegida</h2><div class="muted" style="margin-top:6px">O painel online é público; posição, trajetos e lugares precisam da senha de localização.</div>
      <form id="locForm" style="display:flex;gap:8px;flex-wrap:wrap;margin-top:12px"><input type="password" id="locPw" placeholder="Senha de localização" autocomplete="current-password" aria-label="Senha de localização" style="flex:1 1 200px" required>
      <button class="btn" type="submit">Desbloquear</button></form><div class="muted" id="locMsg" style="font-size:13px;margin-top:6px" role="status"></div></div>`;
  }
  function bindLock(root, after) {
    const f = $("#locForm", root); if (!f) return;
    f.addEventListener("submit", async (e) => {
      e.preventDefault();
      $("#locMsg", root).textContent = "Conferindo…";
      const r = await fetch("/loc/unlock", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ password: $("#locPw", root).value }) }).catch(() => null);
      if (r && r.ok) { const c = cloud(); if (c) c.loc = "aberta"; lbCache = null; trkCache.clear(); after(); }
      else $("#locMsg", root).textContent = r && r.status === 401 ? "Senha incorreta." : "Não foi possível conferir agora.";
    });
  }

  // ------------------------------------------------------------ aba Mapa
  let sub = "diario", maps = [], liveTimer = null, status = null;
  const EXTRA = {};          // telas das fases seguintes (data-g → função), registradas nos blocos de cada fase
  const TRIP_EXTRA = [];     // acréscimos ao detalhe da viagem ({card, map, det, trk, t}), registrados nos blocos de cada fase
  const killMaps = () => { for (const m of maps) { try { m.remove(); } catch { } } maps = []; clearInterval(liveTimer); liveTimer = null; };
  async function render() {
    const root = $("#v-mapa"); if (!root) return;
    const body = $("#gBody", root), head = $("#gHead", root);
    killMaps();
    root.querySelectorAll("[data-g]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.g === sub)));
    if (!locOpen()) { head.innerHTML = ""; body.innerHTML = lockedHtml(); bindLock(body, render); return; }
    status = await get("/api/gps/status").catch(() => null);
    const st = status?.state;
    head.innerHTML = (!status ? "" : (st === "desconectado" || st === "desligado")
      ? `<div class="banner" style="--s:var(--muted)"><span class="ic">📡</span><div>${st === "desligado" ? "GPS desligado na configuração (gps.enabled / gps.source)." : NO_GPS}</div></div>` : "")
      + (cloud() ? `<div style="text-align:right"><button class="rbtn" type="button" id="locLock">🔒 Trancar a localização neste aparelho</button></div>` : "");
    $("#locLock", head)?.addEventListener("click", async () => {
      await fetch("/loc/lock", { method: "POST" }).catch(() => null);
      const c = cloud(); if (c) c.loc = "trancada"; lbCache = null; trkCache.clear(); render();
    });
    try {
      if (EXTRA[sub]) await EXTRA[sub](body);
      else if (sub === "mapa") await viewMap(body);
      else if (sub === "lugares") await viewPlaces(body);
      else await viewLog(body);
    } catch (e) {
      if (e instanceof Locked) { body.innerHTML = lockedHtml(); bindLock(body, render); return; }
      body.innerHTML = `<div class="empty">${e.status === 404 ? (cloud() ? "Ainda não sincronizado pelo Pi." : NO_GPS) : "Não foi possível carregar: " + esc(e.message)}</div>`;
    }
  }

  // ---- Diário
  async function viewLog(body) {
    const lb = await logbook(true);
    const items = lb.items || [];
    if (!items.length) { body.innerHTML = `<div class="empty">Nenhuma viagem ainda.</div>`; return; }
    const withTrk = items.filter((x) => x.has_track).length;
    let shown = 50;
    const row = (x) => {
      const r = x.route || (x.has_track ? "Trajeto sem nome" : "Sem trajeto (antes do GPS)");
      return `<tr class="click" data-id="${x.id}"><td class="head"><b>${esc(r)}</b><br>${dt(x.start_ts)}${x.clock_synced === 0 ? ' <span class="pill" title="Hora aproximada (relógio do Pi sem sincronizar)">≈</span>' : ""}${x.note ? ` <span title="${esc(x.note)}">📝</span>` : ""}</td>
        <td class="num" data-l="Km">${nf(x.distance_km, 1)}<span class="only-m"> km</span></td><td class="num" data-l="Duração">${dur(x.duration_s)}</td>
        <td class="num" data-l="Média">${nf(x.avg_moving ?? x.avg_speed)} km/h</td><td class="num" data-l="Consumo">${nf(x.km_l, 1)}<span class="only-m"> km/L</span>${x.drive_score != null ? `<br><span class="pill" title="Nota de condução (freadas, arrancadas e curvas fortes, excesso de velocidade)">nota ${x.drive_score}</span>` : ""}</td>
        <td class="num" data-l="Custo">${x.cost != null ? "R$ " + nf(x.cost, 2) : "—"}</td><td class="num" data-l="Paradas">${x.n_stops ?? "—"}</td><td class="num" data-l="Alertas">${x.n_alerts ?? 0}</td></tr>`;
    };
    const paint = () => {
      body.innerHTML = `<div class="card"><div class="row-head"><div><h2>📒 Diário de bordo</h2><div class="lbl" style="margin-top:4px">${lb.total} viagens · ${withTrk} com trajeto</div></div>
        ${cloud() ? "" : `<a class="btn" href="/api/gps/export.gpx" download>⬇ GPX (30 dias)</a>`}</div>
        <div class="tbl" style="margin-top:10px"><table class="cards"><thead><tr><th>Viagem</th><th>Km</th><th>Duração</th><th>Média</th><th>km/L</th><th>Custo</th><th>Paradas</th><th>Alertas</th></tr></thead>
        <tbody id="gLog">${items.slice(0, shown).map(row).join("")}</tbody></table></div>
        ${items.length > shown ? `<button class="btn" type="button" id="gMore" style="margin-top:10px">Mostrar mais</button>` : ""}</div>`;
      $("#gLog").addEventListener("click", (e) => { const tr = e.target.closest("tr[data-id]"); if (tr) openTrip(+tr.dataset.id); });
      $("#gMore")?.addEventListener("click", () => { shown += 100; paint(); });
    };
    paint();
  }

  // abre o detalhe da viagem na aba Viagens (e, se pedido, o replay 8 s antes de um instante)
  let pendingReplay = null;
  function openTrip(id, ts = null) {
    pendingReplay = ts ? { id, ts } : null;
    const click = () => { const tr = document.querySelector(`#tripRows tr[data-id="${id}"]`); if (tr) tr.click(); };
    if (location.hash === "#viagens") { click(); return; }
    window.addEventListener("obdtrips", click, { once: true });
    location.hash = "#viagens";
  }

  // ---- Mapa: trajetos recentes, calor por sinal, alertas (linha do tempo), lugares, estacionado, posição atual
  async function viewMap(body) {
    body.innerHTML = `<div class="card"><div class="row-head"><h2>🗺️ Mapa</h2>
        <div class="gctl"><label class="lbl" for="gLayer">Camada</label><select id="gLayer"><option value="trk">Trajetos recentes</option>${HEAT.map(([k, l]) => `<option value="heat:${k}">Calor: ${l}</option>`).join("")}</select></div></div>
      <div class="gchecks"><label><input type="checkbox" id="gAl" checked> Alertas</label><label><input type="checkbox" id="gPl" checked> Lugares</label><label><input type="checkbox" id="gPk" checked> Estacionado</label><label><input type="checkbox" id="gLv" checked> Agora</label>${!cloud() && status?.geo ? `<label title="Radares do mapa offline (OpenStreetMap): auxílio, pode estar desatualizado"><input type="checkbox" id="gCam"> 📷 Radares</label>` : ""}</div>
      <div class="gmap" id="gMap" style="margin-top:10px"></div><div class="gm-legend" id="gLeg"></div>
      <div id="gAlBox" style="margin-top:12px"></div></div>`;
    const map = M().create($("#gMap"));
    if (!map) return;
    maps.push(map);
    const L_ = window.L, layers = { base: L_.layerGroup().addTo(map), al: L_.layerGroup().addTo(map), pl: L_.layerGroup().addTo(map), pk: L_.layerGroup().addTo(map), cam: L_.layerGroup().addTo(map) };
    let cur = null, fitted = false;
    const fitTo = (b) => { if (!fitted && b && b.isValid()) { fitted = true; M().fit(map, b); } };
    const [lb, al, pl, pk] = await Promise.all([logbook().catch((e) => { if (e instanceof Locked) throw e; return { items: [] }; }),
      get("/api/gps/alerts?days=90").catch(() => null), get("/api/gps/places").catch(() => null), get("/api/gps/parked").catch(() => null)]);
    // trajetos recentes (até 30), cada um numa cor discreta; toque abre a viagem
    async function drawTracks() {
      layers.base.clearLayers();
      const ids = (lb.items || []).filter((x) => x.has_track).slice(0, 30);
      if (!ids.length) { $("#gLeg").innerHTML = `<span class="muted">Nenhum trajeto ainda.</span>`; return; }
      const bounds = L_.latLngBounds([]);
      $("#gLeg").innerHTML = `<span class="muted">${ids.length} viagem(ns) mais recentes · toque num trajeto para abrir</span>`;
      let k = 0;
      for (let i = 0; i < ids.length; i += 4) {
        const part = await Promise.all(ids.slice(i, i + 4).map((x) => trackOf(x.id).then((t) => [x, t]).catch(() => [x, null])));
        if (map._gone) return;
        for (const [x, t] of part) {
          if (!t || !t.lat || !t.lat.length || $("#gLayer")?.value !== "trk") continue;
          const pts = t.lat.map((la, j) => [la, t.lon[j]]);
          const line = L_.polyline(pts, { color: k++ === 0 ? "#ff6b3d" : "#2ec8ff", weight: 4, opacity: 0.75 }).addTo(layers.base);   // a mais recente em laranja
          line.bindPopup(`<b>${esc(x.route || "Viagem")}</b><br>${dt(x.start_ts)} · ${nf(x.distance_km, 1)} km<br><a href="#viagens" data-open="${x.id}">Abrir a viagem</a>`);
          bounds.extend(line.getBounds());
        }
      }
      fitTo(bounds);
    }
    async function drawHeat(signal) {
      layers.base.clearLayers();
      const h = await get(`/api/gps/heat?signal=${signal}&days=30`).catch((e) => { if (e instanceof Locked) throw e; return null; });
      if (map._gone) return;
      if (!h || !h.cells.length) { $("#gLeg").innerHTML = `<span class="muted">Sem dados deste sinal nos últimos 30 dias.</span>`; return; }
      const half = h.cell_deg / 2, bounds = L_.latLngBounds([]);
      for (const [la, lo, v, n] of h.cells) {
        const r = L_.rectangle([[la - half, lo - half], [la + half, lo + half]], { stroke: false, fillColor: M().heatColor(v, h.min, h.max, signal), fillOpacity: 0.65 });
        r.bindPopup(`${esc(HEAT.find((x) => x[0] === signal)?.[1] || signal)}: <b>${nf(v, signal === "kml" || signal === "stft1" ? 1 : 0)}</b> · ${n} ponto(s)`);
        r.addTo(layers.base); bounds.extend(r.getBounds());
      }
      const lg = M().BAND_LEGEND[signal] || M().SCALE5.map((c, i) => [c, i === 0 ? nf(h.min, 1) : i === 4 ? nf(h.max, 1) : ""]);
      $("#gLeg").innerHTML = lg.map(([c, l]) => `<span><i style="background:${c}"></i>${esc(l)}</span>`).join("") + `<span class="muted">${h.trips} viagem(ns) · células de ~${Math.round(h.cell_deg * 111000)} m</span>`;
      fitTo(bounds);
    }
    // alertas: alfinetes por severidade, filtro por regra, linha do tempo e pontos repetidos
    function drawAlerts() {
      layers.al.clearLayers();
      const box = $("#gAlBox");
      if (!al || !$("#gAl").checked) { box.innerHTML = ""; return; }
      const withPos = al.items.filter((x) => x.lat != null);
      if (!withPos.length) { box.innerHTML = `<div class="muted" style="font-size:13px">Nenhum alerta com posição nos últimos 90 dias.</div>`; return; }
      const rule = $("#gRule")?.value || "";
      const list = withPos.filter((x) => !rule || x.rule === rule).sort((a, b) => a.ts - b.ts);
      const t0 = list.length ? list[0].ts : 0, t1 = list.length ? list[list.length - 1].ts : 0;
      if (!$("#gRule")) {
        box.innerHTML = `<div class="row-head"><h3 class="lbl">Alertas no mapa (90 dias) · ${withPos.length} com posição</h3>
          <select id="gRule" aria-label="Filtrar por regra"><option value="">Todas as regras</option>${al.rules.filter((r) => r.with_pos).map((r) => `<option value="${esc(r.rule)}">${esc(r.title)} (${r.with_pos})</option>`).join("")}</select></div>
          <div class="gtime"><button class="rbtn" type="button" id="gPlay">▶</button><input type="range" id="gTime" aria-label="Linha do tempo dos alertas" style="flex:1"><span class="muted num" id="gTimeL" style="font-size:12px"></span></div>
          ${al.hotspots.length ? `<div class="list" style="margin-top:8px">${al.hotspots.slice(0, 5).map((h) => `<div class="alert" style="--s:var(--warn)"><span class="ic">📍</span><div><div class="t">${esc(h.title)}: ${h.n} vezes no mesmo lugar</div><div class="d">Este alerta acontece sempre neste ponto (subida, semáforo, trecho).</div></div><button class="rbtn" type="button" data-hot="${h.lat},${h.lon}">ver</button></div>`).join("")}</div>` : ""}`;
        $("#gRule").addEventListener("change", () => { $("#gTime").value = $("#gTime").max; drawAlerts(); });
        $("#gTime").addEventListener("input", drawAlerts);
        box.addEventListener("click", (e) => { const b = e.target.closest("[data-hot]"); if (b) { const [la, lo] = b.dataset.hot.split(",").map(Number); map.setView([la, lo], 17); } });
        let play = null;
        $("#gPlay").addEventListener("click", () => {
          const s = $("#gTime"); if (play) { clearInterval(play); play = null; $("#gPlay").textContent = "▶"; return; }
          if (+s.value >= +s.max) s.value = s.min;
          $("#gPlay").textContent = "⏸";
          play = setInterval(() => { if (!document.body.contains(s)) return clearInterval(play); s.value = Math.min(+s.max, +s.value + Math.max(1, (+s.max - +s.min) / 60)); drawAlerts(); if (+s.value >= +s.max) { clearInterval(play); play = null; $("#gPlay").textContent = "▶"; } }, 150);
        });
      }
      const s = $("#gTime");
      if (+s.min !== t0 || +s.max !== t1) { const atEnd = !s.value || +s.value >= +s.max; s.min = t0; s.max = t1 || t0 + 1; s.step = 1; if (atEnd) s.value = s.max; }
      const upTo = +s.value;
      $("#gTimeL").textContent = list.length ? `até ${dt(upTo)}` : "";
      M().pins(map, list.filter((x) => x.ts <= upTo).map((x) => ({
        lat: x.lat, lon: x.lon, color: (SEV[x.severity] || SEV.info)[0], r: x.repeat ? 9 : 7, ring: x.repeat ? "#ffb020" : null,
        html: `<b>${esc(x.title)}</b><br>${dt(x.ts)}${x.speed != null ? ` · ${nf(x.speed)} km/h` : ""}${x.alt != null ? ` · ${nf(x.alt)} m` : ""}${x.repeat ? `<br>📍 ${x.repeat} vezes neste ponto` : ""}${x.trip_id ? `<br><a href="#viagens" data-open="${x.trip_id}" data-ts="${x.ts}">Replay 8 s antes</a>` : ""}`,
      })), layers.al);
    }
    function drawPlaces() {
      layers.pl.clearLayers();
      if (!pl || !$("#gPl").checked) return;
      for (const p of pl.places || []) {
        L_.circle([p.lat, p.lon], { radius: p.radius_m, color: "#8b7bff", weight: 2, fillOpacity: 0.08 }).bindPopup(`<b>${esc(p.name)}</b><br>${esc(KIND[p.kind] || p.kind)} · raio ${nf(p.radius_m)} m · ${p.visits || 0} visita(s)`).addTo(layers.pl);
      }
    }
    function drawParked() {
      layers.pk.clearLayers();
      if (!pk || !pk.known || !$("#gPk").checked) return;
      M().pins(map, [{ lat: pk.lat, lon: pk.lon, label: "🅿️", html: `<b>Estacionado</b>${pk.name ? "<br>" + esc(pk.name) : ""}<br>${dt(pk.ts)}${pk.maps_url ? `<br><a href="${esc(pk.maps_url)}" target="_blank" rel="noopener">Abrir no Mapas</a>` : ""}` }], layers.pk);
    }
    // F7: radares do geo.db na área vista (só no painel do Pi; o geo.db não vai para a nuvem)
    let camSeq = 0;
    async function drawCams() {
      layers.cam.clearLayers();
      if (!$("#gCam")?.checked) return;
      if (map.getZoom() < 11) { $("#gLeg").insertAdjacentHTML("beforeend", `<span class="muted" id="gCamZ">📷 aproxime o mapa para ver os radares</span>`); return; }
      $("#gCamZ")?.remove();
      const b = map.getBounds(), seq = ++camSeq;
      const j = await get(`/api/gps/cameras?bbox=${[b.getSouth(), b.getWest(), b.getNorth(), b.getEast()].map((x) => x.toFixed(4)).join(",")}`).catch(() => null);
      if (map._gone || seq !== camSeq || !j) return;
      const when = j.osm_ts ? new Date(+j.osm_ts).toLocaleDateString("pt-BR", { month: "2-digit", year: "numeric" }) : "";
      M().pins(map, j.items.map((c) => ({ lat: c.lat, lon: c.lon, label: "📷",
        html: `<b>Radar</b>${c.limit ? ` · limite ${nf(c.limit)} km/h` : ""}${c.dir != null ? `<br>sentido ${nf(c.dir)}°` : ""}<br><span class="muted">OpenStreetMap${when ? " (" + when + ")" : ""}: auxílio, pode estar desatualizado. Vale a placa.</span>` })), layers.cam);
    }
    map.on("moveend", () => { if ($("#gCam")?.checked) drawCams().catch(console.error); });
    async function liveTick() {
      if (map._gone || !document.body.contains(map.getContainer()) || location.hash !== "#mapa") { clearInterval(liveTimer); liveTimer = null; return; }
      if (!$("#gLv").checked) { cur = M().cursor(map, null, null, null, cur); return; }
      const g = await get("/api/gps/live").catch(() => null);
      if (map._gone) return;
      const fresh = g && g.lat != null && g.state === "ok" && (g.age_s == null || g.age_s < 120);
      cur = M().cursor(map, fresh ? g.lat : null, fresh ? g.lon : null, fresh ? g.heading : null, cur);
      if (fresh) fitTo(L_.latLngBounds([[g.lat, g.lon]]));
    }
    map.getContainer().addEventListener("click", (e) => { const a = e.target.closest("a[data-open]"); if (a) { e.preventDefault(); openTrip(+a.dataset.open, a.dataset.ts ? +a.dataset.ts : null); } });
    const redraw = async () => { const v = $("#gLayer").value; if (v.startsWith("heat:")) await drawHeat(v.slice(5)); else await drawTracks(); };
    $("#gLayer").addEventListener("change", () => redraw().catch(console.error));
    $("#gAl").addEventListener("change", drawAlerts); $("#gPl").addEventListener("change", drawPlaces); $("#gPk").addEventListener("change", drawParked);
    $("#gLv").addEventListener("change", liveTick);
    $("#gCam")?.addEventListener("change", () => { $("#gCamZ")?.remove(); drawCams().catch(console.error); });
    if (map._gone) return;
    drawPlaces(); drawParked(); drawAlerts();
    if (pk && pk.known) fitTo(L_.latLngBounds([[pk.lat, pk.lon]]).pad(0.5));
    await redraw();
    if (map._gone) return;
    if (!fitted) { const b = L_.latLngBounds([]); for (const p of pl?.places || []) b.extend([p.lat, p.lon]); fitTo(b); }
    liveTick(); liveTimer = setInterval(liveTick, 5000);
  }

  // ---- Lugares: lista, criar (toque no mapa ou posição atual), editar, apagar
  async function viewPlaces(body) {
    const pl = await get("/api/gps/places");
    const kinds = (pl.kinds || Object.keys(KIND)).filter((k) => k !== "auto");
    body.innerHTML = `<div class="card"><h2>📍 Lugares</h2><div class="lbl" style="margin-top:4px">Casa, trabalho, oficina, postos: o diário usa os nomes e a cerca avisa a entrada e a saída</div>
      <div class="gmap sm" id="gPMap" style="margin-top:10px"></div>
      <form id="gPForm" class="gpform"><input type="hidden" id="gPid">
        <input type="text" id="gPName" placeholder="Nome (ex.: Casa)" aria-label="Nome do lugar" maxlength="60" required>
        <select id="gPKind" aria-label="Tipo">${kinds.map((k) => `<option value="${k}">${esc(KIND[k] || k)}</option>`).join("")}</select>
        <label class="muted" style="display:flex;gap:6px;align-items:center;font-size:13px">Raio <input type="number" id="gPRad" value="80" min="20" max="2000" step="10" style="width:90px" aria-label="Raio em metros"> m</label>
        <span class="muted num" id="gPPos" style="font-size:12px">Sem posição escolhida</span>
        <div id="gPAct"></div>
        <div style="display:flex;gap:8px;flex-wrap:wrap"><button class="btn" type="button" id="gPPick">👆 Tocar no mapa</button><button class="btn" type="button" id="gPHere">📡 Usar a posição atual</button>
          <button class="btn" type="submit" id="gPSave">Salvar</button><button class="btn" type="button" id="gPNew" hidden>Novo</button></div>
        <div class="muted" id="gPMsg" style="font-size:13px" role="status"></div></form>
      <div class="list" id="gPList" style="margin-top:12px"></div></div>`;
    const map = M().create($("#gPMap"));
    if (!map) return;
    maps.push(map);
    const L_ = window.L, lay = L_.layerGroup().addTo(map);
    let pos = null, mark = null;
    const setPos = (la, lo) => { pos = [la, lo]; $("#gPPos").textContent = `${nf(Math.abs(la), 5)}° ${la < 0 ? "S" : "N"}, ${nf(Math.abs(lo), 5)}° ${lo < 0 ? "O" : "L"}`; mark = M().cursor(map, la, lo, null, mark); };
    const list = pl.places || [];
    const b = L_.latLngBounds([]);
    for (const p of list) { L_.circle([p.lat, p.lon], { radius: p.radius_m, color: "#8b7bff", weight: 2, fillOpacity: 0.1 }).bindTooltip(p.name).addTo(lay); b.extend([p.lat, p.lon]); }
    M().fit(map, b);
    if (!list.length) {
      const pk = await get("/api/gps/parked").catch(() => null);
      if (pk && pk.known) map.setView([pk.lat, pk.lon], 15);
    }
    $("#gPList").innerHTML = list.length ? list.map((p) => `<div class="alert" style="--s:${p.kind === "auto" ? "var(--warn)" : "var(--volt)"}"><span class="ic">${esc((KIND[p.kind] || "📍").split(" ")[0])}</span>
        <div><div class="t">${esc(p.name)}${p.kind === "auto" ? ' <span class="pill">dê um nome</span>' : ""}</div><div class="d">raio ${nf(p.radius_m)} m · ${p.visits || 0} visita(s)${p.last_visit_ts ? " · última " + dt(p.last_visit_ts) : ""}${actText(p.actions)}</div></div>
        <span style="display:flex;gap:6px"><button class="rbtn" type="button" data-edit="${p.id}">Editar</button><button class="rbtn" type="button" data-del="${p.id}">Apagar</button></span></div>`).join("")
      : `<div class="empty">Nenhum lugar salvo. Toque no mapa ou use a posição atual para criar "Casa" e "Trabalho".</div>`;
    const reset = () => { $("#gPid").value = ""; $("#gPName").value = ""; $("#gPRad").value = 80; $("#gPNew").hidden = true; pos = null; mark = M().cursor(map, null, null, null, mark); $("#gPPos").textContent = "Sem posição escolhida"; actEdit($("#gPAct"), null); };
    actEdit($("#gPAct"), null);
    $("#gPList").addEventListener("click", async (e) => {
      const ed = e.target.closest("[data-edit]"), del = e.target.closest("[data-del]");
      if (ed) { const p = list.find((x) => x.id === +ed.dataset.edit); $("#gPid").value = p.id; $("#gPName").value = p.kind === "auto" ? "" : p.name; $("#gPKind").value = kinds.includes(p.kind) ? p.kind : "outro"; $("#gPRad").value = p.radius_m; setPos(p.lat, p.lon); map.setView([p.lat, p.lon], 16); $("#gPNew").hidden = false; actEdit($("#gPAct"), p.actions || []); $("#gPName").focus(); }
      if (del && confirm("Apagar este lugar? O diário continua com o nome antigo.")) {
        try { const j = await post(`/api/gps/places/${del.dataset.del}`, { delete: true }); $("#gPMsg").textContent = j.queued ? "Pedido enviado; o Pi apaga na próxima conexão." : "Apagado."; if (!j.queued) render(); }
        catch (err) { $("#gPMsg").textContent = "Erro: " + err.message; }
      }
    });
    let cancel = null;
    $("#gPPick").addEventListener("click", () => { if (cancel) cancel(); $("#gPMsg").textContent = "Toque no mapa no ponto do lugar."; cancel = M().pick(map, (la, lo) => { cancel = null; setPos(la, lo); $("#gPMsg").textContent = ""; }); });
    $("#gPHere").addEventListener("click", async () => {
      const g = await get("/api/gps/live").catch(() => null);
      if (g && g.lat != null) { setPos(g.lat, g.lon); map.setView([g.lat, g.lon], 16); $("#gPMsg").textContent = g.state === "ok" ? "" : "Última posição conhecida (o GPS está sem sinal agora)."; }
      else $("#gPMsg").textContent = "Sem posição atual do GPS. Toque no mapa.";
    });
    $("#gPNew").addEventListener("click", reset);
    $("#gPForm").addEventListener("submit", async (e) => {
      e.preventDefault();
      const id = $("#gPid").value;
      if (!pos) { $("#gPMsg").textContent = "Escolha o ponto: toque no mapa ou use a posição atual."; return; }
      const body = { name: $("#gPName").value.trim(), kind: $("#gPKind").value, radius_m: +$("#gPRad").value || 80, lat: +pos[0].toFixed(6), lon: +pos[1].toFixed(6) };
      const acts = actGet($("#gPAct")); if (acts) body.actions = acts;          // F8: sem mexer num lugar novo, o Pi usa as ações padrão do tipo
      try {
        const j = await post(id ? `/api/gps/places/${id}` : "/api/gps/places", body);
        $("#gPMsg").textContent = j.queued ? "Salvo; o Pi aplica na próxima conexão." : "Salvo.";
        if (!j.queued) render();
      } catch (err) { $("#gPMsg").textContent = err instanceof Locked ? "Localização protegida: desbloqueie primeiro." : "Erro: " + err.message; }
    });
  }

  // ------------------------------------------------------------ detalhe da viagem: trajeto colorido + replay
  let tripState = null;
  async function trip(t) {
    const host = $("#tripDetail"); if (!host || !t) return;
    const card = document.createElement("div");
    card.className = "card"; card.id = "gTrip";
    const rp = $("#rpSlider")?.closest(".card");
    if (rp) host.insertBefore(card, rp); else host.appendChild(card);
    if (tripState?.map) { try { tripState.map.remove(); } catch { } }
    tripState = null;
    if (!locOpen()) { card.innerHTML = `<div class="row-head"><h2>🗺️ Trajeto</h2><a class="btn" href="#mapa">🔒 Desbloquear</a></div><div class="muted" style="margin-top:6px">Localização protegida no painel online.</div>`; return; }
    card.innerHTML = `<h2>🗺️ Trajeto</h2><div class="empty" style="margin-top:10px">Carregando…</div>`;
    let trk = null, det = null;
    try { [trk, det] = await Promise.all([trackOf(t.id), get(`/api/gps/trips/${t.id}`).catch(() => null)]); }
    catch (e) {
      if (!document.body.contains(card)) return;
      card.innerHTML = e instanceof Locked ? `<div class="row-head"><h2>🗺️ Trajeto</h2><a class="btn" href="#mapa">🔒 Desbloquear</a></div>`
        : `<div class="row-head"><h2>🗺️ Trajeto</h2><span class="muted">${e.status === 404 ? "Sem trajeto (antes do GPS)" : "Trajeto indisponível"}</span></div>`;
      return;
    }
    if (!document.body.contains(card)) return;
    const g = det?.gps || {};
    const opts = COLOR_BY.filter(([k]) => k === "alt" ? (trk.alt || []).some((v) => v != null) : (trk.cols?.[k] || []).some((v) => v != null));
    let colorBy = (() => { try { return localStorage.getItem("gpsColorBy") || "speed"; } catch { return "speed"; } })();
    if (!opts.some(([k]) => k === colorBy)) colorBy = "speed";
    card.innerHTML = `<div class="row-head"><div><h2>🗺️ Trajeto</h2><div class="lbl" style="margin-top:4px">${esc(g.route || "")}${g.coverage != null ? ` · GPS em ${nf(g.coverage * 100)} % do tempo andando` : ""}${g.n_stops != null ? ` · ${g.n_stops} parada(s)` : ""}</div></div>
      <div class="gctl"><label class="lbl" for="gColor">Colorir por</label><select id="gColor">${opts.map(([k, l]) => `<option value="${k}"${k === colorBy ? " selected" : ""}>${l}</option>`).join("")}</select>
      ${cloud() ? "" : `<a class="btn" href="/api/gps/trips/${t.id}/export.gpx" download>GPX</a><a class="btn" href="/api/gps/trips/${t.id}/export.kml" download>KML</a>`}</div></div>
      <div class="gmap" id="gTMap" style="margin-top:10px"></div><div class="gm-legend" id="gTLeg"></div>
      <div class="muted" style="font-size:12px;margin-top:6px">Toque no traçado para ver os sinais daquele ponto e levar o replay até ele.${(trk.gaps || []).length ? " Tracejado: trecho sem GPS (túnel, garagem)." : ""}</div>`;
    const map = M().create($("#gTMap", card));
    if (!map) return;
    const L_ = window.L, pinLayer = L_.layerGroup().addTo(map);
    const st = { id: t.id, trk, map, colorBy, layer: null, colors: [], prog: null, cur: null, lastIdx: -1 };
    tripState = st;
    const paint = () => {
      const r = M().track(map, trk, st.colorBy, st.layer);
      st.layer = r.layer; st.colors = r.colors;
      const meta = COLOR_BY.find((x) => x[0] === st.colorBy) || ["", "", "", 1];
      const lg = r.legend || (r.stats ? M().SCALE5.map((c, i) => [c, ["baixo", "", "", "", "alto"][i]]) : []);
      $("#gTLeg", card).innerHTML = lg.map(([c, l]) => `<span><i style="background:${c}"></i>${esc(l)}</span>`).join("")
        + (r.stats ? `<span class="muted num">mín ${nf(r.stats.min, meta[3])} · mediana ${nf(r.stats.med, meta[3])} · máx ${nf(r.stats.max, meta[3])} ${esc(meta[2])}</span>` : "");
      r.layer._hit?.on("click", (e) => onTap(e.latlng));
      st.lastIdx = -1;
      if (st.prog) st.prog.clearLayers();
    };
    const onTap = (ll) => {
      const i = M().nearest(trk, ll.lat, ll.lng); if (i < 0) return;
      const ts = trk.t0 + trk.t[i] * 1000;
      const rows = COLOR_BY.filter(([k]) => k !== "alt" && trk.cols?.[k]?.[i] != null).map(([k, l, u, d]) => `<tr><td>${l}</td><td class="num">${k === "gear" ? (trk.cols[k][i] || "N") : nf(trk.cols[k][i], d)} ${u}</td></tr>`).join("");
      L_.popup({ maxWidth: 240 }).setLatLng([trk.lat[i], trk.lon[i]]).setContent(`<b>${tm(ts)}</b>${trk.alt?.[i] != null ? ` · ${nf(trk.alt[i])} m` : ""}<table class="gm-tbl">${rows}</table>`).openOn(map);
      const sl = $("#rpSlider");
      if (sl) { sl.value = ts; sl.dispatchEvent(new Event("input")); }
    };
    paint();
    // alfinetes: início, fim, paradas e alertas desta viagem
    const pins = [{ lat: trk.lat[0], lon: trk.lon[0], label: "🟢", html: `<b>Saída</b>${g.start_name ? "<br>" + esc(g.start_name) : ""}` },
      { lat: trk.lat[trk.lat.length - 1], lon: trk.lon[trk.lon.length - 1], label: "🏁", html: `<b>Chegada</b>${g.end_name ? "<br>" + esc(g.end_name) : ""}` }];
    for (const s of g.stops || []) pins.push({ lat: s.lat, lon: s.lon, color: "#8592a6", r: 5, html: `Parada de ${dur(s.s)}${s.name ? " · " + esc(s.name) : ""}<br>${tm(s.ts)}` });
    for (const a of det?.alerts || []) pins.push({ lat: a.lat, lon: a.lon, color: (SEV[a.severity] || SEV.info)[0], r: 7,
      html: `<b>${esc(a.title)}</b><br>${tm(a.ts)}${a.speed != null ? ` · ${nf(a.speed)} km/h` : ""}<br><a href="#" data-rp="${a.ts}">Replay 8 s antes</a>` });
    M().pins(map, pins, pinLayer);
    for (const fn of TRIP_EXTRA) { try { fn({ card, map, det, trk, t }); } catch (e) { console.error(e); } }
    map.getContainer().addEventListener("click", (e) => { const a = e.target.closest("a[data-rp]"); if (a) { e.preventDefault(); const sl = $("#rpSlider"); if (sl) { sl.value = Math.max(+sl.min, +a.dataset.rp - 8000); sl.dispatchEvent(new Event("input")); } } });
    const b = trk.bbox; if (b) M().fit(map, L_.latLngBounds([[b[0], b[1]], [b[2], b[3]]]), 17);
    $("#gColor", card).addEventListener("change", (e) => { st.colorBy = e.target.value; try { localStorage.setItem("gpsColorBy", st.colorBy); } catch { } paint(); });
    // veio de "replay 8 s antes" no mapa
    if (pendingReplay && pendingReplay.id === t.id) {
      const sl = $("#rpSlider"), ts = pendingReplay.ts; pendingReplay = null;
      if (sl) setTimeout(() => { sl.value = Math.max(+sl.min, ts - 8000); sl.dispatchEvent(new Event("input")); sl.scrollIntoView({ behavior: "smooth", block: "center" }); }, 50);
    }
    const sl = $("#rpSlider"); if (sl) onReplay({ detail: { ts: +sl.value, trip: t.id } });
  }

  // replay (extras.js) → cursor no mapa e trecho percorrido destacado
  function onReplay(e) {
    const st = tripState, d = e.detail || {};
    if (!st || d.trip !== st.id || st.map._gone || !document.body.contains(st.map.getContainer())) return;
    const p = M().at(st.trk, d.ts);
    st.cur = M().cursor(st.map, p ? p.lat : null, p ? p.lon : null, p ? p.hdg : null, st.cur);
    const idx = p ? p.idx : -1;
    if (idx !== st.lastIdx) { st.lastIdx = idx; st.prog = M().progress(st.map, st.trk, idx, st.colors, st.prog); }
    if (p && !st.map.getBounds().pad(-0.1).contains([p.lat, p.lon])) st.map.panTo([p.lat, p.lon], { animate: false });
  }
  window.addEventListener("obdreplay", onReplay);

  // lista de viagens: "Casa → Trabalho" sob a data de cada linha
  window.addEventListener("obdtrips", async () => {
    if (!locOpen()) return;
    const lb = await logbook().catch(() => null); if (!lb) return;
    const by = new Map((lb.items || []).filter((x) => x.route).map((x) => [String(x.id), x.route]));
    document.querySelectorAll("#tripRows tr[data-id]").forEach((tr) => {
      const r = by.get(tr.dataset.id), td = tr.querySelector("td.head");
      if (r && td && !td.querySelector(".gps-route")) td.insertAdjacentHTML("beforeend", `<div class="gps-route">🗺️ ${esc(r)}</div>`);
    });
  });

  // botões internos da aba (Diário · Mapa · Lugares)
  document.addEventListener("click", (e) => {
    const b = e.target.closest("#v-mapa [data-g]"); if (!b) return;
    sub = b.dataset.g; try { localStorage.setItem("gpsSub", sub); } catch { }
    render().catch(console.error);
  });
  try { sub = localStorage.getItem("gpsSub") || "diario"; } catch { }

  // ---- F4: rotas e trechos de referência (seções 9.3 e 9.5)
  const tabs = $("#v-mapa .gtabs");
  if (tabs && !$('[data-g="rotas"]', tabs)) tabs.insertAdjacentHTML("beforeend", `<button type="button" class="rbtn" data-g="rotas">🛣️ Rotas</button><button type="button" class="rbtn" data-g="trechos">📏 Trechos</button>`);
  if (!["diario", "mapa", "lugares", "rotas", "trechos"].includes(sub)) sub = "diario";
  const money = (v, d = 2) => v == null ? "—" : "R$ " + nf(v, d);
  const FUEL = { gasolina: "Gasolina", etanol: "Etanol", mistura: "Mistura" };
  let routeSel = null, segSel = null;          // detalhe aberto (null = lista; segSel "novo" = criar)
  // trocar de botão interno volta para a lista (fase de captura: antes do render do bloco principal)
  document.addEventListener("click", (e) => { if (e.target.closest("#v-mapa [data-g]")) { routeSel = null; segSel = null; } }, true);
  const back = (id) => `<button class="rbtn" type="button" id="${id}">← Voltar</button>`;
  const msgOf = (j, done) => j.queued ? "Pedido enviado; o Pi aplica na próxima conexão." : done;

  // ---- Rotas: lista e detalhe
  EXTRA.rotas = async (body) => {
    if (routeSel) return viewRoute(body, routeSel);
    const list = (await get("/api/gps/routes")).routes || [];
    body.innerHTML = `<div class="card"><h2>🛣️ Rotas</h2><div class="lbl" style="margin-top:4px">Cada caminho que se repete vira uma rota: quanto gasta e quanto custa, viagem a viagem</div>
      ${list.length ? `<div class="tbl" style="margin-top:10px"><table class="cards"><thead><tr><th>Rota</th><th>Viagens</th><th>Km</th><th>Tempo</th><th>km/L</th><th>L/100 km</th><th>R$/km</th></tr></thead>
        <tbody id="gRt">${list.map((x) => `<tr class="click" data-id="${x.id}"><td class="head"><b>${esc(x.name)}</b>${x.last_ts ? `<br>última ${dt(x.last_ts)}` : ""}</td>
          <td class="num" data-l="Viagens">${x.n}</td><td class="num" data-l="Km">${nf(x.km, 1)}</td><td class="num" data-l="Tempo">${x.dur_s != null ? dur(x.dur_s) : "—"}</td>
          <td class="num" data-l="km/L">${nf(x.km_l, 1)}</td><td class="num" data-l="L/100 km">${nf(x.l100, 1)}</td><td class="num" data-l="R$/km">${money(x.cost_km)}</td></tr>`).join("")}</tbody></table></div>
        <div class="muted" style="font-size:12px;margin-top:6px">Medianas das viagens normais (viagens com parada de mais de 10 min no meio ficam de fora). Toque numa rota para ver o detalhe.</div>`
      : `<div class="empty" style="margin-top:10px">Nenhuma rota ainda. Cada caminho feito com o GPS ligado em quase toda a viagem (a partir de 1 km) vira uma rota sozinho; ida e volta contam separado.</div>`}</div>`;
    $("#gRt")?.addEventListener("click", (e) => { const tr = e.target.closest("tr[data-id]"); if (tr) { routeSel = +tr.dataset.id; render(); } });
  };

  async function viewRoute(body, id) {
    const r = await get(`/api/gps/routes/${id}`), st = r.stats || {};
    const grp = (rows, first) => rows.length ? `<div class="tbl" style="margin-top:8px"><table><thead><tr><th>${first}</th><th>Viagens</th><th>Tempo</th><th>km/L</th><th>L/100 km</th><th>R$/km</th></tr></thead><tbody>
      ${rows.map(([k, x]) => `<tr><td>${esc(k)}</td><td class="num">${x.n}</td><td class="num">${x.dur_s != null ? dur(x.dur_s) : "—"}</td><td class="num">${nf(x.km_l, 1)}</td><td class="num">${nf(x.l100, 1)}</td><td class="num">${money(x.cost_km)}</td></tr>`).join("")}</tbody></table></div>` : "";
    const bw = (lbl, x) => x ? `<div><span class="lbl">${lbl}</span><b>${nf(x.km_l, 1)} km/L</b><span class="muted" style="font-size:12px">${dt(x.start_ts)}</span></div>` : "";
    body.innerHTML = `<div class="card"><div class="row-head"><div><h2>🛣️ ${esc(r.name)}</h2><div class="lbl" style="margin-top:4px">${st.n || 0} viagem(ns)${st.n_atipicas ? ` · ${st.n_atipicas} atípica(s), fora das medianas` : ""}</div></div>
        <span style="display:flex;gap:6px">${back("gRBack")}<button class="rbtn" type="button" id="gRName">Renomear</button></span></div>
      <div class="stats" style="margin-top:10px"><div><span class="lbl">Distância</span><b>${nf(st.km, 1)} km</b></div><div><span class="lbl">Tempo</span><b>${st.dur_s != null ? dur(st.dur_s) : "—"}</b></div>
        <div><span class="lbl">Consumo</span><b>${nf(st.km_l, 1)} km/L</b><span class="muted" style="font-size:12px">${nf(st.l100, 1)} L/100 km</span></div><div><span class="lbl">Custo</span><b>${money(st.cost)}</b><span class="muted" style="font-size:12px">${money(st.cost_km)}/km</span></div>
        ${bw("Melhor viagem", st.best)}${bw("Pior viagem", st.worst)}</div>
      <div class="muted" id="gRMsg" style="font-size:13px" role="status"></div>
      <div class="gmap sm" id="gRMap" style="margin-top:10px"></div>
      ${grp(Object.entries(st.by_fuel || {}).map(([k, x]) => [FUEL[k] || k, x]), "Combustível")}
      ${grp((st.by_hour || []).map((x) => [x.faixa, x]), "Saída")}
      <h3 class="lbl" style="margin-top:14px">Viagens desta rota</h3>
      <div class="tbl" style="margin-top:8px"><table class="cards"><thead><tr><th>Quando</th><th>Km</th><th>Tempo</th><th>km/L</th><th>R$/km</th><th>Combustível</th></tr></thead><tbody id="gRTrips">
        ${(r.trips || []).map((x) => `<tr class="click" data-id="${x.id}"><td class="head">${dt(x.start_ts)}${x.atipica ? ' <span class="pill" title="Parada longa no meio: fora das medianas">atípica</span>' : ""}</td><td class="num" data-l="Km">${nf(x.km, 1)}</td>
          <td class="num" data-l="Tempo">${x.dur_s != null ? dur(x.dur_s) : "—"}</td><td class="num" data-l="km/L">${nf(x.km_l, 1)}</td><td class="num" data-l="R$/km">${money(x.cost_km)}</td><td data-l="Combustível">${esc(FUEL[x.combustivel] || "—")}</td></tr>`).join("")}</tbody></table></div></div>`;
    $("#gRBack").addEventListener("click", () => { routeSel = null; render(); });
    $("#gRTrips").addEventListener("click", (e) => { const tr = e.target.closest("tr[data-id]"); if (tr) openTrip(+tr.dataset.id); });
    $("#gRName").addEventListener("click", async () => {
      const name = prompt("Nome da rota", r.name); if (!name || !name.trim()) return;
      try { const j = await post(`/api/gps/routes/${id}`, { name: name.trim() }); $("#gRMsg").textContent = msgOf(j, "Renomeada."); if (!j.queued) render(); }
      catch (err) { $("#gRMsg").textContent = "Erro: " + err.message; }
    });
    if (!r.ref_trip_id) return;
    const map = M().create($("#gRMap")); if (!map) return;
    maps.push(map);
    const trk = await trackOf(r.ref_trip_id).catch(() => null);
    if (!trk || map._gone) return;
    M().track(map, trk, "speed");
    const b = trk.bbox; if (b) M().fit(map, window.L.latLngBounds([[b[0], b[1]], [b[2], b[3]]]));
  }

  // ---- Trechos: lista, criar (tocando no traçado), histórico com os serviços
  const SEG_SIG = [["stft", "Ajuste curto", "%", 1], ["residuo_pct", "Consumo × esperado", "%", 0], ["air", "Ar medido ÷ esperado", "%", 0],
    ["lam_err", "Erro da sonda λ", "", 3], ["dur_s", "Duração", "s", 0], ["avg_speed", "Velocidade média", "km/h", 0], ["l100", "L/100 km", "", 1]];
  const segVal = (p, k) => k === "air" ? (p.air_ratio != null ? p.air_ratio * 100 : null) : p[k];
  const VERD = { melhorou: ["var(--ok)", "▲ melhorou"], piorou: ["var(--bad)", "▼ piorou"], igual: ["var(--muted)", "= igual"], misto: ["var(--warn)", "± misto"], "sem dados": ["var(--muted)", "? sem dados"] };

  EXTRA.trechos = async (body) => {
    if (segSel === "novo") return segNew(body);
    if (segSel) return segView(body, segSel);
    const list = (await get("/api/gps/segments")).segments || [];
    body.innerHTML = `<div class="card"><div class="row-head"><div><h2>📏 Trechos de referência</h2><div class="lbl" style="margin-top:4px">O mesmo pedaço de rua medido a cada passagem: a prova mais firme do antes e depois de um conserto</div></div>
        <button class="btn" type="button" id="gSNew">➕ Novo trecho</button></div>
      ${list.length ? `<div class="list" id="gSList" style="margin-top:12px">${list.map((s) => `<div class="alert click" data-id="${s.id}" style="--s:${s.active ? "var(--volt)" : "var(--muted)"};cursor:pointer"><span class="ic">📏</span>
          <div><div class="t">${esc(s.name)} <span class="muted" style="font-weight:500">(${esc(s.label)})</span>${s.active ? "" : ' <span class="pill">desativado</span>'}</div>
          <div class="d">${nf((s.length_m || 0) / 1000, 2)} km · ${s.n_valid} de ${s.n} passagem(ns) válida(s)${s.dur_med != null ? ` · ${dur(s.dur_med)} típico` : ""}${s.last_ts ? ` · última ${dt(s.last_ts)}` : ""}</div></div><span class="more">›</span></div>`).join("")}</div>`
      : `<div class="empty" style="margin-top:10px">Nenhum trecho ainda. Escolha um pedaço de rua que você faz sempre do mesmo jeito (uma reta ou subida sem semáforo, de 0,5 a 3 km): toque em "Novo trecho", escolha uma viagem e marque o início e o fim no traçado. Toda passagem por ali, antes e depois, passa a ser medida.</div>`}</div>`;
    $("#gSNew").addEventListener("click", () => { segSel = "novo"; render(); });
    $("#gSList")?.addEventListener("click", (e) => { const it = e.target.closest("[data-id]"); if (it) { segSel = +it.dataset.id; render(); } });
  };

  async function segNew(body) {
    const lb = await logbook();
    const trips = (lb.items || []).filter((x) => x.has_track).slice(0, 150);
    body.innerHTML = `<div class="card"><div class="row-head"><h2>➕ Novo trecho</h2>${back("gSBack")}</div>
      ${trips.length ? `<div class="gctl" style="margin-top:10px"><label class="lbl" for="gSTrip">Viagem</label><select id="gSTrip" style="flex:1 1 220px;min-width:0">${trips.map((x) => `<option value="${x.id}">${esc(dt(x.start_ts))} · ${esc(x.route || "Viagem")} · ${nf(x.distance_km, 1)} km</option>`).join("")}</select></div>
      <div class="muted" id="gSStep" style="margin-top:8px;font-weight:600" role="status">Toque no traçado no ponto de INÍCIO do trecho.</div>
      <div class="gmap" id="gSMap" style="margin-top:8px"></div>
      <form id="gSForm" class="gpform" style="margin-top:10px"><input type="text" id="gSName" placeholder="Nome (ex.: Subida da avenida)" aria-label="Nome do trecho" maxlength="60" required>
        <span class="muted num" id="gSInfo" style="font-size:12px"></span>
        <div style="display:flex;gap:8px;flex-wrap:wrap"><button class="btn" type="button" id="gSReset">Recomeçar</button><button class="btn" type="submit">Salvar trecho</button></div>
        <div class="muted" id="gSMsg" style="font-size:13px" role="status"></div></form>
      <div class="muted" style="font-size:12px;margin-top:6px">Dica: o trecho vale no sentido em que foi marcado. Evite semáforos e cruzamentos: parar no meio invalida a passagem (a não ser que a referência também pare).</div>`
      : `<div class="empty" style="margin-top:10px">Nenhuma viagem com trajeto ainda. O trecho é marcado sobre o traçado de uma viagem gravada com o GPS.</div>`}</div>`;
    $("#gSBack").addEventListener("click", () => { segSel = null; render(); });
    if (!trips.length) return;
    const map = M().create($("#gSMap")); if (!map) return;
    maps.push(map);
    const L_ = window.L, marks = L_.layerGroup().addTo(map);
    let trk = null, layer = null, ia = -1, ib = -1;
    const lenM = (i, j) => { let d = 0; for (let k = i; k < j; k++) d += map.distance([trk.lat[k], trk.lon[k]], [trk.lat[k + 1], trk.lon[k + 1]]); return d; };
    const show = () => {
      const pins = [];
      if (ia >= 0) pins.push({ lat: trk.lat[ia], lon: trk.lon[ia], label: "🅰️", html: `Início · ${tm(trk.t0 + trk.t[ia] * 1000)}` });
      if (ib >= 0) pins.push({ lat: trk.lat[ib], lon: trk.lon[ib], label: "🅱️", html: `Fim · ${tm(trk.t0 + trk.t[ib] * 1000)}` });
      M().pins(map, pins, marks);
      $("#gSStep").textContent = ia < 0 ? "Toque no traçado no ponto de INÍCIO do trecho." : ib < 0 ? "Agora toque no ponto de FIM (mais adiante no caminho)." : "Trecho marcado. Dê um nome e salve.";
      $("#gSInfo").textContent = ia >= 0 && ib >= 0 ? `${nf(lenM(ia, ib) / 1000, 2)} km · ${dur(trk.t[ib] - trk.t[ia])} nesta viagem` : "";
    };
    const load = async () => {
      ia = ib = -1; show();
      trk = await trackOf(+$("#gSTrip").value).catch(() => null);
      if (!trk || map._gone) return;
      const r = M().track(map, trk, "speed", layer); layer = r.layer;
      r.layer._hit?.on("click", (e) => {
        const i = M().nearest(trk, e.latlng.lat, e.latlng.lng); if (i < 0) return;
        if (ia < 0 || ib >= 0) { ia = i; ib = -1; }
        else if (i <= ia) { $("#gSStep").textContent = "O fim tem de vir depois do início, no sentido da viagem. Toque mais adiante."; return; }
        else ib = i;
        show();
      });
      const b = trk.bbox; if (b) M().fit(map, L_.latLngBounds([[b[0], b[1]], [b[2], b[3]]]));
      show();
    };
    $("#gSTrip").addEventListener("change", () => load().catch(console.error));
    $("#gSReset").addEventListener("click", () => { ia = ib = -1; show(); });
    $("#gSForm").addEventListener("submit", async (e) => {
      e.preventDefault();
      if (ia < 0 || ib < 0) { $("#gSMsg").textContent = "Marque o início e o fim no traçado."; return; }
      $("#gSMsg").textContent = "Procurando as passagens no histórico…";
      try {
        const j = await post("/api/gps/segments", { name: $("#gSName").value.trim(), trip_id: +$("#gSTrip").value, t_a: Math.round(trk.t0 + trk.t[ia] * 1000), t_b: Math.round(trk.t0 + trk.t[ib] * 1000) });
        if (j.queued) { $("#gSMsg").textContent = msgOf(j, ""); return; }
        segSel = j.id; render();
      } catch (err) { $("#gSMsg").textContent = err instanceof Locked ? "Localização protegida: desbloqueie primeiro." : "Erro: " + err.message; }
    });
    await load();
  }

  async function segView(body, id) {
    const h = await get(`/api/gps/segments/${id}`), s = h.segment, ps = h.passes || [];
    let sig = (() => { try { return localStorage.getItem("gpsSegSig") || "stft"; } catch { return "stft"; } })();
    if (!SEG_SIG.some(([k]) => k === sig)) sig = "stft";
    body.innerHTML = `<div class="card"><div class="row-head"><div><h2>📏 ${esc(s.name)}</h2><div class="lbl" style="margin-top:4px">${esc(s.label)} · ${nf((s.length_m || 0) / 1000, 2)} km · referência ${dur(s.ref_dur_s || 0)} · ${h.n_valid} de ${h.n} passagem(ns) válida(s)${s.active ? "" : " · desativado"}</div></div>
        <span style="display:flex;gap:6px;flex-wrap:wrap">${back("gSBack")}<button class="rbtn" type="button" data-a="name">Renomear</button><button class="rbtn" type="button" data-a="rescan">Reprocessar</button>
          <button class="rbtn" type="button" data-a="active">${s.active ? "Desativar" : "Ativar"}</button><button class="rbtn" type="button" data-a="delete">Apagar</button></span></div>
      <div class="muted" id="gSMsg" style="font-size:13px" role="status"></div>
      <div class="gctl" style="margin-top:10px"><label class="lbl" for="gSSig">Mostrar</label><select id="gSSig">${SEG_SIG.map(([k, l]) => `<option value="${k}"${k === sig ? " selected" : ""}>${l}</option>`).join("")}</select></div>
      <svg class="chart" id="gSChart" viewBox="0 0 600 190" preserveAspectRatio="none" style="margin-top:8px"></svg>
      <div class="gm-legend"><span><i style="background:var(--ok)"></i>válida</span><span><i style="background:var(--muted)"></i>inválida (cinza)</span><span><i style="background:var(--warn)"></i>combustível bem diferente</span><span><i style="background:var(--bad)"></i>serviço anotado</span></div>
      <div class="muted" id="gSNote" style="font-size:12px;margin-top:4px"></div>
      <div id="gSSvc" class="list" style="margin-top:12px"></div>
      <div class="gmap sm" id="gSMap" style="margin-top:12px"></div>
      <h3 class="lbl" style="margin-top:14px">Passagens</h3>
      ${ps.length ? `<div class="tbl" style="margin-top:8px"><table class="cards"><thead><tr><th>Quando</th><th>Tempo</th><th>Média</th><th>Ajuste curto</th><th>L/100 km</th><th>Ar ÷ esperado</th><th>Situação</th></tr></thead><tbody id="gSPass">
        ${ps.slice().reverse().map((p) => `<tr class="click" data-id="${p.trip_id ?? ""}" style="${p.valid ? "" : "opacity:.55"}"><td class="head">${dt(p.ts_start)}${p.mistura_diferente ? ' <span class="pill" title="Mistura no tanque bem diferente das outras passagens">⛽ outra mistura</span>' : ""}</td>
          <td class="num" data-l="Tempo">${dur(p.dur_s)}</td><td class="num" data-l="Média">${nf(p.avg_speed)} km/h</td><td class="num" data-l="Ajuste curto">${p.stft != null ? nf(p.stft, 1) + " %" : "—"}</td>
          <td class="num" data-l="L/100 km">${nf(p.l100, 1)}${p.residuo_pct != null ? ` <span class="muted">(${p.residuo_pct > 0 ? "+" : ""}${nf(p.residuo_pct)} %)</span>` : ""}</td>
          <td class="num" data-l="Ar ÷ esperado">${p.air_ratio != null ? nf(p.air_ratio * 100) + " %" : "—"}</td><td data-l="Situação">${p.valid ? "✓ válida" : esc((p.motivos || []).join(", "))}</td></tr>`).join("")}</tbody></table></div>`
      : `<div class="empty" style="margin-top:8px">Nenhuma passagem encontrada ainda. Cada viagem que passar pelo trecho, no mesmo sentido, aparece aqui.</div>`}</div>`;
    $("#gSBack").addEventListener("click", () => { segSel = null; render(); });
    $("#gSPass")?.addEventListener("click", (e) => { const tr = e.target.closest("tr[data-id]"); if (tr && tr.dataset.id) openTrip(+tr.dataset.id); });
    body.querySelectorAll("[data-a]").forEach((b) => b.addEventListener("click", async () => {
      const a = b.dataset.a; let req = null;
      if (a === "name") { const n = prompt("Nome do trecho", s.name); if (!n || !n.trim()) return; req = { name: n.trim() }; }
      else if (a === "rescan") req = { rescan: true };
      else if (a === "active") req = { active: !s.active };
      else if (a === "delete") { if (!confirm("Apagar este trecho e todas as passagens medidas?")) return; req = { delete: true }; }
      $("#gSMsg").textContent = a === "rescan" ? "Procurando no histórico…" : "";
      try { const j = await post(`/api/gps/segments/${id}`, req); $("#gSMsg").textContent = msgOf(j, "Feito.");
        if (!j.queued) { if (a === "delete") segSel = null; render(); } }
      catch (err) { $("#gSMsg").textContent = "Erro: " + err.message; }
    }));
    // gráfico: passagens válidas (verde), inválidas (cinza), mistura diferente (âmbar) e uma linha por serviço
    const chart = () => {
      const meta = SEG_SIG.find((x) => x[0] === sig);
      const pts = ps.map((p) => [p.ts_start, segVal(p, sig), p]).filter((x) => x[1] != null);
      const ys = pts.map((x) => x[1]), lo = ys.length ? Math.min(...ys) : 0, hi = ys.length ? Math.max(...ys) : 1;
      const series = [{ pts: pts.filter((x) => !x[2].valid).map((x) => [x[0], x[1]]), c: "--muted", dots: true, r: 4 },
        { pts: pts.filter((x) => x[2].valid && !x[2].mistura_diferente).map((x) => [x[0], x[1]]), c: "--ok", dots: true, r: 5 },
        { pts: pts.filter((x) => x[2].valid && x[2].mistura_diferente).map((x) => [x[0], x[1]]), c: "--warn", dots: true, r: 5 }];
      for (const v of h.services || []) series.push({ pts: [[v.ts, lo], [v.ts, hi]], c: "--bad", w: 1.5, end: false });
      if (pts.length) U().lineChart($("#gSChart"), series, pts.length === 1 ? { x0: pts[0][0] - 86400e3, x1: pts[0][0] + 86400e3 } : {});
      else $("#gSChart").innerHTML = `<text x="300" y="95" text-anchor="middle">Sem dados deste sinal ainda</text>`;
      $("#gSNote").textContent = sig === "residuo_pct" ? `Consumo de cada passagem comparado com o esperado para a velocidade dela${h.regression ? ` (reta de ${h.regression.n} passagens válidas)` : " (mediana do trecho)"}: negativo = gastou menos.`
        : sig === "stft" ? "Perto de zero é o ideal. Positivo = a central está pondo combustível a mais (ar falso, falta de combustível)." : `${meta[1]}${meta[2] ? " (" + meta[2] + ")" : ""} em cada passagem.`;
    };
    $("#gSSig").addEventListener("change", (e) => { sig = e.target.value; try { localStorage.setItem("gpsSegSig", sig); } catch { } chart(); });
    chart();
    $("#gSSvc").innerHTML = (h.services || []).map((v) => { const [c, l] = VERD[v.veredito] || VERD["sem dados"];
      return `<div class="alert" style="--s:${c};align-items:start"><span class="ic">🔧</span><div style="min-width:0"><div class="t">${esc(v.text)} <span class="muted" style="font-weight:500">· ${dt(v.ts)}</span></div><div class="d">${esc(v.texto)}</div>
        ${v.indicadores.length ? `<div class="tbl" style="margin-top:6px"><table><thead><tr><th></th><th>Antes (${v.antes.n})</th><th>Depois (${v.depois.n})</th><th></th></tr></thead><tbody>${v.indicadores.map((i) => `<tr><td>${esc(i.nome)}</td><td class="num">${nf(i.antes, Number.isInteger(i.antes) ? 0 : 1)} ${esc(i.unidade)}</td><td class="num">${nf(i.depois, Number.isInteger(i.depois) ? 0 : 1)} ${esc(i.unidade)}</td><td style="color:${(VERD[i.veredito] || VERD.igual)[0]};font-weight:600;white-space:nowrap">${(VERD[i.veredito] || VERD.igual)[1]}</td></tr>`).join("")}</tbody></table></div>` : ""}</div><span class="pill">${l}</span></div>`; }).join("");
    // mapa: traçado da viagem de referência e os dois portões
    const map = M().create($("#gSMap")); if (!map) return;
    maps.push(map);
    const L_ = window.L;
    for (const [la, lo, c, t] of [[s.a_lat, s.a_lon, "#36d67a", "Início"], [s.b_lat, s.b_lon, "#ff4d5e", "Fim"]])
      L_.circle([la, lo], { radius: s.radius_m || 35, color: c, weight: 2, fillOpacity: 0.15 }).bindTooltip(t).addTo(map);
    M().fit(map, L_.latLngBounds([[s.a_lat, s.a_lon], [s.b_lat, s.b_lon]]).pad(0.3), 17);
    const trk = s.ref_trip_id ? await trackOf(s.ref_trip_id).catch(() => null) : null;
    if (trk && !map._gone) { const lay = M().track(map, trk, "speed").layer; lay.eachLayer((l) => l !== lay._hit && l.setStyle && l.setStyle({ opacity: 0.5 })); }
  }

  // ---- F5: marchas, embreagem, velocímetro e nota de condução (seções 9.6, 9.10 e 9.11)
  if (tabs && !$('[data-g="desempenho"]', tabs)) tabs.insertAdjacentHTML("beforeend", `<button type="button" class="rbtn" data-g="desempenho">🏁 Desempenho</button>`);
  try { sub = localStorage.getItem("gpsSub") || sub; } catch { }
  if (!["diario", "mapa", "lugares", "rotas", "trechos", "desempenho"].includes(sub)) sub = "diario";
  const EV = { freada: ["#ff4d5e", "🛑", "Freada forte", (v) => `${nf(v, 1)} km/h por segundo`],
    arrancada: ["#ffb020", "🚀", "Arrancada forte", (v) => `+${nf(v, 1)} km/h por segundo`],
    curva: ["#8b7bff", "↪️", "Curva forte", (v) => `${nf(v, 1)} m/s² de força lateral`],
    patinacao: ["#ff6b3d", "⚙️", "Embreagem patinando", (v) => `rotação ${nf(v * 100)} % acima do esperado`] };
  const PEN = { freada: "Freadas fortes", arrancada: "Arrancadas fortes", curva: "Curvas fortes", velocidade: "Excesso de velocidade", motor_frio: "Motor frio forçado", giro_limite: "Giro no limite" };
  const scoreColor = (n) => n == null ? "var(--muted)" : n >= 85 ? "var(--ok)" : n >= 70 ? "var(--warn)" : "var(--bad)";
  const pct = (v, d = 1) => v == null ? "—" : (v > 0 ? "+" : v < 0 ? "−" : "") + nf(Math.abs(v), d) + " %";

  EXTRA.desempenho = async (body) => {
    const [ge, sp, sc] = await Promise.all(["/api/gps/gears", "/api/gps/speedo", "/api/gps/score?days=90"].map((p) => get(p).catch((e) => { if (e instanceof Locked) throw e; return null; })));
    const pi = !cloud();
    const gearsHtml = !ge ? `<div class="empty">Ainda não sincronizado.</div>` : `
      <div class="muted" style="margin-top:4px">${esc(ge.texto)}</div>
      <div class="tbl" style="margin-top:10px"><table><thead><tr><th>Marcha</th><th>Fábrica</th><th>Aprendida (GPS)</th><th>Diferença</th><th>Leituras</th></tr></thead><tbody>
        ${ge.gears.map((g) => `<tr><td>${g.gear}ª</td><td class="num">${nf(g.factory, 2)}</td><td class="num">${g.learned != null ? "<b>" + nf(g.learned, 2) + "</b>" : "—"}</td><td class="num">${pct(g.diff_pct)}</td><td class="num">${g.count}</td></tr>`).join("")}</tbody></table></div>
      <div class="muted" style="font-size:12px;margin-top:6px">km/h a cada 1.000 rpm. A diferença para a fábrica vem do tamanho real do pneu (gasto, pressão, medida).${(ge.rejected || []).length ? ` ${ge.rejected.length} relação(ões) fora do esperado foram descartadas.` : ""}</div>
      <h3 class="lbl" style="margin-top:14px">Embreagem (${ge.days} dias)</h3>
      ${ge.slips.length ? `<div class="list" style="margin-top:8px">${ge.slips.slice(0, 20).map((x) => `<div class="alert click" data-trip="${x.trip_id ?? ""}" style="--s:var(--bad);cursor:pointer"><span class="ic">⚙️</span><div><div class="t">Patinou em ${x.gear ?? "?"}ª · ${dt(x.ts)}</div>
          <div class="d">rotação ${nf(x.pct)} % acima do que a velocidade explica, por ${nf(x.dur_s, 1)} s a ${nf(x.kmh)} km/h</div></div><span class="more">›</span></div>`).join("")}</div>
          ${ge.slips_7d >= 3 ? `<div class="banner" style="--s:var(--bad);margin-top:8px"><span class="ic">⚠️</span><div>${ge.slips_7d} patinações em 7 dias: leve o carro para avaliar a embreagem.</div></div>` : ""}`
        : `<div class="muted" style="margin-top:6px">✓ Nenhuma patinação de embreagem percebida. O aviso aparece se, acelerando forte em 3ª, 4ª ou 5ª, a rotação subir mais do que a velocidade explica.</div>`}
      ${pi ? `<div style="margin-top:10px"><button class="rbtn" type="button" id="gGReset">Esquecer as relações aprendidas</button> <span class="muted" style="font-size:12px">(depois de trocar pneus ou câmbio)</span></div>` : ""}`;
    const spHtml = !sp ? `<div class="empty">Ainda não sincronizado.</div>` : `
      <div style="margin-top:6px;font-weight:600">${esc(sp.texto)}</div>
      ${sp.bins.length ? `<div class="tbl" style="margin-top:10px"><table><thead><tr><th>Faixa (central)</th><th>Leituras</th><th>Central</th><th>Verdadeira (GPS)</th><th>Diferença</th></tr></thead><tbody>
        ${sp.bins.map((b) => `<tr style="${b.n >= sp.min_pairs ? "" : "opacity:.55"}"><td>${b.lo}–${b.lo + 9} km/h</td><td class="num">${b.n}</td><td class="num">${nf(b.obd, 1)}</td><td class="num">${nf(b.gps, 1)}</td><td class="num">${pct(b.err_pct)}</td></tr>`).join("")}</tbody></table></div>
        <div class="muted" style="font-size:12px;margin-top:6px">${sp.n_bins} de ${sp.min_bins} faixas com ${sp.min_pairs}+ leituras em velocidade constante. "Diferença" = quanto a central marca a mais que a velocidade verdadeira.</div>` : ""}
      <div class="muted" style="font-size:12px;margin-top:6px">A central é o que o OBD lê. O <b>ponteiro do painel</b> costuma marcar ainda mais que a central (por lei ele nunca pode marcar menos que a velocidade real).</div>
      ${sp.pneus && sp.pneus.texto ? `<div class="banner" style="--s:var(--warn);margin-top:8px"><span class="ic">🛞</span><div>${esc(sp.pneus.texto)}</div></div>` : ""}
      ${sp.ponteiro ? `<div class="muted" style="margin-top:6px">Ponteiro anotado: ${nf(sp.ponteiro.needle)} km/h com o carro a ${nf(sp.ponteiro.real)} (${pct(sp.ponteiro.vs_real_pct)}) · ${dt(sp.ponteiro.ts)}</div>` : ""}
      ${pi ? `<form id="gNForm" class="gpform" style="margin-top:8px"><span class="muted" style="font-size:13px">Em velocidade constante, anote o ponteiro e o GPS (modo direção) no mesmo instante:</span>
        <label class="muted" style="display:flex;gap:6px;align-items:center;font-size:13px">Ponteiro <input type="number" id="gNNd" min="10" max="250" step="1" style="width:80px" aria-label="Ponteiro do painel em km/h" required> km/h</label>
        <label class="muted" style="display:flex;gap:6px;align-items:center;font-size:13px">GPS <input type="number" id="gNRl" min="10" max="250" step="0.5" style="width:80px" aria-label="Velocidade do GPS em km/h" required> km/h</label>
        <button class="btn" type="submit">Salvar</button><span class="muted" id="gNMsg" style="font-size:13px" role="status"></span></form>` : ""}`;
    const items = sc?.items || [];
    const scHtml = !sc ? `<div class="empty">Ainda não sincronizado.</div>` : !items.length
      ? `<div class="muted" style="margin-top:6px">Nenhuma viagem com nota ainda. A nota sai para viagens com GPS de 2 km e 3 min ou mais.</div>` : `
      <div class="stats" style="margin-top:10px"><div><span class="lbl">Média (${sc.days} dias)</span><b style="color:${scoreColor(sc.media)}">${nf(sc.media)}</b></div>
        <div><span class="lbl">Viagens</span><b>${sc.n}</b></div>${sc.tendencia ? `<div><span class="lbl">Tendência</span><b>${esc(sc.tendencia.texto)}</b><span class="muted" style="font-size:12px">${sc.tendencia.delta > 0 ? "+" : ""}${nf(sc.tendencia.delta, 1)} ponto(s)</span></div>` : ""}
        ${sc.por_100km ? `<div><span class="lbl">A cada 100 km</span><b>${nf(sc.por_100km.freadas, 1)}</b><span class="muted" style="font-size:12px">freadas · ${nf(sc.por_100km.curvas, 1)} curvas · ${nf(sc.por_100km.arrancadas, 1)} arrancadas</span></div>` : ""}</div>
      <svg class="chart" id="gScChart" viewBox="0 0 600 160" preserveAspectRatio="none" style="margin-top:8px"></svg>
      <div class="tbl" style="margin-top:8px"><table class="cards"><thead><tr><th>Viagem</th><th>Nota</th><th>Freadas</th><th>Arrancadas</th><th>Curvas</th><th>Km</th></tr></thead><tbody id="gScRows">
        ${items.slice().reverse().slice(0, 30).map((x) => `<tr class="click" data-id="${x.trip_id}"><td class="head">${dt(x.start_ts)}</td><td class="num" data-l="Nota"><b style="color:${scoreColor(x.score)}">${x.score}</b></td>
          <td class="num" data-l="Freadas">${x.freadas}</td><td class="num" data-l="Arrancadas">${x.arrancadas}</td><td class="num" data-l="Curvas">${x.curvas}</td><td class="num" data-l="Km">${nf(x.km, 1)}</td></tr>`).join("")}</tbody></table></div>
      <div class="muted" style="font-size:12px;margin-top:6px">Mede segurança e suavidade pelo GPS (a nota econômica, pelo motor, continua na aba Viagens). O excesso de velocidade usa o limite de cada rua quando o mapa offline (OpenStreetMap) tem a placa; sem ele, o tempo acima de 115 km/h.</div>`;
    body.innerHTML = `<div class="card"><h2>⚙️ Marchas e embreagem</h2>${gearsHtml}</div>
      <div class="card"><h2>🧭 Velocímetro e pneus</h2>${spHtml}</div>
      <div class="card"><h2>🏁 Nota de condução</h2>${scHtml}</div>`;
    if (items.length && U().lineChart) U().lineChart($("#gScChart"), [{ pts: items.map((x) => [x.start_ts, x.score]), c: "--ok", dots: true, r: 4 }],
      items.length === 1 ? { x0: items[0].start_ts - 86400e3, x1: items[0].start_ts + 86400e3 } : {});
    $("#gScRows")?.addEventListener("click", (e) => { const tr = e.target.closest("tr[data-id]"); if (tr) openTrip(+tr.dataset.id); });
    body.querySelectorAll("[data-trip]").forEach((el) => el.addEventListener("click", () => { if (el.dataset.trip) openTrip(+el.dataset.trip); }));
    $("#gGReset")?.addEventListener("click", async () => {
      if (!confirm("Esquecer as relações aprendidas? O aprendizado recomeça do zero (use depois de trocar pneus).")) return;
      try { await post("/api/gps/gears/reset", {}); render(); } catch (err) { alert("Erro: " + err.message); }
    });
    $("#gNForm")?.addEventListener("submit", async (e) => {
      e.preventDefault();
      try { const j = await post("/api/gps/speedo", { needle: +$("#gNNd").value, real: +$("#gNRl").value }); $("#gNMsg").textContent = `Salvo: o ponteiro marca ${pct(j.vs_real_pct)}.`; }
      catch (err) { $("#gNMsg").textContent = "Erro: " + err.message; }
    });
  };

  // detalhe da viagem: nota de condução e alfinetes de freadas, arrancadas, curvas e patinações
  TRIP_EXTRA.push(({ card, map, det }) => {
    const g = det?.gps || {};
    const evs = (det?.events || []).filter((e) => EV[e.kind] && e.lat != null);
    let d = null; try { d = g.drive_json ? JSON.parse(g.drive_json) : null; } catch { }
    if (evs.length) M().pins(map, evs.map((e) => ({ lat: e.lat, lon: e.lon, color: EV[e.kind][0], r: 7, ring: "#fff",
      html: `<b>${EV[e.kind][1]} ${EV[e.kind][2]}</b><br>${tm(e.ts)} · ${esc(EV[e.kind][3](e.value))}<br><a href="#" data-rp="${e.ts}">Replay 8 s antes</a>` })));
    if (!d) return;
    const box = document.createElement("div");
    box.style.marginTop = "10px";
    if (g.drive_score == null) box.innerHTML = `<div class="muted" style="font-size:13px">Nota de condução: sem nota (${esc(d.motivo || "—")}).</div>`;
    else {
      const pen = Object.entries(d.penalidades || {}).filter(([, v]) => v > 0);
      box.innerHTML = `<div class="row-head"><div><span class="lbl">Nota de condução</span> <b style="font-size:20px;color:${scoreColor(g.drive_score)}">${g.drive_score}</b></div>
        <div class="gm-legend">${Object.keys(EV).filter((k) => k !== "patinacao").map((k) => `<span><i style="background:${EV[k][0]}"></i>${EV[k][2]}: ${d[k === "freada" ? "freadas" : k === "arrancada" ? "arrancadas" : "curvas"] || 0}</span>`).join("")}</div></div>
        ${pen.length ? `<div class="muted" style="font-size:12px;margin-top:4px">Descontos: ${pen.map(([k, v]) => `${esc(PEN[k] || k)} −${nf(v, 1)}`).join(" · ")}</div>` : ""}
        ${(d.dicas || []).map((x) => `<div class="muted" style="font-size:13px;margin-top:4px">💡 ${esc(x)}</div>`).join("")}`;
    }
    const leg = $("#gTLeg", card); if (leg) leg.insertAdjacentElement("afterend", box); else card.appendChild(box);
  });

  // ---- F6: desempenho — 0–100 e retomadas, dinamômetro, inclinação e altitude (seções 9.8, 9.9, 9.12 e 9.23)
  const CV = 1.35962, KGFM = 1 / 9.80665;
  const FUELS = { gasolina: "gasolina", etanol: "etanol", mistura: "mistura" };
  const runTag = (x) => [x.source === "gps" ? (x.auto ? "automática" : "teste armado") : "pelo OBD", x.hz ? `${nf(x.hz, x.hz >= 3 ? 0 : 1)} Hz` : "",
    x.fuel ? FUELS[x.fuel] || x.fuel : "", x.note ? "⚠ " + x.note : ""].filter(Boolean).join(" · ");
  const secs = (v) => v == null ? "—" : nf(v, 2) + " s";

  function perfHtml(pf) {
    if (!pf) return `<div class="empty">Ainda não sincronizado.</div>`;
    const runs = pf.runs || [];
    if (!runs.length) return `<div class="muted" style="margin-top:6px">Nenhuma arrancada ainda. Com o GPS, o Delfos mede sozinho toda arrancada de propósito (pé no fundo, 0–60 em até 12 s) e as retomadas
      40–80, 60–100 e 80–120 na mesma marcha. O botão ⏱ 0–100 do modo direção continua valendo.</div>`;
    const rec = pf.records || [];
    return `${rec.length ? `<h3 class="lbl" style="margin-top:8px">Melhores tempos (em plano, GPS bom)</h3>
      <div class="tbl" style="margin-top:6px"><table><thead><tr><th>Medida</th><th>Combustível</th><th>Tempo</th><th>Quando</th></tr></thead><tbody>
      ${rec.map((r) => `<tr><td>${esc(r.label)}</td><td>${esc(FUELS[r.fuel] || "—")}</td><td class="num"><b>${secs(r.t_s)}</b></td><td>${dt(r.ts)}${r.source === "obd" ? " · OBD" : ""}</td></tr>`).join("")}</tbody></table></div>` : ""}
      <h3 class="lbl" style="margin-top:12px">Últimas</h3>
      <div class="list" style="margin-top:6px">${runs.slice(0, 12).map((x, i) => `<div class="alert click" data-run="${i}" style="--s:${x.t_s != null && x.valid ? "var(--ok)" : "var(--muted)"};cursor:pointer">
        <span class="ic">${x.record ? "🏆" : x.kind === "0-100" ? "⏱" : "↗"}</span><div><div class="t">${esc(x.label)}${x.t_s != null ? " em " + secs(x.t_s) : " · incompleta"}</div>
        <div class="d">${x.kind === "0-100" && x.t60 != null ? `0–60 ${secs(x.t60)} · 0–80 ${secs(x.t80)} · ` : ""}${esc(runTag(x))} · ${dt(x.ts)}</div></div>${x.curve ? '<span class="more">›</span>' : ""}</div>`).join("")}</div>
      <svg class="chart" id="gPfChart" viewBox="0 0 600 190" preserveAspectRatio="none" style="margin-top:8px" hidden></svg>
      <div class="muted" style="font-size:12px;margin-top:6px">Tempo pelo GPS (5 vezes por segundo, ±0,05 s; a 1 Hz ±0,25 s). Corridas em subida ou descida de mais de 2 % e com GPS fraco ficam marcadas e não contam como recorde.
        A comparação que vale é o carro com ele mesmo: antes e depois de um reparo, gasolina contra etanol.</div>`;
  }

  function dynoHtml(dy, pi) {
    if (!dy) return `<div class="empty">Ainda não sincronizado.</div>`;
    const v = dy.vehicle || {}, runs = dy.runs || [], ok = runs.filter((x) => x.valid);
    const b = dy.best, ref = dy.ref || {};
    const cl = dy.coast_live, co = dy.coast;
    return `${b ? `<div class="stats" style="margin-top:10px"><div><span class="lbl">Melhor (corrigido)</span><b>${nf(b.peak_cv_corr)} cv</b><span class="muted" style="font-size:12px">a ${nf(b.peak_kw_rpm)} rpm · medido ${nf(b.peak_cv)} cv</span></div>
        <div><span class="lbl">Torque (corrigido)</span><b>${nf(b.peak_kgfm_corr, 1)} kgfm</b><span class="muted" style="font-size:12px">a ${nf(b.peak_nm_rpm)} rpm</span></div>
        <div><span class="lbl">Referência de fábrica</span><b>~${ref.cv} cv</b><span class="muted" style="font-size:12px">a ${nf(ref.cv_rpm)} rpm · ~${nf(ref.kgfm, 1)} kgfm</span></div>
        <div><span class="lbl">Precisão</span><b style="font-size:14px">${esc(dy.precisao || "")}</b></div></div>
        <svg class="chart" id="gDyP" viewBox="0 0 600 190" preserveAspectRatio="none" style="margin-top:8px"></svg>
        <div class="gm-legend"><span><i style="background:var(--rpm)"></i>potência (cv, corrigida)</span><span><i style="background:var(--speed)"></i>torque (kgfm × 5, corrigido)</span><span class="muted" id="gDyCap"></span></div>`
      : `<div class="muted" style="margin-top:6px">Nenhuma puxada ainda. Para medir: motor quente, em 2ª, 3ª ou 4ª a ~1.500 rpm, pé no fundo sem trocar de marcha até perto do corte, num trecho plano e seguro.
        O Delfos grava 5 posições por segundo durante a puxada e calcula a curva quando a viagem termina.</div>`}
      ${ok.length ? `<div class="tbl" style="margin-top:10px"><table class="cards"><thead><tr><th>Quando</th><th>Marcha</th><th>Potência</th><th>Torque</th><th>Ar</th><th>Observação</th></tr></thead><tbody id="gDyRows">
        ${runs.map((x, i) => [x, i]).filter(([x]) => x.valid).slice(0, 20).map(([x, i]) => `<tr class="click" data-dy="${i}"><td class="head">${dt(x.ts)}</td><td data-l="Marcha">${x.gear ? x.gear + "ª" : "—"}</td>
          <td class="num" data-l="Potência"><b>${nf(x.peak_cv_corr)}</b> cv a ${nf(x.peak_kw_rpm)}</td><td class="num" data-l="Torque">${nf(x.peak_kgfm_corr, 1)} kgfm a ${nf(x.peak_nm_rpm)}</td>
          <td class="num" data-l="Ar">${x.baro != null ? nf(x.baro) + " kPa" : "—"}${x.iat != null ? " · " + nf(x.iat) + " °C" : ""}</td><td data-l="Observação">${esc(x.why || "ok")}</td></tr>`).join("")}</tbody></table></div>
        <div class="muted" style="font-size:12px;margin-top:6px">${ok.length} puxada(s) válida(s); toque numa linha para ver a curva. "Corrigido" = como se fosse ao nível do mar a 20 °C (aqui o ar tem ~10 % menos oxigênio). Resultado fora de 60–160 cv sai marcado como suspeito.</div>` : ""}
      ${runs.length > ok.length ? `<div class="muted" style="font-size:12px;margin-top:6px">Pé no fundo que não virou curva: ${(() => { const by = {}; for (const x of runs) if (!x.valid) by[x.why || "—"] = (by[x.why || "—"] || 0) + 1;
        return Object.entries(by).map(([w, n]) => `${esc(w)} (${n})`).join(" · "); })()}.</div>` : ""}
      <h3 class="lbl" style="margin-top:14px">O carro</h3>
      <div class="muted" style="margin-top:4px">Massa usada: <b>${nf(v.mass)} kg</b> (${nf(v.mass_base)} kg do carro com o motorista e combustível + ${Math.max(0, (v.occupants || 1) - 1)} passageiro(s) de 75 kg).
        Arrasto: CdA ${nf(v.cda, 2)} m² e rolagem Crr ${nf(v.crr, 3)} (${esc(v.aero_src || "padrão")}). A massa pesa direto no resultado: confira antes de comparar.</div>
      ${pi ? `<form id="gVeh" class="gpform" style="grid-template-columns:repeat(auto-fit,minmax(150px,1fr));align-items:end">
        <label class="muted" style="font-size:13px">Massa (kg, com o motorista)<input type="number" id="gVM" min="800" max="2500" step="5" value="${v.mass_base ?? ""}"></label>
        <label class="muted" style="font-size:13px">Pessoas no carro<input type="number" id="gVO" min="1" max="5" step="1" value="${v.occupants ?? 1}"></label>
        <button class="btn" type="submit">Salvar</button><span class="muted" id="gVMsg" style="font-size:13px" role="status"></span></form>` : ""}
      <h3 class="lbl" style="margin-top:14px">Calibrar o arrasto (descida em ponto morto)</h3>
      <div class="muted" style="margin-top:4px">Melhora a precisão de ±8–10 % para ±5 %. Numa reta plana e sem vento: chegue a ~90 km/h, ponha em <b>ponto morto</b> e deixe o carro cair até ~45 km/h sem frear.
        Depois faça o mesmo no <b>sentido contrário</b>, no mesmo trecho (a média dos dois cancela a inclinação e o vento). Só onde for seguro.</div>
      ${co && co.ok ? `<div class="muted" style="margin-top:6px">Calibrado em ${dt(co.ts)}: CdA ${nf(co.cda, 2)} m² e Crr ${nf(co.crr, 3)}${co.prev ? ` (antes: ${nf(co.prev.cda, 2)} e ${nf(co.prev.crr, 3)})` : ""}.</div>` : ""}
      ${cl ? `<div class="banner" style="--s:${cl.state === "recusado" ? "var(--warn)" : "var(--speed)"};margin-top:8px"><span class="ic">${cl.state === "concluido" ? "✅" : cl.state === "recusado" ? "⚠️" : "🛞"}</span><div>${esc(cl.msg || cl.state)} ${cl.n ? `(${cl.n} descida(s) medida(s))` : ""}</div></div>` : ""}
      ${pi ? `<div style="margin-top:8px;display:flex;gap:8px;flex-wrap:wrap">${cl && !["concluido", "recusado"].includes(cl.state) ? `<button class="rbtn" type="button" id="gCoX">Cancelar a calibração</button>`
          : `<button class="btn" type="button" id="gCoGo">Começar a calibração</button>`}${co ? `<button class="rbtn" type="button" id="gCoRs">Voltar aos valores padrão</button>` : ""}<span class="muted" id="gCoMsg" style="font-size:13px" role="status"></span></div>` : ""}`;
  }

  function terrainHtml(te) {
    if (!te) return `<div class="empty">Ainda não sincronizado.</div>`;
    const L = te.leitura || {}, bands = (te.bands || []).filter((x) => x.s > 0), P = te.power || {};
    const cell = (v, d = 1, u = "") => v == null ? "—" : nf(v, d) + u;
    const now = te.agora || {};
    return `${now.avail_pct != null ? `<div class="stats" style="margin-top:10px"><div><span class="lbl">Potência disponível agora</span><b>${nf(now.avail_pct)} %</b><span class="muted" style="font-size:12px">da que o motor teria ao nível do mar</span></div>
        ${now.slope != null ? `<div><span class="lbl">Inclinação agora</span><b>${now.slope > 0 ? "+" : ""}${nf(now.slope, 1)} %</b></div>` : ""}</div>` : ""}
      <div class="banner" style="--s:${L.msg ? "var(--warn)" : "var(--muted)"};margin-top:10px"><span class="ic">${L.msg ? "💨" : "⛰"}</span><div>${L.msg ? `<b>${esc(L.msg)}</b><br>` : ""}${esc(L.texto || "")}</div></div>
      ${bands.length ? `<div class="tbl" style="margin-top:10px"><table><thead><tr><th>Inclinação</th><th>Tempo</th><th>Carga</th><th>MAP</th><th>Borboleta</th><th>Ajuste curto</th><th>Sonda (erro)</th><th>Avanço</th><th>L/100 km</th><th>Ar ÷ esperado</th></tr></thead><tbody>
        ${bands.map((x) => `<tr><td>${esc(x.band)}</td><td class="num">${dur(x.s)}</td><td class="num">${cell(x.load, 0, " %")}</td><td class="num">${cell(x.map, 0)}</td><td class="num">${cell(x.tps, 1, " %")}</td>
          <td class="num">${x.stft == null ? "—" : (x.stft > 0 ? "+" : "") + nf(x.stft, 1) + " %"}</td><td class="num">${cell(x.lam_err, 3)}</td><td class="num">${cell(x.timing, 1, "°")}</td><td class="num">${cell(x.l100, 1)}</td><td class="num">${cell(x.air, 2)}</td></tr>`).join("")}</tbody></table></div>
        <div class="muted" style="font-size:12px;margin-top:6px">${te.trips} viagem(ns) com GPS em ${te.days} dias. Inclinação pela altitude do GPS (e pela velocidade vertical do receptor, quando vem); este carro não tem ajuste longo: tudo usa o curto.</div>`
        : `<div class="muted" style="margin-top:6px">Sem viagens com GPS no período.</div>`}
      ${(P.bands || []).length ? `<h3 class="lbl" style="margin-top:14px">Altitude × potência</h3>
        <div class="tbl" style="margin-top:6px"><table><thead><tr><th>Altitude</th><th>Tempo</th><th>Pressão</th><th>MAP no fundo</th><th>Teoria</th><th>Puxadas</th><th>Medido</th><th>Corrigido</th></tr></thead><tbody>
        ${P.bands.map((x) => `<tr style="${x.restricao ? "color:var(--warn)" : ""}"><td>${nf(x.alt)}–${nf(x.alt_hi)} m</td><td class="num">${dur(x.s)}</td><td class="num">${cell(x.baro, 1, " kPa")}</td><td class="num">${cell(x.map_wot, 1, " kPa")}</td>
          <td class="num">${cell(x.theory_pct, 0, " %")}</td><td class="num">${x.pulls}</td><td class="num">${x.peak_kw != null ? nf(x.peak_kw * CV) + " cv" : "—"}</td><td class="num">${x.peak_kw_corr != null ? nf(x.peak_kw_corr * CV) + " cv" : "—"}</td></tr>`).join("")}</tbody></table></div>
        <div class="muted" style="font-size:12px;margin-top:6px">Motor aspirado perde ~1 % de potência a cada 100 m de altitude ("Teoria" = % do ar ao nível do mar). Se a potência corrigida cair mais que isso, ou se com o pé no fundo o MAP ficar mais de 8 kPa abaixo da pressão, há algo além do ar rarefeito.</div>
        ${(P.msgs || []).map((m) => `<div class="banner" style="--s:var(--warn);margin-top:8px"><span class="ic">⚠️</span><div>${esc(m)}</div></div>`).join("")}` : ""}`;
  }

  const f5Desempenho = EXTRA.desempenho;
  EXTRA.desempenho = async (body) => {
    await f5Desempenho(body);
    const [pf, dy, te] = await Promise.all(["/api/gps/perf", "/api/gps/dyno", "/api/gps/terrain?days=90"].map((p) => get(p).catch((e) => { if (e instanceof Locked) throw e; return null; })));
    const pi = !cloud();
    body.insertAdjacentHTML("beforeend", `<div class="card"><h2>⏱ Arrancadas e retomadas</h2>${perfHtml(pf)}</div>
      <div class="card"><h2>📈 Dinamômetro</h2>${dynoHtml(dy, pi)}</div>
      <div class="card"><h2>⛰ Inclinação e altitude × motor</h2>${terrainHtml(te)}</div>`);
    const LC = U().lineChart;
    const runs = pf?.runs || [];
    const showRun = (i) => { const x = runs[i], svg = $("#gPfChart", body); if (!x?.curve || !svg || !LC) return; svg.hidden = false;
      LC(svg, [{ pts: x.curve.map((p) => [p[0], p[1]]), c: "--speed", area: true }], { ymin: 0, xfmt: (t) => nf(t, 0) + " s" }); };
    body.querySelectorAll("[data-run]").forEach((el) => el.addEventListener("click", () => showRun(+el.dataset.run)));
    const drows = dy?.runs || [];
    const showDyno = (i) => { const x = drows[i], svg = $("#gDyP", body); if (!x?.valid || !x.curve || !svg || !LC) return;
      const c = x.corr || 1, cv = x.curve;
      LC(svg, [{ pts: cv.rpm.map((r, k) => [r, cv.kw[k] * c * CV]), c: "--rpm", w: 2.5 }, { pts: cv.rpm.map((r, k) => [r, cv.nm[k] * c * KGFM * 5]), c: "--speed" }],
        { ymin: 0, xfmt: (r) => nf(r, 0) + " rpm" });
      const cap = $("#gDyCap", body); if (cap) cap.textContent = `${dt(x.ts)} · ${x.gear}ª · ${nf(x.mass_kg)} kg`; };
    const firstOk = drows.findIndex((x) => x.valid && x.curve);
    if (firstOk >= 0) showDyno(firstOk);
    $("#gDyRows", body)?.addEventListener("click", (e) => { const tr = e.target.closest("tr[data-dy]"); if (tr) showDyno(+tr.dataset.dy); });
    $("#gVeh", body)?.addEventListener("submit", async (e) => {
      e.preventDefault();
      try { await post("/api/gps/dyno/config", { mass_kg: +$("#gVM", body).value, occupants: +$("#gVO", body).value }); $("#gVMsg", body).textContent = "Salvo. Vale para as próximas puxadas (refaça a análise da viagem para recalcular as antigas)."; }
      catch (err) { $("#gVMsg", body).textContent = "Erro: " + err.message; }
    });
    $("#gCoGo", body)?.addEventListener("click", async () => { try { await post("/api/gps/dyno/coast", {}); $("#gCoMsg", body).textContent = "Pronto: o Delfos está esperando a descida em ponto morto. Acompanhe pelo modo direção ou aqui."; } catch (err) { $("#gCoMsg", body).textContent = "Erro: " + err.message; } });
    $("#gCoX", body)?.addEventListener("click", async () => { try { await post("/api/gps/dyno/coast", { cancel: true }); render(); } catch (err) { $("#gCoMsg", body).textContent = "Erro: " + err.message; } });
    $("#gCoRs", body)?.addEventListener("click", async () => {
      if (!confirm("Esquecer a calibração e voltar aos valores padrão de arrasto?")) return;
      try { await post("/api/gps/dyno/config", { reset_coast: true }); render(); } catch (err) { $("#gCoMsg", body).textContent = "Erro: " + err.message; }
    });
  };

  // detalhe da viagem: arrancadas e retomadas medidas nela
  TRIP_EXTRA.push(({ card, det }) => {
    const runs = (det?.accel || []).filter((x) => x.source === "gps");
    if (!runs.length) return;
    const box = document.createElement("div");
    box.style.marginTop = "8px";
    box.innerHTML = runs.map((x) => { let lbl = x.kind || "0-100"; try { lbl = JSON.parse(x.json || "{}").label || lbl; } catch { }
      return `<div class="muted" style="font-size:13px">⏱ ${esc(lbl)} ${x.t_s != null ? "em <b>" + secs(x.t_s) + "</b>" : "incompleta"} · ${tm(x.ts)}${x.note ? " · " + esc(x.note) : ""}</div>`; }).join("");
    card.appendChild(box);
  });

  // ---- F7: vias — cidade × estrada (seção 9.20), mapa offline de radares e limites (9.17)
  if (tabs && !$('[data-g="vias"]', tabs)) tabs.insertAdjacentHTML("beforeend", `<button type="button" class="rbtn" data-g="vias">🏙️ Cidade × estrada</button>`);
  try { sub = localStorage.getItem("gpsSub") || sub; } catch { }
  if (!["diario", "mapa", "lugares", "rotas", "trechos", "desempenho", "vias"].includes(sub)) sub = "diario";
  const CLS = { cidade: ["🏙️ Cidade", "--speed"], estrada: ["🛣️ Estrada", "--o2"], misto: ["↔️ Misto", "--muted"] };
  const mmYY = (ts) => ts ? new Date(+ts).toLocaleDateString("pt-BR", { month: "2-digit", year: "numeric" }) : "—";
  const gearTxt = (gs) => Object.entries(gs || {}).filter(([, v]) => v >= 1).map(([g, v]) => `${g}ª ${nf(v)} %`).join(" · ") || "—";
  function geoCard(st) {
    const g = st?.geo;
    if (!g) return `<div class="card"><h2>📷 Mapa de radares e limites</h2><div class="muted" style="margin-top:6px">Mapa de radares não instalado. Sem ele, o modo direção não mostra limite nem radar,
      e "cidade × estrada" é decidido pela velocidade (média e paradas).</div>${cloud() ? "" : `<div class="muted" style="font-size:13px;margin-top:8px">Para instalar, no PC (com internet), escolha a região por onde o carro anda:<br>
      <code class="k">python3 scripts/geo-build.py --bbox S,W,N,E --out data/geo/geo.db</code><br>e depois <code class="k">scripts/deploy.sh</code>. O arquivo sai do OpenStreetMap (dados abertos).</div>`}</div>`;
    return `<div class="card"><h2>📷 Mapa de radares e limites</h2><div class="stats" style="margin-top:10px">
      <div><span class="lbl">Dados de</span><b>${mmYY(g.osm_ts || g.built_ts)}</b><span class="muted" style="font-size:12px">${g.age_d != null ? g.age_d + " dia(s)" : ""}</span></div>
      <div><span class="lbl">Radares</span><b>${esc(g.cameras ?? "—")}</b><span class="muted" style="font-size:12px">${esc(g.cameras_dir ?? "0")} com sentido</span></div>
      <div><span class="lbl">Vias</span><b>${esc(g.roads ?? "—")}</b><span class="muted" style="font-size:12px">${esc(g.roads_maxspeed ?? "0")} com placa de limite</span></div>
      <div><span class="lbl">Tamanho</span><b>${nf(g.mb, 1)} MB</b></div></div>
      ${g.velho ? `<div class="banner" style="--s:var(--warn);margin-top:8px"><span class="ic">⚠️</span><div>O mapa tem mais de 6 meses: radares e limites podem ter mudado. Gere de novo no PC (scripts/geo-build.py) e envie com o deploy.</div></div>` : ""}
      <div class="muted" style="font-size:12px;margin-top:8px">É um auxílio, como os aplicativos de navegação: os dados são do OpenStreetMap (© colaboradores do OpenStreetMap, ODbL) e podem estar desatualizados.
        O Delfos não detecta radar; a placa da via vale sempre. Limite sem placa no mapa é presumido pelo tipo de via, aparece esmaecido e nunca é falado.</div></div>`;
  }
  EXTRA.vias = async (body) => {
    const pi = !cloud();
    let days = 30; try { days = pi ? +(localStorage.getItem("gpsMixDays") || 30) : 30; } catch { }
    const rm = await get(`/api/gps/roadmix?days=${days}`);
    const rows = (rm.classes || []).filter((x) => x.s > 0);
    body.innerHTML = `<div class="card"><div class="row-head"><div><h2>🏙️ Cidade × estrada</h2><div class="lbl" style="margin-top:4px">O consumo certo para comparar: cidade com cidade, estrada com estrada</div></div>
        ${pi ? `<select id="gMixD" aria-label="Período">${[30, 90, 365].map((d) => `<option value="${d}"${d === days ? " selected" : ""}>${d} dias</option>`).join("")}</select>` : ""}</div>
      ${rows.length ? `<div style="font-weight:600;margin-top:10px">${esc(rm.texto)}</div>
        <div class="tbl" style="margin-top:10px"><table class="cards"><thead><tr><th>Tipo</th><th>Km</th><th>Tempo</th><th>Média</th><th>km/L</th><th>L/100 km</th><th>Rotação</th><th>Carga</th><th>Ajuste curto</th><th>Motor</th><th>Marchas</th></tr></thead><tbody>
        ${rows.map((x) => `<tr><td class="head"><b>${CLS[x.cls][0]}</b>${x.pct_km != null ? `<br>${nf(x.pct_km)} % dos km` : ""}</td><td class="num" data-l="Km">${nf(x.km, 1)}</td><td class="num" data-l="Tempo">${dur(x.s)}</td>
          <td class="num" data-l="Média">${nf(x.avg_kmh)} km/h</td><td class="num" data-l="km/L"><b>${nf(x.kml, 1)}</b></td><td class="num" data-l="L/100 km">${nf(x.l100, 1)}</td><td class="num" data-l="Rotação">${nf(x.rpm)}</td>
          <td class="num" data-l="Carga">${nf(x.load)} %</td><td class="num" data-l="Ajuste curto">${x.stft == null ? "—" : (x.stft > 0 ? "+" : "") + nf(x.stft, 1) + " %"}</td><td class="num" data-l="Motor">${nf(x.ect)} °C</td><td data-l="Marchas" style="font-size:12px">${gearTxt(x.gears)}</td></tr>`).join("")}</tbody></table></div>
        ${(rm.monthly || []).length >= 2 ? `<h3 class="lbl" style="margin-top:14px">km/L por mês</h3><svg class="chart" id="gMixChart" viewBox="0 0 600 190" preserveAspectRatio="none" style="margin-top:6px"></svg>
          <div class="gm-legend"><span><i style="background:var(--speed)"></i>cidade</span><span><i style="background:var(--o2)"></i>estrada</span></div>` : ""}
        <div class="muted" style="font-size:12px;margin-top:6px">${rm.trips} viagem(ns) com GPS. ${rm.geo ? "Tipo de via pelo mapa offline (rodovia, ou limite de 80 km/h ou mais, é estrada)." : "Sem mapa offline: o tipo sai da velocidade em janelas de 1 minuto (média de 70 km/h ou mais sem parar = estrada; abaixo de 45 ou com parada = cidade; entre os dois = misto)."}
          Km pelo GPS; litros pelo consumo calculado do motor.</div>`
        : `<div class="empty" style="margin-top:10px">Nenhuma viagem com GPS no período.</div>`}</div>${geoCard(status)}`;
    $("#gMixD", body)?.addEventListener("change", (e) => { try { localStorage.setItem("gpsMixDays", e.target.value); } catch { } render(); });
    const mo = rm.monthly || [];
    if (mo.length >= 2 && U().lineChart) { const t = (m) => new Date(m + "-15T12:00:00").getTime();
      U().lineChart($("#gMixChart", body), [{ pts: mo.map((m) => [t(m.month), m.cidade]), c: "--speed", dots: false }, { pts: mo.map((m) => [t(m.month), m.estrada]), c: "--o2" }],
        { xfmt: (x) => new Date(x).toLocaleDateString("pt-BR", { month: "2-digit", year: "2-digit" }) }); }
  };

  // detalhe da viagem: km e consumo na cidade e na estrada
  TRIP_EXTRA.push(({ card, det }) => {
    let d = null; try { d = det?.gps?.road_json ? JSON.parse(det.gps.road_json) : null; } catch { }
    if (!d?.classes) return;
    const parts = ["cidade", "estrada", "misto"].map((c) => [c, d.classes[c]]).filter(([, x]) => x && x.km >= 0.1).map(([c, x]) => {
      const fl = x.fuel_l || 0, kml = fl > 0.05 && x.km > 0.2 ? x.km / fl : null;
      return `${CLS[c][0]} ${nf(x.km, 1)} km${kml ? ` (${nf(kml, 1)} km/L)` : ""}`; });
    if (!parts.length) return;
    const box = document.createElement("div");
    box.className = "muted"; box.style.cssText = "font-size:13px;margin-top:8px";
    box.innerHTML = `${parts.join(" · ")} <span style="font-size:12px">· ${d.fonte === "perfil" ? "pela velocidade" : "pelo mapa offline"}</span>`;
    card.appendChild(box);
  });

  // ---- F8: rotina — ações dos lugares (9.16), parada em posto e ranking (9.14), trânsito (9.19)
  const ACT_ON = { entrou: "Ao chegar", saiu: "Ao sair", partiu_apos: "Ao partir, se ficou parado ali" };
  const ACT_DO = { avisar: "avisar no celular", falar: "falar no modo direção", sincronizar: "sincronizar com a nuvem", pos_servico: "lembrete do serviço (oficina)", nota: "anotar um texto" };
  function actText(list) {
    const a = (list || []).map((x) => `${ACT_ON[x.on] || x.on}${x.on === "partiu_apos" ? ` ${x.arg || 0} min` : ""}: ${ACT_DO[x.do] || x.do}`);
    return a.length ? `<br>⚡ ${esc(a.join(" · "))}` : "";
  }
  // editor de ações dentro do formulário de lugares; null = lugar novo sem mexer (o Pi põe as ações padrão do tipo)
  function actEdit(box, list) {
    if (!box) return;
    box._acts = list == null ? null : list.map((x) => ({ ...x }));
    const paint = () => {
      const acts = box._acts;
      box.innerHTML = `<div class="lbl" style="margin-top:2px">⚡ Ações deste lugar</div>
        ${acts == null ? `<div class="muted" style="font-size:12px;margin:4px 0">Ao salvar entram as ações padrão do tipo: Casa sincroniza com a nuvem ao chegar; Oficina anota a visita ao sair depois de 20 min parado. Posto e os demais: nenhuma.</div>` : ""}
        ${(acts || []).map((a, i) => `<div class="gpact" style="display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin-top:6px" data-i="${i}">
          <select data-k="on" aria-label="Quando">${Object.entries(ACT_ON).map(([k, l]) => `<option value="${k}"${a.on === k ? " selected" : ""}>${l}</option>`).join("")}</select>
          <select data-k="do" aria-label="O quê">${Object.entries(ACT_DO).map(([k, l]) => `<option value="${k}"${a.do === k ? " selected" : ""}>${l}</option>`).join("")}</select>
          ${a.on === "partiu_apos" ? `<label class="muted" style="font-size:13px">depois de <input type="number" data-k="arg" min="0" max="1440" step="5" value="${esc(a.arg ?? 20)}" style="width:72px" aria-label="Minutos parado"> min</label>` : ""}
          ${a.do === "nota" && a.on !== "partiu_apos" ? `<input type="text" data-k="arg" maxlength="200" placeholder="Texto da anotação" value="${esc(a.arg ?? "")}" style="flex:1 1 160px" aria-label="Texto da anotação">` : ""}
          <button class="rbtn" type="button" data-rm="${i}" aria-label="Tirar esta ação">✕</button></div>`).join("")}
        <button class="rbtn" type="button" data-add style="margin-top:6px">➕ Ação</button>
        <div class="muted" style="font-size:12px;margin-top:4px">"Avisar" manda um aviso pelo ntfy que vale por 30 min. "Falar" só funciona com o modo direção aberto e a localização liberada.</div>`;
    };
    paint();
    box.onclick = (e) => {
      if (e.target.closest("[data-add]")) { box._acts = [...(box._acts || []), { on: "entrou", do: "avisar" }]; paint(); }
      const rm = e.target.closest("[data-rm]"); if (rm) { box._acts.splice(+rm.dataset.rm, 1); paint(); }
    };
    box.onchange = (e) => {
      const row = e.target.closest("[data-i]"); if (!row) return;
      const a = box._acts[+row.dataset.i], k = e.target.dataset.k;
      if (k === "arg") a.arg = e.target.type === "number" ? +e.target.value : e.target.value;
      else { a[k] = e.target.value; if (k === "on") a.arg = a.on === "partiu_apos" ? 20 : undefined; paint(); }
    };
  }
  function actGet(box) {
    if (!box || box._acts == null) return null;
    return box._acts.map((a) => { const x = { on: a.on, do: a.do };
      if (a.on === "partiu_apos") x.arg = Math.max(0, +a.arg || 0); else if (a.do === "nota" && a.arg) x.arg = String(a.arg).slice(0, 200);
      return x; });
  }

  // Consumo: "Abastecimento detectado" (paradas em posto sem tanque registrado) e o ranking de postos
  const brl = (v, d = 2) => v == null ? "—" : "R$ " + nf(v, d);
  async function fuelCard() {
    const el = $("#gpsFuel"); if (!el) return;
    if (!locOpen()) { el.innerHTML = ""; return; }
    let j; try { j = await get("/api/gps/stations"); } catch { el.innerHTML = ""; return; }
    const stops = j.stops || [], sts = (j.stations || []).filter((s) => !s.deleted && (s.n || s.paradas));
    if (!stops.length && !sts.length) { el.innerHTML = ""; return; }
    el.innerHTML = (stops.length ? `<div class="card" style="--s:var(--warn)"><h2>⛽ Abastecimento detectado</h2><div class="lbl" style="margin-top:4px">O carro parou num posto. Abasteceu? Basta o valor pago; o posto fica ligado ao tanque.</div>
      ${stops.map((s) => `<form data-stop="${s.id}" style="margin-top:12px;padding-top:10px;border-top:1px solid var(--line, rgba(127,127,127,.25))">
        <div style="font-weight:600">Parou em ${esc(s.place_name)} por ${s.min} min</div><div class="muted" style="font-size:13px">${dt(s.ts_start)}</div>
        <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-top:8px"><input type="number" inputmode="decimal" step="0.01" min="1" max="5000" placeholder="R$ pago" aria-label="Valor pago (R$)" style="flex:1 1 110px;max-width:160px" required>
          <button class="btn" type="submit">Registrar</button><button class="rbtn" type="button" data-no="${s.id}">Não abasteci</button></div>
        <div class="muted" data-msg style="font-size:13px;margin-top:4px" role="status"></div></form>`).join("")}</div>` : "")
      + (sts.length ? `<div class="card"><h2>🏆 Postos</h2><div class="lbl" style="margin-top:4px">Quanto rende o combustível de cada posto: o km/L medido no tanque seguinte, comparado com a sua média do mesmo combustível</div>
      <div class="tbl" style="margin-top:10px"><table class="cards"><thead><tr><th>Posto</th><th>Vezes</th><th>Último R$/L</th><th>Rendimento</th><th>Ajuste curto</th></tr></thead><tbody>
      ${sts.map((s) => `<tr><td class="head"><b>${esc(s.name)}</b><br><span class="muted" style="font-size:12px">${esc(s.opiniao)}</span></td><td class="num" data-l="Vezes">${s.n}</td>
        <td class="num" data-l="Último R$/L">${s.last_price ? nf(s.last_price, 2) : "—"}</td>
        <td class="num" data-l="Rendimento">${s.kml.avg != null ? nf(s.kml.avg, 1) + " km/L" : "—"}${s.kml.vs_media_pct != null ? `<br><b style="color:var(${s.kml.vs_media_pct >= 2 ? "--ok" : s.kml.vs_media_pct <= -2 ? "--bad" : "--muted"})">${s.kml.vs_media_pct > 0 ? "+" : ""}${nf(s.kml.vs_media_pct, 1)} %</b>` : ""}</td>
        <td class="num" data-l="Ajuste curto">${s.stft?.delta != null ? (s.stft.delta > 0 ? "+" : "") + nf(s.stft.delta, 1) + " pts" : "—"}</td></tr>`).join("")}</tbody></table></div>
      <div class="muted" style="font-size:12px;margin-top:6px">Precisa de ${j.min_tanks || 2} tanques cheios medidos no mesmo posto para opinar. "Ajuste curto": quanto a correção da injeção muda nos 20 km depois de abastecer ali, contra os 20 km antes (combustível ruim mexe nela).</div></div>` : "");
    el.querySelectorAll("form[data-stop]").forEach((f) => f.addEventListener("submit", async (e) => {
      e.preventDefault();
      const id = +f.dataset.stop, s = stops.find((x) => x.id === id), total = +f.querySelector("input").value, msg = f.querySelector("[data-msg]");
      try {
        // no Pi: o "Tanque cheio" normal com a parada; no painel online o formulário público não leva a parada, então vai pela fila da localização
        const j2 = cloud() ? await post(`/api/gps/fuelstop/${id}`, { status: "confirmado", total })
          : await post("/api/fuel/fill", { total, full: true, ts: (s.ts_end || Date.now()) - 60000, stop_id: id });
        msg.textContent = j2.queued ? "✅ Pedido enviado; o Pi registra o tanque quando conectar." : `✅ Tanque cheio de R$ ${nf(total, 2)} registrado em ${s.place_name}.`;
        setTimeout(() => window.dispatchEvent(new HashChangeEvent("hashchange")), 1200);
      } catch (err) { msg.textContent = "Não foi possível salvar: " + err.message; }
    }));
    el.querySelectorAll("[data-no]").forEach((b) => b.addEventListener("click", async () => {
      const msg = b.closest("form").querySelector("[data-msg]");
      try { const j2 = await post(`/api/gps/fuelstop/${b.dataset.no}`, { status: "descartado" }); msg.textContent = j2.queued ? "Ok; o Pi esquece esta parada quando conectar." : "Ok, esquecida."; if (!j2.queued) setTimeout(fuelCard, 800); }
      catch (err) { msg.textContent = "Erro: " + err.message; }
    }));
  }
  const fuelHook = () => { if ((location.hash || "") === "#consumo") fuelCard().catch(console.error); };
  window.addEventListener("hashchange", fuelHook);
  setTimeout(fuelHook, 600);

  // Trânsito: por rota e horário de saída, melhor horário, onde o carro fica parado
  if (tabs && !$('[data-g="transito"]', tabs)) tabs.insertAdjacentHTML("beforeend", `<button type="button" class="rbtn" data-g="transito">🚦 Trânsito</button>`);
  try { sub = localStorage.getItem("gpsSub") || sub; } catch { }
  if (!["diario", "mapa", "lugares", "rotas", "trechos", "desempenho", "vias", "transito"].includes(sub)) sub = "diario";
  const mins = (s) => s == null ? "—" : `${nf(s / 60, 0)} min`;
  const DAYK = { "útil": "Dias úteis", "sábado": "Sábados", "domingo": "Domingos" };
  EXTRA.transito = async (body) => {
    const tr = await get("/api/gps/traffic?days=60");
    const ok = (tr.routes || []).filter((r) => r.status === "ok"), few = (tr.routes || []).filter((r) => r.status !== "ok");
    const tot = tr.total || {};
    body.innerHTML = `<div class="card"><h2>🚦 Trânsito</h2><div class="lbl" style="margin-top:4px">Aprendido só com as suas viagens (nenhum serviço de fora): quanto tempo e combustível o trânsito leva, e a melhor hora de sair</div>
      ${tot.trips ? `<div class="stats" style="margin-top:10px"><div><span class="lbl">Parado no trânsito</span><b>${dur(tot.stopped_s)}</b><span class="muted" style="font-size:12px">motor ligado · ${nf(tot.stopped_l, 1)} L</span></div>
        <div><span class="lbl">Arrastando (3–15 km/h)</span><b>${dur(tot.crawl_s)}</b><span class="muted" style="font-size:12px">${nf(tot.crawl_l, 1)} L</span></div>
        <div><span class="lbl">Viagens com GPS</span><b>${tot.trips}</b><span class="muted" style="font-size:12px">últimos ${tr.days} dias</span></div></div>` : `<div class="empty" style="margin-top:10px">Nenhuma viagem com GPS nos últimos ${tr.days} dias.</div>`}</div>
      ${ok.map((r) => `<div class="card"><h2>🛣️ ${esc(r.name)}</h2><div style="font-weight:600;margin-top:8px">${esc(r.texto)}</div>
        <div class="stats" style="margin-top:10px"><div><span class="lbl">Tempo típico</span><b>${mins(r.dur_s)}</b><span class="muted" style="font-size:12px">sem trânsito ${mins(r.free_s)}</span></div>
          <div><span class="lbl">Atraso típico</span><b>${mins(r.delay_s)}</b></div><div><span class="lbl">Parado por viagem</span><b>${mins(r.stopped_s)}</b><span class="muted" style="font-size:12px">${nf(r.stopped_l, 2)} L</span></div>
          <div><span class="lbl">Viagens</span><b>${r.n}</b></div></div>
        ${Object.entries(r.por_dia || {}).map(([k, d]) => `<h3 class="lbl" style="margin-top:12px">${DAYK[k] || k} · ${d.n} viagem(ns)</h3><div class="tbl" style="margin-top:6px"><table class="cards"><thead><tr><th>Saída</th><th>Viagens</th><th>Tempo</th><th>Atraso</th><th>Litros</th></tr></thead><tbody>
          ${d.faixas.map((f) => `<tr><td class="head">${esc(f.faixa)}<br><span class="muted" style="font-size:12px">em geral às ${esc(f.saida)}</span></td><td class="num" data-l="Viagens">${f.n}</td><td class="num" data-l="Tempo">${mins(f.dur_s)}</td><td class="num" data-l="Atraso">${mins(f.delay_s)}</td><td class="num" data-l="Litros">${nf(f.fuel_l, 2)}</td></tr>`).join("")}</tbody></table></div>`).join("")}</div>`).join("")}
      ${few.length ? `<div class="card"><h2>Rotas com poucos dados</h2><div class="muted" style="font-size:13px;margin-top:6px">${few.map((r) => `${esc(r.name)}: ${r.n} viagem(ns)`).join(" · ")}. A análise começa com 6 viagens na mesma rota.</div></div>` : ""}
      <div class="card"><h2>📍 Onde o carro fica parado</h2><div class="lbl" style="margin-top:4px">Pontos em que a velocidade costuma ficar bem abaixo do normal daquele lugar (semáforo, retorno, gargalo)</div>
        ${(tr.hotspots || []).length ? `<div class="gmap sm" id="gTMap" style="margin-top:10px"></div>` : `<div class="empty" style="margin-top:10px">Ainda sem pontos repetidos (precisa de 3 viagens passando no mesmo lugar).</div>`}</div>`;
    const hs = tr.hotspots || [];
    if (!hs.length) return;
    const map = M().create($("#gTMap", body)); if (!map) return;
    maps.push(map);
    const L_ = window.L, b = L_.latLngBounds([]);
    for (const [la, lo, med, p85, n] of hs) {
      L_.circleMarker([la, lo], { radius: 7, color: "#ff4d5e", weight: 1, fillOpacity: 0.55 }).bindTooltip(`${nf(med)} km/h em geral (normal ali: ${nf(p85)} km/h) · ${n} passagens`).addTo(map);
      b.extend([la, lo]);
    }
    M().fit(map, b);
  };

  // detalhe da viagem: tempo parado e arrastando no trânsito
  TRIP_EXTRA.push(({ card, det }) => {
    let x = null; try { x = det?.gps?.traffic_json ? JSON.parse(det.gps.traffic_json) : null; } catch { }
    if (!x || !(x.stopped_s >= 30 || x.crawl_s >= 30)) return;
    const box = document.createElement("div");
    box.className = "muted"; box.style.cssText = "font-size:13px;margin-top:8px";
    box.textContent = `🚦 Parado com o motor ligado ${dur(x.stopped_s)} (${nf(x.stopped_l, 2)} L)` + (x.crawl_s >= 20 ? ` · arrastando ${dur(x.crawl_s)} (${nf(x.crawl_l, 2)} L)` : "");
    card.appendChild(box);
  });

  // ---- F9: compartilhar a viagem ao vivo (seção 9.21) — link com validade que abre no celular de quem recebe
  if (tabs && !$('[data-g="compartilhar"]', tabs)) tabs.insertAdjacentHTML("beforeend", `<button type="button" class="rbtn" data-g="compartilhar">📤 Compartilhar</button>`);
  try { if (localStorage.getItem("gpsSub") === "compartilhar") sub = "compartilhar"; } catch { }
  const SH_MODES = [["24h", "24 horas"], ["sempre", "Até eu cancelar"]];        // decisão do dono: só estas duas na tela
  const SH_MODE = { sempre: "sem prazo", viagem: "até o fim da viagem", tempo: "prazo fixo" };
  const shUntil = (x) => x.mode === "sempre" || x.expires_ts == null ? "♾️ sem prazo — até você cancelar" : "até " + dt(x.expires_ts);
  const SH_STATE = { ativo: ["--ok", "Ativo"], enviado: ["--ok", "Ativo"], "aguardando a viagem": ["--warn", "Aguardando a viagem"], pendente: ["--warn", "Enviando para a nuvem…"],
    encerrando: ["--muted", "Encerrando…"], encerrado: ["--muted", "Encerrado"], vencido: ["--muted", "Venceu"], "viagem terminou": ["--muted", "A viagem terminou"], recusado: ["--bad", "Recusado pela nuvem"] };
  // folha de compartilhar do iPhone; sem ela (ou se o navegador recusar depois da espera), copia o link
  async function shareSend(url) {
    if (navigator.share) {
      try { await navigator.share({ title: "Delfos · viagem ao vivo", text: "Acompanhe a viagem do Civic ao vivo:", url }); return "ok"; }
      catch (e) { if (e && e.name === "AbortError") return "cancelado"; }
    }
    try { await navigator.clipboard.writeText(url); return "copiado"; } catch { return "manual"; }
  }
  const SH_SENT = { ok: "✅ Link enviado.", cancelado: "", copiado: "📋 Link copiado: cole na conversa.", manual: "Copie o link acima e envie." };
  function sharedBox(box, j) {
    box.innerHTML = `<div style="margin-top:12px;padding-top:10px;border-top:1px solid var(--line)"><div style="font-weight:600">Link criado (${j.mode === "sempre" ? "sem prazo: vale até você encerrar aqui" : "vale " + shUntil(j)}). Ele só aparece agora: envie ou guarde.</div>
      <input type="text" readonly value="${esc(j.url)}" aria-label="Link de compartilhamento" style="width:100%;margin-top:8px" id="shUrl">
      <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:8px"><button class="btn" type="button" id="shSend">📤 Enviar o link</button><button class="rbtn" type="button" id="shCopy">📋 Copiar</button></div>
      <div class="muted" id="shMsg" style="font-size:13px;margin-top:6px" role="status">${j.pending ? "Começa a valer em até 10 s, quando o Pi enviar para a nuvem." : ""}</div></div>`;
    const msg = (t) => { if (t) $("#shMsg", box).textContent = t; };
    $("#shSend", box).addEventListener("click", async () => msg(SH_SENT[await shareSend(j.url)]));
    $("#shCopy", box).addEventListener("click", async () => { try { await navigator.clipboard.writeText(j.url); msg(SH_SENT.copiado); } catch { $("#shUrl", box).select(); msg(SH_SENT.manual); } });
  }
  EXTRA.compartilhar = async (body) => {
    const j = await get("/api/gps/share");
    const pi = !cloud(), links = j.links || [];
    const why = pi && !j.cloud ? "Compartilhar precisa do painel online configurado ([cloud] url e token no Pi): o link é servido por ele."
      : pi && j.loc === false ? "O painel online está sem a senha de localização (LOC_PASSWORD). Sem ela nenhuma posição sai do Pi, nem para um link." : "";
    const act = links.filter((x) => ["ativo", "enviado", "aguardando a viagem", "pendente"].includes(x.state));
    body.innerHTML = `<div class="card"><h2>📤 Compartilhar a viagem</h2>
      <div class="muted" style="margin-top:6px;font-size:14px">Quem recebe o link vê em tempo real o carro andando no mapa e o trajeto desta viagem, com velocidade, rotação, temperatura do motor, consumo, tempo e km, e o aviso do carro se houver. Não vê lugares salvos, onde o carro estaciona, outras viagens nem o resto do painel. Não precisa de senha nem de instalar nada.</div>
      ${why ? `<div class="empty" style="margin-top:10px">${esc(why)}</div>` : `<form id="shForm" style="margin-top:12px;display:grid;gap:10px">
        <div role="radiogroup" aria-label="Validade" style="display:flex;gap:8px;flex-wrap:wrap">${SH_MODES.map(([k, l], i) => `<label class="rbtn" style="display:inline-flex;gap:6px;align-items:center"><input type="radio" name="shMode" value="${k}"${i ? "" : " checked"}> ${l}</label>`).join("")}</div>
        <label class="muted" style="font-size:13px"><input type="checkbox" id="shDest"> Mostrar o nome do destino (ex.: "Casa") junto da chegada prevista. Desligado, aparece só "chegada prevista 14:35".</label>
        <input type="text" id="shLabel" maxlength="40" placeholder="Nome do link (opcional, ex.: Para a Ana)" aria-label="Nome do link">
        <div><button class="btn" type="submit">📤 Compartilhar viagem</button></div>
        <div class="muted" style="font-size:12px">"Até eu cancelar" não vence sozinho: fica valendo até você tocar em Encerrar aqui. Com o carro desligado a página mostra só "Carro desligado" e a hora do último sinal, nunca onde ele parou; na viagem seguinte volta o trajeto dela. Depois que um link acaba, a página de quem recebeu para de mostrar o carro. No máximo ${j.max || 5} links ativos.</div>
      </form><div id="shOut" role="status"></div>`}</div>
      ${act.some((x) => x.mode === "sempre") ? `<div class="banner" style="--s:var(--warn)"><span class="ic">♾️</span><div><b>${act.filter((x) => x.mode === "sempre").length} link(s) sem prazo ativo(s).</b> Quem tem o link vê as suas viagens até você encerrar.</div></div>` : ""}
      <div class="card"><h2>Links</h2>${links.length ? `<div class="tbl" style="margin-top:10px"><table class="cards"><thead><tr><th>Link</th><th>Validade</th><th>Visto</th><th>Situação</th><th></th></tr></thead><tbody>
        ${links.map((x) => { const s = SH_STATE[x.state] || ["--muted", x.state || "—"], live = act.includes(x);
          return `<tr><td class="head"><b>${esc(x.label || "Link " + x.id)}</b><br><span class="muted" style="font-size:12px">${esc(SH_MODE[x.mode] || x.mode || "")} · criado ${dt(x.created_ts)}${x.src === "nuvem" && pi ? " no painel online" : ""}${x.dest ? " · mostra o destino" : ""}</span></td>
            <td class="num" data-l="Validade">${x.mode === "sempre" && live ? `<b style="color:var(--warn)">${shUntil(x)}</b>` : shUntil(x)}</td><td class="num" data-l="Visto">${x.views ? x.views + "×" : "—"}</td>
            <td data-l="Situação"><b style="color:var(${s[0]})">${esc(s[1])}</b></td>
            <td>${live ? `<button class="rbtn" type="button" data-shx="${esc(x.id)}">Encerrar</button>` : ""}</td></tr>`; }).join("")}</tbody></table></div>`
        : `<div class="empty" style="margin-top:10px">Nenhum link ativo nem criado nas últimas 24 h.</div>`}</div>`;
    const f = $("#shForm", body);
    f?.addEventListener("submit", async (e) => {
      e.preventDefault();
      const out = $("#shOut", body), btn = f.querySelector("button[type=submit]");
      btn.disabled = true; out.textContent = "Criando o link…";
      try {
        const r = await post("/api/gps/share", { mode: f.querySelector("input[name=shMode]:checked").value, dest: $("#shDest", body).checked, label: $("#shLabel", body).value });
        sharedBox(out, r);
        const how = await shareSend(r.url);                 // o iPhone pode recusar depois da espera: o botão "Enviar o link" fica
        await EXTRA.compartilhar(body);                      // lista atualizada, com o link novo
        sharedBox($("#shOut", body), r);
        if (SH_SENT[how]) $("#shMsg", body).textContent = SH_SENT[how];
        return;
      } catch (err) { out.textContent = err instanceof Locked ? "Localização trancada: desbloqueie para criar o link." : "Não foi possível criar: " + err.message; }
      btn.disabled = false;
    });
    body.querySelectorAll("[data-shx]").forEach((b) => b.addEventListener("click", async () => {
      b.disabled = true;
      try { await post(`/api/gps/share/${b.dataset.shx}`, { revoke: true }); await EXTRA.compartilhar(body); }
      catch (err) { b.disabled = false; b.textContent = "Erro: " + err.message; }
    }));
  };

  window.obdGps = { render, trip, openTrip, lock: { html: lockedHtml, bind: bindLock } };   // lock: reaproveitado no mapa do Ao vivo (vivo.js)
})();
