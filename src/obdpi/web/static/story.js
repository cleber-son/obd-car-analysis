/* window.obdStory — "Diagnóstico da viagem": conta como foi a viagem (manchete, história, saúde do motor, dinheiro,
   direção, mini gráficos, linha do tempo e o trajeto). O conteúdo vem pronto de /api/trips/<id>/story, montado no Pi por
   regras fixas (analytics/tripstory.py) e copiado para o painel online pelo obd-sync: aqui é só desenhar, falar e copiar.
   Qualquer elemento com data-story="<id da viagem>" abre o diagnóstico (lista de viagens, detalhe e cartão do Ao vivo).
   O trajeto usa as rotas /api/gps/* (no painel online, só com a localização desbloqueada). */
(() => {
  "use strict";
  const $ = (s, r = document) => r.querySelector(s);
  const U = () => window.OBD || {};
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const nf = (v, d = 0) => (v === null || v === undefined || Number.isNaN(v)) ? "—" : Number(v).toLocaleString("pt-BR", { minimumFractionDigits: d, maximumFractionDigits: d });
  const cloud = () => (U().cloud ? U().cloud() : null);
  const ST = {
    ok: ["var(--ok)", "OK", "✓"], note: ["var(--speed)", "Observação", "i"], warn: ["var(--warn)", "Atenção", "!"],
    bad: ["var(--bad)", "Cuidado", "!"], na: ["var(--muted)", "Sem dados", "–"],
  };
  let cur = null;            // { id, story, map }
  let seq = 0;

  // ------------------------------------------------------------ voz (o iPhone só fala se começar dentro de um toque)
  const plain = (t) => String(t ?? "").replace(/<[^>]+>/g, " ").replace(/[\u{1F000}-\u{1FAFF}←-➿⬀-⯿️〰]/gu, "").replace(/\s+/g, " ").trim();
  // a voz tropeça em símbolos: troca pelos nomes
  const spoken = (t) => plain(t).replace(/λ/g, "lambda").replace(/°C/g, "graus").replace(/km\/L/g, "quilômetros por litro").replace(/km\/h/g, "quilômetros por hora")
    .replace(/(\d) V\b/g, "$1 volts").replace(/(\d) L\b/g, "$1 litros").replace(/(\d) mL\b/g, "$1 mililitros").replace(/±/g, "mais ou menos ").replace(/[−]/g, "menos ");
  const talking = [];        // o Safari descarta a fala se o objeto for coletado
  let speaking = false;
  function stopVoice() { speaking = false; try { window.speechSynthesis?.cancel(); } catch { } talking.length = 0; paintVoice(); }
  function paintVoice() { const b = $("#stListen"); if (b) { b.textContent = speaking ? "⏹ Parar" : "🔊 Ouvir"; b.setAttribute("aria-pressed", String(speaking)); } }
  function listen(story) {
    const ss = window.speechSynthesis;
    if (!ss || !window.SpeechSynthesisUtterance) { msg("Este navegador não tem voz. O texto está logo abaixo."); return; }
    if (speaking) return stopVoice();
    const parts = [story.mood?.headline, ...(story.narrative || [])].map(spoken).filter(Boolean)
      .flatMap((p) => p.length > 220 ? p.split(/[.;:] /).map((x, i, a) => i < a.length - 1 ? x + "." : x) : [p]);
    const vs = ss.getVoices?.() || [];
    const voice = vs.find((v) => /pt[-_]BR/i.test(v.lang)) || vs.find((v) => /^pt/i.test(v.lang)) || null;
    try { ss.cancel(); } catch { }
    speaking = true; paintVoice();
    const mine = ++seq;
    // logo depois de cancel() o iPhone descarta a fala: espera um instante (ainda dentro da permissão do toque)
    setTimeout(() => {
      if (!speaking || mine !== seq) return;
      parts.forEach((p, i) => {
        const u = new SpeechSynthesisUtterance(p); u.lang = "pt-BR"; u.rate = 1.03; if (voice) u.voice = voice;
        if (i === parts.length - 1) u.onend = u.onerror = () => { if (mine === seq) { speaking = false; talking.length = 0; paintVoice(); } };
        talking.push(u);
        try { ss.resume?.(); ss.speak(u); } catch { }
      });
    }, 160);
  }

  // ------------------------------------------------------------ copiar / compartilhar
  const msg = (t) => { const m = $("#stMsg"); if (m) m.textContent = t; };
  async function copy(text) {
    let ok = false;
    try { await navigator.clipboard.writeText(text); ok = true; } catch { }
    if (!ok) {
      try { const ta = document.createElement("textarea"); ta.value = text; ta.style.cssText = "position:fixed;opacity:0"; document.body.appendChild(ta); ta.select(); ok = document.execCommand("copy"); ta.remove(); } catch { }
    }
    msg(ok ? "Texto copiado. É só colar no WhatsApp." : "Não consegui copiar sozinho. Segure o dedo sobre o texto para selecionar.");
  }
  async function share(story) {
    try { await navigator.share({ title: "Delfos · " + story.title, text: story.share }); } catch (e) { if (e?.name !== "AbortError") copy(story.share); }
  }

  // ------------------------------------------------------------ pedaços da tela
  const hhmm = (ts) => U().tm ? U().tm(ts) : new Date(ts).toLocaleTimeString("pt-BR", { hour: "2-digit", minute: "2-digit" });
  function sysHtml(s) {
    const [c, l, i] = ST[s.status] || ST.na;
    return `<details class="alert st-sys" style="--s:${c}"><summary><span class="ic">${i}</span><div><div class="st-sh"><span class="t">${esc(s.icon)} ${esc(s.name)}</span><span class="pill">${l}</span></div><div class="d">${esc(s.txt)}</div></div></summary>
      <div class="st-ev">${s.ev?.length ? `<div class="lbl">Os números por trás</div><ul>${s.ev.map((e) => `<li>${esc(e)}</li>`).join("")}</ul>` : `<div class="muted">Sem medição suficiente nesta viagem.</div>`}</div></details>`;
  }
  function moneyHtml(s) {
    const m = s.money || {};
    if (m.litros == null && m.custo == null) return `<div class="empty">Sem dados de consumo nesta viagem.</div>`;
    const p = Math.max(0, Math.min(1, m.parado?.parte || 0)), g = Math.max(0, Math.min(1 - p, m.giro_alto?.parte || 0)), r = Math.max(0, 1 - p - g);
    const seg = (w, c, t) => w > 0.004 ? `<i style="width:${(w * 100).toFixed(1)}%;background:${c}" title="${esc(t)}"></i>` : "";
    const part = (x, lbl, c) => x?.litros != null ? `<span><i style="background:${c}"></i>${lbl}: <b>${x.parte != null ? nf(x.parte * 100) + " %" : nf(x.litros, 2) + " L"}</b>${x.custo ? " · R$ " + nf(x.custo, 2) : ""}</span>` : "";
    const dk = m.km_l != null && m.km_l_usual != null ? m.km_l - m.km_l_usual : null;
    return `<div class="stats">
        <div><span class="lbl">Gastou</span><b>${m.custo != null ? "R$ " + nf(m.custo, 2) : nf(m.litros, 2) + " L"}</b><span class="muted st-s">${m.custo != null && m.litros != null ? nf(m.litros, 2) + " L" + (m.preco_l ? " a R$ " + nf(m.preco_l, 2) : "") : "sem preço registrado"}</span></div>
        <div><span class="lbl">Consumo</span><b>${m.km_l != null ? nf(m.km_l, 1) + " km/L" : "—"}</b><span class="muted st-s">${m.km_l_usual != null ? `seu normal: ${nf(m.km_l_usual, 1)}${dk != null && Math.abs(dk) >= 0.5 && !s.in_progress ? (dk > 0 ? " · melhor" : " · pior") : ""}` : m.km_l != null ? "sem histórico para comparar" : "viagem curta demais"}</span></div>
        ${m.custo_km != null ? `<div><span class="lbl">Por km</span><b>R$ ${nf(m.custo_km, 2)}</b><span class="muted st-s">nesta viagem</span></div>` : ""}
        ${m.economia ? `<div style="box-shadow:inset 0 0 0 1px color-mix(in srgb, var(--ok) 45%, transparent)"><span class="lbl">Dava para poupar</span><b>R$ ${nf(m.economia, 2)}</b><span class="muted st-s">veja as dicas abaixo</span></div>` : ""}</div>
      ${m.parado?.parte != null ? `<div class="st-split" role="img" aria-label="Para onde foi o combustível">${seg(r, "var(--ok)", "rodando")}${seg(p, "var(--warn)", "parado")}${seg(g, "var(--rpm)", "giro alto")}</div>
        <div class="st-leg"><span><i style="background:var(--ok)"></i>Rodando: <b>${nf(r * 100)} %</b></span>${part(m.parado, "Parado ligado", "var(--warn)")}${part(m.giro_alto, "Acima de 3.000 rpm", "var(--rpm)")}</div>` : ""}
      ${(s.tips || []).map((t) => `<div class="alert st-tip" style="--s:var(--ok)"><span class="ic">${esc(t.icon || "💡")}</span><div><div class="t">Se tivesse…</div><div class="d">${esc(t.txt)}</div></div>${t.save_rs ? `<span class="pill">≈ R$ ${nf(t.save_rs, 2)}</span>` : "<span></span>"}</div>`).join("")}`;
  }
  function driveHtml(s) {
    const d = s.driving || {};
    const tiles = [];
    const col = (v) => v == null ? "var(--muted)" : v >= 85 ? "var(--ok)" : v >= 60 ? "var(--warn)" : "var(--bad)";
    if (d.drive_score != null) tiles.push(`<div><span class="lbl">Nota de condução</span><b style="color:${col(d.drive_score)}">${d.drive_score}<small>/100</small></b><span class="muted st-s">pelo GPS</span></div>`);
    if (d.eco_score != null) tiles.push(`<div><span class="lbl">Direção econômica</span><b style="color:${col(d.eco_score)}">${d.eco_score}<small>/100</small></b><span class="muted st-s">pelo OBD</span></div>`);
    if (d.freadas != null) tiles.push(`<div><span class="lbl">Freadas fortes</span><b>${d.freadas}</b><span class="muted st-s">${d.fonte === "GPS" ? "medidas pelo GPS" : "pela velocidade do OBD"}</span></div>`);
    if (d.arrancadas != null) tiles.push(`<div><span class="lbl">Arrancadas fortes</span><b>${d.arrancadas}</b></div>`);
    if (d.curvas != null) tiles.push(`<div><span class="lbl">Curvas fortes</span><b>${d.curvas}</b></div>`);
    if (d.acima_pct) tiles.push(`<div><span class="lbl">Acima do limite</span><b>${nf(d.acima_pct)} %</b><span class="muted st-s">do tempo andando</span></div>`);
    if (!tiles.length && !(d.dicas || []).length) return "";
    return `<section><h3>🏅 Como você dirigiu</h3>${tiles.length ? `<div class="stats">${tiles.join("")}</div>` : ""}
      ${(d.dicas || []).map((x) => `<div class="alert" style="--s:var(--ok);margin-top:8px"><span class="ic">💡</span><div><div class="d" style="color:var(--fg)">${esc(x)}</div></div><span></span></div>`).join("")}</section>`;
  }
  const CH = [["speed", "Velocidade", "km/h", "--speed", 0], ["rpm", "Rotação", "rpm", "--rpm", 0], ["ect", "Temperatura", "°C", "--temp", 0], ["stft1", "Ajuste (mistura)", "%", "--tps", 1]];
  function chartsHtml(s) {
    const sp = s.spark || {};
    const have = CH.filter(([k]) => (sp[k] || []).some((v) => v != null));
    if (!have.length) return "";
    const N = s.numbers || {}, top = { speed: N.max_speed, rpm: N.max_rpm };
    return `<section><h3>📈 A viagem em quatro linhas</h3><div class="st-charts">${have.map(([k, l, u, , d]) => { const ys = sp[k].filter((v) => v != null);
      return `<div class="st-ch"><div class="st-chh"><span class="lbl">${l}</span><span class="muted num">${top[k] != null ? "máx. " + nf(top[k]) : nf(Math.min(...ys), d) + " a " + nf(Math.max(...ys), d)} ${u}</span></div><svg class="chart sm" id="stc-${k}" viewBox="0 0 600 190" preserveAspectRatio="none"></svg></div>`; }).join("")}</div>
      ${(sp.marks || []).length ? `<div class="muted st-s" style="margin-top:6px">● Os pontos marcam ${sp.marks.some((m) => m.k === "carga") ? "os instantes em que uma carga extra entrou com o carro parado" : ""}${sp.marks.some((m) => m.k === "carga") && sp.marks.some((m) => m.k === "aviso") ? " e " : ""}${sp.marks.some((m) => m.k === "aviso") ? "os avisos da viagem" : ""}.</div>` : ""}</section>`;
  }
  function drawCharts(s) {
    const sp = s.spark || {}, lc = U().lineChart; if (!lc) return;
    const x = (i) => s.start_ts + (i + 0.5) * (sp.dt || 1) * 1000;
    for (const [k, , , c] of CH) {
      const el = $("#stc-" + k); if (!el) continue;
      const pts = (sp[k] || []).map((v, i) => [x(i), v]);
      const marks = (sp.marks || []).map((m) => { const i = Math.max(0, Math.min(pts.length - 1, Math.floor(m.s / (sp.dt || 1)))); return [pts[i]?.[0], pts[i]?.[1]]; }).filter((p) => p[1] != null);
      lc(el, [{ pts, c, area: k !== "stft1", end: false }, ...(marks.length ? [{ pts: marks, c: "--fg", dots: true, r: 4, op: 0.85 }] : [])], { x0: s.start_ts, x1: s.end_ts, ...(k === "speed" ? { ymin: 0 } : {}) });
    }
  }
  function html(s) {
    const [c] = ST[s.mood?.level] || ST.na;
    const flags = [s.in_progress ? `<span class="pill" style="--s:var(--ok)">viagem em andamento</span>` : "", s.unclosed ? `<span class="pill" style="--s:var(--warn)">números parciais</span>` : "",
      s.short ? `<span class="pill" style="--s:var(--muted)">viagem curta</span>` : ""].join("");
    return `<div class="st-hero" style="--s:${c}"><div class="st-emoji" aria-hidden="true">${esc(s.mood?.emoji || "🚗")}</div>
        <div><div class="st-head">${esc(s.mood?.headline || "")}</div><div class="muted st-sub">${esc(s.mood?.sub || "")}</div>${flags ? `<div class="st-flags">${flags}</div>` : ""}</div></div>
      <div class="st-acts"><button class="btn" type="button" id="stListen" aria-pressed="false">🔊 Ouvir</button><button class="btn" type="button" id="stCopy">📋 Copiar</button>
        ${navigator.share ? `<button class="btn" type="button" id="stShare">📤 Compartilhar</button>` : ""}<button class="btn" type="button" id="stAi" hidden>🤖 Pedir comentário da IA</button>
        <a class="btn" href="#viagens" id="stOpen">Ver a viagem completa</a></div>
      <div class="muted st-s" id="stMsg" role="status"></div>
      <div id="stAiBox"></div>
      <section><h3>📖 Como foi</h3><div class="st-nar st-cap">${(s.narrative || []).map((p) => `<p>${esc(p)}</p>`).join("")}</div></section>
      ${(s.highlights || []).length ? `<section><h3>✨ Destaques</h3><div class="stats st-hi">${s.highlights.map((h) => `<div><span class="lbl">${esc(h.icon)} ${esc(h.l)}</span><b>${esc(h.v)}</b>${h.s ? `<span class="muted st-s">${esc(h.s)}</span>` : ""}</div>`).join("")}</div></section>` : ""}
      <section><h3>🩺 Como estava o carro</h3><div class="muted st-s" style="margin-bottom:8px">Toque em cada linha para ver os números medidos.</div><div class="list">${(s.systems || []).map(sysHtml).join("")}</div></section>
      ${chartsHtml(s)}
      <section><h3>💸 O bolso</h3>${moneyHtml(s)}</section>
      ${driveHtml(s)}
      ${s.compare?.txt ? `<section><h3>🔁 Comparando com as suas viagens</h3><div class="st-nar"><p>${esc(s.compare.txt)}</p></div></section>` : ""}
      <section id="stMapSec" hidden><h3>🗺️ O trajeto</h3><div id="stMapBox"></div></section>
      ${(s.timeline || []).length ? `<section><h3>🕒 Linha do tempo</h3><ol class="st-tl">${s.timeline.map((e) => `<li><time>${esc(e.t)}</time><span class="st-ic" aria-hidden="true">${esc(e.icon)}</span><span>${esc(e.txt)}</span></li>`).join("")}</ol></section>` : ""}
      <div class="muted st-s st-foot">Montado por regras fixas a partir das leituras do OBD desta viagem, sem inteligência artificial. Cada veredito mostra o número que o sustenta. É uma leitura dos dados, não substitui o mecânico.</div>`;
  }

  // ------------------------------------------------------------ trajeto (só se houver GPS e a localização estiver aberta)
  async function loadMap(s, mine) {
    const sec = $("#stMapSec"), box = $("#stMapBox"); if (!sec || !box || !window.obdMap || !window.L) return;
    const c = cloud();
    if (c && c.loc !== "aberta") {
      if (c.loc === "trancada") { sec.hidden = false; box.innerHTML = `<div class="empty">🔒 O trajeto é protegido pela senha de localização. <a href="#mapa" id="stUnlock">Desbloquear na aba Mapa</a></div>`; }
      return;
    }
    const r = await fetch(`/api/gps/trips/${s.trip_id}/track`, { cache: "no-store" }).catch(() => null);
    if (!r || !r.ok || mine !== seq || !cur) return;
    const trk = await r.json().catch(() => null);
    if (!trk || !(trk.lat || []).length || mine !== seq || !cur) return;
    sec.hidden = false;
    box.innerHTML = `<div class="gmap st-map" id="stMap"></div><div class="muted st-s" style="margin-top:6px">Cor pela velocidade (azul devagar, vermelho rápido). O mapa completo, com replay, fica em "Ver a viagem completa".</div>`;
    const map = window.obdMap.create($("#stMap"), { zoomControl: false, dragging: !window.L.Browser.mobile, scrollWheelZoom: false });
    if (!map) return;
    cur.map = map;
    window.obdMap.track(map, trk, "speed", null);
    const n = trk.lat.length - 1;
    window.obdMap.pins(map, [{ lat: trk.lat[0], lon: trk.lon[0], label: "🟢", html: "<b>Saída</b>" }, { lat: trk.lat[n], lon: trk.lon[n], label: "🏁", html: "<b>Chegada</b>" }], window.L.layerGroup().addTo(map));
    const b = trk.bbox; if (b) window.obdMap.fit(map, window.L.latLngBounds([[b[0], b[1]], [b[2], b[3]]]), 17);
  }

  // ------------------------------------------------------------ comentário da IA (opcional: painel do Pi com a chave)
  async function aiSetup(s) {
    if (cloud()) return;
    const on = await fetch("/api/ai/settings").then((r) => r.ok ? r.json() : null).then((c) => !!c && !!(c.provider === "openai" ? c.openai_key : c.anthropic_key)).catch(() => false);
    const b = $("#stAi"); if (!b || !on || !cur || cur.id !== s.trip_id) return;
    b.hidden = false;
    b.addEventListener("click", async () => {
      b.hidden = true;
      const mine = seq, box = (h) => { const e = $("#stAiBox"); if (e && mine === seq) e.innerHTML = `<div class="alert" style="--s:var(--speed);display:block">${h}</div>`; };
      box(`<span class="spin"></span>Enviando o diagnóstico para a IA…`);
      try {
        const r = await fetch(`/api/trips/${s.trip_id}/story/ai`, { method: "POST" }), j = await r.json().catch(() => ({}));
        if (!r.ok || j.ok === false) throw new Error(j.error || j.detail || "indisponível neste painel");
        for (let i = 1; i <= 90; i++) {
          box(`<span class="spin"></span>A IA está lendo a viagem… (${i * 4} s)`); await new Promise((res) => setTimeout(res, 4000));
          if (mine !== seq || !cur) return;
          const a = ((await fetch("/api/ai/chat").then((x) => x.json()).catch(() => [])) || []).find((x) => x.id === j.id);
          if (a && a.status !== "running") { box(`<b>🤖 Comentário da IA</b> <span class="muted">(segunda opinião; também é hipótese)</span><div class="md" style="margin-top:6px">${U().md ? U().md(a.answer || "") : esc(a.answer || "").replace(/\n/g, "<br>")}</div>`); return; }
        }
        box("A IA demorou demais. A resposta aparece depois em Diagnóstico → Análise por IA.");
      } catch (e) { box(`Comentário da IA indisponível: ${esc(e.message)}. O diagnóstico acima não depende dela.`); }
    });
  }

  // ------------------------------------------------------------ abrir / fechar
  function modal() {
    let m = $("#storyModal");
    if (m) return m;
    document.body.insertAdjacentHTML("beforeend", `<div class="modal" id="storyModal" hidden role="dialog" aria-modal="true" aria-labelledby="stTitle">
      <div class="sheet card story"><div class="row-head st-top"><div><h2 id="stTitle">Diagnóstico da viagem</h2><div class="lbl" id="stWhen" style="margin-top:4px"></div></div>
        <button class="btn" type="button" id="stClose" aria-label="Fechar">✕</button></div><div id="stBody"></div></div></div>`);
    m = $("#storyModal");
    $("#stClose").addEventListener("click", close);
    m.addEventListener("click", (e) => { if (e.target === m) close(); });
    document.addEventListener("keydown", (e) => { if (e.key === "Escape" && cur) close(); });
    return m;
  }
  function close() {
    seq++; stopVoice();
    if (cur?.map) { try { cur.map.remove(); } catch { } }
    cur = null;
    const m = $("#storyModal"); if (m) m.hidden = true;
    document.documentElement.classList.remove("st-open");
  }
  async function open(id) {
    id = +id; if (!id) return;
    const m = modal(), mine = ++seq;
    if (cur?.map) { try { cur.map.remove(); } catch { } }
    stopVoice();
    cur = { id, story: null, map: null };
    m.hidden = false; document.documentElement.classList.add("st-open");
    $("#stTitle").textContent = "Diagnóstico da viagem"; $("#stWhen").textContent = "";
    $("#stBody").innerHTML = `<div class="empty"><span class="spin"></span>Lendo a viagem…</div>`;
    $(".sheet", m).scrollTop = 0;
    let s = null, status = 0;
    try { const r = await fetch(`/api/trips/${id}/story`); status = r.status; if (r.ok) s = await r.json(); } catch { }
    if (mine !== seq) return;
    if (!s || !s.mood) {
      $("#stBody").innerHTML = `<div class="empty">${status === 404 && cloud() ? "O Pi ainda não mandou o diagnóstico desta viagem para o painel online. Ele chega na próxima sincronização (o Pi precisa estar atualizado e com internet)."
        : status === 404 ? "Não encontrei esta viagem." : "Não consegui carregar o diagnóstico agora. Tente de novo em instantes."}</div>`;
      return;
    }
    // cópia antiga (painel online com o Pi desligado junto com o carro): "em andamento" já não vale
    if (s.in_progress && Date.now() - s.end_ts > 6 * 60e3) {
      s.in_progress = false; s.unclosed = true;
      s.mood.headline = String(s.mood.headline || "").replace(/^Viagem em andamento: /, "Viagem ainda não fechada pelo Pi: ");
      s.mood.sub = "Última leitura às " + hhmm(s.end_ts) + ". O Pi fecha a viagem na próxima partida; até lá os números são parciais.";
    }
    cur.story = s;
    $("#stTitle").textContent = "Diagnóstico · " + (s.title || "viagem"); $("#stWhen").textContent = s.when || "";
    $("#stBody").innerHTML = html(s);
    drawCharts(s);
    $("#stListen").addEventListener("click", () => listen(s));
    $("#stCopy").addEventListener("click", () => copy(s.share || ""));
    $("#stShare")?.addEventListener("click", () => share(s));
    $("#stOpen").addEventListener("click", (e) => { e.preventDefault(); close(); window.obdGps?.openTrip ? window.obdGps.openTrip(id) : (location.hash = "#viagens"); });
    $("#stBody").addEventListener("click", (e) => { if (e.target.closest("#stUnlock")) close(); });
    loadMap(s, mine).catch(() => { });
    aiSetup(s).catch(() => { });
  }

  // qualquer [data-story] abre; na fase de captura, para o toque não abrir também a linha da tabela.
  // O cartão do Ao vivo é redesenhado a cada leitura: se o botão foi trocado no meio do toque, acha pelo ponto tocado.
  document.addEventListener("click", (e) => {
    let b = e.target.closest?.("[data-story]");
    if (!b && e.target.closest?.("#engineCard") && e.clientX) b = document.elementFromPoint(e.clientX, e.clientY)?.closest?.("[data-story]");
    if (!b) return;
    e.preventDefault(); e.stopPropagation();
    open(b.dataset.story);
  }, true);

  window.obdStory = { open, close };
})();
