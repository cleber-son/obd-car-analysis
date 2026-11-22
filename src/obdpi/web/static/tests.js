/* Testes guiados "caça-problema": o painel orienta o que fazer no carro e interpreta as leituras ao vivo.
   Usa o mesmo fluxo ao vivo do painel (evento "obdlive", disparado pelo app.js a cada leitura). */
(() => {
  "use strict";
  const $ = (s) => document.querySelector(s);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const nf = (v, d = 0) => (v == null || Number.isNaN(v)) ? "—" : Number(v).toLocaleString("pt-BR", { minimumFractionDigits: d, maximumFractionDigits: d });
  const sg = (v, d = 0) => v == null ? "—" : (v > 0 ? "+" : "") + nf(v, d);
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const mean = (a) => a.length ? a.reduce((x, y) => x + y, 0) / a.length : null;
  const sd = (a) => { if (a.length < 2) return 0; const m = mean(a); return Math.sqrt(mean(a.map((x) => (x - m) ** 2))); };
  const col = (s, k) => s.map((x) => x.v[k]).filter((x) => x != null);
  const lam = (s) => col(s, "o2c1_lambda").filter((x) => x < 1.9);          // 1,99 = corte de combustível, não é mistura
  const volts = (s) => { const e = col(s, "ecu_volt"); return e.length ? e : col(s, "batt"); };
  class Cancel extends Error { }

  let live = null, buf = [], running = null, cancelled = false, spark = [];
  window.addEventListener("obdlive", (e) => {
    live = e.detail;
    if (!running) return;
    buf.push({ t: Date.now(), v: { ...(live.values || {}) }, on: live.engine_on });
    if (buf.length > 6000) buf.shift();
    paintLive();
  });

  // ------------------------------------------------------------ som e vibração
  let actx = null;
  function beep(freq = 880, ms = 160, times = 1) {
    try {
      actx ||= new (window.AudioContext || window.webkitAudioContext)();
      for (let i = 0; i < times; i++) { const o = actx.createOscillator(), g = actx.createGain(); o.frequency.value = freq; o.connect(g); g.connect(actx.destination);
        const t0 = actx.currentTime + i * (ms + 90) / 1000; g.gain.setValueAtTime(0.25, t0); g.gain.exponentialRampToValueAtTime(0.001, t0 + ms / 1000); o.start(t0); o.stop(t0 + ms / 1000); }
      navigator.vibrate?.(times > 1 ? [200, 80, 200] : 100);
    } catch { }
  }

  // ------------------------------------------------------------ voz (fala em português; o iPhone exige o 1º toque para liberar)
  let voiceOn = true; try { voiceOn = localStorage.getItem("obdVoice") !== "0"; } catch { }
  const plain = (t) => String(t ?? "").replace(/<[^>]+>/g, " ").replace(/[\u{1F300}-\u{1FAFF}\u2600-\u27BF\uFE0F]/gu, "").replace(/\s+/g, " ").trim();
  let ptVoice = null;
  function pickVoice() { const vs = window.speechSynthesis?.getVoices() || []; ptVoice = vs.find((v) => /pt[-_]BR/i.test(v.lang)) || vs.find((v) => /^pt/i.test(v.lang)) || null; }
  try { pickVoice(); window.speechSynthesis?.addEventListener?.("voiceschanged", pickVoice); } catch { }
  const talking = [];                                   // guarda as falas: o Safari descarta a fala se o objeto for coletado
  function speak(text, interrupt = true) {
    const t = plain(text), ss = window.speechSynthesis; if (!voiceOn || !t || !ss) return;
    try {
      if (!ptVoice) pickVoice();
      const u = new SpeechSynthesisUtterance(t.slice(0, 260)); u.lang = "pt-BR"; u.rate = 1.05; u.volume = 1; if (ptVoice) u.voice = ptVoice;
      talking.push(u); if (talking.length > 12) talking.shift();
      u.onend = u.onerror = () => { const i = talking.indexOf(u); if (i >= 0) talking.splice(i, 1); };
      ss.resume?.();
      // iPhone: speak() logo depois de cancel() é descartado em silêncio — cancela e fala um instante depois
      if (interrupt && (ss.speaking || ss.pending)) { ss.cancel(); setTimeout(() => { ss.resume?.(); ss.speak(u); }, 150); }
      else ss.speak(u);
    } catch { }
  }
  let voiceUnlocked = false;
  function unlockVoice() {                              // chamado dentro de um toque: libera o som e a fala no iPhone
    try { actx ||= new (window.AudioContext || window.webkitAudioContext)(); actx.resume?.(); } catch { }
    if (voiceUnlocked || !window.speechSynthesis) return; voiceUnlocked = true;
    try { const u = new SpeechSynthesisUtterance("ok"); u.lang = "pt-BR"; u.volume = 0.01; talking.push(u); speechSynthesis.speak(u); } catch { }
  }
  function paintVoice() { const b = $("#tstVoice"); if (b) { b.textContent = voiceOn ? "🔊" : "🔇"; b.title = voiceOn ? "Voz ligada (toque para silenciar)" : "Voz desligada"; } }
  document.addEventListener("click", (e) => { if (!e.target.closest?.("#tstVoice")) return;
    voiceOn = !voiceOn; try { localStorage.setItem("obdVoice", voiceOn ? "1" : "0"); } catch { } unlockVoice(); paintVoice();
    if (voiceOn) speak("Voz ligada"); else speechSynthesis?.cancel(); });

  // ------------------------------------------------------------ janela do teste
  const SIG = { rpm: ["Rotação", "rpm", 0, "--rpm"], stft1: ["Ajuste curto", "%", 1, "--tps"], o2c1_lambda: ["Sonda (λ)", "", 2, "--speed"], tps: ["Borboleta", "%", 1, "--tps"],
    pedal_d: ["Pedal", "%", 1, "--pedal"], batt: ["Bateria", "V", 1, "--volt"], ecu_volt: ["Tensão", "V", 1, "--volt"], ect: ["Motor", "°C", 0, "--temp"], o2s2_v: ["Sonda pós-cat", "V", 2, "--temp"],
    maf: ["Fluxo de ar", "g/s", 1, "--speed"], map: ["Vácuo (MAP)", "kPa", 0, "--speed"], timing: ["Avanço", "°", 1, "--volt"] };
  let shown = ["rpm", "stft1", "o2c1_lambda"];
  const M = () => $("#tstModal");
  function open(title, keys) {
    cancelled = false; buf = []; running = title; shown = keys; spark = keys.slice(0, 2);
    $("#tstTitle").textContent = title; $("#tstLog").innerHTML = ""; $("#tstBtns").innerHTML = ""; $("#tstChecks").innerHTML = ""; say("", "");
    M().hidden = false; paintLive(); paintVoice(); unlockVoice();
    try { navigator.wakeLock?.request("screen").catch(() => { }); } catch { }
  }
  function close() { running = null; cancelled = true; M().hidden = true; try { speechSynthesis?.cancel(); } catch { } renderHistory(); }
  function say(big, small, tone = "") { $("#tstBig").innerHTML = big; $("#tstBig").className = "tbig " + tone; $("#tstSmall").innerHTML = small; }
  function log(ok, text) { $("#tstLog").insertAdjacentHTML("beforeend", `<div class="alert" style="--s:${ok === true ? "var(--ok)" : ok === false ? "var(--bad)" : "var(--warn)"}"><span class="ic">${ok === true ? "✓" : ok === false ? "!" : "i"}</span><div><div class="t">${text}</div></div><span></span></div>`); }
  function buttons(list) { return new Promise((res) => { $("#tstBtns").innerHTML = list.map((b, i) => `<button class="btn${b.main ? " big" : ""}" type="button" data-i="${i}">${esc(b.t)}</button>`).join("");
    $("#tstBtns").onclick = (e) => { const b = e.target.closest("[data-i]"); if (b) { unlockVoice(); $("#tstBtns").innerHTML = ""; $("#tstBtns").onclick = null; res(list[+b.dataset.i].v); } }; }); }
  function paintLive() {
    const v = live?.values || {};
    $("#tstLive").innerHTML = shown.map((k) => { const [l, u, d] = SIG[k]; return `<div><span class="lbl">${l}</span><b>${k === "stft1" ? sg(v[k], d) : nf(v[k], d)}<small> ${u}</small></b></div>`; }).join("");
    const st = live?.state === "pi_offline" ? "Carro desligado (sem dados ao vivo)" : live?.engine_on ? "Motor ligado" : live?.state === "coletando" ? "Ignição ligada, motor desligado" : "Sem leitura do OBD";
    $("#tstState").textContent = st + (live?.cloud ? " · painel online" : "");
    drawSpark();
  }
  // gráfico dos últimos 45 s dos dois primeiros sinais do teste, cada um na sua escala
  function drawSpark() {
    const c = $("#tstSpark"); if (!c) return;
    const w = c.width = c.clientWidth * 2, h = c.height = c.clientHeight * 2, g = c.getContext("2d"), now = Date.now(), win = 45000;
    g.clearRect(0, 0, w, h);
    const css = getComputedStyle(document.documentElement);
    spark.forEach((k, i) => {
      const pts = buf.filter((x) => now - x.t < win && x.v[k] != null); if (pts.length < 2) return;
      const vals = pts.map((x) => x.v[k]), lo = Math.min(...vals), hi = Math.max(...vals), span = Math.max(hi - lo, k === "rpm" ? 200 : k === "stft1" ? 10 : (hi || 1) * 0.05);
      const mid = (hi + lo) / 2;
      g.beginPath(); g.lineWidth = 3; g.strokeStyle = css.getPropertyValue(SIG[k][3]).trim() || "#fff";
      pts.forEach((p, j) => { const x = w - (now - p.t) / win * w, y = h / 2 - (p.v[k] - mid) / span * (h * 0.8); j ? g.lineTo(x, y) : g.moveTo(x, y); });
      g.stroke();
      g.fillStyle = g.strokeStyle; g.font = "600 20px sans-serif"; g.fillText(SIG[k][0], 8, i ? h - 10 : 24);
    });
  }
  // espera `sec` segundos mostrando contagem; `fn` (opcional) roda a cada passo e pode devolver {big, small, tone} ou "stop"
  async function phase(sec, big, small, tone = "", fn = null, talk = big) {
    const t0 = Date.now();
    if (talk) speak(talk);
    while (Date.now() - t0 < sec * 1000) {
      if (cancelled) throw new Cancel();
      const left = `<b>${Math.ceil(sec - (Date.now() - t0) / 1000)} s</b>`;
      const r = fn ? fn(buf.filter((x) => x.t >= t0), (Date.now() - t0) / 1000) : null;
      if (r === "stop") break;
      say(r?.big ?? big, `${r?.small ?? small} · ${left}`, r?.tone ?? tone);
      await sleep(200);
    }
    return buf.filter((x) => x.t >= t0);
  }
  // condições para começar, com marcação ao vivo; resolve quando todas estão ok ou o dono manda seguir
  async function ready(intro, conds) {
    say("Antes de começar", intro);
    const paint = () => { $("#tstChecks").innerHTML = conds.map((c) => { const ok = !!c.ok(); return `<div class="tchk ${ok ? "ok" : ""}"><span>${ok ? "✓" : "○"}</span>${c.t}</div>`; }).join(""); return conds.every((c) => c.ok()); };
    let go = null; const iv = setInterval(() => { if (!running) return clearInterval(iv); const all = paint(); const b = $("#tstBtns [data-i='0']"); if (b) b.textContent = all ? "Começar" : "Começar assim mesmo"; }, 300);
    paint();
    for (;;) {
      go = await buttons([{ t: "Começar", v: 1, main: true }, { t: "🔊 Testar o som", v: 2 }, { t: "Cancelar", v: 0 }]);
      if (go !== 2) break;
      voiceOn = true; try { localStorage.setItem("obdVoice", "1"); } catch { } paintVoice();
      beep(880, 150, 2);
      if (!window.speechSynthesis) { say("Antes de começar", "Este navegador não tem voz. Só os bipes vão tocar."); continue; }
      speak("Teste de som. Se você está ouvindo, a voz está funcionando.");
      say("Antes de começar", `Tocou dois bipes e falou "teste de som"?<br>Se <b>não ouviu nada</b>: tire o iPhone do <b>modo silencioso</b> (chave na lateral), suba o volume e toque de novo. Se ouviu os bipes mas não a voz, me avise.`);
    }
    clearInterval(iv); $("#tstChecks").innerHTML = "";
    if (!go) { close(); throw new Cancel(); }
  }
  const C = {
    on: { t: "Motor ligado", ok: () => !!live?.engine_on },
    hot: { t: "Motor quente (acima de 80 °C)", ok: () => (live?.values?.ect ?? 0) >= 80 },
    stopped: { t: "Carro parado, ponto morto, freio de mão puxado", ok: () => !!live && (live.values?.speed || 0) === 0 },
    koeo: { t: "Ignição ligada com o motor desligado", ok: () => live?.state === "coletando" && !live.engine_on },
    calm: { t: "Pé fora do acelerador", ok: () => (live?.values?.rpm ?? 0) < 1100 },
    alloff: { t: "Ar, faróis e desembaçador desligados (confira você)", ok: () => true },
  };
  async function finish(id, title, bad, lines, okMsg, badMsg, incomplete = false) {
    const verdict = incomplete ? "incompleto" : bad ? `${bad} ponto(s) de atenção` : "tudo certo";
    say(incomplete ? "Teste incompleto" : bad ? "⚠️ Encontrei algo" : "✅ Tudo certo", incomplete || bad ? badMsg : okMsg, incomplete || bad ? "bad" : "go");
    beep(bad ? 440 : 990, 200, bad ? 2 : 1);
    speak(incomplete ? "Teste incompleto." : bad ? "Atenção, encontrei algo. " + plain(badMsg) : "Tudo certo. " + plain(okMsg));
    const rec = { id, title, ts: Date.now(), verdict, lines };
    try { const all = JSON.parse(localStorage.getItem("obdTests") || "[]"); all.unshift(rec); localStorage.setItem("obdTests", JSON.stringify(all.slice(0, 40))); } catch { }
    // no painel do Pi o resultado também vai para o diário de manutenção (entra no contexto da IA)
    fetch("/api/annotations", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ kind: "teste", ts: rec.ts, text: `${title}: ${verdict}. ${lines.join("; ")}`.slice(0, 480) }) }).catch(() => { });
    await buttons([{ t: "Fechar", v: 1, main: true }]); close();
  }
  const run = (f) => async () => { try { await f(); } catch (e) { if (!(e instanceof Cancel)) { console.error(e); say("O teste parou", esc(e.message), "bad"); buttons([{ t: "Fechar", v: 1 }]).then(close); } } };
  // segurar uma rotação: mostra a rotação atual e fica verde dentro da faixa
  const hold = (target, tol = 300) => (s) => { const r = live?.values?.rpm ?? 0, ok = Math.abs(r - target) <= tol;
    return { big: `${nf(r)} rpm`, small: ok ? `Isso, segure perto de ${nf(target)}` : r < target ? `Acelere até ${nf(target)} rpm` : `Alivie até ${nf(target)} rpm`, tone: ok ? "go" : "bad" }; };
  const steady = (s, target, tol = 400) => s.filter((x) => x.v.rpm != null && Math.abs(x.v.rpm - target) <= tol);

  // ------------------------------------------------------------ 1. caça-vazamento (entrada falsa de ar)
  const POINTS = ["Junta do corpo de borboleta (em volta toda)", "Duto entre o filtro/MAF e a borboleta (emendas e abraçadeiras)", "Mangueira do servofreio", "Mangueira da PCV / respiro do motor",
    "Base do coletor de admissão (junto ao cabeçote)", "Em volta dos bicos injetores",
    "Bico 1 (lado da correia)", "Bico 2", "Bico 3", "Bico 4 (lado do câmbio)", "Outro ponto"];
  const spoken = (p) => p.replace(/\s*\(.*?\)/g, "").replace(/\//g, " e ");
  async function leakTest(only = null) {                // only: lista de pontos para já começar em sequência (ex.: só os bicos)
    if (Array.isArray(only) && !only.length) only = null; else if (!Array.isArray(only)) only = null;
    open(only ? "Caça-vazamento nos bicos" : "Caça-vazamento de ar", ["rpm", "stft1", "o2c1_lambda"]);
    await ready(`O celular <b>fala</b> cada passo: "prepare", "borrife agora", "pare de borrifar". Você borrifa <b>limpa-carburador</b> num ponto por vez, só quando ouvir. Se houver vazamento ali, o motor suga o produto e as leituras mudam: ele avisa "reagiu".<br><br>
      Use <b>Sequência por voz</b> para passar por todos os pontos sem tocar na tela. Volume do celular alto; 🔊 no topo liga e desliga a voz.<br><br>
      ⚠️ Produto inflamável: longe do escapamento quente e do alternador, sem cigarro nem faísca por perto.`, [C.on, C.hot, C.stopped, C.calm]);
    const hits = [], clean = [], count = {};
    const AUTO = POINTS.filter((p) => p !== "Outro ponto" && p !== "Em volta dos bicos injetores");   // na sequência, bico por bico
    let queue = only ? only.slice() : null;
    for (;;) {
      const base = await phase(8, "Não borrife ainda", "Medindo o motor em repouso", "", null, queue ? "Não borrife. Medindo o repouso." : "Não borrife ainda. Medindo o motor em repouso.");
      const b = { stft: mean(col(base, "stft1")), sst: sd(col(base, "stft1")), rpm: mean(col(base, "rpm")), srpm: sd(col(base, "rpm")), lam: mean(lam(base)) };
      if (b.rpm == null) { say("Sem leitura", "Não recebi dados do motor. Confira se o painel está ao vivo.", "bad"); speak("Sem leitura do motor"); break; }
      let p;
      if (queue && queue.length) p = queue.shift();
      else {
        if (queue) { queue = null; speak("Sequência terminada. Escolha um ponto para repetir, ou termine o teste."); }
        else speak("Escolha onde vai borrifar", false);
        say("Onde você vai borrifar?", `Repouso: ${nf(b.rpm)} rpm · ajuste ${sg(b.stft, 1)} %${b.lam ? " · λ " + nf(b.lam, 2) : ""}`);
        p = await buttons([{ t: "▶ Sequência por voz (todos os pontos)", v: "*", main: true }].concat(POINTS.map((t) => ({ t: (count[t] ? "↻ " : "") + t, v: t })), [{ t: "✅ Terminar o teste", v: null }]));
        if (!p) break;
        if (p === "*") { queue = AUTO.slice(); p = queue.shift(); }
      }
      count[p] = (count[p] || 0) + 1;
      // 1) posicionar: fala o ponto e conta 3, 2, 1
      speak(`${/^Bico/.test(p) ? "" : "Próximo ponto: "}${spoken(p)}. Posicione o spray na base.`);
      await phase(6, "Posicione o spray", `${esc(p)}<br>Ainda não borrife`, "", null, "");
      for (const n of [3, 2, 1]) { beep(660, 90); speak(String(n)); await phase(1, String(n), esc(p), "", null, ""); }
      // 2) borrifar 3 s, 3) parar e observar 9 s — a reação é medida desde o início do jato
      let hit = null, lastTick = 0; const tSpray = Date.now();
      const watch = (stage) => () => {
        const w = buf.filter((x) => x.t >= tSpray);
        const st = col(w, "stft1"), rp = col(w, "rpm"), la = lam(w);
        const dS = b.stft != null && st.length ? b.stft - Math.min(...st) : 0;                // ajuste cai: o motor recebeu "combustível" extra
        const dR = rp.length ? Math.max(...rp.map((x) => Math.abs(x - b.rpm))) : 0;           // rotação sobe ou engasga
        const dL = b.lam != null && la.length ? b.lam - Math.min(...la) : 0;                  // sonda vê mistura mais rica
        const strong = [dS > Math.max(6, 4 * b.sst), dR > Math.max(70, 4 * b.srpm), dL > 0.06].filter(Boolean).length;
        // "contador Geiger": quanto mais o ajuste cai (ou a rotação/sonda mexe), mais rápido e mais agudo o bipe
        const lvl = Math.max(dS / 6, dR / 70, dL / 0.06);
        if (lvl >= 0.4 && Date.now() - lastTick > Math.max(110, 700 - lvl * 220)) { lastTick = Date.now(); beep(Math.min(1800, 600 + lvl * 300), 60); }
        if (strong >= 1 && !hit) { hit = { dS, dR, dL, strong }; beep(1200, 220, 2); speak("Reagiu!", false); }
        if (hit) hit = { dS: Math.max(hit.dS, dS), dR: Math.max(hit.dR, dR), dL: Math.max(hit.dL, dL), strong: Math.max(hit.strong, strong) };
        const meter = `<span class="tmeter">ajuste ${sg(-dS, 1)} · rotação ±${nf(dR)} · λ ${sg(-dL, 2)}</span>`;
        const big = hit ? "🎯 REAGIU!" : stage === "spray" ? "💨 BORRIFE AGORA" : "✋ PARE de borrifar";
        return { big, small: `${esc(p)}<br>${meter}<br>${stage === "spray" ? "Jatos curtos" : hit ? "O motor sentiu o produto neste ponto" : "Observando a reação"}`, tone: hit ? "bad" : stage === "spray" ? "go" : "" };
      };
      beep(1000, 250);
      await phase(3, "💨 BORRIFE AGORA", esc(p), "go", watch("spray"), "Borrife agora!");
      beep(500, 300);
      await phase(9, "✋ PARE de borrifar", esc(p), "", watch("stop"), "Pare de borrifar.");
      if (hit) { const why = [hit.dS > 6 ? `ajuste caiu ${nf(hit.dS, 0)} pontos` : "", hit.dR > 70 ? `rotação variou ${nf(hit.dR)} rpm` : "", hit.dL > 0.06 ? `sonda enriqueceu ${nf(hit.dL, 2)}` : ""].filter(Boolean).join(", ");
        if (!hits.some((h) => h.startsWith(p))) hits.push(`${p} (${why})`);
        speak(`${spoken(p)}: reagiu. ${why}.${hit.strong < 2 && count[p] < 2 ? " Repita este ponto depois para confirmar." : ""}`);
        log(false, `<b>${esc(p)}</b>: reagiu (${why})${hit.strong < 2 && count[p] < 2 ? ". Repita neste ponto para confirmar." : count[p] > 1 ? ". Confirmado na repetição." : ""}`); }
      else { if (!clean.includes(p)) clean.push(p); speak(`${spoken(p)}: sem reação.`); log(true, `${esc(p)}: sem reação`); }
      await phase(8, "Espere", "Deixando o motor voltar ao normal", "", null, "");
    }
    if (!hits.length && !clean.length) return close();
    await finish("leak", "Caça-vazamento", hits.length, hits.length ? hits : clean.map((c) => c + ": sem reação"),
      `Testados ${clean.length} ponto(s), nenhum reagiu. Se a mistura continua pobre parado, peça um <b>teste de fumaça</b> na oficina: ele acha vazamentos pequenos que o spray não alcança.`,
      `O ar falso entra por: <b>${hits.map(esc).join("; ")}</b>.<br>Corrija a vedação desse ponto, faça o reaprendizado da marcha lenta e rode 30–50 km.`);
  }

  // ------------------------------------------------------------ bicos injetores novos: antes da partida, partida, lenta, 2.500 rpm e comparação com a linha de base
  // linha de base de 2026-11-21 (anéis de baixo dos bicos rasgados): ajuste curto +46,9 % na lenta, λ 1,17
  const INJ_BASE = { stft: 46.9, lam: 1.17 };
  const BICOS = ["Bico 1 (lado da correia)", "Bico 2", "Bico 3", "Bico 4 (lado do câmbio)"];
  async function ask(big, small, opts, talk = big) { say(big, small); speak(talk); return buttons(opts); }
  async function injTest() {
    open("Bicos injetores novos", ["rpm", "stft1", "o2c1_lambda", "ect"]);
    await ready(`Teste completo depois de trocar os bicos. O celular <b>fala</b> cada passo:<br>
      1. <b>antes da partida</b>: confere a montagem e procura vazamento de combustível com a bomba pressurizando;<br>
      2. <b>partida</b> e aquecimento;<br>3. <b>marcha lenta</b> por 1 minuto e <b>2.500 rpm</b> por 25 s;<br>
      4. compara com antes da troca (ajuste curto <b>+46,9 %</b>, λ <b>1,17</b>) e oferece o caça-vazamento só nos 4 bicos.<br><br>
      ⚠️ Cheiro forte de gasolina ou bico molhado: <b>não dê a partida</b>. Tenha um extintor por perto.`, [C.stopped]);
    const lines = []; let bad = 0;
    const started = !!live?.engine_on || await ask("O motor já foi ligado depois da troca?", "Se ainda não, o teste começa pela checagem antes da partida.",
      [{ t: "Ainda não: começar antes da partida", v: false, main: true }, { t: "Já liguei: pular para a marcha lenta", v: true }], "O motor já foi ligado depois da troca dos bicos?");

    if (!started) {
      // 1) montagem: cada item confirmado pelo dono
      const CHECK = [["Os 4 bicos encaixados até o fim na flauta, com as presilhas no lugar (deu o clique)", "Bicos encaixados e presilhas no lugar?"],
        ["Anéis novos de cima e de baixo, lubrificados com óleo de motor (sem graxa nem silicone)", "Anéis novos, de cima e de baixo, com óleo de motor?"],
        ["Flauta parafusada por igual e os 4 conectores encaixados com clique", "Flauta parafusada e os quatro conectores encaixados?"],
        ["Nenhuma ferramenta, pano ou tampão esquecido sobre o motor ou nos furos", "Nada esquecido em cima do motor?"]];
      for (const [t, talk] of CHECK) {
        const r = await ask("Confira a montagem", esc(t), [{ t: "✓ Conferido", v: 1, main: true }, { t: "Ainda não: vou arrumar", v: 0 }], talk);
        if (!r) { log(null, `Montagem pendente: ${esc(t)}`); return finish("inj", "Bicos injetores novos", 0, ["montagem pendente antes da partida"], "", `Arrume isto antes de ligar: <b>${esc(t)}</b>. Depois rode o teste de novo.`, true); }
      }
      log(true, "Montagem conferida (bicos, anéis, flauta, conectores)");
      // 2) pressurizar sem ligar: 3 ciclos de ignição; a bomba roda ~2 s em cada um
      for (let i = 1; i <= 3; i++) {
        beep(880, 150);
        await phase(5, `Ligue a ignição (${i} de 3)`, "Só a chave na posição 2, <b>sem dar partida</b>. Escute a bomba zumbir uns 2 segundos.", "go", null, `Ligue a ignição, sem dar partida. Vez ${i} de 3.`);
        beep(660, 150);
        await phase(4, "Desligue a ignição", "Espere a contagem", "", null, "Desligue.");
      }
      const leak = await ask("Olhe e cheire em volta dos bicos", "Com uma lanterna, olhe a base de cada bico e as emendas da flauta. Tem <b>gotejando, molhado ou cheiro forte</b> de combustível?",
        [{ t: "Seco, sem cheiro", v: 0, main: true }, { t: "Tem vazamento", v: 1 }], "Com a lanterna, olhe a base de cada bico e as emendas da flauta. Está seco?");
      if (leak) { log(false, "<b>Vazamento de combustível com a bomba pressurizando</b>"); speak("Não dê a partida.");
        return finish("inj", "Bicos injetores novos", 1, ["vazamento de combustível antes da partida"], "",
          "<b>Não dê a partida.</b> Desligue a ignição, solte a flauta e confira o anel de cima do bico que vazou (torcido, cortado ou sem óleo) e se ele entrou reto. Seque tudo e repita este teste."); }
      log(true, "Sem vazamento de combustível com a linha pressurizada (3 ciclos de ignição)");
      // 3) partida: pode demorar um pouco na primeira (ar na linha)
      beep(990, 200);
      const t0 = Date.now(); let ok = false;
      await phase(40, "Dê a partida agora", "Se não pegar em 5 s, solte, espere 10 s e tente de novo.", "go", () => { if (live?.engine_on) { ok = true; return "stop"; } return {}; }, "Pode dar a partida.");
      if (!ok) return finish("inj", "Bicos injetores novos", 1, ["motor não pegou em 40 s"], "", "O motor não pegou. Confira os conectores dos bicos e o fusível da bomba (FI EM). Se tentar de novo, faça 2 ou 3 ciclos de ignição antes.", true);
      const tStart = (Date.now() - t0) / 1000; lines.push(`pegou em ${nf(tStart, 0)} s`);
      log(true, `Motor pegou (${nf(tStart, 0)} s depois do pedido)`);
      await phase(15, "Deixe em marcha lenta", "Não acelere. Já já confira os bicos de novo.", "", null, "Pegou. Deixe em marcha lenta, sem acelerar.");
      const leak2 = await ask("Olhe os bicos com o motor ligado", "A pressão agora é a de trabalho. Alguma gota ou cheiro de combustível na base dos bicos ou na flauta?",
        [{ t: "Seco", v: 0, main: true }, { t: "Tem vazamento", v: 1 }], "Olhe os bicos de novo com o motor ligado. Está seco?");
      if (leak2) { log(false, "<b>Vazamento de combustível com o motor ligado</b>"); speak("Desligue o motor agora.");
        return finish("inj", "Bicos injetores novos", 1, [...lines, "vazamento de combustível com o motor ligado"], "", "<b>Desligue o motor agora.</b> Confira o anel de cima do bico que vazou e a presilha. Não rode o carro assim."); }
      log(true, "Seco com o motor ligado");
    }

    // 4) aquecer até a malha fechada (a sonda passa a corrigir a mistura)
    if ((live?.values?.ect ?? 0) < 80) {
      let said = 0;
      await phase(900, "Aquecendo", "", "", () => { const e = live?.values?.ect; if (e == null) return {};
        if (e >= 80) return "stop";
        if (e - said >= 10) { said = Math.floor(e / 10) * 10; speak(`Motor em ${nf(e)} graus.`, false); }
        return { big: `${nf(e)} °C`, small: "Esperando chegar a 80 °C. Pé fora do pedal; o teste segue sozinho." }; }, "Agora vamos esperar o motor aquecer. Não acelere.");
      if ((live?.values?.ect ?? 0) < 80) log(null, `Motor não chegou a 80 °C em 15 min (${nf(live?.values?.ect)} °C): medindo assim mesmo.`);
    }
    // 5) marcha lenta 60 s: os primeiros 15 s descartados (ajuste ainda assentando)
    beep(880);
    const idle = (await phase(60, "Marcha lenta", "Pé fora do pedal, tudo desligado. Medindo 1 minuto.", "", null, "Marcha lenta por um minuto. Não toque no pedal.")).filter((x, _, a) => x.t > (a[0]?.t ?? 0) + 15000);
    const st0 = mean(col(idle, "stft1")), la0 = mean(lam(idle)), rp = col(idle, "rpm"), r0 = mean(rp), dev = sd(rp);
    const dips = r0 == null ? 0 : rp.filter((x) => x < r0 - 100).length;
    if (r0 == null || st0 == null) return finish("inj", "Bicos injetores novos", 0, lines, "", "Não recebi o ajuste curto e a rotação durante a marcha lenta. Confira se o painel está ao vivo e repita.", true);
    // 6) 2.500 rpm 25 s
    beep(880);
    const hi = steady(await phase(25, "", "", "", hold(2500, 300), "Agora acelere e segure em dois mil e quinhentos rotações."), 2500, 450).slice(3);
    beep(660); await phase(5, "Pode soltar", "Voltando à marcha lenta", "", null, "Pode soltar.");
    const st1 = mean(col(hi, "stft1")), la1 = mean(lam(hi));

    // veredito
    const okIdle = Math.abs(st0) <= 10, warnIdle = !okIdle && Math.abs(st0) <= 20;
    lines.push(`lenta: ajuste ${sg(st0, 1)} % (antes ${sg(INJ_BASE.stft, 1)} %)`, `λ ${nf(la0, 2)} (antes ${nf(INJ_BASE.lam, 2)})`, `rotação ${nf(r0)} ±${nf(dev)} rpm`);
    if (st1 != null) lines.push(`2.500 rpm: ajuste ${sg(st1, 1)} %${la1 ? ", λ " + nf(la1, 2) : ""}`);
    const gain = Math.abs(INJ_BASE.stft) - Math.abs(st0);
    log(okIdle, `Ajuste curto na lenta: <b>${sg(st0, 1)} %</b> (antes ${sg(INJ_BASE.stft, 1)} %${st0 > 0 ? `, ${gain > 0 ? `melhorou ${nf(gain, 0)} pontos` : "não melhorou"}` : ""}) ${okIdle ? "· dentro da meta −10 a +10 %" : st0 < -10 ? "· <b>rico: a central está tirando combustível</b>" : warnIdle ? "· <b>melhor, mas ainda fora da meta</b>" : "· <b>ainda pobre: entra ar</b>"}`);
    if (!okIdle) bad++;
    if (la0 != null) { const okL = la0 >= 0.95 && la0 <= 1.05; if (!okL) bad++; log(okL, `Sonda na lenta: λ <b>${nf(la0, 2)}</b> (antes ${nf(INJ_BASE.lam, 2)}) ${okL ? "· perto de 1,00" : la0 > 1.05 ? "· <b>pobre</b>" : "· <b>rica</b>"}`); }
    const okR = dev < 30 && dips <= 1 && r0 >= 650 && r0 <= 900; if (!okR) bad++;
    log(okR, `Marcha lenta ${nf(r0)} rpm, variação ±${nf(dev)} rpm${dips > 1 ? `, <b>${dips} quedas acima de 100 rpm</b> (falha de queima: bico entupido, conector solto ou vela)` : ""} ${okR ? "· estável" : r0 > 900 ? "· <b>alta: a central ainda compensa ar; faça o reaprendizado</b>" : ""}`);
    if (st1 != null) { const ok1 = Math.abs(st1) <= 10; if (!ok1) bad++; log(ok1, `Ajuste curto a 2.500 rpm: <b>${sg(st1, 1)} %</b> ${ok1 ? "· normal" : st1 > 0 ? "· <b>pobre também com rotação: falta combustível (bico, pressão ou filtro)</b>" : "· <b>rico</b>"}`); }
    else log(null, "Não consegui leitura estável a 2.500 rpm.");
    const mil = live?.mil, codes = [...(live?.dtc?.confirmed || []), ...(live?.dtc?.pending || [])];
    if (mil || codes.length) { bad++; log(false, `Central com ${mil ? "<b>luz de injeção acesa</b>" : "código registrado"}${codes.length ? ": " + codes.map(esc).join(", ") : ""}`); lines.push(`códigos: ${codes.join(", ") || "MIL"}`); }
    else log(true, "Nenhum código de falha na central");

    let msg;
    if (okIdle && !bad) msg = `<b>Vazamento resolvido.</b> O ajuste caiu de ${sg(INJ_BASE.stft, 0)} % para ${sg(st0, 0)} % e a sonda está em λ ${nf(la0, 2)}. Agora faça o <b>Reaprendizado da marcha lenta</b> e rode 30–50 km; o painel compara sozinho.`;
    else if (st0 > 10 && st1 != null && st0 - st1 > 8) msg = `A mistura ainda fica pobre <b>parado</b> (${sg(st0, 0)} %) e melhora com rotação (${sg(st1, 0)} %): ainda entra ar. Faça o <b>caça-vazamento nos bicos</b> (anel de baixo mal assentado) e depois na junta do coletor, servofreio e PCV.`;
    else if (st0 > 10) msg = `A mistura ainda está pobre (${sg(st0, 0)} %) em todas as rotações: confira se os 4 conectores dos bicos estão encaixados e a pressão de combustível. Rode também o <b>Raio-X da mistura</b>.`;
    else if (st0 < -10) msg = `Mistura <b>rica</b> (${sg(st0, 0)} %): um bico pode estar pingando ou o anel de cima vazando para dentro. Desligue e confira se algum bico molhou a vela ao redor.`;
    else msg = "A mistura está boa, mas veja os itens em vermelho acima.";
    log(okIdle && !bad, msg);
    // caça-vazamento só nos bicos, já com o motor quente
    if (st0 > 10) {
      const go = await ask(bad ? "⚠️ Encontrei algo" : "Quase", msg + "<br><br>Fazer agora o caça-vazamento <b>só nos 4 bicos</b>?", [{ t: "🎯 Caça-vazamento nos bicos", v: 1, main: true }, { t: "Não, ver o resultado", v: 0 }], "A mistura ainda está pobre. Quer fazer o caça-vazamento nos quatro bicos agora?");
      if (go) { saveInj(bad, lines); close(); return run(() => leakTest(BICOS))(); }
    }
    await finish("inj", "Bicos injetores novos", bad, lines, msg, msg);
  }
  // guarda o resultado sem a tela final, quando o teste emenda no caça-vazamento
  function saveInj(bad, lines) {
    const rec = { id: "inj", title: "Bicos injetores novos", ts: Date.now(), verdict: bad ? `${bad} ponto(s) de atenção` : "tudo certo", lines };
    try { const all = JSON.parse(localStorage.getItem("obdTests") || "[]"); all.unshift(rec); localStorage.setItem("obdTests", JSON.stringify(all.slice(0, 40))); } catch { }
    fetch("/api/annotations", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ kind: "teste", ts: rec.ts, text: `${rec.title}: ${rec.verdict}. ${lines.join("; ")}`.slice(0, 480) }) }).catch(() => { });
  }

  // ------------------------------------------------------------ reaprendizado da marcha lenta (depois de bateria desligada / corpo de borboleta)
  async function relearnTest() {
    open("Reaprendizado da marcha lenta", ["ect", "rpm", "stft1"]);
    await ready(`Use depois de desligar a bateria ou mexer no corpo de borboleta. O celular acompanha e <b>fala</b> cada etapa:<br>
      1. marcha lenta com tudo desligado, <b>sem tocar no pedal</b>;<br>2. espera a <b>ventoinha ligar e desligar</b>;<br>3. conta mais <b>10 minutos</b>;<br>4. avisa "pode desligar".<br><br>
      Se o motor ainda estiver frio, tudo bem: ele espera aquecer. Deixe o celular apoiado com a tela ligada.`, [C.on, C.stopped, C.calm, C.alloff]);
    let peak = 0, onAt = null, low = null, fanDone = false, warnPedal = 0, lastTalk = 0; const t0 = Date.now(); const lines = [];
    speak("Reaprendizado começou. Não toque no pedal e deixe tudo desligado.");
    await phase(1800, "Aguardando a ventoinha", "", "", () => {
      const v = live?.values || {}, e = v.ect; if (e == null) return {};
      if ((v.rpm ?? 0) > 1400 && Date.now() - warnPedal > 15000 && e >= 70) { warnPedal = Date.now(); speak("Tire o pé do acelerador"); }
      if (e > peak) peak = e;
      if (onAt == null && peak >= 94 && e <= peak - 2) { onAt = peak; beep(990, 160, 2); speak(`A ventoinha ligou, em ${nf(peak)} graus. Agora espere ela desligar.`); }
      if (onAt != null) { low = low == null ? e : Math.min(low, e); if (e >= low + 1 && low <= onAt - 2) { fanDone = true; return "stop"; } }
      if (onAt == null && Date.now() - lastTalk > 180000) { lastTalk = Date.now(); if (Date.now() - t0 > 60000) speak(`Motor em ${nf(e)} graus. Ainda esperando a ventoinha.`, false); }
      if (peak >= 106) return "stop";
      return { big: `${nf(e)} °C`, small: onAt == null ? (e < 90 ? "Aquecendo. A ventoinha costuma ligar perto de 95–100 °C" : "Quase lá: esperando a ventoinha ligar") : `Ventoinha ligou em ${nf(onAt)} °C · esperando desligar (temperatura parar de cair)`, tone: onAt == null ? "" : "go" };
    }, "");
    if (peak >= 106) { lines.push(`temperatura chegou a ${nf(peak)} °C sem a ventoinha baixar`); return finish("relearn", "Reaprendizado da marcha lenta", 1, lines, "", `A temperatura passou de 105 °C. <b>Desligue o motor</b> e confira a ventoinha e o nível do líquido com o motor frio.`); }
    if (!fanDone) { lines.push("ventoinha não completou o ciclo em 30 min"); return finish("relearn", "Reaprendizado da marcha lenta", 0, lines, "", "A ventoinha não completou o ciclo em 30 min. Rode o teste Ventoinha do radiador.", true); }
    lines.push(`ventoinha ligou em ${nf(onAt)} °C e desligou em ${nf(low)} °C`);
    beep(990, 200, 2); speak("A ventoinha desligou. Agora mais dez minutos em marcha lenta, sem tocar no pedal.");
    const rp = []; let said = 10;
    await phase(600, "Mais 10 minutos", "", "go", (w, el) => {
      const left = Math.ceil((600 - el) / 60); const v = live?.values || {};
      if (v.rpm != null) rp.push(v.rpm);
      if (left < said && left % 2 === 0 && left > 0) { said = left; speak(`Faltam ${left} minutos.`, false); }
      if ((v.rpm ?? 0) > 1400 && Date.now() - warnPedal > 15000) { warnPedal = Date.now(); speak("Tire o pé do acelerador"); }
      return { big: `${left} min`, small: `Marcha lenta ${nf(v.rpm)} rpm · motor ${nf(v.ect)} °C · não toque no pedal` };
    }, "");
    const m = mean(rp), dev = sd(rp); lines.push(`marcha lenta no fim ${nf(m)} rpm (±${nf(dev)})`);
    const st = mean(col(buf.filter((x) => x.t > Date.now() - 120000), "stft1")); if (st != null) lines.push(`ajuste curto ${sg(st, 0)} %`);
    const bad = (m && (m < 650 || m > 900) ? 1 : 0) + (st != null && st > 25 ? 1 : 0);
    speak("Pronto! Reaprendizado concluído. Pode desligar o motor, ou seguir direto para o caça-vazamento, que precisa do motor quente.");
    await finish("relearn", "Reaprendizado da marcha lenta", bad, lines,
      `Concluído. Marcha lenta em ${nf(m)} rpm. <b>Pode desligar o motor</b>, ou seguir direto para o <b>Caça-vazamento</b> (motor quente é o ideal).`,
      `Concluído, mas: marcha lenta ${nf(m)} rpm${st != null && st > 25 ? ` e ajuste curto em ${sg(st, 0)} % (mistura pobre: ainda entra ar falso)` : ""}. <b>Pode desligar</b>, ou faça agora o <b>Caça-vazamento</b>.`);
  }

  // ------------------------------------------------------------ 2. raio-X da mistura (parado, 3 rotações)
  async function mixTest() {
    open("Raio-X da mistura", ["rpm", "stft1", "o2c1_lambda", "maf", "map"]);
    await ready("Em 1 minuto, parado, o painel mede a correção de combustível em <b>marcha lenta, 1.500 e 2.500 rpm</b>. O jeito que ela muda com a rotação mostra a causa: entrada de ar, MAF sujo ou falta de combustível.", [C.on, C.hot, C.stopped]);
    const a = await phase(15, "Marcha lenta", "Pé fora do pedal");
    beep(880);
    const b = steady(await phase(18, "", "", "", hold(1500, 250), "Acelere e segure em mil e quinhentos rotações"), 1500, 350).slice(3);
    beep(880);
    const c = steady(await phase(18, "", "", "", hold(2500, 300), "Agora segure em dois mil e quinhentos"), 2500, 450).slice(3);
    beep(660);
    await phase(4, "Pode soltar", "Terminando");
    const s = [a, b, c].map((x) => mean(col(x, "stft1"))), l = [a, b, c].map((x) => mean(lam(x)));
    const lines = [`ajuste: lenta ${sg(s[0], 0)} %, 1.500 rpm ${sg(s[1], 0)} %, 2.500 rpm ${sg(s[2], 0)} %`];
    if (s[0] == null || (b.length < 4 && c.length < 4)) { log(null, "Não consegui leituras estáveis em 1.500/2.500 rpm."); return finish("mix", "Raio-X da mistura", 0, lines, "", "Repita segurando a rotação parada em cada etapa até o bipe.", true); }
    ["Marcha lenta", "1.500 rpm", "2.500 rpm"].forEach((n, i) => { if (s[i] != null) log(Math.abs(s[i]) <= 12, `${n}: ajuste ${sg(s[i], 1)} %${l[i] ? " · λ " + nf(l[i], 2) : ""} ${Math.abs(s[i]) <= 12 ? "(normal)" : s[i] > 0 ? "<b>(pobre: a central está adicionando combustível)</b>" : "<b>(rica: a central está tirando combustível)</b>"}`); });
    const hi = s[2] ?? s[1], worst = Math.max(...s.filter((x) => x != null).map(Math.abs));
    let bad = 0, msg = "";
    if (worst <= 12) msg = "A correção ficou dentro de ±12 % nas três rotações: mistura saudável.";
    else if (s[0] > 12 && hi != null && s[0] - hi > 8) { bad = 1; msg = `A correção é alta parado (${sg(s[0], 0)} %) e <b>some conforme a rotação sobe</b> (${sg(hi, 0)} %). Essa é a assinatura de <b>entrada falsa de ar</b>: faça o teste Caça-vazamento.`; lines.push("assinatura: entrada falsa de ar"); }
    else if (s[0] > 12 && hi != null && hi - s[0] > 8) { bad = 1; msg = `A correção <b>cresce com a rotação</b> (${sg(s[0], 0)} % → ${sg(hi, 0)} %): falta combustível quando o motor pede mais. Verifique <b>pressão da bomba, filtro de combustível e bicos</b>.`; lines.push("assinatura: falta de combustível com carga"); }
    else if (s[0] > 12) { bad = 1; msg = `A correção é alta e <b>parecida em todas as rotações</b> (${sg(s[0], 0)} % → ${sg(hi, 0)} %): o erro é proporcional. O mais comum é <b>sensor MAF sujo</b> (limpe com spray próprio) ou combustível com mistura diferente da esperada.`; lines.push("assinatura: erro proporcional (MAF)"); }
    else { bad = 1; msg = `Mistura <b>rica</b> (${sg(Math.min(...s.filter((x) => x != null)), 0)} %): confira filtro de ar entupido, bico vazando, pressão de combustível alta ou sensor de temperatura lendo frio.`; lines.push("assinatura: mistura rica"); }
    log(!bad, msg);
    await finish("mix", "Raio-X da mistura", bad, lines, msg, msg);
  }

  // ------------------------------------------------------------ 3. acelerada e volta à marcha lenta
  async function snapTest() {
    open("Acelerada e volta à marcha lenta", ["rpm", "tps", "stft1", "o2c1_lambda"]);
    await ready("Você dá <b>3 aceleradas rápidas</b> até uns 3.000 rpm e solta. O painel mede como a rotação volta: se cai demais, quase morre ou fica oscilando, é sinal de corpo de borboleta sujo, entrada de ar ou falta do reaprendizado da marcha lenta.", [C.on, C.hot, C.stopped, C.calm]);
    const base = await phase(8, "Marcha lenta", "Medindo o repouso");
    const r0 = mean(col(base, "rpm")), s0 = sd(col(base, "rpm"));
    if (r0 == null) return finish("snap", "Volta à marcha lenta", 0, [], "", "Sem leitura do motor.", true);
    const res = [];
    for (let i = 1; i <= 3; i++) {
      beep(880);
      let peak = 0, tPeak = 0;
      await phase(8, `Acelerada ${i} de 3`, "Acelere rápido até ~3.000 rpm e solte", "go", (w) => { const r = live?.values?.rpm ?? 0; if (r > peak) { peak = r; tPeak = Date.now(); }
        if (peak > 2200 && r < 1400 && Date.now() - tPeak > 600) return "stop"; return { big: `${nf(r)} rpm` }; });
      if (peak < 2000) { log(null, `Acelerada ${i}: só chegou a ${nf(peak)} rpm, não valeu.`); await phase(3, "Espere", ""); continue; }
      const after = await phase(7, "Solte e espere", "Medindo a volta à marcha lenta");
      const rp = after.filter((x) => x.v.rpm != null), low = Math.min(...rp.map((x) => x.v.rpm));
      const settle = rp.find((x, j) => rp.slice(j).every((y) => Math.abs(y.v.rpm - r0) < 90));
      const tSet = settle ? (settle.t - tPeak) / 1000 : null;
      const tail = rp.filter((x) => x.t > rp[0].t + 3500).map((x) => x.v.rpm), wob = sd(tail);
      res.push({ peak, low, tSet, wob });
      const under = r0 - low, ok = under < 150 && tSet != null && tSet < 5 && wob < 60 && low > 450;
      log(ok, `Acelerada ${i}: pico ${nf(peak)} rpm · voltou em ${tSet == null ? "<b>mais de 7 s</b>" : nf(tSet, 1) + " s"} · caiu até ${nf(low)} rpm${under >= 150 ? ` <b>(${nf(under)} abaixo da marcha lenta)</b>` : ""}${low <= 450 ? " <b>(quase morreu)</b>" : ""}${wob >= 60 ? ` · <b>ficou oscilando ±${nf(wob)}</b>` : ""}`);
    }
    if (res.length < 2) return finish("snap", "Volta à marcha lenta", 0, [], "", "Não consegui medir. Repita acelerando até uns 3.000 rpm e soltando de uma vez.", true);
    const low = Math.min(...res.map((r) => r.low)), tS = Math.max(...res.map((r) => r.tSet ?? 9)), wob = Math.max(...res.map((r) => r.wob));
    const bad = [r0 - low >= 150 || low <= 450, tS >= 5, wob >= 60].filter(Boolean).length;
    await finish("snap", "Volta à marcha lenta", bad, [`marcha lenta ${nf(r0)} rpm (±${nf(s0)})`, `mínima na volta ${nf(low)} rpm`, `tempo para assentar ${tS >= 9 ? "> 7" : nf(tS, 1)} s`, `oscilação ±${nf(wob)} rpm`],
      "A rotação volta rápido e assenta sem cair nem oscilar: a central controla bem a marcha lenta.",
      "A volta à marcha lenta não está limpa. Em ordem: resolver entrada de ar (teste Caça-vazamento), fazer o <b>reaprendizado da marcha lenta</b> (obrigatório depois de trocar o corpo de borboleta) e conferir os conectores do corpo de borboleta.");
  }

  // ------------------------------------------------------------ 4. varredura do pedal e da borboleta (motor desligado)
  function fit(xs, ys) { const n = xs.length, mx = mean(xs), my = mean(ys); const sxx = xs.reduce((s, x) => s + (x - mx) ** 2, 0); if (n < 5 || sxx < 1) return null;
    const a = xs.reduce((s, x, i) => s + (x - mx) * (ys[i] - my), 0) / sxx, b = my - a * mx; return { a, b, res: Math.max(...xs.map((x, i) => Math.abs(ys[i] - (a * x + b)))) }; }
  async function sweepTest() {
    open("Varredura do pedal e da borboleta", ["pedal_d", "tps", "batt"]);
    await ready("Com o motor desligado dá para pisar até o fundo sem acelerar. Você pisa <b>bem devagar</b> e solta devagar, duas vezes. O painel confere se os dois sensores do pedal andam juntos, se há salto ou ponto morto no sinal e se a borboleta acompanha.", [C.koeo]);
    await phase(3, "Pé fora do pedal", "Medindo o repouso");
    let s = [];
    for (let i = 1; i <= 2; i++) {
      beep(660, 120); s = s.concat(await phase(9, `Pise devagar até o fundo (${i}/2)`, "Leve uns 8 segundos", "go", () => ({ big: `${nf(live?.values?.pedal_d, 0)} %` })));
      beep(880, 120); s = s.concat(await phase(2, "Segure no fundo", ""));
      beep(660, 120); s = s.concat(await phase(7, "Solte devagar", "Leve uns 6 segundos", "go", () => ({ big: `${nf(live?.values?.pedal_d, 0)} %` })));
    }
    const ok = s.filter((x) => x.v.pedal_d != null), pd = ok.map((x) => x.v.pedal_d), lines = []; let bad = 0;
    const range = pd.length ? Math.max(...pd) - Math.min(...pd) : 0;
    if (range < 40) { log(null, `O pedal só variou ${nf(range)} pontos.`); return finish("sweep", "Varredura do pedal", 0, [`pedal variou só ${nf(range)} pontos`], "", "Repita pisando devagar até o fundo.", true); }
    log(true, `Curso do pedal: ${nf(Math.min(...pd), 1)} % solto → ${nf(Math.max(...pd), 1)} % no fundo`); lines.push(`curso ${nf(Math.min(...pd), 0)}–${nf(Math.max(...pd), 0)} %`);
    const both = ok.filter((x) => x.v.pedal_e != null), f = fit(both.map((x) => x.v.pedal_d), both.map((x) => x.v.pedal_e));
    if (f) { const good = f.res < 5; if (!good) bad++; log(good, `Sensores D e E do pedal ${good ? "andam juntos" : "<b>se separaram</b>"} (maior diferença: ${nf(f.res, 1)} pontos${good ? "" : "; bom é menos de 5"})`); lines.push(`pedal D×E desvio ${nf(f.res, 1)}`); }
    else log(null, "Sem leitura do segundo sensor do pedal (E).");
    let jumps = 0; for (let i = 2; i < pd.length; i++) if (Math.abs(pd[i - 1] - pd[i - 2]) > 25 && Math.abs(pd[i] - pd[i - 2]) < 6) jumps++;
    if (jumps) bad++; log(!jumps, jumps ? `<b>${jumps} salto(s)</b> no sinal do pedal (vai e volta numa leitura): mau contato ou pista gasta` : "Sinal do pedal sem saltos"); lines.push(`${jumps} salto(s)`);
    const rest = pd.filter((x) => x < Math.min(...pd) + 2).length, stuck = sd(pd.slice(0, 6)) > 1.5;
    if (stuck) { bad++; log(false, "O sinal do pedal <b>treme com o pé fora</b>: deveria ficar parado em repouso."); } else if (rest) log(true, "Em repouso o sinal do pedal fica parado");
    const tp = ok.map((x) => x.v.tps).filter((x) => x != null), trange = tp.length ? Math.max(...tp) - Math.min(...tp) : 0;
    if (trange > 15) {
      log(true, `A borboleta acompanhou o pedal: ${nf(Math.min(...tp), 1)} % fechada → ${nf(Math.max(...tp), 1)} % aberta`); lines.push(`borboleta ${nf(Math.min(...tp), 0)}–${nf(Math.max(...tp), 0)} %`);
      const ab = ok.filter((x) => x.v.tps != null && x.v.tps_b != null), g = fit(ab.map((x) => x.v.tps), ab.map((x) => x.v.tps_b));
      if (g) { const good = g.res < 6; if (!good) bad++; log(good, `Sensores A e B da borboleta ${good ? "concordam" : "<b>discordaram</b>"} em todo o curso (maior diferença: ${nf(g.res, 1)} pontos)`); lines.push(`borboleta A×B desvio ${nf(g.res, 1)}`); }
    } else log(null, `Com o motor desligado a central não abriu a borboleta (variou só ${nf(trange, 1)} pontos). Em muitos carros isso é normal; o resultado vale para o pedal.`);
    await finish("sweep", "Varredura do pedal", bad, lines, "Pedal e sensores responderam como esperado em todo o curso.",
      "Veja os itens em vermelho. Costuma ser conector com mau contato: desligue a chave, desencaixe, limpe com limpa-contato e reencaixe. Depois repita o teste.");
  }

  // ------------------------------------------------------------ 5. marcha lenta com carga elétrica
  async function loadTest() {
    open("Marcha lenta com tudo ligado", ["rpm", "ecu_volt", "stft1"]);
    await ready("Primeiro o painel mede o motor sem nada ligado. Depois você liga <b>ar-condicionado no máximo, farol alto e desembaçador traseiro</b>. Um carro saudável segura a rotação e a tensão.", [C.on, C.hot, C.stopped, C.alloff]);
    const a = await phase(12, "Tudo desligado", "Medindo sem carga");
    beep(880, 150);
    const b = await phase(25, "Ligue tudo agora", "Ar no máximo + farol alto + desembaçador traseiro", "go");
    beep(660, 150);
    await phase(10, "Desligue tudo", "Medindo a volta ao normal");
    const r0 = mean(col(a, "rpm")), late = b.filter((x) => x.t > b[0]?.t + 8000), r1 = mean(col(late, "rpm")), dip = r0 - Math.min(...col(b, "rpm"));
    const v0 = mean(volts(a)), v1 = mean(volts(late)), sdl = sd(col(late, "rpm")), lines = []; let bad = 0;
    if (r0 == null || r1 == null) return finish("load", "Marcha lenta com carga", 0, [], "", "Não recebi dados suficientes do motor.", true);
    const hold1 = r1 > r0 - 80; if (!hold1) bad++;
    log(hold1, `Rotação: ${nf(r0)} rpm sem carga → ${nf(r1)} rpm com tudo ligado ${hold1 ? "(a central compensou)" : "<b>(caiu demais: a marcha lenta não está compensando)</b>"}`); lines.push(`rpm ${nf(r0)}→${nf(r1)}`);
    const dipOk = dip < 200; if (!dipOk) bad++;
    log(dipOk, `Queda na hora de ligar: ${nf(dip)} rpm ${dipOk ? "" : "<b>(quase morreu)</b>"}`); lines.push(`queda ${nf(dip)} rpm`);
    const st = sdl < 50; if (!st) bad++;
    log(st, `Estabilidade com carga: ±${nf(sdl)} rpm ${st ? "" : "<b>(oscilando)</b>"}`);
    if (v1 != null) { const vok = v1 >= 13.0; if (!vok) bad++;
      log(vok, `Tensão: ${nf(v0, 1)} V sem carga → ${nf(v1, 1)} V com tudo ligado ${vok ? "(alternador dá conta)" : "<b>(baixa: alternador, correia ou bateria fracos)</b>"}`); lines.push(`tensão ${nf(v0, 1)}→${nf(v1, 1)} V`); }
    else log(null, "Sem leitura de tensão durante o teste.");
    await finish("load", "Marcha lenta com carga", bad, lines, "O motor segurou a rotação e o alternador manteve a tensão com tudo ligado.",
      "Veja os itens em vermelho. Rotação que cai ou oscila com carga costuma vir de mistura pobre, corpo de borboleta sujo ou falta do reaprendizado da marcha lenta; tensão baixa vem de alternador, correia ou bateria.");
  }

  // ------------------------------------------------------------ 6. bateria: repouso, partida e carga
  async function crankTest() {
    open("Bateria na partida", ["batt", "rpm"]);
    await ready("O painel mede a bateria <b>em repouso</b>, o quanto ela afunda <b>na partida</b> e se o alternador <b>carrega</b> em seguida. Deixe faróis e ar desligados.", [C.koeo]);
    const a = await phase(8, "Não dê a partida ainda", "Medindo a bateria em repouso");
    const rest = mean(col(a, "batt"));
    if (rest == null) return finish("crank", "Bateria na partida", 0, [], "", "Sem leitura de tensão.", true);
    beep(880, 200);
    let started = 0, minV = 99;
    const b = await phase(25, "Dê a partida agora", "", "go", (w) => { const v = col(w, "batt"); if (v.length) minV = Math.min(minV, ...v);
      if (live?.engine_on && !started) started = Date.now(); if (started && Date.now() - started > 9000) return "stop";
      return started ? { big: "Motor pegou", small: "Medindo a carga do alternador", tone: "go" } : { small: `mínima até agora ${minV < 99 ? nf(minV, 1) + " V" : "—"}` }; });
    if (!started) return finish("crank", "Bateria na partida", 0, [`repouso ${nf(rest, 1)} V`], "", "O motor não ligou durante o teste. Repita e dê a partida quando o painel pedir.", true);
    const chg = mean(volts(b.filter((x) => x.t > started + 3000))) ?? mean(col(b.filter((x) => x.t > started + 3000), "batt"));
    const lines = [`repouso ${nf(rest, 1)} V`, `mínima na partida ${nf(minV, 1)} V`, `carregando ${nf(chg, 1)} V`]; let bad = 0;
    const r = rest >= 12.4; if (!r) bad++; log(r, `Em repouso: ${nf(rest, 1)} V ${r ? "(carregada)" : rest >= 12.0 ? "<b>(meia carga: bateria cansada ou carro pouco usado)</b>" : "<b>(descarregada)</b>"}`);
    const k = minV >= 9.6; if (!k) bad++; log(k, `Mínima na partida: ${nf(minV, 1)} V ${k ? "(segura bem)" : "<b>(afunda demais: bateria fraca ou terminais ruins)</b>"}<br><span class="muted">O Pi lê ~2 vezes por segundo: a queda real pode ser um pouco maior.</span>`);
    if (chg != null) { const c = chg >= 13.2 && chg <= 15.0; if (!c) bad++; log(c, `Carregando: ${nf(chg, 1)} V ${c ? "(alternador ok)" : chg < 13.2 ? "<b>(baixa: alternador ou correia)</b>" : "<b>(alta demais: regulador)</b>"}`); }
    await finish("crank", "Bateria na partida", bad, lines, "Bateria carregada, segura a partida e o alternador repõe a carga.",
      "Veja os itens em vermelho. Limpe e aperte os terminais; se continuar, faça um teste de carga da bateria numa autoelétrica.");
  }

  // ------------------------------------------------------------ 7. ventoinha e arrefecimento
  async function fanTest() {
    open("Ventoinha do radiador", ["ect", "rpm", "ecu_volt"]);
    await ready("Com o carro parado em marcha lenta, a temperatura sobe até a ventoinha ligar e depois cai. O painel acompanha <b>um ciclo completo</b> e mede em que temperatura ela liga e o quanto consegue baixar. Pode levar até 12 minutos: deixe o celular apoiado.", [C.on, C.hot, C.stopped]);
    let peak = 0, tPeak = 0, onAt = null, low = null, done = false; const t0 = Date.now();
    await phase(720, "Aguardando a ventoinha", "", "", () => {
      const e = live?.values?.ect; if (e == null) return {};
      if (e > peak) { peak = e; tPeak = Date.now(); }
      if (onAt == null && peak >= 94 && e <= peak - 2) { onAt = peak; beep(990, 160, 2); speak("A ventoinha ligou"); }           // começou a cair: a ventoinha ligou
      if (onAt != null) { low = low == null ? e : Math.min(low, e); if (e >= low + 1 && low <= onAt - 3) { done = true; return "stop"; } }
      if (peak >= 106) return "stop";
      return { big: `${nf(e)} °C`, small: onAt == null ? `Subindo… pico até agora ${nf(peak)} °C` : `Ventoinha ligou em ${nf(onAt)} °C · baixando (mínimo ${nf(low)} °C)`, tone: onAt != null ? "go" : e >= 103 ? "bad" : "" };
    });
    const mins = (Date.now() - t0) / 60000, lines = [`pico ${nf(peak)} °C`]; let bad = 0;
    if (peak >= 106 && onAt == null) { bad++; log(false, `A temperatura chegou a <b>${nf(peak)} °C sem a ventoinha derrubar</b>. Desligue o motor.`); }
    else if (onAt == null) { log(null, `Em ${nf(mins, 0)} min a temperatura só chegou a ${nf(peak)} °C e a ventoinha não precisou ligar.`); return finish("fan", "Ventoinha", 0, lines, "", "Não deu para ver um ciclo. Repita com o motor mais quente (depois de rodar) ou num dia mais quente.", true); }
    else { const okOn = onAt <= 102; if (!okOn) bad++; log(okOn, `A ventoinha ligou em ${nf(onAt)} °C ${okOn ? "(normal: 93–101 °C)" : "<b>(tarde: sensor, relé ou ventoinha fraca)</b>"}`); lines.push(`ligou em ${nf(onAt)} °C`);
      const drop = onAt - (low ?? onAt), okD = drop >= 3 && done; if (!okD) bad++; log(okD, `Baixou ${nf(drop)} °C, até ${nf(low)} °C ${okD ? "e desligou" : "<b>(não conseguiu baixar: radiador sujo/entupido, pouco líquido ou ventoinha fraca)</b>"}`); lines.push(`baixou até ${nf(low)} °C`); }
    await finish("fan", "Ventoinha", bad, lines, "A ventoinha liga na temperatura certa e consegue baixar a temperatura.",
      "Com o motor FRIO, confira o nível do líquido de arrefecimento. Depois: fusível e relé da ventoinha, colmeia do radiador suja por fora e tampa do radiador.");
  }

  // ------------------------------------------------------------ 8. sondas e catalisador
  async function catTest() {
    open("Sondas e catalisador", ["rpm", "o2s2_v", "o2c1_lambda", "stft1"]);
    await ready("Você segura <b>2.500 rpm por 45 segundos</b>. Um catalisador bom deixa a sonda de trás quase parada; se ela oscila junto com a da frente, o catalisador está fraco. O painel também confere se a sonda da frente responde.", [C.on, C.hot, C.stopped]);
    const w = steady(await phase(45, "", "", "", hold(2500, 300), "Acelere e segure em dois mil e quinhentos rotações por quarenta e cinco segundos"), 2500, 500).slice(4);
    beep(660); await phase(3, "Pode soltar", "");
    const post = col(w, "o2s2_v"), la = lam(w), lines = []; let bad = 0;
    if (w.length < 20 || post.length < 5) { log(null, `Leituras estáveis insuficientes (${post.length} da sonda de trás).`); return finish("cat", "Sondas e catalisador", 0, [], "", "Repita segurando a rotação parada em 2.500 rpm até o bipe.", true); }
    const pm = mean(post), ps = sd(post), pr = Math.max(...post) - Math.min(...post), lm = mean(la), ls = sd(la);
    lines.push(`pós-cat ${nf(pm, 2)} V (variação ${nf(pr, 2)} V)`, `sonda da frente λ ${nf(lm, 2)}`);
    const front = lm != null && lm > 0.9 && lm < 1.1 && ls > 0.002; if (!front) bad++;
    log(front, lm == null ? "Sem leitura da sonda da frente." : `Sonda da frente: λ médio ${nf(lm, 2)} ${front ? "(perto de 1,00 e respondendo)" : lm >= 1.1 ? "<b>(pobre mesmo a 2.500 rpm)</b>" : lm <= 0.9 ? "<b>(rica)</b>" : "<b>(parada demais: pode estar lenta)</b>"}`);
    if (pm < 0.2) { bad++; log(false, `Sonda de trás em ${nf(pm, 2)} V: <b>escape pobre</b> (mistura pobre, vazamento no escapamento antes da sonda ou sonda fraca).`); }
    else if (pr > 0.5) { bad++; log(false, `Sonda de trás <b>oscilando ${nf(pr, 2)} V</b> (de ${nf(Math.min(...post), 2)} a ${nf(Math.max(...post), 2)} V): o catalisador não está segurando o oxigênio. Indica <b>catalisador fraco</b> (código típico: P0420).`); }
    else log(true, `Sonda de trás estável em ${nf(pm, 2)} V (variação de ${nf(pr, 2)} V): catalisador trabalhando.`);
    void ps;
    await finish("cat", "Sondas e catalisador", bad, lines, "Sondas respondendo e catalisador armazenando oxigênio como deve.",
      "Veja os itens em vermelho. Antes de trocar catalisador ou sonda, resolva a mistura (Raio-X da mistura) e procure vazamento no escapamento: os dois enganam este teste.");
  }

  // ------------------------------------------------------------ lista e histórico
  const TESTS = [
    { id: "inj", f: injTest, ic: "💉", t: "Bicos injetores novos", d: "Depois de trocar os bicos: confere a montagem e vazamento de combustível antes da partida, mede a mistura na lenta e a 2.500 rpm e compara com antes da troca.", need: "Antes da partida ou motor ligado, parado", min: "5–20 min", g: "Mistura e admissão" },
    { id: "relearn", f: relearnTest, ic: "🔁", t: "Reaprendizado da marcha lenta", d: "Depois de desligar a bateria ou mexer na borboleta: acompanha a ventoinha, conta 10 min e avisa por voz quando pode desligar.", need: "Motor ligado, parado", min: "15–30 min", g: "Mistura e admissão" },
    { id: "leak", f: leakTest, ic: "🎯", t: "Caça-vazamento de ar", d: "Borrife limpa-carburador em cada ponto: o celular apita quando o motor reage. Acha entrada falsa de ar.", need: "Motor quente, parado", min: "5 min", g: "Mistura e admissão" },
    { id: "mix", f: mixTest, ic: "🔬", t: "Raio-X da mistura", d: "Mede a correção de combustível em 3 rotações e diz a causa: entrada de ar, MAF ou falta de combustível.", need: "Motor quente, parado", min: "1 min", g: "Mistura e admissão" },
    { id: "cat", f: catTest, ic: "🧪", t: "Sondas e catalisador", d: "Segurando 2.500 rpm: confere a sonda da frente e se o catalisador ainda trabalha.", need: "Motor quente, parado", min: "1 min", g: "Mistura e admissão" },
    { id: "snap", f: snapTest, ic: "⚡", t: "Acelerada e volta à marcha lenta", d: "3 aceleradas rápidas: mede se a rotação volta limpa ou cai, oscila e quase morre.", need: "Motor quente, parado", min: "1 min", g: "Borboleta e marcha lenta" },
    { id: "sweep", f: sweepTest, ic: "🦶", t: "Varredura do pedal e da borboleta", d: "Pise devagar até o fundo: confere os sensores em dupla, saltos de sinal e pontos mortos.", need: "Ignição ligada, motor desligado", min: "1 min", g: "Borboleta e marcha lenta" },
    { id: "load", f: loadTest, ic: "💡", t: "Marcha lenta com tudo ligado", d: "Liga ar, farol alto e desembaçador: confere se a rotação e o alternador seguram.", need: "Motor quente, parado", min: "1 min", g: "Elétrica e arrefecimento" },
    { id: "crank", f: crankTest, ic: "🔋", t: "Bateria na partida", d: "Mede a bateria em repouso, o quanto afunda na partida e se o alternador carrega.", need: "Ignição ligada, motor desligado", min: "1 min", g: "Elétrica e arrefecimento" },
    { id: "fan", f: fanTest, ic: "🌀", t: "Ventoinha do radiador", d: "Acompanha um ciclo: em que temperatura a ventoinha liga e quanto ela consegue baixar.", need: "Motor quente, parado", min: "até 12 min", g: "Elétrica e arrefecimento" },
  ];
  const good = (r) => /certo/.test(r.verdict), inc = (r) => /incompleto/.test(r.verdict);
  function history() { try { return JSON.parse(localStorage.getItem("obdTests") || "[]"); } catch { return []; } }
  function renderHistory() {
    const box = $("#tstHist"); if (!box) return;
    const all = history();
    box.innerHTML = all.length ? all.slice(0, 12).map((r) => `<div class="alert" style="--s:${good(r) ? "var(--ok)" : inc(r) ? "var(--muted)" : "var(--bad)"}"><span class="ic">${good(r) ? "✓" : "!"}</span>
      <div><div class="t">${esc(r.title)}: ${esc(r.verdict)}</div><div class="d">${new Date(r.ts).toLocaleString("pt-BR", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" })} · ${esc((r.lines || []).join(" · "))}</div></div><span></span></div>`).join("")
      : `<div class="empty">Nenhum teste feito neste aparelho ainda.</div>`;
    renderList();
  }
  function renderList() {
    const box = $("#tstList"); if (!box) return;
    const all = history(); let g = "";
    box.innerHTML = TESTS.map((t) => { const last = all.find((r) => r.id === t.id && !inc(r));
      const head = t.g !== g ? `<h3 class="lbl" style="margin:${g ? 14 : 0}px 0 2px">${t.g}</h3>` : ""; g = t.g;
      return `${head}<button type="button" class="tcard" data-t="${t.id}"><span class="tic">${t.ic}</span><span><b>${t.t}</b><span class="d">${t.d}</span><span class="need">${t.need} · ${t.min}</span>${last ? `<span class="last" style="--s:${good(last) ? "var(--ok)" : "var(--bad)"}">${good(last) ? "✓" : "!"} último: ${esc(last.verdict)}</span>` : ""}</span><span class="go">Iniciar ›</span></button>`; }).join("");
  }
  window.obdTests = { render() { renderList(); renderHistory(); } };
  document.addEventListener("click", (e) => {
    const b = e.target.closest(".tcard[data-t]"); if (b) { run(TESTS.find((t) => t.id === b.dataset.t).f)(); return; }
    if (e.target.closest("#tstClose")) close();
  });
})();
