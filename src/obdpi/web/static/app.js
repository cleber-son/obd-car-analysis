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
  };
  const COLORS = { rpm: "--rpm", speed: "--speed", ect: "--temp", iat: "--temp", batt: "--volt", ecu_volt: "--volt", tps: "--tps", tps_rel: "--tps", tps_b: "--tps", tps_cmd: "--speed", pedal_d: "--pedal", pedal_e: "--pedal", stft1: "--tps", ltft1: "--tps", map: "--speed", load: "--rpm", timing: "--volt" };
  let catalog = {};      // key -> {name, unit, decimals}
  let live = null;
  const buf = [];        // histórico ao vivo para o gráfico pedal × borboleta

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
    return `<div class="alert" style="--s:${c}"><span class="ic">${i}</span><div><div class="t">${esc(a.title)}</div><div class="d">${esc(a.detail || "")} · ${dt(a.ts)}</div></div><span class="pill">${l}</span></div>`; };

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
    const stale = d.age_s != null && d.age_s > 15;
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
    // sensores
    const recentBad = {}; for (const a of d.alerts || []) if (Date.now() - a.ts < 15 * 60e3) recentBad[a.source === "throttle" ? "tps" : (a.rule || "").split(":")[0]] = a.sev;
    const keys = Object.keys(v).sort((a, b) => (catalog[a]?.name || a).localeCompare(catalog[b]?.name || b, "pt"));
    $("#sensCount").textContent = `${keys.length} sinais · ${d.supported || 0} PIDs suportados pela ECU`;
    $("#liveSensors").innerHTML = keys.length ? keys.map((k) => { const c = catalog[k] || {}; const sv = recentBad[k];
      return `<button class="sensor" data-k="${esc(k)}" style="--s:${sv ? SEV[sv][0] : `var(${COLORS[k] || "--line"})`}"><span class="n">${esc(c.name || k)}</span>
      <span class="val num">${nf(v[k], c.decimals ?? 1)}<small>${esc(c.unit || "")}</small></span><span class="pid">${esc(k)}${d.value_ts?.[k] ? " · " + Math.max(0, Math.round((d.ts - d.value_ts[k]) / 1000)) + " s" : ""}</span></button>`; }).join("")
      : `<div class="empty" style="grid-column:1/-1">Aguardando a primeira leitura do OBD. Ligue a ignição com o adaptador conectado.</div>`;
    // banners de alerta recentes
    const crit = (d.alerts || []).filter((a) => Date.now() - a.ts < 10 * 60e3 && a.sev !== "info").slice(0, 3);
    $("#banners").innerHTML = crit.map((a) => `<div class="banner" style="--s:${SEV[a.sev][0]}"><span class="ic">!</span><b>${esc(a.title)}</b><span class="muted">${esc(a.detail)} · ${tm(a.ts)}</span></div>`).join("");
    const nb = (d.alerts || []).filter((a) => Date.now() - a.ts < 24 * 3600e3 && a.sev === "crit").length;
    $("#badge").hidden = !nb; $("#badge").textContent = nb;
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
    R1: "Borboleta segue o comando", R2: "Sensores do pedal coerentes", R3: "Sem travamento / modo de emergência", R3b: "Pedal abre a borboleta",
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
    const trips = await api("/api/trips?limit=100");
    $("#tripRows").innerHTML = trips.length ? trips.map((t) => { const s = t.tb_score; const c = s == null ? "var(--muted)" : s >= 85 ? "var(--ok)" : s >= 60 ? "var(--warn)" : "var(--bad)";
      return `<tr class="click" data-id="${t.id}"><td>${dt(t.start_ts)}${t.finalized ? "" : ' <span class="pill" style="--s:var(--ok)">agora</span>'}</td><td class="num">${dur(t.duration_s ?? ((t.end_ts - t.start_ts) / 1000))}</td>
      <td class="num">${nf(t.distance_km, 1)}</td><td class="num">${nf(t.avg_speed)} km/h</td><td class="num">${nf(t.max_ect)} °C</td><td class="num">${t.n_alerts ?? 0}</td>
      <td><div class="bar" style="--s:${c}" title="${s ?? "—"}/100"><i style="width:${s ?? 0}%"></i></div></td></tr>`; }).join("")
      : `<tr><td colspan="7"><div class="empty">Nenhuma viagem ainda. A primeira aparece quando o motor ligar com o OBD conectado.</div></td></tr>`;
  }
  async function showTrip(id) {
    const t = await api(`/api/trips/${id}`);
    const S = t.series;
    $("#tripDetail").innerHTML = `<div class="card"><div class="row-head"><h2>Viagem de ${dt(t.start_ts)}</h2><span class="muted">${esc(t.end_reason || "em andamento")}</span></div>
      <div class="stats" style="margin-top:12px">
        <div><span class="lbl">Duração</span><b>${dur(t.duration_s)}</b></div><div><span class="lbl">Distância</span><b>${nf(t.distance_km, 1)} km</b></div>
        <div><span class="lbl">Parado</span><b>${dur(t.idle_s)}</b></div><div><span class="lbl">Vel. máx</span><b>${nf(t.max_speed)} km/h</b></div>
        <div><span class="lbl">Rot. máx</span><b>${nf(t.max_rpm)}</b></div><div><span class="lbl">Temp. máx</span><b>${nf(t.max_ect)} °C</b></div>
        <div><span class="lbl">Bateria</span><b>${nf(t.min_volt, 1)}–${nf(t.max_volt, 1)} V</b></div><div><span class="lbl">Borboleta</span><b>${t.tb_score ?? "—"}/100</b></div></div></div>
      <div class="grid2e"><div class="card"><h2>Rotação e velocidade</h2><div class="legend" style="margin:6px 0"><span style="--c:var(--rpm)">rpm ÷ 50</span><span style="--c:var(--speed)">km/h</span></div><svg class="chart" id="tc1" viewBox="0 0 600 190" preserveAspectRatio="none"></svg></div>
      <div class="card"><h2>Pedal × borboleta</h2><div class="legend" style="margin:6px 0"><span style="--c:var(--pedal)">Pedal</span><span style="--c:var(--tps)">Borboleta</span><span style="--c:var(--speed)">Comandado</span></div><svg class="chart" id="tc2" viewBox="0 0 600 190" preserveAspectRatio="none"></svg></div>
      <div class="card"><h2>Temperatura do motor</h2><svg class="chart" id="tc3" viewBox="0 0 600 190" preserveAspectRatio="none"></svg></div>
      <div class="card"><h2>Ajustes de combustível</h2><div class="legend" style="margin:6px 0"><span style="--c:var(--tps)">Curto</span><span style="--c:var(--volt)">Longo</span></div><svg class="chart" id="tc4" viewBox="0 0 600 190" preserveAspectRatio="none"></svg></div></div>
      <div class="card"><h2>Alertas desta viagem</h2><div class="list" style="margin-top:10px">${t.alerts.length ? t.alerts.map(alertItem).join("") : '<div class="empty">Nenhum alerta.</div>'}</div></div>`;
    lineChart($("#tc1"), [{ pts: (S.rpm || []).map((p) => [p[0], p[1] / 50]), c: "--rpm" }, { pts: S.speed || [], c: "--speed" }], { ymin: 0 });
    lineChart($("#tc2"), [{ pts: S.tps_cmd || [], c: "--speed", w: 1.5 }, { pts: S.pedal_d || [], c: "--pedal" }, { pts: S.tps || [], c: "--tps" }], { ymin: 0, ymax: 100 });
    lineChart($("#tc3"), [{ pts: S.ect || [], c: "--temp", area: true }]);
    lineChart($("#tc4"), [{ pts: S.stft1 || [], c: "--tps" }, { pts: S.ltft1 || [], c: "--volt" }]);
    $("#tripDetail").scrollIntoView({ behavior: "smooth" });
  }
  $("#tripRows").addEventListener("click", (e) => { const tr = e.target.closest("tr[data-id]"); if (tr) showTrip(tr.dataset.id).catch(console.error); });

  // ------------------------------------------------------------ alertas / DTC
  async function loadAlerts() {
    const sev = $("#alertFilter").value;
    const [al, d] = await Promise.all([api(`/api/alerts?days=90${sev ? "&severity=" + sev : ""}`), api("/api/dtc")]);
    $("#alertList").innerHTML = al.length ? al.map(alertItem).join("") : `<div class="empty">Nenhum alerta nos últimos 90 dias.</div>`;
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
    const r = await api(`/api/series?signals=${encodeURIComponent(k)}&from=${to - span}&to=${to}&max_points=500`);
    lineChart($("#sigChart"), [{ pts: r.series[k] || [], c: COLORS[k] || "--speed", area: true }], { x0: to - span, x1: to });
    $("#sigChartCard").scrollIntoView({ behavior: "smooth" });
  }
  $("#sigRows").addEventListener("click", (e) => { const tr = e.target.closest("tr[data-k]"); if (tr) showSignal(tr.dataset.k).catch(console.error); });
  $("#sigRange").addEventListener("change", () => sigKey && showSignal(sigKey).catch(console.error));
  $("#liveSensors").addEventListener("click", (e) => { const b = e.target.closest("[data-k]"); if (b) { location.hash = "sensores"; setTimeout(() => showSignal(b.dataset.k).catch(console.error), 50); } });

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
  $("#annForm").addEventListener("submit", async (e) => {
    e.preventDefault(); const text = $("#annText").value.trim(); if (!text) return;
    await fetch("/api/annotations", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text }) });
    $("#annText").value = ""; loadSystem().catch(console.error);
  });

  // ------------------------------------------------------------ roteamento
  const LOAD = { vivo: () => loadThrottle("live"), borboleta: loadThrottle, viagens: loadTrips, alertas: loadAlerts, sensores: loadSignals, relatorio: loadReport, sistema: loadSystem };
  function route() {
    const v = (location.hash || "#vivo").slice(1); const view = LOAD[v] ? v : "vivo";
    document.querySelectorAll(".view[id^=v-]").forEach((s) => { s.hidden = s.id !== "v-" + view; });
    document.querySelectorAll("#tabs a").forEach((a) => a.dataset.v === view ? a.setAttribute("aria-current", "page") : a.removeAttribute("aria-current"));
    LOAD[view]().catch((e) => console.error(view, e));
  }
  window.addEventListener("hashchange", route);
  setInterval(() => { const v = (location.hash || "#vivo").slice(1); if (["vivo", "alertas", "viagens"].includes(v)) LOAD[v]?.().catch(() => { }); }, 30000);

  api("/api/signals").then((s) => { for (const x of s) catalog[x.key] = x; }).catch(() => { }).finally(() => { connectStream(); route(); });
})();
