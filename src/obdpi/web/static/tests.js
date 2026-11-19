/* Testes guiados "caça-problema": o painel orienta o que fazer no carro e interpreta as leituras ao vivo.
   Usa o mesmo fluxo ao vivo do painel (evento "obdlive", disparado pelo app.js a cada leitura). */
(() => {
  "use strict";
  const $ = (s) => document.querySelector(s);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const nf = (v, d = 0) => (v == null || Number.isNaN(v)) ? "—" : Number(v).toLocaleString("pt-BR", { minimumFractionDigits: d, maximumFractionDigits: d });
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const mean = (a) => a.length ? a.reduce((x, y) => x + y, 0) / a.length : null;
  const sd = (a) => { if (a.length < 2) return 0; const m = mean(a); return Math.sqrt(mean(a.map((x) => (x - m) ** 2))); };
  const col = (s, k) => s.map((x) => x.v[k]).filter((x) => x != null);

  let live = null, buf = [], running = null, cancelled = false;
  window.addEventListener("obdlive", (e) => { live = e.detail; if (running) { buf.push({ t: Date.now(), v: { ...(live.values || {}) }, on: live.engine_on }); if (buf.length > 4000) buf.shift(); paintLive(); } });

  // ------------------------------------------------------------ som e vibração
  let actx = null;
  function beep(freq = 880, ms = 180, times = 1) {
    try {
      actx ||= new (window.AudioContext || window.webkitAudioContext)();
      for (let i = 0; i < times; i++) { const o = actx.createOscillator(), g = actx.createGain(); o.frequency.value = freq; o.connect(g); g.connect(actx.destination);
        const t0 = actx.currentTime + i * (ms + 90) / 1000; g.gain.setValueAtTime(0.25, t0); g.gain.exponentialRampToValueAtTime(0.001, t0 + ms / 1000); o.start(t0); o.stop(t0 + ms / 1000); }
      navigator.vibrate?.(times > 1 ? [200, 80, 200] : 120);
    } catch { }
  }

  // ------------------------------------------------------------ janela do teste
  const M = () => $("#tstModal");
  function open(title) { cancelled = false; buf = []; running = title; $("#tstTitle").textContent = title; $("#tstLog").innerHTML = ""; $("#tstBtns").innerHTML = ""; say("", ""); M().hidden = false; paintLive(); }
  function close() { running = null; cancelled = true; M().hidden = true; renderHistory(); }
  function say(big, small, tone = "") { $("#tstBig").innerHTML = big; $("#tstBig").className = "tbig " + tone; $("#tstSmall").innerHTML = small; }
  function log(ok, text) { $("#tstLog").insertAdjacentHTML("beforeend", `<div class="alert" style="--s:${ok === true ? "var(--ok)" : ok === false ? "var(--bad)" : "var(--warn)"}"><span class="ic">${ok === true ? "✓" : "!"}</span><div><div class="t">${text}</div></div><span></span></div>`); }
  function buttons(list) { return new Promise((res) => { $("#tstBtns").innerHTML = list.map((b, i) => `<button class="btn${b.main ? " big" : ""}" type="button" data-i="${i}">${esc(b.t)}</button>`).join("");
    $("#tstBtns").onclick = (e) => { const b = e.target.closest("[data-i]"); if (b) { $("#tstBtns").innerHTML = ""; res(list[+b.dataset.i].v); } }; }); }
  const SHOW = [["rpm", "Rotação", "rpm", 0], ["stft1", "Ajuste curto", "%", 1], ["o2c1_lambda", "Sonda (λ)", "", 2], ["tps", "Borboleta", "%", 1], ["pedal_d", "Pedal", "%", 1], ["batt", "Bateria", "V", 1]];
  function paintLive() {
    const v = live?.values || {};
    $("#tstLive").innerHTML = SHOW.map(([k, l, u, d]) => `<div><span class="lbl">${l}</span><b>${nf(v[k], d)}<small> ${u}</small></b></div>`).join("");
    const st = live?.state === "pi_offline" ? "Carro desligado (sem dados ao vivo)" : live?.engine_on ? "Motor ligado" : live?.state === "coletando" ? "Ignição ligada, motor desligado" : "Sem leitura do OBD";
    $("#tstState").textContent = st + (live?.cloud ? " · painel online (atraso de ~1–2 s)" : "");
  }
  // coleta por `sec` segundos mostrando uma contagem; devolve as leituras do intervalo
  async function collect(sec, big, small, tone) {
    const t0 = Date.now();
    while (Date.now() - t0 < sec * 1000) { if (cancelled) throw new Error("cancelado"); say(big, `${small} · <b>${Math.ceil(sec - (Date.now() - t0) / 1000)} s</b>`, tone); await sleep(250); }
    return buf.filter((x) => x.t >= t0);
  }
  function needs(cond, msg) { if (!cond) { say("Não dá para começar", msg, "bad"); return false; } return true; }
  async function finish(id, title, verdict, lines) {
    const rec = { id, title, ts: Date.now(), verdict, lines };
    try { const all = JSON.parse(localStorage.getItem("obdTests") || "[]"); all.unshift(rec); localStorage.setItem("obdTests", JSON.stringify(all.slice(0, 30))); } catch { }
    // no painel do Pi o resultado também vai para o diário de manutenção (entra no contexto da IA)
    fetch("/api/annotations", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ kind: "teste", ts: rec.ts, text: `${title}: ${verdict}. ${lines.join("; ")}`.slice(0, 480) }) }).catch(() => { });
    await buttons([{ t: "Fechar", v: 1, main: true }]); close();
  }

  // ------------------------------------------------------------ 1. caça-vazamento (entrada falsa de ar)
  const POINTS = ["Junta do corpo de borboleta (em volta toda)", "Duto entre o filtro/MAF e a borboleta (emendas e abraçadeiras)", "Mangueira do servofreio", "Mangueira da PCV / respiro do motor",
    "Base do coletor de admissão (junto ao cabeçote)", "Em volta dos bicos injetores", "Outro ponto"];
  async function leakTest() {
    open("Caça-vazamento de ar");
    if (!needs(live?.engine_on, "Ligue o motor e deixe em marcha lenta. O ideal é com o motor quente (ventoinha já ligou uma vez).")) return void buttons([{ t: "Fechar", v: 1 }]).then(close);
    if ((live.values?.speed || 0) > 0) { say("Pare o carro", "Este teste é com o carro parado, em ponto morto e freio de mão puxado.", "bad"); return void buttons([{ t: "Fechar", v: 1 }]).then(close); }
    say("Como funciona", `Você borrifa <b>limpa-carburador</b> (jatos curtos de 1–2 s) em um ponto por vez. Se houver vazamento ali, o motor suga o produto e a leitura muda na hora: o painel apita.<br><br>
      ⚠️ Produto inflamável: motor em marcha lenta, longe do escapamento quente e do alternador, sem cigarro nem faísca por perto.`);
    if (!await buttons([{ t: "Começar", v: 1, main: true }, { t: "Cancelar", v: 0 }])) return close();
    const hits = [], clean = [];
    try {
      for (;;) {
        const base = await collect(8, "Não borrife ainda", "Medindo o motor em repouso");
        const b = { stft: mean(col(base, "stft1")), sst: sd(col(base, "stft1")), rpm: mean(col(base, "rpm")), srpm: sd(col(base, "rpm")), lam: mean(col(base, "o2c1_lambda").filter((x) => x < 1.9)) };
        if (b.rpm == null) { say("Sem leitura", "Não recebi dados do motor. Confira se o painel está ao vivo.", "bad"); break; }
        say("Onde você vai borrifar?", `Repouso: ${nf(b.rpm)} rpm · ajuste ${b.stft == null ? "—" : (b.stft > 0 ? "+" : "") + nf(b.stft, 1) + " %"}${b.lam ? " · λ " + nf(b.lam, 2) : ""}`);
        const p = await buttons(POINTS.map((t) => ({ t, v: t })).concat([{ t: "✅ Terminar o teste", v: null, main: true }]));
        if (!p) break;
        beep(660, 120);
        const t0 = Date.now(); let hit = null;
        while (Date.now() - t0 < 12000) {
          if (cancelled) throw new Error("cancelado");
          const w = buf.filter((x) => x.t >= t0);
          const st = col(w, "stft1"), rp = col(w, "rpm"), la = col(w, "o2c1_lambda").filter((x) => x < 1.9);
          const dS = b.stft != null && st.length ? b.stft - Math.min(...st) : 0;                // ajuste cai: o motor recebeu "combustível" extra
          const dR = rp.length ? Math.max(...rp.map((x) => Math.abs(x - b.rpm))) : 0;           // rotação sobe ou engasga
          const dL = b.lam != null && la.length ? b.lam - Math.min(...la) : 0;                  // sonda vê mistura mais rica
          const strong = [dS > Math.max(6, 4 * b.sst), dR > Math.max(70, 4 * b.srpm), dL > 0.06].filter(Boolean).length;
          if (strong >= 1 && !hit) { hit = { dS, dR, dL, strong }; beep(1200, 220, 2); }
          if (hit) { hit = { dS: Math.max(hit.dS, dS), dR: Math.max(hit.dR, dR), dL: Math.max(hit.dL, dL), strong: Math.max(hit.strong, strong) }; }
          say(hit ? "🎯 REAGIU!" : "Borrife agora", `${esc(p)}<br>${hit ? "O motor sentiu o produto neste ponto." : "Jatos curtos, por 2–3 s. Observando o motor"} · <b>${Math.ceil(12 - (Date.now() - t0) / 1000)} s</b>`, hit ? "bad" : "go");
          await sleep(250);
        }
        if (hit) { const why = [hit.dS > 6 ? `ajuste caiu ${nf(hit.dS, 0)} pontos` : "", hit.dR > 70 ? `rotação variou ${nf(hit.dR)} rpm` : "", hit.dL > 0.06 ? `sonda enriqueceu ${nf(hit.dL, 2)}` : ""].filter(Boolean).join(", ");
          hits.push(`${p} (${why})`); log(false, `<b>${esc(p)}</b>: reagiu (${why})${hit.strong < 2 ? ". Repita neste ponto para confirmar." : ""}`); }
        else { clean.push(p); log(true, `${esc(p)}: sem reação`); }
        await collect(6, "Espere", "Deixando o motor voltar ao normal");
      }
    } catch { return; }
    const verdict = hits.length ? `vazamento provável em ${hits.length} ponto(s)` : clean.length ? "nenhum ponto reagiu" : "teste interrompido";
    say(hits.length ? "🎯 Achou!" : "Nenhum ponto reagiu", hits.length ? `O ar falso entra por: <b>${hits.map(esc).join("; ")}</b>.<br>Corrija a vedação desse ponto, faça o reaprendizado da marcha lenta e rode 30–50 km.`
      : `Testados: ${clean.length} ponto(s). Se a mistura continua pobre parado, peça um <b>teste de fumaça</b> na oficina: ele acha vazamentos pequenos que o spray não alcança.`, hits.length ? "bad" : "go");
    await finish("leak", "Caça-vazamento", verdict, hits.length ? hits : clean.map((c) => c + ": sem reação"));
  }

  // ------------------------------------------------------------ 2. varredura do pedal e da borboleta (motor desligado)
  function fit(xs, ys) { const n = xs.length, mx = mean(xs), my = mean(ys); const sxx = xs.reduce((s, x) => s + (x - mx) ** 2, 0); if (n < 5 || sxx < 1) return null;
    const a = xs.reduce((s, x, i) => s + (x - mx) * (ys[i] - my), 0) / sxx, b = my - a * mx; return { a, b, res: Math.max(...xs.map((x, i) => Math.abs(ys[i] - (a * x + b)))) }; }
  async function sweepTest() {
    open("Varredura do pedal e da borboleta");
    if (!needs(live && live.state === "coletando" && !live.engine_on, "Deixe a <b>ignição ligada com o motor desligado</b> (chave no II, sem dar partida). Assim dá para pisar até o fundo sem acelerar o motor.")) return void buttons([{ t: "Fechar", v: 1 }]).then(close);
    say("Como funciona", "Você pisa no acelerador <b>bem devagar</b> até o fundo e solta devagar. O painel confere se os dois sensores do pedal andam juntos, se há ponto morto ou salto no sinal e se a borboleta acompanha.");
    if (!await buttons([{ t: "Começar", v: 1, main: true }, { t: "Cancelar", v: 0 }])) return close();
    let s;
    try {
      await collect(3, "Pé fora do pedal", "Medindo o repouso");
      beep(660, 120);
      s = await collect(10, "Pise devagar até o fundo", "Leve uns 8 segundos para chegar no fundo", "go");
      beep(880, 120);
      s = s.concat(await collect(3, "Segure no fundo", "Mantendo"));
      beep(660, 120);
      s = s.concat(await collect(8, "Solte devagar", "Leve uns 6 segundos para soltar tudo", "go"));
    } catch { return; }
    const ok = s.filter((x) => x.v.pedal_d != null);
    const pd = ok.map((x) => x.v.pedal_d), lines = []; let bad = 0;
    const range = pd.length ? Math.max(...pd) - Math.min(...pd) : 0;
    if (range < 40) { log(null, `O pedal só variou ${nf(range)} pontos: não deu para avaliar. Repita pisando até o fundo.`); say("Teste incompleto", "Repita pisando devagar até o fundo.", "bad"); return void finish("sweep", "Varredura do pedal", "incompleto", [`pedal variou só ${nf(range)} pontos`]); }
    log(true, `Curso do pedal: ${nf(Math.min(...pd), 1)} % solto → ${nf(Math.max(...pd), 1)} % no fundo`); lines.push(`curso ${nf(Math.min(...pd), 0)}–${nf(Math.max(...pd), 0)} %`);
    const both = ok.filter((x) => x.v.pedal_e != null), f = fit(both.map((x) => x.v.pedal_d), both.map((x) => x.v.pedal_e));
    if (f) { const good = f.res < 5; if (!good) bad++; log(good, `Sensores D e E do pedal ${good ? "andam juntos" : "<b>se separaram</b>"} (maior diferença: ${nf(f.res, 1)} pontos${good ? "" : "; bom é menos de 5"})`); lines.push(`pedal D×E desvio ${nf(f.res, 1)}`); }
    else log(null, "Sem leitura do segundo sensor do pedal (E).");
    let jumps = 0; for (let i = 2; i < pd.length; i++) if (Math.abs(pd[i - 1] - pd[i - 2]) > 25 && Math.abs(pd[i] - pd[i - 2]) < 6) jumps++;
    if (jumps) bad++; log(!jumps, jumps ? `<b>${jumps} salto(s)</b> no sinal do pedal (vai e volta numa leitura): indica mau contato ou pista gasta` : "Sinal do pedal sem saltos"); lines.push(`${jumps} salto(s)`);
    const tp = ok.map((x) => x.v.tps).filter((x) => x != null), trange = tp.length ? Math.max(...tp) - Math.min(...tp) : 0;
    if (trange > 15) {
      log(true, `A borboleta acompanhou o pedal: ${nf(Math.min(...tp), 1)} % fechada → ${nf(Math.max(...tp), 1)} % aberta`); lines.push(`borboleta ${nf(Math.min(...tp), 0)}–${nf(Math.max(...tp), 0)} %`);
      const ab = ok.filter((x) => x.v.tps != null && x.v.tps_b != null), g = fit(ab.map((x) => x.v.tps), ab.map((x) => x.v.tps_b));
      if (g) { const good = g.res < 6; if (!good) bad++; log(good, `Sensores A e B da borboleta ${good ? "concordam" : "<b>discordaram</b>"} em todo o curso (maior diferença: ${nf(g.res, 1)} pontos)`); lines.push(`borboleta A×B desvio ${nf(g.res, 1)}`); }
    } else log(null, "Com o motor desligado a central não abriu a borboleta neste teste (variou só " + nf(trange, 1) + " pontos). Em muitos carros isso é normal; o resultado vale para o pedal.");
    const verdict = bad ? `${bad} problema(s) encontrado(s)` : "tudo certo";
    say(bad ? "Encontrei problema" : "✅ Tudo certo", bad ? "Veja os itens em vermelho abaixo. Costuma ser conector com mau contato: desligue a chave, desencaixe, limpe com limpa-contato e reencaixe. Depois repita o teste." : "Pedal e sensores responderam como esperado em todo o curso.", bad ? "bad" : "go");
    await finish("sweep", "Varredura do pedal", verdict, lines);
  }

  // ------------------------------------------------------------ 3. marcha lenta com carga elétrica (alternador + compensação)
  async function loadTest() {
    open("Marcha lenta com tudo ligado");
    if (!needs(live?.engine_on && (live.values?.speed || 0) === 0, "Ligue o motor, deixe em marcha lenta com o carro parado e <b>tudo desligado</b> (ar, faróis, desembaçador, som).")) return void buttons([{ t: "Fechar", v: 1 }]).then(close);
    say("Como funciona", "Primeiro o painel mede o motor sem nada ligado. Depois você liga <b>ar-condicionado no máximo, farol alto e desembaçador traseiro</b>. Um carro saudável segura a rotação e a tensão da bateria.");
    if (!await buttons([{ t: "Começar", v: 1, main: true }, { t: "Cancelar", v: 0 }])) return close();
    let a, b, c;
    try {
      a = await collect(12, "Tudo desligado", "Medindo sem carga");
      beep(880, 150);
      b = await collect(25, "Ligue tudo agora", "Ar no máximo + farol alto + desembaçador traseiro", "go");
      beep(660, 150);
      c = await collect(12, "Desligue tudo", "Medindo a volta ao normal");
    } catch { return; }
    const r0 = mean(col(a, "rpm")), late = b.filter((x) => x.t > b[0]?.t + 8000), r1 = mean(col(late, "rpm")), dip = r0 - Math.min(...col(b, "rpm"));
    const volt = (s) => mean(col(s, "ecu_volt").length ? col(s, "ecu_volt") : col(s, "batt"));
    const v0 = volt(a), v1 = volt(late), sdl = sd(col(late, "rpm")), lines = []; let bad = 0;
    if (r0 == null || r1 == null) { say("Sem leitura", "Não recebi dados suficientes do motor.", "bad"); return void buttons([{ t: "Fechar", v: 1 }]).then(close); }
    const hold = r1 > r0 - 80; if (!hold) bad++;
    log(hold, `Rotação: ${nf(r0)} rpm sem carga → ${nf(r1)} rpm com tudo ligado ${hold ? "(a central compensou)" : "<b>(caiu demais: a marcha lenta não está compensando)</b>"}`); lines.push(`rpm ${nf(r0)}→${nf(r1)}`);
    const dipOk = dip < 200; if (!dipOk) bad++;
    log(dipOk, `Queda na hora de ligar: ${nf(dip)} rpm ${dipOk ? "" : "<b>(quase morreu)</b>"}`); lines.push(`queda ${nf(dip)} rpm`);
    const st = sdl < 50; if (!st) bad++;
    log(st, `Estabilidade com carga: ±${nf(sdl)} rpm ${st ? "" : "<b>(oscilando)</b>"}`);
    if (v1 != null) { const vok = v1 >= 13.0; if (!vok) bad++;
      log(vok, `Tensão: ${nf(v0, 1)} V sem carga → ${nf(v1, 1)} V com tudo ligado ${vok ? "(alternador dá conta)" : "<b>(baixa: alternador, correia ou bateria fracos)</b>"}`); lines.push(`tensão ${nf(v0, 1)}→${nf(v1, 1)} V`); }
    else log(null, "Sem leitura de tensão durante o teste.");
    const verdict = bad ? `${bad} ponto(s) de atenção` : "tudo certo";
    say(bad ? "Pontos de atenção" : "✅ Tudo certo", bad ? "Veja os itens em vermelho. Rotação que cai ou oscila com carga costuma vir de mistura pobre, corpo de borboleta sujo ou falta do reaprendizado da marcha lenta; tensão baixa vem de alternador, correia ou bateria."
      : "O motor segurou a rotação e o alternador manteve a tensão com tudo ligado.", bad ? "bad" : "go");
    await finish("load", "Marcha lenta com carga", verdict, lines);
  }

  // ------------------------------------------------------------ lista e histórico
  const TESTS = [
    { id: "leak", f: leakTest, ic: "🎯", t: "Caça-vazamento de ar", d: "Borrife limpa-carburador em cada ponto: o painel apita quando o motor reage. Acha entrada falsa de ar.", need: "Motor ligado, quente, parado" },
    { id: "sweep", f: sweepTest, ic: "🦶", t: "Varredura do pedal e da borboleta", d: "Pise devagar até o fundo: confere os sensores em dupla, saltos de sinal e pontos mortos.", need: "Ignição ligada, motor desligado" },
    { id: "load", f: loadTest, ic: "💡", t: "Marcha lenta com tudo ligado", d: "Liga ar, farol alto e desembaçador: confere se a rotação e o alternador seguram.", need: "Motor ligado, parado" },
  ];
  function renderHistory() {
    const box = $("#tstHist"); if (!box) return;
    let all = []; try { all = JSON.parse(localStorage.getItem("obdTests") || "[]"); } catch { }
    box.innerHTML = all.length ? all.slice(0, 10).map((r) => `<div class="alert" style="--s:${/certo|nenhum ponto/.test(r.verdict) ? "var(--ok)" : /incompleto|interrompido/.test(r.verdict) ? "var(--muted)" : "var(--bad)"}"><span class="ic">${/certo|nenhum ponto/.test(r.verdict) ? "✓" : "!"}</span>
      <div><div class="t">${esc(r.title)}: ${esc(r.verdict)}</div><div class="d">${new Date(r.ts).toLocaleString("pt-BR", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" })} · ${esc((r.lines || []).join(" · "))}</div></div><span></span></div>`).join("")
      : `<div class="empty">Nenhum teste feito neste aparelho ainda.</div>`;
  }
  window.obdTests = { render() {
    $("#tstList").innerHTML = TESTS.map((t) => `<button type="button" class="tcard" data-t="${t.id}"><span class="tic">${t.ic}</span><span><b>${t.t}</b><span class="d">${t.d}</span><span class="need">${t.need}</span></span><span class="go">Iniciar ›</span></button>`).join("");
    renderHistory();
  } };
  document.addEventListener("click", (e) => {
    const b = e.target.closest(".tcard[data-t]"); if (b) { TESTS.find((t) => t.id === b.dataset.t).f(); return; }
    if (e.target.closest("#tstClose")) close();
  });
})();
