/* window.obdVivo — aba Ao vivo: autonomia do tanque, último abastecimento e o mapa de onde o carro está agora, com o
   trajeto desde que o motor foi ligado ("Saída HH:MM"). Usa window.OBD (app.js), window.obdMap (gpsmap.js) e a trava de
   localização do window.obdGps (gps.js). Leve para o Pi 3B: uma consulta de posição por segundo no máximo, só com esta
   tela aberta; o trajeto completo vem uma vez por viagem (e a cada 2 min) e o resto é acrescentado ponto a ponto.
   Sem GPS: "GPS não conectado" e nada de mapa. Na nuvem com a localização trancada: o pedido de senha da aba Mapa,
   sem pedir nenhuma posição. */
(() => {
  "use strict";
  const $ = (s, r = document) => r.querySelector(s);
  const U = () => window.OBD || {};
  const esc = (s) => (U().esc || String)(s);
  const nf = (v, d = 0) => U().nf ? U().nf(v, d) : String(v ?? "—");
  const dt = (ts) => U().dt ? U().dt(ts) : String(ts);
  const tm = (ts) => U().tm ? U().tm(ts) : String(ts);
  const M = () => window.obdMap;
  const G = () => window.obdGps || {};
  const cloud = () => (U().cloud ? U().cloud() : null);
  const locOpen = () => { const c = cloud(); return !c || c.loc === "aberta"; };
  const visible = () => { const v = $("#v-vivo"); return !!v && !v.hidden && document.visibilityState !== "hidden"; };
  const FUEL = { gasolina: "Gasolina", etanol: "Etanol" };

  let D = null;              // último quadro ao vivo (público: sem posição)
  let fuel = null, fuelAt = 0, places = null, placesAt = 0;
  let tankAt = 0;

  // ------------------------------------------------------------ tanque
  async function loadFuel(force = false) {
    if (!force && fuel && Date.now() - fuelAt < 60e3) return fuel;
    const r = await fetch("/api/fuel", { cache: "no-store" }).catch(() => null);
    if (r && r.ok) { fuel = await r.json().catch(() => fuel); fuelAt = Date.now(); }
    return fuel;
  }
  // nome do posto (F8): os lugares são dado de localização; na nuvem só com a senha
  async function loadPlaces() {
    if (!locOpen() || (places && Date.now() - placesAt < 300e3)) return places;
    const r = await fetch("/api/gps/places", { cache: "no-store" }).catch(() => null);
    if (r && r.ok) { const j = await r.json().catch(() => null); if (j) { places = {}; for (const p of j.places || []) places[p.id] = p.name; placesAt = Date.now(); } }
    return places;
  }
  // a mesma conta do quadro "Autonomia" do modo direção (drive.html, CAT.auto)
  function autonomy(d, f) {
    const t = d?.trip || {}, g = d?.gps || {};
    const rest = f?.fills?.length ? Math.max(0, f.tank_l - (f.since_fill?.liters_est || 0) - (t.fuel_l || 0)) : null;
    if (g.range_km != null) return { km: g.range_km, rest, how: "pelo GPS: consumo desde o tanque cheio" };
    if (!f?.fills?.length) return null;
    const kml = f.avg_real_kml ?? t.kml ?? f.since_fill?.kml_est;
    return { km: kml ? rest * kml : null, rest, kml, how: kml ? `a ${nf(kml, 1)} km/L` : "" };
  }
  function renderTank() {
    const box = $("#vivoTank"); if (!box) return;
    tankAt = Date.now();
    const f = fuel, a = autonomy(D, f);
    const last = f?.fills?.[0];
    const lvl = a?.rest != null && f?.tank_l ? Math.max(0, Math.min(1, a.rest / f.tank_l)) : null;
    const low = a?.km != null && a.km < 60;
    const c = low ? "var(--bad)" : lvl != null && lvl < 0.25 ? "var(--warn)" : "var(--ok)";
    let fill;
    if (!last) fill = `<div class="muted">Nenhum abastecimento registrado. <a href="#consumo">Registrar tanque cheio</a></div>`;
    else {
      const posto = last.place_id != null && places ? places[last.place_id] : null;
      const parts = [dt(last.ts), last.liters ? nf(last.liters, last.liters % 1 ? 1 : 0) + " L" : last.total ? "R$ " + nf(last.total, 2) : null,
        last.price_l ? "R$ " + nf(last.price_l, 2) + "/L" : null].filter(Boolean);
      fill = `<div class="vt-fill"><span class="lbl">Último abastecimento</span><b class="num">${esc(parts.join(" · "))}</b>
        <span class="muted">${esc([FUEL[last.fuel] || last.fuel, last.full === 0 ? "não completou" : null, posto].filter(Boolean).join(" · "))}${last.pending ? " · aguardando o Pi" : ""}</span></div>`;
    }
    box.innerHTML = `<div class="vt-head"><div><span class="lbl">Autonomia do tanque</span>
        <div class="vt-km num" style="color:${a?.km != null ? c : "var(--muted)"}">${a?.km != null ? "≈ " + nf(a.km, 0) + "<small> km</small>" : "—"}</div>
        <div class="muted vt-sub">${a ? [a.rest != null ? `≈ ${nf(a.rest, 0)} L no tanque` : null, a.how].filter(Boolean).join(" · ") : f ? "registre um tanque cheio" : "carregando…"}</div></div>
        ${lvl != null ? `<div class="vt-gauge" role="img" aria-label="Tanque com cerca de ${Math.round(lvl * 100)}%"><i style="height:${Math.round(lvl * 100)}%;background:${c}"></i></div>` : ""}</div>
      ${fill}`;
  }

  // ------------------------------------------------------------ mapa ao vivo
  const S = { map: null, line: null, start: null, cur: null, follow: true, trip: null, trkAt: 0, remote: false, lastFetch: 0, busy: false,
              timer: null, pos: null, first: true, locked: false };
  const killMap = () => { if (S.map) { try { S.map.remove(); } catch { } } Object.assign(S, { map: null, line: null, start: null, cur: null, trip: null, first: true }); };

  function shell() {
    const box = $("#vivoMap"); if (!box) return null;
    if (!box.dataset.ok) {
      box.dataset.ok = "1";
      box.innerHTML = `<div class="row-head"><div><h2>Onde estou</h2><div class="lbl" style="margin-top:4px" id="vmSub">Trajeto desde que ligou o carro</div></div>
        <div class="gctl" id="vmCtl" hidden><button class="rbtn" type="button" id="vmFollow" aria-pressed="true">◎ Seguir o carro</button>
        <button class="rbtn" type="button" id="vmAll">Ver trajeto</button></div></div>
        <div id="vmBody" style="margin-top:10px"></div><div class="gm-legend" id="vmLeg"></div>`;
      $("#vmFollow", box).addEventListener("click", () => { S.follow = !S.follow; syncFollow(); if (S.follow && S.pos) S.map?.panTo(S.pos); });
      $("#vmAll", box).addEventListener("click", () => { S.follow = false; syncFollow(); fitAll(); });
    }
    return box;
  }
  const syncFollow = () => { const b = $("#vmFollow"); if (b) b.setAttribute("aria-pressed", String(S.follow)); };
  function placeholder(html) {
    const body = $("#vmBody"); if (!body) return;
    if (S.map) killMap();
    $("#vmCtl").hidden = true; $("#vmLeg").innerHTML = "";
    if (body.dataset.ph !== html) { body.dataset.ph = html; body.innerHTML = html; }
  }
  function ensureMap() {
    if (S.map && !S.map._gone) return S.map;
    const body = $("#vmBody"); delete body.dataset.ph;
    body.innerHTML = `<div class="gmap vmap" id="vmMap"></div>`;
    const map = M()?.create($("#vmMap"), { zoom: 15 });
    if (!map) return null;
    S.map = map; S.first = true;
    S.line = L.polyline([], { color: "#2ec8ff", weight: 5, opacity: 0.9, interactive: false }).addTo(map);
    map.on("dragstart", () => { if (S.follow) { S.follow = false; syncFollow(); } });
    $("#vmCtl").hidden = false;
    return map;
  }
  function fitAll() {
    if (!S.map) return;
    const pts = S.line ? S.line.getLatLngs() : [];
    if (pts.length > 1) M().fit(S.map, L.latLngBounds(pts).extend(S.pos || pts[pts.length - 1]), 17);
    else if (S.pos) S.map.setView(S.pos, 16);
  }
  const far = (a, b, m) => { if (!a || !b) return true; const k = Math.cos(a[0] * Math.PI / 180), dy = (a[0] - b[0]) * 111320, dx = (a[1] - b[1]) * 111320 * k; return dx * dx + dy * dy > m * m; };

  // trajeto da viagem aberta desde a partida (um ponto a cada ~20 m, só esta viagem). No Pi vem do banco; na nuvem é o
  // documento que o obd-sync acrescenta a cada ~30 s (atrás da senha de localização, como todo /api/gps/*)
  async function loadTrack(tid, startTs) {
    S.trkAt = Date.now();
    const r = await fetch("/api/gps/current_track", { cache: "no-store" }).catch(() => null);
    if (!S.map || S.trip !== tid) return;
    if (r && r.status === 401) { S.locked = true; return; }
    const j = r && r.ok ? await r.json().catch(() => null) : null;
    // ainda sem o trajeto desta viagem (nuvem esperando o Pi): vai acumulando daqui e tenta de novo em 30 s
    if (!j || j.trip !== tid || !Array.isArray(j.pts) || !j.pts.length) { S.remote = false; return; }
    S.remote = true;
    const pts = j.pts.map((p) => [p[0], p[1]]);
    if (S.pos && (!pts.length || far(pts[pts.length - 1], S.pos, 5))) pts.push(S.pos);
    S.line.setLatLngs(pts);
    if (pts.length) startMark(pts[0], j.start_ts || startTs || j.pts[0][2]);
    if (S.first && pts.length > 1) { S.first = false; if (!S.follow) fitAll(); }
  }
  function startMark(ll, ts, label = null) {
    const html = `<span class="vm-start">${esc(label || "Saída " + (ts ? tm(ts) : ""))}</span>`;
    const icon = L.divIcon({ className: "vm-startpin", html: `<i></i>${html}`, iconSize: [14, 14], iconAnchor: [7, 7] });
    if (S.start) { S.start.setLatLng(ll); S.start.setIcon(icon); }
    else S.start = L.marker(ll, { icon, interactive: false, keyboard: false, zIndexOffset: 500 }).addTo(S.map);
  }

  async function tick() {
    S.timer = null;
    if (!visible()) return;                              // volta a rodar quando a aba Ao vivo abrir de novo (load)
    const on = !!D?.engine_on;
    schedule(on ? 1000 : 5000);
    if (S.busy || Date.now() - S.lastFetch < 900) return;
    if (!locOpen() || S.locked) return lockedView();
    const gs = D?.gps?.state;
    if (D && (!D.gps || gs === "desligado" || gs === "desconectado") && !S.map) {
      return placeholder(`<div class="empty vm-ph">📡 ${gs === "desligado" ? "GPS desligado na configuração." : "GPS não conectado."}</div>`);
    }
    S.busy = true; S.lastFetch = Date.now();
    let g = null;
    try {
      const r = await fetch("/api/gps/live", { cache: "no-store" });
      if (r.status === 401) { const c = cloud(); const j = await r.json().catch(() => ({})); if (c && j.locked) c.loc = j.configured ? "trancada" : "desativada"; S.locked = true; return lockedView(); }
      g = r.ok ? await r.json() : null;
    } catch { g = null; } finally { S.busy = false; }
    if (!visible()) return;
    draw(g);
  }
  function schedule(ms) { if (!S.timer) S.timer = setTimeout(tick, ms); }

  function draw(g) {
    const fresh = g && g.lat != null && g.state === "ok" && (g.age_s == null || g.age_s < 120);
    const pk = !fresh && g?.parked && g.parked.lat != null ? g.parked : null;
    const on = !!(D?.engine_on ?? g?.engine_on);
    const tid = on ? g?.trip_id ?? null : null;
    const sub = $("#vmSub");
    if (!fresh && !pk) {
      if (!S.map) {
        const st = g?.state;
        return placeholder(`<div class="empty vm-ph">📡 ${!g || st === "coletor_parado" ? "Posição indisponível agora." : st === "desconectado" ? "GPS não conectado." : st === "desligado" ? "GPS desligado na configuração." : "Procurando satélites…"}</div>`);
      }
      S.cur = M().cursor(S.map, null, null, null, S.cur);
      if (sub) sub.textContent = "Sem sinal do GPS agora: última posição conhecida";
      return;
    }
    const map = ensureMap(); if (!map) return;
    const pos = fresh ? [g.lat, g.lon] : [pk.lat, pk.lon];
    // viagem nova (ou motor desligado): recomeça o traçado
    if (tid !== S.trip) {
      S.trip = tid; S.line.setLatLngs([]); if (S.start) { S.start.remove(); S.start = null; } S.first = true; S.follow = true; syncFollow();
      if (tid != null) loadTrack(tid, D?.trip_start).catch(() => { });
    } else if (tid != null && Date.now() - S.trkAt > (S.remote ? 120e3 : 30e3)) loadTrack(tid, D?.trip_start).catch(() => { });
    if (tid != null && fresh) {
      const pts = S.line.getLatLngs(), lastp = pts.length ? [pts[pts.length - 1].lat, pts[pts.length - 1].lng] : null;
      if (far(lastp, pos, 15)) S.line.addLatLng(pos);
      if (!S.remote && !S.start && pts.length === 0) startMark(pos, null, "desde aqui");   // nuvem: começa onde a tela abriu
    }
    S.pos = pos;
    $("#vmAll").hidden = tid == null;
    S.cur = M().cursor(map, pos[0], pos[1], fresh ? g.heading : null, S.cur);
    if (S.first) { S.first = false; map.setView(pos, 16); }
    else if (S.follow) { const c = map.getCenter(); if (far([c.lat, c.lng], pos, 8)) map.panTo(pos, { animate: false }); }
    if (sub) sub.textContent = tid != null ? (S.remote ? "Trajeto desde que ligou o carro" : "Trajeto desde que esta tela abriu (o da saída chega em instantes)")
      : pk ? "Carro estacionado" + (pk.name ? " · " + pk.name : "") : "Posição agora (motor desligado)";
    const t = D?.trip || {}, leg = [];
    if (tid != null && D?.trip_start) leg.push(`<span><i class="vm-sdot"></i>Saída ${esc(tm(D.trip_start))}</span>`);
    if (tid != null && t.km != null) leg.push(`<span class="num">${nf(t.km, 1)} km rodados</span>`);
    if (fresh && g.speed != null) leg.push(`<span class="num">${nf(g.speed)} km/h</span>`);
    if (fresh && g.sats != null) leg.push(`<span class="muted">${g.sats} satélites</span>`);
    $("#vmLeg").innerHTML = leg.join("");
  }

  function lockedView() {
    const body = $("#vmBody"); if (!body) return;
    if (S.map) killMap();
    $("#vmCtl").hidden = true; $("#vmLeg").innerHTML = "";
    const lk = G().lock;
    if (body.dataset.ph === "lock") return;
    body.dataset.ph = "lock";
    body.innerHTML = lk ? lk.html().replace(/^<div class="card">/, '<div class="vm-lock">') : `<div class="empty vm-ph">🔒 Localização protegida: destrave na aba <a href="#mapa">Mapa</a>.</div>`;
    lk?.bind(body, () => { S.locked = false; places = null; delete body.dataset.ph; body.innerHTML = ""; S.lastFetch = 0; loadPlaces().then(renderTank).catch(() => { }); tick(); });
  }

  // ------------------------------------------------------------ entradas (app.js)
  async function load() {
    shell();
    if (S.locked && locOpen()) S.locked = false;
    renderTank();
    await loadFuel(true);
    renderTank();
    await loadPlaces();
    renderTank();
    clearTimeout(S.timer); S.timer = null; tick();
  }
  function frame(d) {
    D = d;
    if (!visible()) return;
    if (Date.now() - tankAt > 2000) renderTank();          // texto leve, no máximo a cada 2 s
    if (!S.timer && !S.busy) schedule(0);
  }
  document.addEventListener("visibilitychange", () => { if (visible() && !S.timer) schedule(0); });

  window.obdVivo = { load, frame };
})();
