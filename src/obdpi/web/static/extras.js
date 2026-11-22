/* Telas de apoio: impressão digital do motor, antes e depois de consertos, plano de manutenção, gasolina × etanol,
   custo dos hábitos, replay da viagem, perguntas à IA e link do mecânico. Usa os utilitários do app.js (window.OBD). */
(() => {
  "use strict";
  const { api, esc, nf, dt, tm, dur, md } = window.OBD;
  const $ = (s) => document.querySelector(s);
  const ST = { ok: ["var(--ok)", "Igual", "✓"], warn: ["var(--warn)", "Mudou", "!"], bad: ["var(--bad)", "Mudou muito", "!"], unk: ["var(--muted)", "Sem dados", "?"] };
  const post = async (url, body) => { const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
    const j = await r.json().catch(() => ({})); if (!r.ok) throw new Error(j.error || j.detail || r.status); return j; };
  const money = (v) => v == null ? "—" : "R$ " + nf(v, 2);

  // ------------------------------------------------------------ impressão digital do motor
  async function loadFp() {
    const f = await api("/api/fingerprint").catch(() => null), box = $("#fpBox");
    if (!f) { box.innerHTML = `<div class="empty">Disponível depois que o Pi atualizar e sincronizar.</div>`; return; }
    const ro = !!window.OBD.cloud()?.readonly;
    $("#fpSave").hidden = ro || !f.pode_gravar; $("#fpSave").textContent = f.referencia ? "Regravar referência" : "Gravar referência agora";
    $("#fpSub").textContent = f.referencia ? `Referência: ${f.referencia.label} · gravada em ${dt(f.referencia.ts)} · ${nf(f.referencia.leituras)} leituras` : "O carro de hoje comparado com ele mesmo quando estava saudável";
    const [c, , ic] = ST[f.nivel] || ST.unk;
    box.innerHTML = `<div class="banner" style="--s:${c};margin-bottom:10px"><span class="ic">${ic}</span><b>${esc(f.texto)}</b><span class="muted">${f.referencia ? `Comparando ${nf(f.atual.leituras)} leituras recentes em ${f.atual.faixas} faixas de rotação e carga` : ro ? "A referência é gravada no painel do Pi (obd.local ou 172.20.10.14)." : `${nf(f.atual.leituras)} leituras disponíveis nos últimos ${f.atual.dias} dias`}</span></div>
      <div class="list">${(f.sinais || []).map((s) => { const [sc, sl, si] = ST[s.status] || ST.unk;
        return `<details class="alert ins" style="--s:${sc}"${s.status === "bad" ? " open" : ""}><summary><span class="ic">${si}</span><div><div class="t">${esc(s.nome)}${s.desvio && s.status !== "unk" ? ` <span class="num muted" style="font-weight:500">${esc(s.desvio)}</span>` : ""}</div><div class="d">${esc(s.texto)}</div></div><span class="pill">${sl}</span></summary>
          <div class="adv"><div class="muted">${s.faixas ? `Comparado em ${s.faixas} faixa(s).${s.pior ? " Maior diferença em " + esc(s.pior) + "." : ""}` : "Sem faixas em comum ainda."}</div></div></details>`; }).join("")}</div>`;
  }
  $("#fpSave")?.addEventListener("click", async () => {
    const b = $("#fpSave"); b.disabled = true;
    try { const r = await post("/api/fingerprint", { label: "Carro saudável", days: 7 }); $("#fpSub").textContent = `Referência gravada com ${nf(r.leituras)} leituras.`; await loadFp(); }
    catch (e) { $("#fpSub").textContent = e.message; } finally { b.disabled = false; }
  });

  // ------------------------------------------------------------ antes e depois dos consertos
  const VER = { melhorou: ["var(--ok)", "▲ melhorou"], piorou: ["var(--bad)", "▼ piorou"], igual: ["var(--muted)", "= igual"] };
  // trecho de referência do GPS (F4): mesmo lugar, mesma velocidade, até 5 passagens de cada lado
  const VERT = { ...VER, misto: ["var(--warn)", "± misto"], "sem dados": ["var(--muted)", "? sem dados"] };
  const trechos = (list) => (list || []).map((s) => `<div style="margin-top:12px"><div class="t" style="font-weight:600">📏 ${esc(s.trecho)}: <span style="color:${(VERT[s.veredito] || VERT["sem dados"])[0]}">${(VERT[s.veredito] || VERT["sem dados"])[1]}</span></div>
    <div class="muted" style="font-size:12px">${esc(s.texto)} ${s.antes.n} passagem(ns) antes e ${s.depois.n} depois.</div>
    ${s.indicadores.length ? `<div class="tbl" style="margin-top:6px"><table><tbody>${s.indicadores.map((i) => `<tr><td>${esc(s.trecho)} · ${esc(i.nome)}</td><td class="num">${nf(i.antes, Number.isInteger(i.antes) ? 0 : 1)} ${esc(i.unidade)}</td><td class="num">${nf(i.depois, Number.isInteger(i.depois) ? 0 : 1)} ${esc(i.unidade)}</td><td style="color:${(VERT[i.veredito] || VERT.igual)[0]};font-weight:600;white-space:nowrap">${(VERT[i.veredito] || VERT.igual)[1]}</td></tr>`).join("")}</tbody></table></div>` : ""}</div>`).join("");
  async function loadImpact() {
    const list = await api("/api/impact").catch(() => null), box = $("#impBox");
    if (!list) { box.innerHTML = `<div class="empty">Disponível depois que o Pi atualizar e sincronizar.</div>`; return; }
    box.innerHTML = list.length ? list.map((x, k) => { const [c, , ic] = ST[x.nivel === "ok" ? "ok" : x.nivel === "bad" ? "bad" : x.nivel === "warn" ? "warn" : "unk"];
      return `<details class="alert ins" style="--s:${c}"${k === 0 ? " open" : ""}><summary><span class="ic">${x.nivel === "ok" ? "✓" : ic}</span><div><div class="t">${esc(x.texto)}</div><div class="d">${dt(x.ts)} · ${esc(x.veredito)}</div></div><span></span></summary>
        <div class="adv">${x.indicadores.length ? `<div class="tbl"><table><thead><tr><th>Indicador</th><th>Antes</th><th>Depois</th><th></th></tr></thead><tbody>${x.indicadores.map((i) => `<tr><td>${esc(i.nome)}</td><td class="num">${nf(i.antes, Number.isInteger(i.antes) ? 0 : 1)} ${esc(i.unidade)}</td><td class="num">${nf(i.depois, Number.isInteger(i.depois) ? 0 : 1)} ${esc(i.unidade)}</td><td style="color:${VER[i.veredito][0]};font-weight:600;white-space:nowrap">${VER[i.veredito][1]}</td></tr>`).join("")}</tbody></table></div>` : ""}
        <div class="muted" style="font-size:12px">Compara ${x.janela_dias} dias antes (${nf(x.antes_h, 1)} h de motor) com ${x.janela_dias} dias depois (${nf(x.depois_h, 1)} h).</div>${trechos(x.trechos)}</div></details>`; }).join("")
      : `<div class="empty">Nenhum serviço anotado ainda. Anote acima o próximo conserto (por exemplo, a troca da junta) e volte aqui depois de rodar.</div>`;
  }
  $("#svcForm")?.addEventListener("submit", async (e) => {
    e.preventDefault(); const text = $("#svcText").value.trim(); if (!text) return;
    try { const r = await post("/api/annotations", { text, kind: "manutencao", ts: Date.now() }); $("#svcText").value = "";
      $("#svcMsg").textContent = r.queued ? "✅ Anotado. Entra no histórico quando o Pi ligar; a comparação aparece depois de rodar." : "✅ Anotado. A comparação aparece depois de uns 15 minutos de motor quente."; loadImpact(); }
    catch (err) { $("#svcMsg").textContent = "Não foi possível anotar: " + err.message; }
  });
  $("#mechCopy")?.addEventListener("click", async () => {
    const url = location.origin + "/oficina";
    try { await navigator.clipboard.writeText(url); $("#mechMsg").textContent = "Link copiado: " + url; } catch { $("#mechMsg").textContent = "Copie o link: " + url; }
  });

  // ------------------------------------------------------------ plano de manutenção
  let odoNow = null;
  async function loadMaint() {
    const m = await api("/api/maintenance").catch(() => null);
    if (!m) { $("#mtList").innerHTML = `<div class="empty">Disponível depois que o Pi atualizar e sincronizar.</div>`; return; }
    odoNow = m.hodometro.km;
    $("#mtUse").textContent = m.uso.motivo + (m.uso.severo ? " Os itens marcados usam metade do intervalo." : "");
    $("#odoMsg").innerHTML = m.hodometro.km != null ? `Hodômetro estimado agora: <b class="num">${nf(m.hodometro.km, 0)} km</b> (${m.hodometro.composicao ? `${m.hodometro.composicao}; informado em ${dt(m.hodometro.base_ts)}` : `informado ${nf(m.hodometro.base_km, 0)} km em ${dt(m.hodometro.base_ts)} + ${nf(m.hodometro.rodados_desde, 1)} km registrados pelo Pi`})` : "Informe o hodômetro uma vez: depois o Pi soma sozinho os km de cada viagem.";
    $("#mtList").innerHTML = m.itens.map((i) => { const c = { ok: "var(--ok)", warn: "var(--warn)", bad: "var(--bad)", unk: "var(--muted)" }[i.status];
      const every = [i.km_efetivo ? nf(i.km_efetivo, 0) + " km" : "", i.meses_efetivo ? nf(i.meses_efetivo, 0) + " meses" : ""].filter(Boolean).join(" ou ");
      return `<details class="alert ins" style="--s:${c}"><summary><span class="ic">${i.status === "ok" ? "✓" : i.status === "unk" ? "?" : "!"}</span><div><div class="t">${esc(i.nome)}</div><div class="d">${esc(i.texto)} · a cada ${every}${i.severo ? " (uso severo)" : ""}</div></div><span class="pill">${{ ok: "Em dia", warn: "Chegando", bad: "Vencido", unk: "Informar" }[i.status]}</span></summary>
        <div class="adv">${i.obs ? `<div class="muted">${esc(i.obs)}</div>` : ""}
          <div class="muted">${i.last_ts ? `Último: ${new Date(i.last_ts).toLocaleDateString("pt-BR")}${i.last_odo != null ? " aos " + nf(i.last_odo, 0) + " km" : ""}${i.nota ? " · " + esc(i.nota) : ""}` : "Nunca registrado."}</div>
          <form class="mtdone" data-k="${i.key}" style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">
            <input type="number" inputmode="numeric" name="odo" placeholder="km na troca" value="${odoNow != null ? Math.round(odoNow) : ""}" aria-label="Hodômetro na troca" style="flex:1 1 110px;min-width:0">
            <input type="date" name="date" value="${new Date().toISOString().slice(0, 10)}" aria-label="Data da troca" style="flex:1 1 130px;min-width:0">
            <button class="btn" type="submit">Feito</button></form></div></details>`; }).join("");
  }
  $("#odoForm")?.addEventListener("submit", async (e) => {
    e.preventDefault();
    try { const r = await post("/api/maintenance/odometer", { km: $("#odoKm").value, ts: Date.now() }); $("#odoKm").value = "";
      $("#odoMsg").textContent = r.queued ? "✅ Hodômetro registrado. Vale quando o Pi ligar." : "✅ Hodômetro salvo."; if (!r.queued) loadMaint(); }
    catch (err) { $("#odoMsg").textContent = "Não foi possível salvar: " + err.message; }
  });
  $("#mtList")?.addEventListener("submit", async (e) => {
    const f = e.target.closest(".mtdone"); if (!f) return; e.preventDefault();
    const d = f.elements.date.value ? new Date(f.elements.date.value + "T12:00:00").getTime() : Date.now();
    try { const r = await post("/api/maintenance/done", { key: f.dataset.k, odo: f.elements.odo.value, ts: d });
      if (r.queued) { f.insertAdjacentHTML("afterend", `<div class="muted">✅ Registrado. Entra no plano quando o Pi ligar.</div>`); f.remove(); } else loadMaint(); }
    catch (err) { f.insertAdjacentHTML("afterend", `<div style="color:var(--bad)">${esc(err.message)}</div>`); }
  });

  // ------------------------------------------------------------ gasolina × etanol
  let fuelData = null;
  function flex() {
    const f = fuelData, out = $("#flexOut"); if (!f || !out) return;
    const g = f.kml_by_fuel?.gasolina, e = f.kml_by_fuel?.etanol;
    const own = g && e;                                         // rendimento medido nos dois combustíveis
    const ratio = own ? e.kml / g.kml : 0.70;
    $("#flexSub").textContent = own ? `Pelo seu carro: ${nf(g.kml, 1)} km/L na gasolina e ${nf(e.kml, 1)} km/L no etanol (etanol rende ${nf(ratio * 100, 0)} %)`
      : `Ainda sem tanques medidos nos dois combustíveis: usando a regra geral (etanol rende 70 %). ${g ? "Gasolina medida: " + nf(g.kml, 1) + " km/L." : e ? "Etanol medido: " + nf(e.kml, 1) + " km/L." : "Registre os tanques cheios para usar o rendimento do seu carro."}`;
    const pg = parseFloat($("#flexG").value), pe = parseFloat($("#flexE").value);
    try { if (pg) localStorage.setItem("flexG", pg); if (pe) localStorage.setItem("flexE", pe); } catch { }
    if (!pg || !pe) { out.innerHTML = `<div class="empty">Etanol compensa quando custar menos de <b>${nf(ratio * 100, 0)} %</b> do preço da gasolina.</div>`; return; }
    const kg = g?.kml ?? (e ? e.kml / 0.70 : f.avg_real_kml ?? f.since_fill?.kml_est ?? 10), ke = e?.kml ?? kg * ratio;
    const cg = pg / kg, ce = pe / ke, eth = ce < cg, diff = Math.abs(cg - ce);
    out.innerHTML = `<div class="banner" style="--s:var(--ok)"><span class="ic">✓</span><b>${eth ? "Etanol" : "Gasolina"} sai mais barato: ${money(Math.min(cg, ce))} por km</b>
        <span class="muted">Economia de ${money(diff * 100)} a cada 100 km (${nf(diff / Math.max(cg, ce) * 100, 0)} %). O etanol está a ${nf(pe / pg * 100, 0)} % do preço da gasolina; compensa abaixo de ${nf(ratio * 100, 0)} %.</span></div>
      <div class="stats" style="margin-top:10px"><div><span class="lbl">Gasolina</span><b>${money(cg)}/km</b><span class="muted" style="font-size:12px">${nf(kg, 1)} km/L${g ? "" : " (estimado)"}</span></div>
        <div><span class="lbl">Etanol</span><b>${money(ce)}/km</b><span class="muted" style="font-size:12px">${nf(ke, 1)} km/L${e ? "" : " (estimado)"}</span></div></div>`;
  }
  ["#flexG", "#flexE"].forEach((id) => $(id)?.addEventListener("input", flex));
  try { if ($("#flexG")) { $("#flexG").value = localStorage.getItem("flexG") || ""; $("#flexE").value = localStorage.getItem("flexE") || ""; } } catch { }

  // ------------------------------------------------------------ custo dos hábitos
  function habitsHtml(h, title) {
    if (!h || !h.litros) return "";
    const row = (ic, name, x, tip) => x.litros >= 0.01 ? `<div class="alert" style="--s:${(x.parte || 0) > 0.3 ? "var(--warn)" : "var(--muted)"}"><span class="ic">${ic}</span><div><div class="t">${name}: ${nf(x.litros, 2)} L${x.custo != null ? " = " + money(x.custo) : ""} <span class="muted" style="font-weight:500">(${nf((x.parte || 0) * 100, 0)} % do combustível)</span></div>
      <div class="d">${dur(x.segundos)} · ${tip}</div></div><span></span></div>` : "";
    const rows = row("⏸", "Parado com o motor ligado", h.parado, "desligar em paradas de mais de 1 minuto zera esse gasto") + row("⏫", "Acima de 3.000 rpm", h.giro_alto, "subir a marcha mais cedo reduz");
    return rows ? `<h3 class="lbl" style="margin-top:16px">${title}</h3><div class="list" style="margin-top:8px">${rows}</div>${h.preco_l ? "" : `<div class="muted" style="font-size:12px;margin-top:6px">Informe o preço por litro num abastecimento para ver em reais.</div>`}` : "";
  }

  // ------------------------------------------------------------ replay da viagem
  const RKEYS = [["speed", "Velocidade", "km/h", 0], ["rpm", "Rotação", "rpm", 0], ["gear", "Marcha", "", 0], ["pedal_d", "Pedal", "%", 0], ["tps", "Borboleta", "%", 1], ["stft1", "Ajuste curto", "%", 1],
    ["o2c1_lambda", "Sonda λ", "", 2], ["timing", "Avanço", "°", 1], ["maf", "Ar (MAF)", "g/s", 1], ["load", "Carga", "%", 0], ["ect", "Motor", "°C", 0], ["fuel_lh", "Consumo", "L/h", 1], ["batt", "Bateria", "V", 1]];
  let playTimer = null;
  function replay(t) {
    clearInterval(playTimer);
    const S = t.series || {}, a = t.start_ts, b = t.end_ts || Date.now();
    if (!(S.rpm || []).length) return "";
    const at = (k, ts) => { const p = S[k]; if (!p || !p.length) return null; let lo = 0, hi = p.length - 1; while (lo < hi) { const m = (lo + hi + 1) >> 1; if (p[m][0] <= ts) lo = m; else hi = m - 1; } return p[lo][1]; };
    const marks = (t.alerts || []).filter((x) => x.severity !== "info").slice(0, 12);
    // velocidade do replay: [ms de viagem por passo, intervalo do passo] (o controle anda de 1 em 1 s)
    const SPEEDS = { 1: [1000, 1000], 10: [2000, 200], 60: [12000, 200] };
    let speed = 10;
    try { speed = +localStorage.getItem("replaySpeed") || 10; } catch { }
    if (!SPEEDS[speed]) speed = 10;
    setTimeout(() => {
      const sl = $("#rpSlider"), box = $("#rpVals"); if (!sl) return;
      const paint = () => { const ts = +sl.value; $("#rpTime").textContent = `${tm(ts)} · ${dur((ts - a) / 1000)} de viagem`;
        box.innerHTML = RKEYS.filter(([k]) => S[k] && S[k].length).map(([k, l, u, d]) => { const v = at(k, ts); return `<div><span class="lbl">${l}</span><b>${k === "gear" ? (v ? nf(v) : "N") : nf(v, d)}<small> ${u}</small></b></div>`; }).join("");
        window.dispatchEvent(new CustomEvent("obdreplay", { detail: { ts, trip: t.id } })); };       // mapa da viagem (gps.js)
      sl.addEventListener("input", paint); paint();
      const btn = $("#rpPlay");
      const tick = () => { if (!document.body.contains(sl)) return clearInterval(playTimer);
        sl.value = Math.min(b, +sl.value + SPEEDS[speed][0]); paint(); if (+sl.value >= b) { clearInterval(playTimer); playTimer = null; btn.textContent = "▶ Reproduzir"; } };
      const spd = $("#rpSpeed");
      if (spd) { spd.value = String(speed); spd.addEventListener("change", () => { speed = +spd.value; try { localStorage.setItem("replaySpeed", speed); } catch { }
        if (playTimer) { clearInterval(playTimer); playTimer = setInterval(tick, SPEEDS[speed][1]); } }); }
      btn.addEventListener("click", () => {
        if (playTimer) { clearInterval(playTimer); playTimer = null; btn.textContent = "▶ Reproduzir"; return; }
        if (+sl.value >= b - 1000) sl.value = a;
        btn.textContent = "⏸ Pausar"; playTimer = setInterval(tick, SPEEDS[speed][1]); });
      $("#rpMarks")?.addEventListener("click", (e) => { const m = e.target.closest("[data-ts]"); if (m) { sl.value = Math.max(a, +m.dataset.ts - 8000); paint(); } });
    }, 0);
    return `<div class="card"><div class="row-head"><div><h2>⏯ Replay da viagem</h2><div class="lbl" style="margin-top:4px" id="rpTime"></div></div>
      <span style="display:flex;gap:8px;align-items:center"><select id="rpSpeed" aria-label="Velocidade do replay"><option value="1">×1</option><option value="10">×10</option><option value="60">×60</option></select><button class="btn" type="button" id="rpPlay">▶ Reproduzir</button></span></div>
      <input type="range" id="rpSlider" min="${a}" max="${b}" step="1000" value="${a}" aria-label="Momento da viagem" style="width:100%;margin:12px 0">
      <div class="stats rpv" id="rpVals"></div>
      ${marks.length ? `<h3 class="lbl" style="margin-top:14px">Pular para 8 s antes de um alerta</h3><div id="rpMarks" style="display:flex;gap:6px;flex-wrap:wrap;margin-top:8px">${marks.map((m) => `<button type="button" class="rbtn" data-ts="${m.ts}">${tm(m.ts)} · ${esc(m.title)}</button>`).join("")}</div>` : ""}</div>`;
  }

  // ------------------------------------------------------------ perguntas à IA
  let chatTimer = null;
  async function loadChat() {
    clearTimeout(chatTimer);
    const list = await api("/api/ai/chat").catch(() => []), box = $("#askList"); if (!box) return;
    box.innerHTML = list.map((c) => `<div class="alert" style="--s:var(--volt);align-items:start"><span class="ic">?</span><div><div class="t">${esc(c.question)}</div>
      <div class="md" style="font-size:14px">${c.status === "running" ? `<span class="spin"></span>Pensando…` : md(c.answer || "")}</div><div class="d">${dt(c.ts)}</div></div><span></span></div>`).join("");
    if (list.some((c) => c.status === "running") && !$("#aiModal").hidden) chatTimer = setTimeout(loadChat, 4000);
  }
  $("#askForm")?.addEventListener("submit", async (e) => {
    e.preventDefault(); const q = $("#askText").value.trim(); if (!q) return;
    try { const r = await post("/api/ai/ask", { question: q }); if (r.ok === false) throw new Error(r.error); $("#askText").value = ""; loadChat(); }
    catch (err) { $("#askList").insertAdjacentHTML("afterbegin", `<div class="alert" style="--s:var(--bad)"><span class="ic">!</span><div><div class="t">${esc(err.message)}</div></div><span></span></div>`); }
  });

  window.obdExtras = {
    diag() { loadFp().catch(console.error); loadImpact().catch(console.error); },
    maint() { return loadMaint(); },
    fuel(f) { fuelData = f; flex(); },
    report(r) { const el = $("#perHabits"); if (el) el.innerHTML = habitsHtml(r.habits, "💸 Quanto os hábitos custaram no período"); },
    trip(t) { $("#tripDetail").insertAdjacentHTML("beforeend", (t.habits ? `<div class="card"><h2>💸 Quanto os hábitos custaram nesta viagem</h2>${habitsHtml(t.habits, "Do combustível gasto") || `<div class="empty" style="margin-top:10px">Sem gasto relevante parado ou em giro alto.</div>`}</div>` : "") + replay(t)); },
    chat() { loadChat().catch(console.error); },
  };

  // aplicativo instalável e painel disponível sem internet (última cópia vista)
  if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => { });
})();
