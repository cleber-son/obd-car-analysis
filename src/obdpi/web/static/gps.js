/* window.obdGps — aba Mapa (Diário, Mapa, Lugares, Rotas, Trechos), trajeto no detalhe da viagem e replay sincronizado.
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
        <td class="num" data-l="Média">${nf(x.avg_moving ?? x.avg_speed)} km/h</td><td class="num" data-l="Consumo">${nf(x.km_l, 1)}<span class="only-m"> km/L</span></td>
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
      <div class="gchecks"><label><input type="checkbox" id="gAl" checked> Alertas</label><label><input type="checkbox" id="gPl" checked> Lugares</label><label><input type="checkbox" id="gPk" checked> Estacionado</label><label><input type="checkbox" id="gLv" checked> Agora</label></div>
      <div class="gmap" id="gMap" style="margin-top:10px"></div><div class="gm-legend" id="gLeg"></div>
      <div id="gAlBox" style="margin-top:12px"></div></div>`;
    const map = M().create($("#gMap"));
    if (!map) return;
    maps.push(map);
    const L_ = window.L, layers = { base: L_.layerGroup().addTo(map), al: L_.layerGroup().addTo(map), pl: L_.layerGroup().addTo(map), pk: L_.layerGroup().addTo(map) };
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
        <div><div class="t">${esc(p.name)}${p.kind === "auto" ? ' <span class="pill">dê um nome</span>' : ""}</div><div class="d">raio ${nf(p.radius_m)} m · ${p.visits || 0} visita(s)${p.last_visit_ts ? " · última " + dt(p.last_visit_ts) : ""}</div></div>
        <span style="display:flex;gap:6px"><button class="rbtn" type="button" data-edit="${p.id}">Editar</button><button class="rbtn" type="button" data-del="${p.id}">Apagar</button></span></div>`).join("")
      : `<div class="empty">Nenhum lugar salvo. Toque no mapa ou use a posição atual para criar "Casa" e "Trabalho".</div>`;
    const reset = () => { $("#gPid").value = ""; $("#gPName").value = ""; $("#gPRad").value = 80; $("#gPNew").hidden = true; pos = null; mark = M().cursor(map, null, null, null, mark); $("#gPPos").textContent = "Sem posição escolhida"; };
    $("#gPList").addEventListener("click", async (e) => {
      const ed = e.target.closest("[data-edit]"), del = e.target.closest("[data-del]");
      if (ed) { const p = list.find((x) => x.id === +ed.dataset.edit); $("#gPid").value = p.id; $("#gPName").value = p.kind === "auto" ? "" : p.name; $("#gPKind").value = kinds.includes(p.kind) ? p.kind : "outro"; $("#gPRad").value = p.radius_m; setPos(p.lat, p.lon); map.setView([p.lat, p.lon], 16); $("#gPNew").hidden = false; $("#gPName").focus(); }
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

  window.obdGps = { render, trip, openTrip };
})();
