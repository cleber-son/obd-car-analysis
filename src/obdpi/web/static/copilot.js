/* window.obdCopilot — o "Copiloto" do modo direção: frases curtas, de vez em quando, com os dados do próprio carro.
   Fala pelo obdVoice como fala "de conversa" ({low: true}): só com a voz livre, nunca por cima do radar (F7), da chegada
   (F8) ou do 0–100, e nunca com a página escondida. Cada linha sai uma vez por viagem (as de marco, uma por marco).
   Calado enquanto o carro manobra: rotação alta, velocidade mudando rápido, pé embaixo, radar à frente, teste de 0–100.
   Intervalo mínimo entre falas pela frequência escolhida (pouco/normal/bastante); avisos de evento (motor quente,
   alerta, ar ligado, parado gastando) esperam só um terço disso; alerta crítico fala mesmo em manobra.
   O trânsito vem só do histórico do próprio carro (F8: /api/gps/traffic e o destino de /api/gps/live, protegidos na
   nuvem): sem eles, só as frases pela velocidade (anda e para, trânsito parado).
   feed(d, ctx) a cada leitura; ctx = {fuel, priv, ac, accOpen, recent, traffic}. Escolhas salvas neste aparelho. */
(() => {
  "use strict";
  const LS = { get(k) { try { return localStorage.getItem(k); } catch { return null; } }, set(k, v) { try { localStorage.setItem(k, v); } catch { } } };
  const FREQ = {
    pouco: { gap: 240, km: 20, fact: 1800, facts: 1, idle: 300 },
    normal: { gap: 90, km: 10, fact: 720, facts: 3, idle: 180 },
    bastante: { gap: 45, km: 5, fact: 360, facts: 5, idle: 120 },
  };
  let on = LS.get("obdCopilot") !== "0";
  let freq = FREQ[LS.get("obdCopilotFreq")] ? LS.get("obdCopilotFreq") : "normal";
  const V = () => window.obdVoice;
  const nf = (v, d = 0) => Number(v).toLocaleString("pt-BR", { minimumFractionDigits: d, maximumFractionDigits: d });
  const ORD = { 1: "primeira", 2: "segunda", 3: "terceira", 4: "quarta", 5: "quinta" };
  // R$ falado: "40 centavos", "6 reais e 40 centavos" (o "R$ 6,40" nem sempre sai bem na voz do iPhone)
  const money = (x) => { const c = Math.round(x * 100); if (c < 100) return `${c} centavo${c === 1 ? "" : "s"}`;
    const r = Math.floor(c / 100), k = c % 100; return `${r} rea${r === 1 ? "l" : "is"}${k ? ` e ${k} centavo${k === 1 ? "" : "s"}` : ""}`; };
  const mins = (s) => { const m = Math.max(1, Math.round(s / 60)); return `${m} minuto${m === 1 ? "" : "s"}`; };
  const price = (f) => f?.fills?.find?.((x) => x.price_l)?.price_l || null;
  const median = (xs) => { const a = xs.filter((x) => x != null && isFinite(x)).sort((p, q) => p - q); return a.length ? a[Math.floor((a.length - 1) / 2)] : null; };
  // consumo de costume do carro: tanques medidos > estimado desde o tanque > mediana das viagens
  const usual = (f) => f?.avg_real_kml ?? f?.since_fill?.kml_est ?? median((f?.trips || []).filter((t) => (t.distance_km || 0) >= 3).map((t) => t.km_l));

  // Curiosidades: só fatos conferidos, sem números inventados. Rodízio entre viagens (salvo neste aparelho).
  const FACTS = [
    "O motor deste Civic é o R18A: um 1.8 de 16 válvulas, com comando i-VTEC.",
    "No R18A, o i-VTEC atrasa o fechamento das válvulas de admissão em carga leve. O motor bombeia menos ar à toa e gasta menos.",
    "Descer engrenado, sem pisar no acelerador, corta a injeção: o consumo vai a zero. Em ponto morto, o motor gasta para manter a lenta.",
    "Com etanol, o carro anda uns 30 por cento menos por litro do que com gasolina, porque o etanol tem menos energia por litro.",
    "A regra prática do flex: o etanol compensa quando custa até 70 por cento do preço da gasolina.",
    "A pressão certa dos pneus está na etiqueta da coluna da porta do motorista. Pneu murcho aumenta o consumo e gasta as bordas.",
    "Velocidade constante economiza. A resistência do ar cresce com o quadrado da velocidade: na estrada, é ela que mais pesa.",
    "Trocar de marcha cedo, entre 2 mil e 2.500 giros, costuma ser o jeito mais econômico de acelerar.",
    "O ajuste curto de combustível é a correção que a injeção faz várias vezes por segundo, guiada pela sonda de oxigênio.",
    "O Civic de oitava geração chegou ao Brasil em 2006, com o painel de dois andares: velocidade em cima, conta-giros embaixo.",
    "Peso extra no porta-malas também custa combustível, principalmente no anda e para da cidade.",
  ];

  let lastSay = 0;
  const log = [];                               // o que foi falado (também para os testes)
  let T = null, prevEl = null, offAt = null, acPrev = null, acCount = 0, spd = [];
  function newTrip(t) {
    return { t0: t, done: new Set(), texts: new Set(), coldSeen: false, nextKm: null, stop0: null, stopL: 0, stopLevel: 0,
      eco0: null, cut0: null, facts: 0, lastFact: t, moved: false, last: null, cmp: null, ecoN: 0, lastEco: 0, longN: 0,
      acSaid: 0, acWant: 0, trafficN: 0, lastTraffic: 0, alerts: new Set(), routeSaid: false };
  }
  function nowMs() { return cp.now(); }

  function say(key, text, o = {}) {
    const vo = V(); if (!vo || !text || T.done.has(key) || T.texts.has(text)) return false;
    const ok = o.urgent ? vo.speak(text, { interrupt: o.crit }) : vo.speak(text, { low: true });
    if (!ok) return false;
    T.done.add(key); T.texts.add(text); lastSay = nowMs(); log.push({ t: lastSay, key, text });
    if (log.length > 200) log.shift();
    return true;
  }

  // ---- trânsito pelo histórico do carro (só com GPS e a localização liberada)
  function bandFor(route, startTs) {
    const kinds = ["domingo", "útil", "útil", "útil", "útil", "útil", "sábado"];
    const day = route.por_dia?.[kinds[new Date(startTs).getDay()]];
    const dt = new Date(startTs), m = dt.getHours() * 60 + dt.getMinutes();
    const b = (day?.faixas || []).find((f) => { const x = /^(\d+)h(\d+)/.exec(f.faixa || ""); if (!x) return false; const a = +x[1] * 60 + +x[2]; return m >= a && m < a + 30; });
    return { day, band: b && b.n >= 2 && b.dur_s ? b : null };
  }
  function trafficLines(d, ctx, tr, el) {
    const dest = ctx.priv?.dest, rep = ctx.traffic;
    if (!dest || (dest.conf ?? 0) < 0.6 || !rep?.routes) return null;
    const route = rep.routes.find((r) => r.status === "ok" && (r.name === dest.route || r.name === dest.name));
    if (!route) return null;
    const startTs = nowMs() - el * 1000;
    const { day, band } = bandFor(route, startTs);
    const typ = band?.dur_s ?? route.dur_s;
    // "Neste horário esse trajeto costuma levar 25 minutos; hoje está 4 minutos mais lento" (uma vez, já com 1 km rodado)
    if (typ && dest.eta_ts && (tr.km || 0) >= 1 && !T.done.has("tr:eta")) {
      const total = el + Math.max(0, (dest.eta_ts - nowMs()) / 1000), delta = total - typ;
      const head = `${band ? "Neste horário, esse" : "Esse"} trajeto até ${dest.name} costuma levar ${mins(typ)}`;
      const tail = Math.abs(delta) < 180 ? "; hoje está no ritmo de sempre." : `; hoje está ${mins(Math.abs(delta))} mais ${delta > 0 ? "lento" : "rápido"}.`;
      return ["tr:eta", head + tail, "reg"];
    }
    // "Saindo às 7h10 em vez de 7h40 você costuma ganhar 8 minutos": só no começo, quando hoje saiu no horário de costume
    const best = day?.melhor || route.melhor;
    if (best?.ganha && el < 900 && !T.done.has("tr:best")) {
      const hm = (s) => { const x = /^(\d+)h(\d+)$/.exec(s || ""); return x ? +x[1] * 60 + +x[2] : null; };
      const h = hm(best.habitual), dt = new Date(startTs), m = dt.getHours() * 60 + dt.getMinutes();
      if (h != null && Math.abs(m - h) <= 20) return ["tr:best", `Saindo às ${best.melhor} em vez de ${best.habitual}, você costuma ganhar ${mins(best.ganho_s)} nesse trajeto.`, "reg"];
    }
    return null;
  }

  // ---- trânsito pela velocidade do carro (funciona sem GPS)
  function speedTraffic(n) {
    const w = spd.filter((p) => p[0] >= n - 300e3);
    if (!w.length || n - w[0][0] < 170e3) return null;
    const w3 = w.filter((p) => p[0] >= n - 180e3), mx3 = Math.max(...w3.map((p) => p[1]));
    const moving3 = w3.filter((p) => p[1] > 1.5).length / w3.length;
    // arrastando: 3 minutos inteiros abaixo de 10 km/h, mas andando boa parte do tempo
    if (mx3 < 10 && moving3 > 0.4) return ["crawl", "Trânsito parado: 3 minutos andando a menos de 10 por hora."];
    if (n - w[0][0] < 290e3) return null;
    let stops = 0, was = w[0][1] > 5;
    for (const [, v] of w) { if (was && v < 1.5) { stops++; was = false; } else if (!was && v > 8) was = true; }
    const avg = w.reduce((a, p) => a + p[1], 0) / w.length, mx = Math.max(...w.map((p) => p[1]));
    if (stops >= 3 && avg < 20 && mx > 12) return ["stopgo", `Anda e para há 5 minutos: média de ${nf(avg)} por hora.`];
    return null;
  }

  function feed(d, ctx = {}) {
    const n = nowMs(), v = d.values || {}, tr = d.trip || {}, g = d.gps || {}, F = FREQ[freq];
    const el = tr.elapsed_s;
    // viagem nova: o tempo ligado voltou, ou o motor religou depois de 2 min desligado
    if (!T || (el != null && prevEl != null && el < prevEl - 30) || (d.engine_on && offAt && n - offAt > 120e3)) {
      T = newTrip(n); spd = [];
    }
    if (el != null) prevEl = el;
    // amostras de velocidade (buraco de mais de 30 s zera a janela: não dá para saber o que houve no meio)
    if (v.speed != null) { if (spd.length && n - spd[spd.length - 1][0] > 30e3) spd = []; spd.push([n, v.speed]); while (spd.length && spd[0][0] < n - 300e3) spd.shift(); }
    if (d.engine_on && tr.km != null) T.last = { ...tr };
    if ((v.speed || 0) > 8) T.moved = true;
    if (ctx.ac != null) { if (acPrev === false && ctx.ac) { acCount++; T.acWant = n; } acPrev = !!ctx.ac; }
    // parado: soma o combustível da parada
    const stopped = d.engine_on && v.speed != null && v.speed < 1.5;
    if (stopped) { if (T.stop0 == null) { T.stop0 = n; T.stopL = 0; T.stopLevel = 0; T.stopTs = n; } else { const dt = (n - T.stopTs) / 1000; if (dt > 0 && dt < 10) T.stopL += (v.fuel_lh || 0) * dt / 3600; T.stopTs = n; } }
    else if ((v.speed || 0) > 3) T.stop0 = null;
    if ((v.ect ?? 99) < 70 && d.engine_on) T.coldSeen = true;

    if (!d.engine_on) offAt = offAt ?? n; else offAt = null;
    const vo = V();
    if (!on || !vo?.enabled?.() || !vo.unlocked?.() || document.hidden) return;

    // ---- condições
    const w6 = spd.filter((p) => p[0] >= n - 6000).map((p) => p[1]);
    const smooth = w6.length < 2 || Math.max(...w6) - Math.min(...w6) < 10;
    const perfRun = g.perf?.state === "correndo" || ["pronto", "correndo"].includes(d.accel?.state);
    const radar = g.state === "ok" && g.warn?.kind === "radar";
    const calm = !!d.engine_on && !ctx.accOpen && !perfRun && !radar && (v.rpm ?? 0) < 3300 && (v.load ?? 0) < 80 && smooth;
    const since = (n - lastSay) / 1000;
    const evOk = calm && since >= F.gap / 3, regOk = calm && since >= F.gap;

    // 1) alerta de verdade (o Pi já decidiu que é alerta): em palavras simples, uma vez por regra e gravidade
    for (const a of (d.alerts || []).slice(0, 3)) {
      if (!a || a.sev === "info" || n - (a.ts_end || a.ts || 0) > 180e3) continue;
      const k = `al:${a.rule || a.title}:${a.sev}`; if (T.alerts.has(k)) continue;
      if (a.sev !== "crit" && !(calm && since >= 10)) continue;
      const det = String(a.detail || "").split(/(?<=[.!?])\s/)[0].slice(0, 150);
      if (say(k, `${a.sev === "crit" ? "Atenção" : "Aviso"}: ${a.title}.${det ? " " + det : ""}`, { urgent: true, crit: a.sev === "crit" })) { T.alerts.add(k); return; }
    }
    // 2) fim da viagem (motor desligou depois de rodar)
    if (!d.engine_on && T.moved && T.last && (T.last.km || 0) >= 1 && offAt && n - offAt > 3000 && !T.done.has("end")) {
      const t = T.last, p = price(ctx.fuel), cost = t.cost ?? (p && t.fuel_l ? t.fuel_l * p : null);
      say("end", `Fim da viagem: ${nf(t.km, t.km < 10 ? 1 : 0)} quilômetros${t.kml ? `, média de ${nf(t.kml, 1)} quilômetros por litro` : ""}${cost ? `, ${money(cost)} de combustível` : ""}.`);
      return;
    }
    if (!d.engine_on) return;

    // 3) saudação no começo da viagem (com o motor e a bateria)
    if (!T.done.has("greet") && (el ?? 0) < 600 && v.ect != null && calm && since >= 5) {
      const h = new Date(n).getHours(), hi = h >= 5 && h < 12 ? "Bom dia" : h >= 12 && h < 18 ? "Boa tarde" : "Boa noite";
      const eng = v.ect < 60 ? `Motor frio, ${nf(v.ect)} graus: vá com calma nos primeiros minutos.` : v.ect < 75 ? `Motor esquentando, ${nf(v.ect)} graus.` : "Motor já está quente.";
      let bat = "";
      if (v.batt != null && (v.rpm ?? 0) > 500) { bat = v.batt >= 13.4 ? ` Bateria carregando bem: ${nf(v.batt, 1)} volts.` : v.batt < 13 ? ` Bateria carregando pouco: ${nf(v.batt, 1)} volts. Fique de olho.` : ""; if (bat) T.done.add("batt"); }
      if (say("greet", `${hi}! ${eng}${bat}`)) return;
    }
    if (!T.done.has("batt") && T.done.has("greet") && (el ?? 0) < 900 && v.batt != null && (v.rpm ?? 0) > 500 && evOk) {
      if (v.batt >= 13.4) { if (say("batt", `Bateria carregando bem: ${nf(v.batt, 1)} volts.`)) return; }
      else if (v.batt < 13 && say("batt", `Bateria carregando pouco: ${nf(v.batt, 1)} volts. Fique de olho.`)) return;
    }
    // 4) motor chegou à temperatura (só se foi visto frio nesta viagem)
    if (T.coldSeen && (v.ect ?? 0) >= 80 && evOk && say("warm", `Motor quente, ${nf(v.ect)} graus: pode acelerar à vontade.`)) return;
    // 5) ar-condicionado ligado (toque no ❄️): uma vez a cada 10 min, até 1 min depois do toque
    if (T.acWant && n - T.acWant < 60e3 && n - T.acSaid > 600e3 && calm && since >= 10
      && say("ac:" + acCount, "Ar ligado: o consumo costuma subir uns 10 por cento, mais no anda e para.")) { T.acSaid = n; T.acWant = 0; return; }
    // 6) autonomia baixa sem GPS (com GPS quem fala é o F8, a menos de 60 km)
    if (g.range_km == null && ctx.fuel?.fills?.length && evOk) {
      const f = ctx.fuel, rest = Math.max(0, f.tank_l - (f.since_fill?.liters_est || 0) - (tr.fuel_l || 0)), k = f.avg_real_kml ?? tr.kml ?? f.since_fill?.kml_est;
      const rk = k ? rest * k : null;
      if (rk != null && rk < 60 && say("range", `Autonomia de uns ${Math.max(10, Math.round(rk / 10) * 10)} quilômetros: hora de pensar no posto.`)) return;
    }
    // 7) parado com o motor ligado (depois de já ter andado): quanto custou
    if (stopped && T.moved && T.stop0 != null) {
      const s = (n - T.stop0) / 1000, lvl = s >= 600 ? 2 : s >= F.idle ? 1 : 0;
      if (lvl > T.stopLevel && evOk && T.stopL > 0.005) {
        const p = price(ctx.fuel);
        const what = p ? `isso custou ${money(T.stopL * p)}` : `gastou ${nf(T.stopL, 2)} litro de combustível`;
        if (say(`idle:${T.stop0}:${lvl}`, `Parado há ${mins(s)} com o motor ligado: ${what}.`)) { T.stopLevel = lvl; return; }
      }
    }
    // 8) duas horas rodando: pausa
    if ((tr.moving_s || 0) >= (T.longN + 1) * 7200 && evOk && say("long:" + (T.longN + 1), `${T.longN ? "Mais duas horas" : "Duas horas"} ao volante: que tal uma parada para esticar as pernas?`)) { T.longN++; return; }
    // 9) trânsito pela velocidade (sem GPS também), no máximo a cada 15 min
    if (n - T.lastTraffic > 900e3 && evOk) {
      const st = speedTraffic(n);
      if (st && say(st[0] + ":" + T.trafficN, st[1])) { T.trafficN++; T.lastTraffic = n; return; }
    }
    if (!regOk) return;
    // 10) trânsito do trajeto pelo histórico (F8)
    const tl = el != null ? trafficLines(d, ctx, tr, el) : null;
    if (tl && say(tl[0], tl[1])) return;
    // 11) marcos de distância: "10 quilômetros. Média de 11,2 km/L, 6 reais e 40 centavos até agora."
    const N = F.km, km = tr.km || 0;
    if (T.nextKm == null) T.nextKm = Math.max(N, Math.ceil((km + 0.05) / N) * N);
    if (km >= T.nextKm + 2) T.nextKm = Math.ceil((km + 0.05) / N) * N;      // passou muito: espera o próximo
    const us = usual(ctx.fuel);
    if (km >= T.nextKm && tr.kml) {
      const p = price(ctx.fuel), cost = tr.cost ?? (p && tr.fuel_l ? tr.fuel_l * p : null);
      let cmp = ""; if (us && (tr.fuel_l || 0) > 0.3) { const r = tr.kml / us - 1, c = r > 0.08 ? "+" : r < -0.08 ? "-" : "=";
        if (c !== T.cmp) { cmp = c === "=" ? " Na média do carro." : ` ${nf(Math.abs(r) * 100)} por cento ${c === "+" ? "melhor" : "pior"} que o normal do carro, que é ${nf(us, 1)}.`; T.cmp = c; } }
      if (say("km:" + T.nextKm, `${nf(T.nextKm)} quilômetros. Média de ${nf(tr.kml, 1)} quilômetros por litro${cost ? `, ${money(cost)} até agora` : ""}.${cmp}`)) { T.nextKm += N; return; }
    }
    // 12) comparação com o consumo de costume (uma vez, depois de 6 km, se nenhum marco já disse)
    if (!T.cmp && us && km >= 6 && tr.kml && (tr.fuel_l || 0) > 0.3) {
      const r = tr.kml / us - 1;
      const txt = Math.abs(r) <= 0.08 ? `Consumo de ${nf(tr.kml, 1)} quilômetros por litro: bem na média do carro, que é ${nf(us, 1)}.`
        : `Consumo de ${nf(tr.kml, 1)} quilômetros por litro: ${nf(Math.abs(r) * 100)} por cento ${r > 0 ? "melhor" : "pior"} que o normal do carro, que é ${nf(us, 1)}.`;
      if (say("cmp", txt)) { T.cmp = Math.abs(r) <= 0.08 ? "=" : r > 0 ? "+" : "-"; return; }
    }
    // 13) melhor média em muitas viagens
    const past = (ctx.fuel?.trips || []).filter((t) => (t.distance_km || 0) >= 8 && t.km_l);
    if (km >= 10 && (tr.fuel_l || 0) > 0.5 && tr.kml && past.length >= 5 && tr.kml > Math.max(...past.map((t) => t.km_l))
      && say("best", `Esta é a sua melhor média em ${past.length} viagens: ${nf(tr.kml, 1)} quilômetros por litro.`)) return;
    // 14) bom momento de economia: em quarta ou quinta, giro baixo, média recente bem acima do normal por 20 s
    const rec = ctx.recent;
    if (v.gear >= 4 && (v.rpm ?? 0) >= 1500 && (v.rpm ?? 0) <= 2600 && rec != null && rec >= Math.max(15, (us || 10) * 1.4)) { T.eco0 = T.eco0 ?? n; }
    else T.eco0 = null;
    if (T.eco0 && n - T.eco0 >= 20e3 && T.ecoN < 2 && n - T.lastEco > 1200e3
      && say("eco:" + T.ecoN, `Boa! ${nf(rec)} quilômetros por litro, em ${ORD[v.gear]} a ${nf(Math.round(v.rpm / 100) * 100)} giros: é assim que se economiza.`)) { T.ecoN++; T.lastEco = n; return; }
    // 15) injeção cortada (sonda em "corte", engrenado e andando) por 3 s
    if ((v.o2c1_lambda ?? 0) > 1.9 && v.gear && (v.speed || 0) > 25) T.cut0 = T.cut0 ?? n; else T.cut0 = null;
    if (T.cut0 && n - T.cut0 >= 3000 && say("cut", "Repare: com o pé fora do acelerador e engrenado, a injeção está cortada. Agora o consumo é zero.")) return;
    // 16) curiosidade, rodando tranquilo (30 a 110 km/h) ou parado há 15 s, depois dos 5 primeiros minutos
    const easy = ((v.speed || 0) >= 30 && (v.speed || 0) <= 110) || (stopped && T.stop0 != null && n - T.stop0 >= 15e3);
    if (T.facts < F.facts && n - T.lastFact >= F.fact * 1000 && n - T.t0 >= 300e3 && easy) {
      let k = (+(LS.get("obdCopilotFact") || 0) || 0) % FACTS.length;
      for (let j = 0; j < FACTS.length && T.done.has("fact:" + k); j++) k = (k + 1) % FACTS.length;
      if (say("fact:" + k, FACTS[k])) { LS.set("obdCopilotFact", String(k + 1)); T.facts++; T.lastFact = n; return; }
    }
  }

  function set(v) { on = !!v; LS.set("obdCopilot", on ? "1" : "0"); if (!on) V()?.cancelLow?.(); }
  function setFreq(f) { if (FREQ[f]) { freq = f; LS.set("obdCopilotFreq", f); } }
  const cp = { feed, set, enabled: () => on, freq: () => freq, setFreq, log, now: () => Date.now(), FACTS };
  window.obdCopilot = cp;
})();
