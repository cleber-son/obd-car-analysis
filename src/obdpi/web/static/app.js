/* Painel Civic OBD — JS puro, sem build. Dados: /api/* e SSE /api/stream. */
(() => {
  "use strict";
  const $ = (s) => document.querySelector(s);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const nf = (v, d = 0) => (v === null || v === undefined || Number.isNaN(v)) ? "—" : Number(v).toLocaleString("pt-BR", { minimumFractionDigits: d, maximumFractionDigits: d });
  const dt = (ts) => ts ? new Date(ts).toLocaleString("pt-BR", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" }) : "—";
  const tm = (ts) => new Date(ts).toLocaleTimeString("pt-BR", { hour: "2-digit", minute: "2-digit" });
  const dur = (s) => s == null ? "—" : s >= 3600 ? `${Math.floor(s / 3600)} h ${String(Math.floor(s % 3600 / 60)).padStart(2, "0")}` : `${Math.round(s / 60)} min`;
  const api = async (p) => { const r = await fetch(p); if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || r.status); return r.json(); };
  const SEV = { crit: ["var(--bad)", "Crítico", "!"], warn: ["var(--warn)", "Atenção", "!"], info: ["var(--speed)", "Info", "i"], ok: ["var(--ok)", "OK", "✓"] };
  const STATE = {
    coletando: ["var(--ok)", "Coletando"], ecu_silenciosa: ["var(--warn)", "Ignição desligada"], conectando: ["var(--warn)", "Conectando ao OBD"],
    procurando_adaptador: ["var(--warn)", "Procurando OBD"], descobrindo: ["var(--warn)", "Lendo sensores"], desconectado: ["var(--bad)", "OBD desconectado"],
    coletor_parado: ["var(--bad)", "Coletor parado"], iniciando: ["var(--muted)", "Iniciando"],
    pi_offline: ["var(--muted)", "Carro desligado (Pi offline)"],
  };
  const COLORS = { rpm: "--rpm", speed: "--speed", ect: "--temp", iat: "--temp", batt: "--volt", ecu_volt: "--volt", tps: "--tps", tps_rel: "--tps", tps_b: "--tps", tps_cmd: "--speed", pedal_d: "--pedal", pedal_e: "--pedal", stft1: "--tps", ltft1: "--tps", map: "--speed", load: "--rpm", timing: "--volt" };
  let catalog = {};      // key -> {name, unit, decimals}
  let live = null;
  const buf = [];        // histórico ao vivo para o gráfico pedal × borboleta
  const hist = {};       // tempo real por sinal: key -> [[ts, v]] (últimos 15 min)

  // ------------------------------------------------------------ gráficos SVG
  function lineChart(svg, series, o = {}) {
    const W = 600, H = 190, L = 36, R = 8, T = 8, B = 20;
    const all = series.flatMap((s) => s.pts.filter((p) => p[1] != null));
    if (!all.length) { svg.innerHTML = `<text x="300" y="95" text-anchor="middle">Sem dados ainda</text>`; return; }
    const xs = all.map((p) => p[0]); let x0 = o.x0 ?? Math.min(...xs), x1 = o.x1 ?? Math.max(...xs); if (x1 === x0) x1 = x0 + 1;
    const ys = all.map((p) => p[1]); let y0 = o.ymin ?? Math.min(...ys), y1 = o.ymax ?? Math.max(...ys);
    if (o.ymin == null && o.ymax == null) { const pad = (y1 - y0) * 0.08 || 1; y0 -= pad; y1 += pad; }
    if (y1 === y0) y1 = y0 + 1;
    const X = (x) => L + (x - x0) / (x1 - x0) * (W - L - R), Y = (y) => T + (1 - (y - y0) / (y1 - y0)) * (H - T - B);
    let g = "";
    for (let i = 0; i <= 4; i++) { const v = y0 + (y1 - y0) * i / 4, y = Y(v); g += `<line x1="${L}" x2="${W - R}" y1="${y}" y2="${y}" stroke="var(--line)" vector-effect="non-scaling-stroke"/><text x="${L - 4}" y="${y + 3}" text-anchor="end">${nf(v, Math.abs(y1 - y0) < 5 ? 1 : 0)}</text>`; }
    const fx = o.xfmt || ((x) => x1 - x0 > 2 * 86400e3 ? new Date(x).toLocaleDateString("pt-BR", { day: "2-digit", month: "2-digit" }) : tm(x));
    for (let i = 0; i <= 4; i++) { const x = x0 + (x1 - x0) * i / 4; g += `<text x="${X(x)}" y="${H - 5}" text-anchor="${i === 0 ? "start" : i === 4 ? "end" : "middle"}">${fx(x)}</text>`; }
    for (const s of series) {
      const pts = s.pts.filter((p) => p[1] != null);
      if (!pts.length) continue;
      if (s.bars) { const bw = Math.max(2, (W - L - R) / pts.length * 0.6); g += pts.map((p) => `<rect x="${X(p[0]) - bw / 2}" y="${Y(Math.max(p[1], 0))}" width="${bw}" height="${Math.abs(Y(0) - Y(p[1]))}" rx="2" fill="var(${s.c})"/>`).join(""); continue; }
      if (s.dots) { g += pts.map((p) => `<circle cx="${X(p[0])}" cy="${Y(p[1])}" r="${s.r || 3}" fill="var(${s.c})" opacity="${s.op || 1}"/>`).join(""); continue; }
      const d = pts.map((p, i) => `${i ? "L" : "M"}${X(p[0]).toFixed(1)},${Y(p[1]).toFixed(1)}`).join("");
      if (s.area) g += `<path d="${d}L${X(pts.at(-1)[0])},${Y(y0)}L${X(pts[0][0])},${Y(y0)}Z" fill="var(${s.c})" opacity=".12"/>`;
      g += `<path d="${d}" fill="none" stroke="var(${s.c})" stroke-width="${s.w || 2}" vector-effect="non-scaling-stroke" stroke-linejoin="round"/>`;
      const e = pts.at(-1); if (s.end !== false) g += `<circle cx="${X(e[0])}" cy="${Y(e[1])}" r="3" fill="var(${s.c})"/>`;
    }
    svg.innerHTML = g;
  }
  const arcPt = (p) => { const a = Math.PI * (0.75 + 1.5 * p); return `${75 + 58 * Math.cos(a)} ${75 + 58 * Math.sin(a)}`; };
  function ring(el, score, text) {
    const c = score == null ? "var(--muted)" : score >= 85 ? "var(--ok)" : score >= 60 ? "var(--warn)" : "var(--bad)";
    const off = 314 * (1 - (score ?? 0) / 100);
    el.innerHTML = `<svg viewBox="0 0 120 120"><circle cx="60" cy="60" r="50" fill="none" stroke="var(--panel2)" stroke-width="12"/>
      <circle cx="60" cy="60" r="50" fill="none" stroke="${c}" stroke-width="12" stroke-linecap="round" stroke-dasharray="314" stroke-dashoffset="${off}" transform="rotate(-90 60 60)"/></svg>
      <div><div class="score num">${score ?? "—"}<span style="font-size:16px;color:var(--muted)">/100</span></div><div class="muted" style="font-size:13px">${esc(text)}</div></div>`;
  }
  const alertItem = (a) => { const [c, l, i] = SEV[a.severity || a.sev] || SEV.info;
    if (!a.rule) return `<div class="alert" style="--s:${c}"><span class="ic">${i}</span><div><div class="t">${esc(a.title)}</div><div class="d">${esc(a.detail || "")} · ${dt(a.ts)}</div></div><span class="pill">${l}</span></div>`;
    return `<details class="alert" style="--s:${c}" data-rule="${esc(a.rule)}" data-source="${esc(a.source || "")}"><summary><span class="ic">${i}</span><div><div class="t">${esc(a.title)}</div><div class="d">${esc(a.detail || "")} · ${(a.count || 1) > 1 ? `<b>continua: ${a.count}× desde ${dt(a.ts)}, último ${dt(a.ts_end)}</b>` : dt(a.ts)}</div></div><span style="display:grid;justify-items:end;gap:4px"><span class="pill">${(a.count || 1) >= 3 ? "Continua" : l}</span><span class="more">O que fazer ▾</span></span></summary><div class="adv">Carregando recomendação…</div></details>`; };
  const advHtml = (d) => `<div><h4>O que significa</h4>${esc(d.o_que)}</div>
    ${d.causas?.length ? `<div><h4>Causas prováveis</h4><ul>${d.causas.map((x) => `<li>${esc(x)}</li>`).join("")}</ul></div>` : ""}
    ${d.verificar?.length ? `<div><h4>O que verificar</h4><ul>${d.verificar.map((x) => `<li>${esc(x)}</li>`).join("")}</ul></div>` : ""}
    ${d.fazer?.length ? `<div><h4>O que fazer</h4><ul>${d.fazer.map((x) => `<li>${esc(x)}</li>`).join("")}</ul></div>` : ""}
    <div><h4>Urgência</h4><span class="urg">${esc(d.urgencia_txt || "")}</span>${d.rodar ? ` · ${esc(d.rodar)}` : ""}</div>`;
  const advCache = {};
  async function getAdvice(rule, source) {
    const k = rule + "|" + source;
    if (!(k in advCache)) {
      const get = (r) => api(`/api/advice?rule=${encodeURIComponent(r)}&source=${encodeURIComponent(source)}`).catch(() => null);
      let d = await get(rule);
      const head = rule.split(":")[0];                       // família do alerta (z, missing, R9…) como plano B
      if (!d && head !== rule) d = await get(head);
      advCache[k] = d;
    }
    return advCache[k];
  }
  document.addEventListener("toggle", async (e) => { const el = e.target; if (!(el instanceof HTMLDetailsElement) || !el.open || !el.dataset.rule || el.dataset.loaded) return;
    const d = await getAdvice(el.dataset.rule, el.dataset.source); el.dataset.loaded = 1;
    el.querySelector(".adv").innerHTML = d ? advHtml(d) : "Sem recomendação específica para este alerta."; }, true);

  // ------------------------------------------------------------ ao vivo
  const GAUGES = [
    { k: "rpm", n: "Rotação", u: "rpm", max: 7000, c: "--rpm" },
    { k: "speed", n: "Velocidade", u: "km/h", max: 200, c: "--speed" },
    { k: "ect", n: "Temp. motor", u: "°C", max: 130, c: "--temp", warn: 102 },
    { k: "batt", n: "Bateria", u: "V", max: 16, c: "--volt", d: 1, low: 12.4 },
  ];
  $("#gauges").innerHTML = GAUGES.map((g) => `<div class="card gauge"><svg viewBox="0 0 150 130">
    <path d="M ${arcPt(0)} A 58 58 0 1 1 ${arcPt(1)}" fill="none" stroke="var(--panel2)" stroke-width="12" stroke-linecap="round"/>
    <path id="a-${g.k}" fill="none" stroke="var(${g.c})" stroke-width="12" stroke-linecap="round"/></svg>
    <div class="v num" id="g-${g.k}">—</div><div class="u">${g.u}</div><div class="lbl">${g.n}</div><div class="sub" id="gs-${g.k}"></div></div>`).join("");

  function renderLive(d) {
    live = d;
    const v = d.values || {};
    const [sc, sl] = STATE[d.state] || ["var(--muted)", d.state];
    const stale = d.state !== "pi_offline" && d.age_s != null && d.age_s > 15;
    $("#chips").innerHTML = `<span class="chip"><span class="dot" style="--c:${stale ? "var(--bad)" : sc}"></span>${esc(stale ? "Sem dados há " + Math.round(d.age_s) + " s" : sl)}</span>
      ${d.engine_on ? `<span class="chip"><span class="dot" style="--c:var(--ok)"></span>Motor ligado${d.trip_start ? " · " + dur((d.ts - d.trip_start) / 1000) : ""}</span>` : ""}
      ${d.mil ? `<span class="chip"><span class="dot" style="--c:var(--bad)"></span>Luz de injeção acesa</span>` : ""}
      ${d.rates?.fast_hz ? `<span class="chip num">${nf(d.rates.fast_hz, 1)} leit/s</span>` : ""}`;
    for (const g of GAUGES) {
      const x = v[g.k]; const p = Math.max(0.001, Math.min(1, (x ?? 0) / g.max));
      const el = $("#a-" + g.k); el.setAttribute("d", x == null ? "" : `M ${arcPt(0)} A 58 58 0 ${p > 2 / 3 ? 1 : 0} 1 ${arcPt(p)}`);
      const bad = (g.warn && x > g.warn) || (g.low && x != null && x < g.low && d.engine_on);
      el.setAttribute("stroke", bad ? "var(--bad)" : `var(${g.c})`);
      $("#g-" + g.k).textContent = nf(x, g.d || 0);
    }
    $("#gs-rpm").textContent = v.load != null ? `Carga ${nf(v.load)}%` : "";
    $("#gs-speed").textContent = v.map != null ? `MAP ${nf(v.map)} kPa` : "";
    $("#gs-ect").textContent = v.iat != null ? `Ar admitido ${nf(v.iat)} °C` : "";
    $("#gs-batt").textContent = d.engine_on ? "Carregando: 13,5–14,7 V" : "Parado: ≥ 12,4 V";
    // buffer pedal × borboleta
    if (v.tps != null || v.pedal_d != null) {
      const t = d.ts; if (!buf.length || buf.at(-1).t !== t) buf.push({ t, p: d.pedal_norm ?? v.pedal_d, tps: v.tps, cmd: v.tps_cmd });
      while (buf.length && buf[0].t < t - 180e3) buf.shift();
    }
    lineChart($("#liveChart"), [
      { pts: buf.map((b) => [b.t, b.cmd]), c: "--speed", w: 1.5, end: false },
      { pts: buf.map((b) => [b.t, b.p]), c: "--pedal", w: 2.5 },
      { pts: buf.map((b) => [b.t, b.tps]), c: "--tps", w: 2.5 },
    ], { ymin: 0, ymax: 100, x1: d.ts, x0: d.ts - 180e3 });
    const diffs = buf.filter((b) => b.cmd != null && b.tps != null).map((b) => Math.abs(b.cmd - b.tps));
    $("#liveStats").innerHTML = `<div><span class="lbl">Borboleta</span><b>${nf(v.tps, 1)}%</b></div>
      <div><span class="lbl">Comandado</span><b>${nf(v.tps_cmd, 1)}%</b></div>
      <div><span class="lbl">Pedal</span><b>${nf(d.pedal_norm ?? v.pedal_d, 0)}%</b></div>
      <div><span class="lbl">Dif. média</span><b>${diffs.length ? nf(diffs.reduce((a, b) => a + b, 0) / diffs.length, 1) + " pp" : "—"}</b></div>
      <div><span class="lbl">Ajuste curto/longo</span><b>${nf(v.stft1, 0)} / ${nf(v.ltft1, 0)}%</b></div>`;
    for (const [k, x] of Object.entries(v)) { if (x == null) continue; const h = (hist[k] ??= []); const t = d.value_ts?.[k] || d.ts; if (!h.length || h.at(-1)[0] !== t) h.push([t, x]); while (h.length && h[0][0] < d.ts - 15 * 60e3) h.shift(); }
    renderEngine(d);
    renderTrimTiming(d);
    if (modalKey) updateModal();
    // sensores: cor pelo estado calculado no Pi (faixa normal + comportamento habitual)
    const recentBad = {}; for (const [k, st] of Object.entries(d.status || {})) if (st[0] !== "ok") recentBad[k] = st[0];
    const keys = Object.keys(v).sort((a, b) => (catalog[a]?.name || a).localeCompare(catalog[b]?.name || b, "pt"));
    const nbad = Object.values(d.status || {}).filter((x) => x[0] !== "ok").length;
    $("#sensCount").innerHTML = `${keys.length} sinais · ${d.supported || 0} PIDs da ECU · <span style="color:var(--ok)">● normal</span> <span style="color:var(--bad)">● fora do normal${nbad ? " (" + nbad + ")" : ""}</span> <span class="muted">● sem faixa</span>`;
    $("#liveSensors").innerHTML = keys.length ? keys.map((k) => { const c = catalog[k] || {}; const st = d.status?.[k];
      const cls = !st ? "" : st[0] === "ok" ? "good" : "bad";
      const rng = st?.[2] ? `Normal: ${esc(st[2])} ${esc(c.unit || "")}` : "Sem faixa definida";
      return `<button class="sensor ${cls}" data-k="${esc(k)}" title="${esc(st?.[1] || "")}" style="--s:${cls === "bad" ? "var(--bad)" : cls === "good" ? "var(--ok)" : `var(${COLORS[k] || "--line"})`}"><span class="n">${esc(c.name || k)}</span>
      <span class="val num">${nf(v[k], c.decimals ?? 1)}<small>${esc(c.unit || "")}</small></span><span class="rng">${cls === "bad" ? "⚠ " + esc(st[1] || "Fora do normal") + " · " : ""}${rng}</span><span class="pid">${esc(k)}${d.value_ts?.[k] ? " · " + Math.max(0, Math.round((d.ts - d.value_ts[k]) / 1000)) + " s" : ""}</span></button>`; }).join("")
      : `<div class="empty" style="grid-column:1/-1">Aguardando a primeira leitura do OBD. Ligue a ignição com o adaptador conectado.</div>`;
    // banners de alerta recentes
    const crit = (d.alerts || []).filter((a) => d.ts - (a.ts_end || a.ts) < 10 * 60e3 && a.sev !== "info").slice(0, 3);
    $("#banners").innerHTML = crit.map((a) => `<div class="banner" onclick="location.hash='alertas'" style="cursor:pointer;--s:${SEV[a.sev][0]}"><span class="ic">!</span><b>${esc(a.title)}${(a.count || 1) > 1 ? ` · continua (${a.count}×)` : ""} <span class="more">· O que fazer ›</span></b><span class="muted">${esc(a.detail)} · ${(a.count || 1) > 1 ? "desde " + tm(a.ts) + ", último " + tm(a.ts_end) : tm(a.ts)}</span></div>`).join("");
    const nb = (d.alerts || []).filter((a) => d.ts - (a.ts_end || a.ts) < 24 * 3600e3 && a.sev === "crit").length;
    $("#badge").hidden = !nb; $("#badge").textContent = nb;
  }

  function renderTrimTiming(d) {
    const v = d.values || {}, now = d.ts, x0 = now - 15 * 60e3;
    const st = d.status?.stft1, ok = !st || st[0] === "ok";
    $("#stftNow").textContent = v.stft1 != null ? (v.stft1 > 0 ? "+" : "") + nf(v.stft1, 1) + "%" : "—";
    $("#stftNow").style.color = v.stft1 == null ? "" : ok ? "var(--ok)" : "var(--bad)";
    lineChart($("#stftChart"), [{ pts: [[x0, 10], [now, 10]], c: "--line", w: 1, end: false }, { pts: [[x0, -10], [now, -10]], c: "--line", w: 1, end: false },
      { pts: hist.stft1 || [], c: ok ? "--ok" : "--bad", area: true }], { x0, x1: now, ymin: -30, ymax: 50 });
    $("#stftTxt").textContent = v.stft1 == null ? "" : v.stft1 > 10 ? "Positivo: a ECU está ADICIONANDO combustível (mistura pobre / entrada de ar?)" : v.stft1 < -10 ? "Negativo: a ECU está TIRANDO combustível (mistura rica)" : "Dentro do normal";
    $("#timNow").textContent = v.timing != null ? nf(v.timing, 1) + "°" : "—";
    lineChart($("#timChart"), [{ pts: hist.timing || [], c: "--volt", area: true }], { x0, x1: now });
    $("#timTxt").textContent = v.timing == null ? "" : `Marcha lenta típica 5–15°; em aceleração sobe (25–40°). Quedas bruscas em carga podem indicar detonação.`;
  }
  async function loadAccel() {
    const r = await api("/api/accel").catch(() => []);
    $("#accList").innerHTML = r.length ? r.slice(0, 5).map((a, i) => `<div class="alert" style="--s:${a.t100 ? "var(--ok)" : "var(--muted)"}"><span class="ic">${i + 1}</span><div><div class="t">${a.t100 ? "0–100 em " + nf(a.t100, 2) + " s" : "Incompleto"}</div>
      <div class="d">0–60 ${nf(a.t60, 2)} s · 0–80 ${nf(a.t80, 2)} s · ${nf(a.hz, 1)} leit/s · ${dt(a.ts)}</div></div><span class="pill">${a.fuel_mix > 0.5 ? "Etanol" : "Gasolina"}</span></div>`).join("")
      : `<div class="empty">Nenhum teste ainda. Toque em Iniciar teste (abre o modo direção).</div>`;
  }
  let summary = null;
  async function loadSummary() { summary = await api("/api/summary?days=14"); if (live) renderEngine(live); return summary; }
  function renderEngine(d) {
    const on = d.engine_on; const now = d.ts || Date.now(); const S = summary || {};
    const since = on && d.trip_start ? (now - d.trip_start) / 1000 : S.off_since_ts ? (now - S.off_since_ts) / 1000 : null;
    const ign = d.state === "coletando" && !on;
    const off = d.state === "pi_offline";
    const c = on ? "var(--ok)" : ign ? "var(--warn)" : "var(--muted)";
    const sub = off ? `Pi visto pela última vez ${d.pi_last_seen ? dt(d.pi_last_seen) : "—"}` + (S.off_since_ts ? ` · último motor desligado ${dt(S.off_since_ts)}` : "")
      : since != null ? (on ? "há " : "desde ") + (on ? dur(since) : (since > 86400 ? dt(S.off_since_ts) : "há " + dur(since))) : "—";
    $("#engineCard").innerHTML = `<div class="st"><span class="pulse${on ? " on" : ""}" style="--c:${c}"></span><div><div class="big">${on ? "Carro ligado · motor funcionando" : ign ? "Ignição ligada, motor desligado" : "Carro desligado"}</div>
      <div class="muted">${sub}</div></div></div>
      <div class="kv"><span class="lbl">Esta viagem</span><b>${on ? nf(S.current_trip_km ?? 0, 1) + " km" : "—"}</b></div>
      <div class="kv"><span class="lbl">Hoje</span><b>${nf(S.today?.km ?? 0, 1)} km</b><span class="muted" style="font-size:12px">${dur(S.today?.dur ?? 0)} ligado</span></div>
      <div class="kv"><span class="lbl">Total registrado</span><b>${nf(S.total?.km ?? 0, 0)} km</b><span class="muted" style="font-size:12px">${S.total?.n ?? 0} viagens</span></div>
      <div class="kv"><span class="lbl">Consumo agora</span><b>${d.values?.kml_inst != null ? nf(d.values.kml_inst, 1) + " km/L" : d.values?.fuel_lh != null ? nf(d.values.fuel_lh, 1) + " L/h" : "—"}</b><span class="muted" style="font-size:12px">${d.values?.fuel_lh != null ? nf(d.values.fuel_lh, 2) + " L/h" : ""}</span></div>`;
  }

  let es;
  function connectStream() {
    try { es?.close(); } catch { }
    es = new EventSource("/api/stream");
    es.onmessage = (e) => { try { renderLive(JSON.parse(e.data)); } catch (err) { console.error(err); } };
    es.onerror = () => { $("#chips").innerHTML = `<span class="chip"><span class="dot" style="--c:var(--bad)"></span>Sem conexão com o Pi</span>`; };
  }
  document.addEventListener("visibilitychange", () => { if (document.hidden) es?.close(); else connectStream(); });

  // ------------------------------------------------------------ borboleta
  const RULES = {
    R1: "Borboleta segue o comando", R2: "Sensores do pedal coerentes", R2t: "Sensores da borboleta A/B coerentes", R3: "Sem travamento / modo de emergência", R3b: "Pedal abre a borboleta",
    R4: "Marcha lenta estável (sem oscilar)", R5: "Marcha lenta regular", R5b: "Marcha lenta no alvo", R6: "Sem morrer em marcha lenta", R9: "Sinais sem saltos",
  };
  async function loadThrottle(target = "both") {
    const t = await api("/api/throttle?days=30");
    const counts = {}; for (const r of t.rules) { const k = r.rule; counts[k] ??= { n: 0, sev: "info", last: 0 }; counts[k].n += r.n; counts[k].last = Math.max(counts[k].last, r.last_ts); if ((r.severity === "crit") || (r.severity === "warn" && counts[k].sev !== "crit")) counts[k].sev = r.severity; }
    const dtcRules = Object.keys(counts).filter((k) => /^P\d/.test(k));
    const li = (s, title, d, pill) => `<li style="--s:${SEV[s][0]}"><span class="ic">${SEV[s][2]}</span><div><div class="t">${esc(title)}</div><div class="d">${esc(d)}</div></div><span class="pill">${pill || SEV[s][1]}</span></li>`;
    const ruleItems = Object.entries(RULES).map(([k, name]) => { const c = counts[k];
      return { k, html: li(c ? c.sev : "ok", name, c ? `${c.n}× em 30 dias · último ${dt(c.last)}` : "Nenhuma ocorrência em 30 dias") }; });
    const dtcItems = Object.keys(counts).filter((k) => /^P\d/.test(k)).map((k) => li("crit", `Código ${k}`, `${counts[k].n}× · último ${dt(counts[k].last)}`, "DTC"));
    const checks = ruleItems.map((x) => x.html).join("") + dtcItems.join("");
    const txt = t.score == null ? "Aprendendo: a nota aparece após a primeira viagem completa." : t.score >= 85 ? "Funcionando bem." : t.score >= 60 ? "Atenção: veja os itens em amarelo." : "Problema provável: veja os itens em vermelho.";
    ring($("#healthRing"), t.score, txt); ring($("#tbRing"), t.score, txt);
    $("#healthChecks").innerHTML = ruleItems.filter((x) => ["R1", "R3", "R4", "R6"].includes(x.k)).map((x) => x.html).join("") + dtcItems.join("");
    $("#tbRules").innerHTML = checks;
    if (target === "live") return;
    lineChart($("#scoreChart"), [{ pts: t.trips.map((x) => [x.start_ts, x.tb_score]), c: "--ok", area: true }], { ymin: 0, ymax: 100 });
    lineChart($("#idleChart"), [
      { pts: t.idle.map((x) => [x.day, x.rpm]), c: "--rpm" },
      { pts: t.idle.map((x) => [x.day, x.rpm_std * 10]), c: "--temp", w: 1.5 },
      { pts: t.idle.map((x) => [x.day, x.tps * 100]), c: "--tps", w: 1.5 },
    ], { ymin: 0, xfmt: (x) => new Date(x).toLocaleDateString("pt-BR", { day: "2-digit", month: "2-digit" }) });
    lineChart($("#scatter"), [
      { pts: t.scatter.map((p) => [p[0], p[2]]), c: "--speed", dots: true, r: 1.6, op: .5 },
      { pts: t.scatter.map((p) => [p[0], p[1]]), c: "--tps", dots: true, r: 2, op: .7 },
    ], { ymin: 0, ymax: 100, xfmt: (x) => nf(x) + "%" });
    $("#tbEvents").innerHTML = t.events.length ? t.events.map(alertItem).join("") : `<div class="empty">Nenhum evento registrado. Ótimo sinal.</div>`;
  }

  // ------------------------------------------------------------ viagens
  async function loadTrips() {
    const S = await loadSummary();
    lineChart($("#engDayChart"), [{ pts: S.per_day.map((x) => [x.day + 43200e3, (x.dur || 0) / 3600]), c: "--rpm", bars: true }], { ymin: 0, xfmt: (x) => new Date(x).toLocaleDateString("pt-BR", { day: "2-digit", month: "2-digit" }) });
    const days = []; const t0 = new Date(); t0.setHours(0, 0, 0, 0);
    for (let i = 13; i >= 0; i--) days.push(t0.getTime() - i * 86400e3);
    $("#engTimeline").innerHTML = days.reverse().map((d0) => { const d1 = d0 + 86400e3;
      const bars = S.history.filter((t) => t.start_ts < d1 && (t.end_ts || Date.now()) > d0).map((t) => { const a = Math.max(t.start_ts, d0), b = Math.min(t.end_ts || Date.now(), d1);
        return `<i style="left:${(a - d0) / 864e3}%;width:${(b - a) / 864e3}%" title="${tm(t.start_ts)}–${t.end_ts ? tm(t.end_ts) : "agora"} · ${nf(t.distance_km, 1)} km"></i>`; }).join("");
      return `<div class="d"><span>${new Date(d0).toLocaleDateString("pt-BR", { weekday: "short", day: "2-digit" })}</span><div class="tr">${bars}</div></div>`; }).join("")
      + `<div class="d"><span></span><div class="muted" style="display:flex;justify-content:space-between;font:10px var(--f-mono)"><span>0h</span><span>6h</span><span>12h</span><span>18h</span><span>24h</span></div></div>`;
    const trips = await api("/api/trips?limit=1000");
    const showHidden = $("#tripHidden")?.checked;
    const vis = trips.filter((t) => showHidden || !t.hidden);
    $("#tripCount").textContent = `${vis.length} viagens${trips.length - vis.length ? ` · ${trips.length - vis.length} ocultas` : ""}`;
    $("#tripRows").innerHTML = vis.length ? vis.map((t) => { const s = t.tb_score; const c = s == null ? "var(--muted)" : s >= 85 ? "var(--ok)" : s >= 60 ? "var(--warn)" : "var(--bad)";
      return `<tr class="click" data-id="${t.id}" style="${t.hidden ? "opacity:.5" : ""}"><td><b>${esc(t.title || "")}</b>${t.title ? "<br>" : ""}${dt(t.start_ts)}${t.finalized ? "" : ' <span class="pill" style="--s:var(--ok)">agora</span>'}${t.note ? ` <span title="${esc(t.note)}">📝</span>` : ""}</td><td class="num">${dur(t.duration_s ?? ((t.end_ts - t.start_ts) / 1000))}</td>
      <td class="num">${nf(t.distance_km, 1)}</td><td class="num">${nf(t.avg_speed)} km/h</td><td class="num">${nf(t.km_l, 1)}</td><td class="num">${nf(t.max_ect)} °C</td><td class="num">${t.n_alerts ?? 0}</td>
      <td class="num">${t.eco_score ?? "—"}</td><td><div class="bar" style="--s:${c}" title="${s ?? "—"}/100"><i style="width:${s ?? 0}%"></i></div></td></tr>`; }).join("")
      : `<tr><td colspan="9"><div class="empty">Nenhuma viagem ainda. A primeira aparece quando o motor ligar com o OBD conectado.</div></td></tr>`;
  }
  const AIRL = { entrada_de_ar: "Possível entrada falsa de ar", suspeito: "Suspeito (MAF baixo parado)", maf_baixo: "MAF lendo baixo", normal: "Normal" };
  async function showTrip(id) {
    const t = await api(`/api/trips/${id}`);
    const S = t.series; const eco = t.eco_json || {}; const air = t.air_json || {};
    $("#tripDetail").innerHTML = `<div class="card"><div class="row-head"><h2>${esc(t.title || "Viagem de " + dt(t.start_ts))}</h2><span class="muted">${dt(t.start_ts)} → ${t.end_ts ? tm(t.end_ts) : "agora"} · ${esc(t.end_reason || "em andamento")}</span></div>
      ${t.note ? `<div class="muted" style="margin-top:6px">📝 ${esc(t.note)}</div>` : ""}
      <form id="tripEdit" data-id="${t.id}" style="display:flex;gap:8px;flex-wrap:wrap;margin-top:10px" ${CLOUD?.readonly ? "hidden" : ""}>
        <input type="text" id="tTitle" value="${esc(t.title || "")}" placeholder="Nome (ex.: Casa → trabalho)" aria-label="Nome da viagem" style="flex:1 1 180px">
        <input type="text" id="tNote" value="${esc(t.note || "")}" placeholder="Anotação" aria-label="Anotação" style="flex:2 1 220px">
        <label class="muted" style="display:flex;align-items:center;gap:6px;font-size:13px"><input type="checkbox" id="tHidden" ${t.hidden ? "checked" : ""}> Ocultar</label>
        <button class="btn" type="submit">Salvar</button><span class="muted" id="tMsg" style="font-size:12px;align-self:center"></span></form>
      <div class="stats" style="margin-top:12px">
        <div><span class="lbl">Duração</span><b>${dur(t.duration_s)}</b></div><div><span class="lbl">Distância</span><b>${nf(t.distance_km, 1)} km</b></div>
        <div><span class="lbl">Parado ligado</span><b>${dur(t.idle_s)}</b></div><div><span class="lbl">Vel. média / máx</span><b>${nf(t.avg_speed)} / ${nf(t.max_speed)}</b></div>
        <div><span class="lbl">Rot. máx</span><b>${nf(t.max_rpm)}</b></div><div><span class="lbl">Temp. máx</span><b>${nf(t.max_ect)} °C</b></div>
        <div><span class="lbl">Combustível</span><b>${nf(t.fuel_l, 2)} L</b></div><div><span class="lbl">Consumo</span><b>${nf(t.km_l, 1)} km/L</b></div>
        <div><span class="lbl">Custo estimado</span><b>${t.cost ? "R$ " + nf(t.cost, 2) : "—"}</b></div><div><span class="lbl">Bateria</span><b>${nf(t.min_volt, 1)}–${nf(t.max_volt, 1)} V</b></div>
        <div><span class="lbl">Partida</span><b>${t.crank_v ? nf(t.crank_v, 1) + " V" : "—"}</b></div><div><span class="lbl">Aquecimento</span><b>${t.warmup_s ? Math.round(t.warmup_s / 60) + " min" : t.start_ect != null && t.start_ect >= 50 ? "já quente" : "—"}</b></div>
        <div><span class="lbl">Direção econômica</span><b>${t.eco_score ?? "—"}/100</b></div><div><span class="lbl">Borboleta</span><b>${t.tb_score ?? "—"}/100</b></div>
        <div><span class="lbl">Ar × MAF</span><b style="font-size:14px">${esc(AIRL[t.air_verdict] || "—")}</b></div><div><span class="lbl">Arrancadas / freadas bruscas</span><b>${eco.aceleracoes_bruscas ?? "—"} / ${eco.freadas_bruscas ?? "—"}</b></div></div>
      ${(eco.dicas || []).length ? `<div class="list" style="margin-top:10px">${eco.dicas.map((x) => `<div class="alert" style="--s:var(--ok)"><span class="ic">💡</span><div><div class="t">${esc(x)}</div></div><span></span></div>`).join("")}</div>` : ""}
      ${air.txt ? `<div class="muted" style="font-size:12px;margin-top:8px">Ar × MAF: ${esc(air.txt)}</div>` : ""}
      ${[["Retrato na partida", t.start_snapshot], ["Retrato no fim", t.end_snapshot]].filter(([, x]) => x).map(([lbl, x]) => `<details style="margin-top:10px"><summary class="lbl" style="cursor:pointer">${lbl} · ${Object.keys(x.values || {}).length} sinais · ${dt(x.ts)}</summary>
        <div class="tbl" style="margin-top:6px"><table><tbody>${Object.entries(x.values || {}).filter(([, v]) => v != null).sort().map(([k, v]) => `<tr><td>${esc(catalog[k]?.name || k)}</td><td class="num">${nf(v, catalog[k]?.decimals ?? 2)} ${esc(catalog[k]?.unit || "")}</td></tr>`).join("")}
        ${x.batt_rest ? `<tr><td>Bateria em repouso</td><td class="num">${nf(x.batt_rest, 2)} V</td></tr>` : ""}${x.dtc ? `<tr><td>Códigos</td><td>${esc(JSON.stringify(x.dtc))}</td></tr>` : ""}</tbody></table></div></details>`).join("")}</div>
      <div class="grid2e"><div class="card"><h2>Rotação e velocidade</h2><div class="legend" style="margin:6px 0"><span style="--c:var(--rpm)">rpm ÷ 50</span><span style="--c:var(--speed)">km/h</span></div><svg class="chart" id="tc1" viewBox="0 0 600 190" preserveAspectRatio="none"></svg></div>
      <div class="card"><h2>Pedal × borboleta</h2><div class="legend" style="margin:6px 0"><span style="--c:var(--pedal)">Pedal</span><span style="--c:var(--tps)">Borboleta</span><span style="--c:var(--speed)">Comandado</span></div><svg class="chart" id="tc2" viewBox="0 0 600 190" preserveAspectRatio="none"></svg></div>
      <div class="card"><h2>Temperatura do motor</h2><svg class="chart" id="tc3" viewBox="0 0 600 190" preserveAspectRatio="none"></svg></div>
      <div class="card"><h2>Consumo e marcha</h2><div class="legend" style="margin:6px 0"><span style="--c:var(--tps)">L/h</span><span style="--c:var(--volt)">Marcha</span></div><svg class="chart" id="tc4" viewBox="0 0 600 190" preserveAspectRatio="none"></svg></div>
      <div class="card"><h2>Sonda λ e avanço</h2><div class="legend" style="margin:6px 0"><span style="--c:var(--tps)">λ × 10</span><span style="--c:var(--volt)">Avanço °</span></div><svg class="chart" id="tc5" viewBox="0 0 600 190" preserveAspectRatio="none"></svg></div>
      <div class="card"><h2>MAF e carga</h2><div class="legend" style="margin:6px 0"><span style="--c:var(--speed)">MAF g/s</span><span style="--c:var(--rpm)">Carga %</span></div><svg class="chart" id="tc6" viewBox="0 0 600 190" preserveAspectRatio="none"></svg></div></div>
      <div class="card"><h2>Alertas desta viagem</h2><div class="list" style="margin-top:10px">${t.alerts.length ? t.alerts.map(alertItem).join("") : '<div class="empty">Nenhum alerta.</div>'}</div></div>`;
    lineChart($("#tc1"), [{ pts: (S.rpm || []).map((p) => [p[0], p[1] / 50]), c: "--rpm" }, { pts: S.speed || [], c: "--speed" }], { ymin: 0 });
    lineChart($("#tc2"), [{ pts: S.tps_cmd || [], c: "--speed", w: 1.5 }, { pts: S.pedal_d || [], c: "--pedal" }, { pts: S.tps || [], c: "--tps" }], { ymin: 0, ymax: 100 });
    lineChart($("#tc3"), [{ pts: S.ect || [], c: "--temp", area: true }]);
    lineChart($("#tc4"), [{ pts: S.fuel_lh || [], c: "--tps", area: true }, { pts: S.gear || [], c: "--volt", w: 1.5 }], { ymin: 0 });
    lineChart($("#tc5"), [{ pts: (S.o2c1_lambda || []).map((p) => [p[0], p[1] * 10]), c: "--tps" }, { pts: S.timing || [], c: "--volt" }]);
    lineChart($("#tc6"), [{ pts: S.maf || [], c: "--speed" }, { pts: S.load || [], c: "--rpm", w: 1.5 }], { ymin: 0 });
    $("#tripEdit").addEventListener("submit", async (e) => {
      e.preventDefault();
      const r = await fetch(`/api/trips/${t.id}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ title: $("#tTitle").value, note: $("#tNote").value, hidden: $("#tHidden").checked }) });
      const j = await r.json().catch(() => ({}));
      $("#tMsg").textContent = r.ok ? (j.queued ? "Salvo; aplicado no Pi na próxima conexão." : "Salvo.") : "Erro: " + (j.error || r.status);
      if (r.ok && !j.queued) loadTrips().catch(console.error);
    });
    $("#tripDetail").scrollIntoView({ behavior: "smooth" });
  }
  document.addEventListener("change", (e) => { if (e.target.id === "tripHidden") loadTrips().catch(console.error); });
  $("#tripRows").addEventListener("click", (e) => { const tr = e.target.closest("tr[data-id]"); if (tr) showTrip(tr.dataset.id).catch(console.error); });

  // ------------------------------------------------------------ consumo
  async function loadFuel() {
    const f = await api("/api/fuel");
    const sf = f.since_fill; const last = f.fills[0];
    $("#fillHint").textContent = last ? `Último: ${dt(last.ts)} · ${last.fuel}${last.liters ? ", " + nf(last.liters, 1) + " L" : ""}` : "Toque ao encher o tanque. O 1º abastecimento inicia a medição.";
    const mix = f.mix; const mixTxt = mix == null ? "—" : mix < 0.05 ? "Gasolina" : mix > 0.95 ? "Etanol" : `${Math.round(mix * 100)}% etanol`;
    $("#fuelStats").innerHTML = `<div><span class="lbl">Desde o último tanque</span><b>${nf(sf.km, 0)} km</b></div>
      <div><span class="lbl">Gasto estimado</span><b>${nf(sf.liters_est, 1)} L</b></div>
      <div><span class="lbl">Média estimada</span><b>${nf(sf.kml_est, 1)} km/L</b></div>
      <div><span class="lbl">Média real (5 tanques)</span><b>${nf(f.avg_real_kml, 1)} km/L</b></div>
      <div><span class="lbl">Custo por km</span><b>${f.cost_per_km ? "R$ " + nf(f.cost_per_km, 2) : "—"}</b></div>
      <div><span class="lbl">No tanque</span><b style="font-size:15px">${mixTxt}</b></div>
      <div><span class="lbl">Restante estimado</span><b>${last?.full ? nf(Math.max(0, f.tank_l - sf.liters_est), 0) + " L" : "—"}</b></div>
      <div><span class="lbl">Calibração</span><b>${nf(f.calib, 2)}×</b></div>`;
    const real = f.fills.filter((x) => x.real_kml).reverse();
    lineChart($("#fuelFillChart"), [{ pts: real.map((x) => [x.ts, x.real_kml]), c: "--ok", bars: true }], { ymin: 0, xfmt: (x) => new Date(x).toLocaleDateString("pt-BR", { day: "2-digit", month: "2-digit" }) });
    lineChart($("#fuelTripChart"), [{ pts: f.trips.slice().reverse().map((x) => [x.start_ts, x.km_l]), c: "--speed", dots: true, r: 4 }], { ymin: 0 });
    $("#fillRows").innerHTML = f.fills.length ? f.fills.map((x) => `<tr><td>${dt(x.ts)}</td><td>${esc(x.fuel)}</td><td class="num">${nf(x.liters, 2)}</td><td class="num">${x.price_l ? "R$ " + nf(x.price_l, 2) : "—"}</td>
      <td class="num">${nf(x.km_since_prev, 0)}</td><td class="num">${nf(x.real_kml, 1)}</td><td class="num">${x.mix_after == null ? "—" : Math.round(x.mix_after * 100) + "% etanol"}</td></tr>`).join("")
      : `<tr><td colspan="7"><div class="empty">Nenhum abastecimento registrado.</div></td></tr>`;
  }
  $("#fillBtn").addEventListener("click", () => { $("#fillForm").hidden = false; $("#fillBtn").hidden = true; $("#fillL").focus(); });
  $("#fillCancel").addEventListener("click", () => { $("#fillForm").hidden = true; $("#fillBtn").hidden = false; });
  $("#fillForm").addEventListener("submit", async (e) => {
    e.preventDefault(); $("#fillSave").disabled = true;
    try {
      const body = { fuel: document.querySelector('input[name="fuel"]:checked').value, liters: $("#fillL").value, price_l: $("#fillP").value, full: true, ts: Date.now() };
      const r = await fetch("/api/fuel/fill", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      const j = await r.json(); if (!r.ok) throw new Error(j.error || r.status);
      $("#fillForm").hidden = true; $("#fillBtn").hidden = false; $("#fillL").value = ""; $("#fillP").value = "";
      $("#fillHint").textContent = j.queued ? "Registrado. Será aplicado no Pi na próxima conexão (com a hora de agora)." : j.real_kml ? `Salvo. Este tanque rendeu ${nf(j.real_kml, 1)} km/L (${nf(j.km_since_prev, 0)} km).` : "Salvo. O consumo real aparece no próximo tanque cheio.";
      setTimeout(() => loadFuel().catch(console.error), 1500);
    } catch (err) { $("#fillHint").textContent = "Não foi possível salvar: " + err.message; } finally { $("#fillSave").disabled = false; }
  });

  // ------------------------------------------------------------ alertas / DTC
  async function loadAlerts() {
    const sev = $("#alertFilter").value;
    const [al, d] = await Promise.all([api(`/api/alerts?days=90${sev ? "&severity=" + sev : ""}`), api("/api/dtc")]);
    $("#alertList").innerHTML = al.length ? al.map(alertItem).join("") : `<div class="empty">Nenhum alerta nos últimos 90 dias.</div>`;
    $("#alertRecs").innerHTML = recsHtml(await recsFromAlerts(al.filter((a) => (a.ts_end || a.ts) > Date.now() - 30 * 86400e3)));
    $("#milChip").innerHTML = `<span class="chip"><span class="dot" style="--c:${d.mil ? "var(--bad)" : "var(--ok)"}"></span>Luz de injeção ${d.mil ? "acesa" : "apagada"}</span>`;
    const act = d.state.filter((s) => s.active);
    $("#dtcList").innerHTML = act.length ? act.map((s) => `<div class="alert" style="--s:${s.kind === "confirmed" ? "var(--bad)" : "var(--warn)"}"><code class="k">${esc(s.code)}</code><div><div class="t">${esc(s.description)}</div><div class="d">${s.kind === "confirmed" ? "Confirmado" : "Pendente"} · desde ${dt(s.first_seen_ts)}${s.throttle ? " · borboleta/marcha lenta" : ""}</div></div><span class="pill">${s.count}×</span></div>`).join("")
      : `<div class="alert" style="--s:var(--ok)"><span class="ic">✓</span><div><div class="t">Nenhum código ativo</div><div class="d">Leitura dos modos 03 e 07 a cada 20 s</div></div><span class="pill">OK</span></div>`;
    $("#dtcHist").innerHTML = d.events.length ? d.events.slice(0, 40).map((e) => `<div class="alert" style="--s:${e.event === "sumiu" ? "var(--ok)" : "var(--warn)"}"><code class="k">${esc(e.code)}</code><div><div class="t">${esc(e.description)}</div><div class="d">${e.event} (${e.kind === "confirmed" ? "confirmado" : "pendente"}) · ${dt(e.ts)}</div></div><span></span></div>`).join("") : `<div class="empty">Sem histórico.</div>`;
  }
  $("#alertFilter").addEventListener("change", () => loadAlerts().catch(console.error));

  // ------------------------------------------------------------ sensores
  let sigKey = null;
  async function loadSignals() {
    const s = await api("/api/signals");
    for (const x of s) catalog[x.key] = x;
    $("#sigRows").innerHTML = s.length ? s.map((x) => `<tr class="click" data-k="${esc(x.key)}"><td>${esc(x.name)} <span class="muted num" style="font-size:11px">${esc(x.key)}</span></td>
      <td class="num">${nf(x.last, x.decimals)} ${esc(x.unit)}</td><td class="num">${nf(x.min24h, x.decimals)}</td><td class="num">${nf(x.avg24h, x.decimals)}</td><td class="num">${nf(x.max24h, x.decimals)}</td><td class="num">${x.n24h}</td></tr>`).join("")
      : `<tr><td colspan="6"><div class="empty">Nenhum sinal gravado ainda.</div></td></tr>`;
  }
  async function showSignal(k) {
    sigKey = k; $("#sigChartCard").hidden = false;
    const c = catalog[k] || {}; $("#sigTitle").textContent = `${c.name || k} (${c.unit || ""})`;
    const span = +$("#sigRange").value, to = Date.now();
    const r = await api(`/api/series?signals=${encodeURIComponent(k)}&span=${span}&max_points=500`);
    lineChart($("#sigChart"), [{ pts: r.series[k] || [], c: COLORS[k] || "--speed", area: true }], { x0: to - span, x1: to });
    $("#sigChartCard").scrollIntoView({ behavior: "smooth" });
  }
  $("#sigRows").addEventListener("click", (e) => { const tr = e.target.closest("tr[data-k]"); if (tr) openModal(tr.dataset.k); });
  $("#sigRange").addEventListener("change", () => sigKey && showSignal(sigKey).catch(console.error));
  $("#liveSensors").addEventListener("click", (e) => { const b = e.target.closest("[data-k]"); if (b) openModal(b.dataset.k); });
  document.querySelectorAll(".gauge").forEach((g, i) => { g.style.cursor = "pointer"; g.addEventListener("click", () => openModal(GAUGES[i].k)); });

  // ------------------------------------------------------------ gráfico individual (tempo real + histórico)
  let modalKey = null, modalMode = "rt", modalSpan = 3600e3, histCache = null;
  const RANGES = [["1 h", 3600e3], ["6 h", 6 * 3600e3], ["24 h", 86400e3], ["7 dias", 7 * 86400e3], ["30 dias", 30 * 86400e3]];
  $("#mRange").innerHTML = RANGES.map(([l, v]) => `<button class="rbtn" type="button" data-span="${v}" aria-pressed="${v === modalSpan}">${l}</button>`).join("");
  $("#mRange").addEventListener("click", (e) => { const b = e.target.closest("[data-span]"); if (!b) return; modalSpan = +b.dataset.span;
    $("#mRange").querySelectorAll("button").forEach((x) => x.setAttribute("aria-pressed", x === b)); loadModalHist(); });
  document.querySelectorAll('input[name="mmode"]').forEach((r) => r.addEventListener("change", () => { modalMode = r.value; $("#mRange").hidden = modalMode !== "hist"; modalMode === "hist" ? loadModalHist() : updateModal(); }));
  $("#mClose").addEventListener("click", closeModal);
  $("#sigModal").addEventListener("click", (e) => { if (e.target.id === "sigModal") closeModal(); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && modalKey) closeModal(); });
  function openModal(k) { modalKey = k; histCache = null; const c = catalog[k] || {}; $("#mTitle").textContent = c.name || k; $("#sigModal").hidden = false;
    document.querySelector('input[name="mmode"][value="rt"]').checked = true; modalMode = "rt"; $("#mRange").hidden = true; updateModal(); }
  function closeModal() { modalKey = null; $("#sigModal").hidden = true; }
  function statsOf(pts) { const ys = pts.map((p) => p[1]).filter((y) => y != null); if (!ys.length) return null;
    return { min: Math.min(...ys), max: Math.max(...ys), avg: ys.reduce((a, b) => a + b, 0) / ys.length, n: ys.length }; }
  function updateModal() {
    const k = modalKey; if (!k) return; const c = catalog[k] || {}; const v = live?.values?.[k]; const st = live?.status?.[k];
    $("#mVal").textContent = `${nf(v, c.decimals ?? 1)} ${c.unit || ""}`;
    const sev = st ? st[0] : null; const [col, lab] = sev && sev !== "ok" ? SEV[sev] : ["var(--ok)", "Normal"];
    $("#mVal").style.color = sev && sev !== "ok" ? col : ""; $("#mPill").style.setProperty("--s", col); $("#mPill").textContent = st ? (sev === "ok" ? "Normal" : st[1] || lab) : "Sem faixa definida";
    $("#mSub").textContent = `${k}${st?.[2] ? " · normal: " + st[2] + " " + (c.unit || "") : ""}`;
    const rk = st && st[0] !== "ok" ? st[3] : null;
    if (rk !== $("#mAdv").dataset.rule) { $("#mAdv").dataset.rule = rk || ""; $("#mAdv").innerHTML = "";
      if (rk) getAdvice(rk, "").then((d) => { if (d && $("#mAdv").dataset.rule === rk) $("#mAdv").innerHTML = `<div class="adv card" style="--s:${col}">${advHtml(d)}</div>`; }); }
    if (modalMode !== "rt") return;
    const pts = hist[k] || []; const now = live?.ts || Date.now();
    lineChart($("#mChart"), [{ pts, c: COLORS[k] || "--speed", area: true }], { x0: now - 15 * 60e3, x1: now });
    renderModalStats(pts, "Últimos 15 min");
  }
  function renderModalStats(pts, label) { const s = statsOf(pts); const c = catalog[modalKey] || {}; const d = c.decimals ?? 1;
    $("#mStats").innerHTML = s ? `<div><span class="lbl">Mínimo</span><b>${nf(s.min, d)}</b></div><div><span class="lbl">Média</span><b>${nf(s.avg, d)}</b></div><div><span class="lbl">Máximo</span><b>${nf(s.max, d)}</b></div><div><span class="lbl">${label}</span><b>${s.n}</b></div>` : `<div class="empty" style="grid-column:1/-1">Sem leituras neste período.</div>`; }
  async function loadModalHist() {
    const k = modalKey; if (!k) return; const to = Date.now();
    const r = await api(`/api/series?signals=${encodeURIComponent(k)}&span=${modalSpan}&max_points=600`);
    const pts = r.series[k] || [];
    const series = [{ pts, c: COLORS[k] || "--speed", area: true }];
    if (pts.length && pts[0].length === 4) series.unshift({ pts: pts.map((p) => [p[0], p[3]]), c: "--line", w: 1, end: false }, { pts: pts.map((p) => [p[0], p[2]]), c: "--line", w: 1, end: false });
    lineChart($("#mChart"), series, { x0: to - modalSpan, x1: to });
    renderModalStats(pts, "Pontos");
  }

  // ------------------------------------------------------------ relatório
  async function loadReport() {
    const days = +$("#repDays").value;
    const r = await api(`/api/report?days=${days}`);
    const t = r.totals; const sev = Object.fromEntries(r.alerts_by_severity.map((x) => [x.severity, x.n]));
    $("#csvLink").href = `/api/export.csv?from=${Date.now() - days * 86400e3}`;
    $("#repStats").innerHTML = `<div><span class="lbl">Viagens</span><b>${t.n}</b></div><div><span class="lbl">Distância</span><b>${nf(t.km, 0)} km</b></div>
      <div><span class="lbl">Tempo rodando</span><b>${dur(t.dur)}</b></div><div><span class="lbl">Parado ligado</span><b>${dur(t.idle)}</b></div>
      <div><span class="lbl">Vel. máx</span><b>${nf(t.vmax)} km/h</b></div><div><span class="lbl">Temp. máx</span><b>${nf(t.ect)} °C</b></div>
      <div><span class="lbl">Bateria mín</span><b>${nf(t.vmin, 1)} V</b></div><div><span class="lbl">Ajuste longo médio</span><b>${nf(t.ltft, 1)}%</b></div>
      <div><span class="lbl">Nota borboleta</span><b>${nf(t.score)}</b></div><div><span class="lbl">Alertas</span><b style="color:var(--bad)">${sev.crit || 0}<span class="muted"> / </span><span style="color:var(--warn)">${sev.warn || 0}</span></b></div>`;
    lineChart($("#repChart"), [{ pts: r.daily.map((d) => [d.day, d.km || 0]), c: "--speed", bars: true }], { ymin: 0, xfmt: (x) => new Date(x).toLocaleDateString("pt-BR", { day: "2-digit", month: "2-digit" }) });
    $("#repRecs").innerHTML = recsHtml(r.recommendations ?? await recsFromAlerts(
      (await api("/api/alerts?days=90").catch(() => [])).filter((a) => (a.ts_end || a.ts) > Date.now() - days * 86400e3)));
    $("#repTop").innerHTML = r.top_alerts.length ? r.top_alerts.map((a) => alertItem({ ...a, detail: `${a.n}× no período, último` , ts: a.last_ts })).join("") : `<div class="empty">Nenhum alerta no período.</div>`;
  }
  $("#repDays").addEventListener("change", () => loadReport().catch(console.error));

  // ------------------------------------------------------------ sistema
  async function loadSystem() {
    const [s, ann] = await Promise.all([api("/api/system"), api("/api/annotations").catch(() => [])]);
    const up = s.uptime_s;
    $("#sysStats").innerHTML = `<div><span class="lbl">Endereço</span><b style="font-size:14px">${esc(s.ips.join(" · ") || "—")}</b></div><div><span class="lbl">WiFi</span><b style="font-size:14px">${esc(s.ssid || "—")}</b></div>
      <div><span class="lbl">CPU</span><b>${nf(s.cpu_temp, 0)} °C</b></div><div><span class="lbl">Alimentação</span><b style="color:${s.undervoltage_now ? "var(--bad)" : s.undervoltage_ever ? "var(--warn)" : "var(--ok)"}">${s.undervoltage_now ? "Fraca agora" : s.undervoltage_ever ? "Caiu desde o boot" : "OK"}</b></div>
      <div><span class="lbl">Disco livre</span><b>${nf(s.disk_free_gb, 1)} GB</b></div><div><span class="lbl">Banco</span><b>${nf(s.db_mb, 1)} MB</b></div>
      <div><span class="lbl">Ligado há</span><b>${dur(up)}</b></div><div><span class="lbl">Relógio</span><b>${s.clock_synced ? "Sincronizado" : "Aproximado"}</b></div>`;
    const a = live?.adapter || {};
    $("#adStats").innerHTML = `<div><span class="lbl">Estado</span><b style="font-size:14px">${esc((STATE[live?.state] || [0, live?.state || "—"])[1])}</b></div><div><span class="lbl">Adaptador</span><b style="font-size:14px">${esc(a.name || a.label || "—")}</b></div>
      <div><span class="lbl">Versão ELM</span><b style="font-size:14px">${esc(a.elm || "—")}</b></div><div><span class="lbl">Protocolo</span><b style="font-size:14px">${esc(a.protocol || "—")}</b></div>
      <div><span class="lbl">Multi-PID</span><b>${a.multi_pid == null ? "—" : a.multi_pid ? "Sim" : "Não"}</b></div><div><span class="lbl">PIDs</span><b>${live?.supported ?? "—"}</b></div>
      <div><span class="lbl">Taxa</span><b>${nf(live?.rates?.fast_hz, 1)}/s</b></div><div><span class="lbl">Versão</span><b>${esc(s.version)}</b></div>`;
    $("#ntfy").innerHTML = s.ntfy_topic ? `<ol style="margin:0;padding-left:18px;display:grid;gap:6px"><li>Instale o app <b>ntfy</b> na App Store.</li><li>Toque em <b>+</b> e assine o tópico:<br><code class="k">${esc(s.ntfy_topic)}</code></li><li>Pronto: alertas críticos chegam como notificação quando o Pi tem internet (hotspot).</li></ol>`
      : `<div class="empty">Notificações desativadas (sem tópico configurado).</div>`;
    $("#annList").innerHTML = ann.length ? ann.map((x) => `<div class="alert" style="--s:var(--volt)"><span class="ic">✎</span><div><div class="t">${esc(x.text)}</div><div class="d">${dt(x.ts)}</div></div><span></span></div>`).join("") : `<div class="empty">Nenhuma anotação.</div>`;
    $("#sysEvents").innerHTML = s.events.map((e) => `<tr><td class="num muted">${dt(e.ts)}</td><td>${esc(e.kind)}</td><td class="muted">${esc(e.detail)}</td></tr>`).join("");
  }
  async function loadRepairs() {
    const r = await api("/api/repairs").catch(() => []);
    const C = { ok: ["var(--ok)", "✓"], revertido: ["var(--warn)", "↺"], falhou: ["var(--bad)", "!"], pulado: ["var(--muted)", "–"], investigando: ["var(--warn)", "…"] };
    $("#repairList").innerHTML = r.length ? r.slice(0, 30).map((x) => { const [c, i] = C[x.result] || ["var(--muted)", "·"];
      let det = x.detail || ""; try { const j = JSON.parse(det); det = j.explanation || det; } catch { }
      return `<details class="alert" style="--s:${c}"><summary><span class="ic">${i}</span><div><div class="t">${esc(x.target)}: ${esc(x.action)}</div><div class="d">${dt(x.ts)} · ${esc(x.kind)}${x.kind === "ia" ? " · Claude Fable" : ""}</div></div><span class="pill">${esc(x.result)}</span></summary><div class="adv">${esc(det).slice(0, 3000) || "Sem detalhes."}</div></details>`; }).join("")
      : `<div class="empty">Nenhum problema encontrado até agora. 👍</div>`;
  }
  async function loadAiSettings() {
    const a = await api("/api/ai/settings").catch(() => null); if (!a) return;
    (a.provider === "openai" ? $("#provO") : $("#provA")).checked = true;
    $("#aiHintA").textContent = a.anthropic_key ? `(salva ${a.anthropic_key})` : "(não configurada)";
    $("#aiHintO").textContent = a.openai_key ? `(salva ${a.openai_key})` : "(não configurada)";
    $("#aiModelO").value = a.openai_model || ""; $("#aiModelA").textContent = a.anthropic_model;
  }
  $("#aiForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const body = { provider: document.querySelector('input[name="aiprov"]:checked')?.value, anthropic_key: $("#aiKeyA").value, openai_key: $("#aiKeyO").value, openai_model: $("#aiModelO").value };
    const r = await fetch("/api/ai/settings", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    const j = await r.json().catch(() => ({}));
    $("#aiMsg").textContent = r.ok ? "Salvo." : "Erro: " + (j.error || r.status);
    if (r.ok) { $("#aiKeyA").value = ""; $("#aiKeyO").value = ""; loadAiSettings(); }
  });
  $("#annForm").addEventListener("submit", async (e) => {
    e.preventDefault(); const text = $("#annText").value.trim(); if (!text) return;
    await fetch("/api/annotations", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text, ts: Date.now() }) });
    $("#annText").value = ""; loadSystem().catch(console.error);
  });

  // ------------------------------------------------------------ diagnóstico
  const AIRV = { entrada_de_ar: ["var(--bad)", "Assinatura de entrada falsa de ar"], suspeito: ["var(--warn)", "Suspeito: MAF baixo em marcha lenta"], maf_baixo: ["var(--warn)", "MAF lendo baixo em todas as cargas"], normal: ["var(--ok)", "Normal"] };
  async function loadDiag() {
    const d = await api("/api/diagnostics?days=60");
    const tr = d.trips; const last = [...tr].reverse().find((t) => t.air_json);
    if (last) { const b = last.air_json.buckets; const [c, l] = AIRV[last.air_verdict] || ["var(--muted)", "Dados insuficientes"];
      $("#airBox").innerHTML = `<div class="alert" style="--s:${c}"><span class="ic">${last.air_verdict === "normal" ? "✓" : "!"}</span><div><div class="t">${l}</div><div class="d">${esc(last.air_json.txt)} · viagem de ${dt(last.start_ts)}</div></div><span></span></div>
        <div class="tbl" style="margin-top:10px"><table><thead><tr><th>Condição</th><th>MAF ÷ esperado</th><th>Típico</th><th>Leituras</th></tr></thead><tbody>${Object.entries(b).map(([k, x]) => { const ok = x.ratio == null || (x.ratio >= x.typical[0] && x.ratio <= x.typical[1]);
          return `<tr><td>${({ marcha_lenta: "Marcha lenta", leve: "Carga leve", media: "Carga média", alta: "Carga alta" })[k]}</td><td class="num" style="color:${x.ratio == null ? "" : ok ? "var(--ok)" : "var(--bad)"}">${x.ratio == null ? "—" : Math.round(x.ratio * 100) + "%"}</td><td class="num">${Math.round(x.typical[0] * 100)}–${Math.round(x.typical[1] * 100)}%</td><td class="num">${x.n}</td></tr>`; }).join("")}</tbody></table></div>`;
    } else $("#airBox").innerHTML = `<div class="empty">Aparece após a primeira viagem com motor quente (precisa de marcha lenta e de carga).</div>`;
    const r = d.readiness || live?.readiness;
    $("#monBox").innerHTML = r && r.monitors?.length ? `<div class="muted" style="font-size:13px">${r.ready} de ${r.total} prontos</div>` + r.monitors.map((m) => `<div class="alert" style="--s:${m.ready ? "var(--ok)" : "var(--warn)"}"><span class="ic">${m.ready ? "✓" : "…"}</span><div><div class="t">${esc(m.name)}</div><div class="d">${m.ready ? "Concluído" : "Ainda não concluído: rode mais para a ECU testar"}</div></div><span class="pill">${m.ready ? "Pronto" : "Pendente"}</span></div>`).join("")
      : `<div class="empty">Lido a cada 20 s com a ignição ligada.</div>`;
    lineChart($("#crankChart"), [{ pts: tr.filter((t) => t.rest_v).map((t) => [t.start_ts, t.rest_v]), c: "--volt", dots: true, r: 4 }, { pts: tr.filter((t) => t.crank_v).map((t) => [t.start_ts, t.crank_v]), c: "--bad", dots: true, r: 4 }, { pts: tr.length ? [[tr[0].start_ts, 9.6], [tr.at(-1).start_ts, 9.6]] : [], c: "--line", w: 1, end: false }], { ymin: 7, ymax: 13.5 });
    lineChart($("#warmChart"), [{ pts: tr.filter((t) => t.warmup_s).map((t) => [t.start_ts, t.warmup_s / 60]), c: "--temp", bars: true }], { ymin: 0 });
    const eco = tr.filter((t) => t.eco_score != null);
    $("#ecoNow").textContent = eco.length ? Math.round(eco.slice(-5).reduce((a, t) => a + t.eco_score, 0) / Math.min(5, eco.length)) + "/100" : "—";
    lineChart($("#ecoChart"), [{ pts: eco.map((t) => [t.start_ts, t.eco_score]), c: "--ok", area: true }], { ymin: 0, ymax: 100 });
    const tips = {}; for (const t of eco.slice(-10)) for (const x of t.eco_json?.dicas || []) tips[x] = (tips[x] || 0) + 1;
    $("#ecoTips").innerHTML = Object.keys(tips).length ? Object.entries(tips).sort((a, b) => b[1] - a[1]).map(([t, n]) => `<div class="alert" style="--s:var(--ok)"><span class="ic">💡</span><div><div class="t">${esc(t)}</div><div class="d">em ${n} das últimas viagens</div></div><span></span></div>`).join("") : `<div class="empty">Sem dicas por enquanto.</div>`;
  }

  // recomendações ("o que fazer") para os dados negativos de um período
  function recsHtml(recs) {
    if (!recs || !recs.length) return `<div class="alert" style="--s:var(--ok)"><span class="ic">✓</span><div><div class="t">Nada negativo no período</div><div class="d">Sem alertas, códigos ou diagnósticos ruins. Continue acompanhando.</div></div><span class="pill">OK</span></div>`;
    return recs.map((x) => { const [c, l, i] = SEV[x.severity] || SEV.info;
      return `<details class="alert" style="--s:${c}"><summary><span class="ic">${i}</span><div><div class="t">${esc(x.title)}${x.n > 1 ? ` · ${x.n}×` : ""}</div>
        <div class="d">${esc(x.urgencia_txt || "")}${x.fazer?.[0] ? " · " + esc(x.fazer[0]) : ""}</div></div><span style="display:grid;justify-items:end;gap:4px"><span class="pill">${l}</span><span class="more">Detalhes ▾</span></span></summary>
        <div class="adv">${x.evidence ? `<div><h4>O que os dados mostraram</h4>${esc(x.evidence)}</div>` : ""}<div><h4>O que significa</h4>${esc(x.o_que || "")}</div>
        ${x.causas?.length ? `<div><h4>Causas prováveis</h4><ul>${x.causas.map((y) => `<li>${esc(y)}</li>`).join("")}</ul></div>` : ""}
        ${x.verificar?.length ? `<div><h4>O que verificar</h4><ul>${x.verificar.map((y) => `<li>${esc(y)}</li>`).join("")}</ul></div>` : ""}
        <div><h4>O que fazer</h4><ol>${(x.fazer || []).map((y) => `<li>${esc(y)}</li>`).join("")}</ol></div>
        <div><h4>Urgência</h4><span class="urg">${esc(x.urgencia_txt || "")}</span>${x.rodar ? ` · ${esc(x.rodar)}` : ""}</div></div></details>`; }).join("");
  }

  // relatório sem "recommendations" (dados enviados por uma versão antiga do Pi): monta a partir dos alertas + orientações
  const RANK = { crit: 0, warn: 1, info: 2 };
  async function recsFromAlerts(alerts) {
    const groups = {};
    for (const a of alerts || []) {
      const sev = a.severity || a.sev; if (!a.rule || !["crit", "warn"].includes(sev)) continue;
      const head = a.rule.split(":")[0]; const key = head === "z" || head === "missing" || /^R|^P\d/.test(head) ? head : a.rule;
      const g = (groups[key] ??= { rule: a.rule, source: a.source || "", title: a.title, severity: sev, n: 0, evidence: a.detail || "" });
      g.n += a.n || a.count || 1; if (RANK[sev] < RANK[g.severity]) g.severity = sev;
    }
    const list = Object.values(groups).sort((x, y) => RANK[x.severity] - RANK[y.severity] || y.n - x.n).slice(0, 12);
    const out = [];
    for (const g of list) {
      const d = await getAdvice(g.rule, g.source);
      if (!d) { out.push({ ...g, o_que: "", fazer: [], urgencia_txt: "" }); continue; }
      const urg = g.severity !== "crit" && d.urgencia === "parar" ? "Resolver nos próximos dias (pare se piorar)" : d.urgencia_txt;
      out.push({ ...g, o_que: d.o_que, causas: (d.causas || []).slice(0, 4), verificar: (d.verificar || []).slice(0, 3), fazer: d.fazer || [], urgencia_txt: urg, rodar: d.rodar });
    }
    return out;
  }

  // ------------------------------------------------------------ relatório semanal / mensal
  let perKind = "week", perOff = 0;
  async function loadPeriod() {
    const r = await api(`/api/report/period?kind=${perKind}&offset=${perOff}`);
    const c = r.cur, d = r.delta, sv = r.alerts_by_severity || {};
    const dl = (x, inv) => x == null ? "" : `<span style="font-size:12px;color:${(x > 0) !== !!inv ? "var(--ok)" : "var(--bad)"}"> ${x > 0 ? "▲" : "▼"}${nf(Math.abs(x), 0)}%</span>`;
    $("#perTitle").textContent = r.label;
    $("#perNext").disabled = perOff >= 0;
    $("#perStats").innerHTML = `<div><span class="lbl">Viagens</span><b>${c.trips}</b></div><div><span class="lbl">Distância</span><b>${nf(c.km, 0)} km${dl(d.km)}</b></div>
      <div><span class="lbl">Motor ligado</span><b>${dur(c.dur)}</b></div><div><span class="lbl">Combustível</span><b>${nf(c.fuel, 1)} L${dl(d.fuel, true)}</b></div>
      <div><span class="lbl">Consumo</span><b>${nf(c.kml, 1)} km/L${dl(d.kml)}</b></div><div><span class="lbl">Real (tanques)</span><b>${nf(r.real_kml, 1)} km/L</b></div>
      <div><span class="lbl">Gasto</span><b>${r.spent ? "R$ " + nf(r.spent, 2) : r.est_cost ? "≈ R$ " + nf(r.est_cost, 2) : "—"}</b></div><div><span class="lbl">Vel. média</span><b>${nf(c.avg_speed)} km/h</b></div>
      <div><span class="lbl">Direção econômica</span><b>${nf(c.eco_score)}${dl(d.eco_score)}</b></div><div><span class="lbl">Borboleta</span><b>${nf(c.tb_score)}${dl(d.tb_score)}</b></div>
      <div><span class="lbl">Alertas</span><b><span style="color:var(--bad)">${sv.crit || 0}</span> / <span style="color:var(--warn)">${sv.warn || 0}</span></b></div><div><span class="lbl">Melhor 0–100</span><b>${r.best_0_100 ? nf(r.best_0_100, 2) + " s" : "—"}</b></div>`;
    lineChart($("#perChart"), [{ pts: r.daily.map((x) => [x.day + 43200e3, x.km || 0]), c: "--speed", bars: true }], { ymin: 0, x0: r.from, x1: r.to, xfmt: (x) => new Date(x).toLocaleDateString("pt-BR", { day: "2-digit", month: "2-digit" }) });
    $("#perRecs").innerHTML = recsHtml(r.recommendations ?? await recsFromAlerts(r.alerts));
    $("#perList").innerHTML = (r.alerts.slice(0, 5).map((a) => alertItem({ ...a, ts: a.last_ts, detail: `${a.n}× no período`, count: 1 })).join("") +
      r.tips.map((t) => `<div class="alert" style="--s:var(--ok)"><span class="ic">💡</span><div><div class="t">${esc(t)}</div></div><span></span></div>`).join("")) || `<div class="empty">Nada a destacar no período.</div>`;
  }
  $("#perKind").addEventListener("click", (e) => { const b = e.target.closest("[data-k]"); if (!b) return; perKind = b.dataset.k; perOff = 0;
    $("#perKind").querySelectorAll("button").forEach((x) => x.setAttribute("aria-pressed", x === b)); loadPeriod().catch(console.error); });
  $("#perPrev").addEventListener("click", () => { perOff--; loadPeriod().catch(console.error); });
  $("#perNext").addEventListener("click", () => { if (perOff < 0) { perOff++; loadPeriod().catch(console.error); } });

  // ------------------------------------------------------------ análise por IA
  function md(src) {
    const inline = (t) => esc(t).replace(/\*\*(.+?)\*\*/g, "<b>$1</b>").replace(/`([^`]+)`/g, "<code>$1</code>").replace(/(^|\s)_(.+?)_(?=\s|$)/g, "$1<i>$2</i>");
    let html = "", list = null;
    const close = () => { if (list) { html += `</${list}>`; list = null; } };
    for (const line of src.split("\n")) {
      let m;
      if ((m = line.match(/^(#{1,3})\s+(.*)/))) { close(); html += `<h${Math.min(3, m[1].length + 1)}>${inline(m[2])}</h${Math.min(3, m[1].length + 1)}>`; }
      else if ((m = line.match(/^\s*[-*•]\s+(.*)/))) { if (list !== "ul") { close(); html += "<ul>"; list = "ul"; } html += `<li>${inline(m[1])}</li>`; }
      else if ((m = line.match(/^\s*\d+[.)]\s+(.*)/))) { if (list !== "ol") { close(); html += "<ol>"; list = "ol"; } html += `<li>${inline(m[1])}</li>`; }
      else if (!line.trim()) close();
      else { close(); html += `<p>${inline(line)}</p>`; }
    }
    close(); return html;
  }
  let aiTimer = null;
  async function aiPoll() {
    clearTimeout(aiTimer);
    const j = await api("/api/ai/analysis").catch((e) => ({ status: "error", error: e.message }));
    if (j.status === "running") {
      $("#aiSub").textContent = "Analisando…"; $("#aiRun").disabled = true;
      $("#aiBody").innerHTML = `<p><span class="spin"></span>O Claude está lendo sensores, viagens, alertas e diagnósticos (${j.elapsed_s || 0} s). Costuma levar 1–3 min.</p>`;
      aiTimer = setTimeout(aiPoll, 3000); return;
    }
    $("#aiRun").disabled = false;
    if (j.status === "error") { $("#aiSub").textContent = "Erro"; $("#aiBody").innerHTML = `<div class="alert" style="--s:var(--bad)"><span class="ic">!</span><div><div class="t">Não foi possível analisar</div><div class="d">${esc(j.error)}</div></div><span></span></div>`; return; }
    if (j.status === "done" && j.text) { $("#aiSub").textContent = `${j.ts ? dt(j.ts) : "agora"} · ${j.model || ""}`; $("#aiBody").innerHTML = md(j.text); return; }
    $("#aiSub").textContent = ""; $("#aiBody").innerHTML = CLOUD?.readonly ? `<div class="empty">Nenhuma análise ainda. Ela é pedida no painel do Pi e aparece aqui depois da sincronização.</div>` : `<div class="empty">Nenhuma análise ainda. Toque em <b>Analisar agora</b>: o Pi envia o resumo dos dados para o Claude (precisa de internet).</div>`;
  }
  $("#aiBtn").addEventListener("click", () => { $("#aiModal").hidden = false; aiPoll(); });
  $("#aiClose").addEventListener("click", () => { $("#aiModal").hidden = true; clearTimeout(aiTimer); });
  $("#aiModal").addEventListener("click", (e) => { if (e.target.id === "aiModal") { $("#aiModal").hidden = true; clearTimeout(aiTimer); } });
  $("#aiRun").addEventListener("click", async () => { $("#aiRun").disabled = true; const r = await fetch("/api/ai/analyze", { method: "POST" }); const j = await r.json().catch(() => ({}));
    if (j.queued) { $("#aiRun").disabled = false; $("#aiSub").textContent = "Pedido enviado: o Pi faz a análise quando conectar na internet e o laudo aparece aqui."; return; } aiPoll(); });

  // ------------------------------------------------------------ tema
  const setTheme = (t) => { document.documentElement.dataset.theme = t; try { localStorage.setItem("theme", t); } catch { }
    document.querySelectorAll("#theme button").forEach((b) => b.setAttribute("aria-pressed", b.dataset.t === t)); };
  $("#theme").addEventListener("click", (e) => { const b = e.target.closest("[data-t]"); if (b) setTheme(b.dataset.t); });
  setTheme(document.documentElement.dataset.theme || "dark");

  // ------------------------------------------------------------ roteamento
  const LOAD = { diagnostico: loadDiag, consumo: loadFuel, vivo: () => Promise.all([loadThrottle("live"), loadSummary(), loadAccel()]), borboleta: loadThrottle, viagens: loadTrips, alertas: loadAlerts, sensores: loadSignals, relatorio: () => Promise.all([loadReport(), loadPeriod()]), sistema: () => Promise.all([loadSystem(), loadAiSettings(), loadRepairs()]) };
  function route() {
    const v = (location.hash || "#vivo").slice(1); const view = LOAD[v] ? v : "vivo";
    document.querySelectorAll(".view[id^=v-]").forEach((s) => { s.hidden = s.id !== "v-" + view; });
    document.querySelectorAll("#tabs a").forEach((a) => a.dataset.v === view ? a.setAttribute("aria-current", "page") : a.removeAttribute("aria-current"));
    LOAD[view]().catch((e) => console.error(view, e));
  }
  window.addEventListener("hashchange", route);
  setInterval(() => { const v = (location.hash || "#vivo").slice(1); if (["vivo", "alertas", "viagens", "consumo"].includes(v)) LOAD[v]?.().catch(() => { }); }, 30000);

  // ------------------------------------------------------------ modo nuvem (painel online com a cópia dos dados)
  let CLOUD = null;
  function applyCloud(env) {
    CLOUD = env; document.documentElement.classList.add("cloud");
    const age = env.last_sync_ts ? (Date.now() - env.last_sync_ts) / 1000 : null;
    const on = age != null && age < 120;
    $("#cloudBar").hidden = false;
    $("#cloudBar").innerHTML = `<span class="dot" style="--c:${on ? "var(--ok)" : "var(--muted)"}"></span>
      <b>Painel online</b> · ${on ? "Pi conectado agora (dados ao vivo)" : env.last_sync_ts ? "Pi offline · dados até " + dt(env.last_sync_ts) : "aguardando a primeira sincronização do Pi"}
      ${env.pending_cmds ? ` · ${env.pending_cmds} ação(ões) esperando o Pi` : ""}
      ${env.readonly ? '<span class="muted" style="margin-left:auto">somente visualização</span>' : '<a href="/logout" style="margin-left:auto">Sair</a>'}`;
    const hide = ["#accGo", "#csvLink"].concat(env.readonly ? ["#fillBtn", "#annForm", "#aiRun"] : []);
    for (const id of hide) { const el = $(id); if (el) el.hidden = true; }
    const f = $("#aiForm"); if (f) f.innerHTML = `<div class="empty">As chaves de IA são configuradas no painel do Pi (rede local). Aqui o botão <b>Analisar dados</b> pede a análise ao Pi na próxima conexão e mostra o último laudo.</div>`;
  }
  fetch("/api/env").then((r) => r.ok ? r.json() : null).then((env) => { if (env && env.cloud) { applyCloud(env); setInterval(() => fetch("/api/env").then((r) => r.json()).then(applyCloud).catch(() => { }), 60000); } }).catch(() => { })
    .finally(() => api("/api/signals").then((s) => { for (const x of s) catalog[x.key] = x; }).catch(() => { }).finally(() => { connectStream(); route(); }));
})();
