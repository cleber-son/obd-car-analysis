// Diagnóstico geral: as regras do veredito (src/obdpi/web/static/checkup.js, função pura avaliar) e a limpeza do
// relatório que o painel público põe na fila para o Pi (cleanCheckup). Sem tela, sem rede, determinístico.
import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { cleanCheckup } from "../src/index.js";
const { avaliar, texto, html, Q, F, TNAME } = createRequire(import.meta.url)("../../src/obdpi/web/static/checkup.js");

const NOW = 1_795_000_000_000;
const okAns = () => Object.fromEntries(Q.map((q) => [q.id, q.multi ? [q.o[0][0]] : q.o[0][0]]));
const okM = () => ({ vRest: 12.6, vCrank: 10.4, vCharge: 14.2, vLoad0: 14.2, vLoad: 13.8, loadRpm0: 750, loadRpm1: 760, idleRpm: 750, idleSd: 12, idleDips: 0, idleStft: 2, idleLam: 1.0, idleEct: 88,
  hiStft: 1, hiLam: 1.0, o2Mean: 0.7, o2Range: 0.12, retLow: 720, tpsAB: 1.2, pedalDE: 0.8, ectMax: 99, fanOn: 97, fanLow: 92, fanDone: true, warmRate: 5.5, warmFrom: 30, warmTo: 80, dtc: [], pend: [], mil: false, readyOk: 8, readyTotal: 8 });
const run = (m = {}, ans = {}, hist = {}, skip = []) => avaliar({ ts: NOW, ans: { ...okAns(), ...ans }, m: { ...okM(), ...m }, skip }, hist, NOW);
const ids = (r) => r.achados.map((a) => a.id);
const get = (r, id) => r.achados.find((a) => a.id === id);

test("catálogo coerente: toda opção aponta para um achado que existe, a 1ª opção é sempre a sem problema", () => {
  for (const q of Q) {
    assert.ok(!q.o[0][2], `${q.id}: a primeira opção não pode gerar achado`);
    for (const o of q.o) if (o[2]) assert.ok(F[o[2]], `${q.id}: achado ${o[2]} não existe`);
  }
  for (const [k, f] of Object.entries(F)) {
    assert.ok(f[1] >= 1 && f[1] <= 3 && "AMB".includes(f[2]) && ["trocar", "verificar", "fazer"].includes(f[3]) && f[4] && f[5] && f[6], k);
    if (f[7]) assert.ok(TNAME[f[7]], `${k}: teste guiado ${f[7]} desconhecido`);
  }
  assert.equal(new Set(Q.map((q) => q.id)).size, Q.length);
});

test("carro saudável: nenhum achado, nível 0, pontos positivos com os números medidos", () => {
  const r = run();
  assert.deepEqual(ids(r), []);
  assert.equal(r.nivel, 0);
  assert.equal(r.parcial, false);
  assert.match(r.rotulo, /Tudo certo/);
  for (const re of [/Bateria: 12,6 V/, /Alternador: 14,2 V/, /Mistura: ajuste \+2 %/, /Marcha lenta firme: 750 rpm/, /ventoinha ligou em 97 °C/, /Catalisador/, /nenhum código/, /Freios: nada de errado/])
    assert.ok(r.bons.some((b) => re.test(b)), String(re));
});

test("bateria fraca: repouso e partida baixos viram um achado só; com idade e partida lenta vira provável troca", () => {
  let r = run({ vRest: 12.1, vCrank: 9.2 });
  assert.deepEqual(ids(r), ["bateria"]);
  assert.equal(get(r, "bateria").acao, "verificar");
  assert.equal(get(r, "bateria").ev.length, 2);
  assert.equal(r.nivel, 1);
  r = run({ vRest: 12.1, vCrank: 9.2 }, { bat_idade: "velha", partida: "lenta" });
  const b = get(r, "bateria");
  assert.equal(b.acao, "trocar"); assert.equal(b.conf, "alta"); assert.equal(b.sev, 2);
  assert.match(b.t, /provável troca/);
  // terminal com zinabre: não manda trocar antes de limpar
  r = run({ vRest: 12.1, vCrank: 8.8 }, { bat_idade: "velha", partida: "lenta", bat_vis: "zinabre" });
  assert.equal(get(r, "bateria").acao, "verificar");
  assert.ok(get(r, "bateria").ev.some((e) => /resolva isso primeiro/.test(e)));
});

test("alternador: não carrega (pare), carrega demais, e fraco só com carga", () => {
  assert.equal(get(run({ vCharge: 12.5, vLoad: 12.1 }), "alternador").sev, 3);
  assert.match(get(run({ vCharge: 15.4 }), "alternador").t, /demais/);
  const r = run({ vCharge: 14.0, vLoad: 12.8 }, { correia: "brilho" });
  assert.equal(get(r, "alternador").sev, 1);
  assert.ok(get(r, "alternador").ev.some((e) => /correia/.test(e)));
});

test("mistura pobre: a assinatura separa entrada de ar, falta de combustível e MAF", () => {
  let r = run({ idleStft: 28, hiStft: 6, idleLam: 1.06 });
  assert.ok(get(r, "ar_falso")); assert.equal(get(r, "ar_falso").teste, "leak"); assert.equal(get(r, "ar_falso").sev, 2);
  r = run({ idleStft: 4, hiStft: 18 }); assert.ok(get(r, "falta_comb"));
  r = run({ idleStft: 18, hiStft: 17 }); assert.ok(get(r, "maf"));
  r = run({ idleStft: 18, hiStft: undefined }); assert.equal(get(r, "pobre").conf, "baixa");
  r = run({ idleStft: -18, hiStft: -15 }, { fumaca: "preta" }); assert.equal(get(r, "rica").conf, "alta");
  // código P0171 confirma a hipótese
  r = run({ idleStft: 28, hiStft: 6, dtc: ["P0171"] });
  assert.equal(get(r, "ar_falso").conf, "alta");
  assert.ok(get(r, "dtc_P0171"));
  // só rodando: parado bom, na rua pobre
  r = run({ drv: { km: 4, min: 9, stft: 16, ectMax: 95, ectEnd: 92, vMin: 13.9 } });
  assert.match(get(r, "falta_comb").t, /rodando/);
});

test("motor frio: a mistura e a marcha lenta não são julgadas", () => {
  const r = run({ idleStft: 30, idleEct: 45, idleSd: 90, hiStft: undefined });
  assert.ok(!ids(r).some((i) => ["pobre", "ar_falso", "lenta"].includes(i)));
  assert.ok(r.pulados.some((p) => /70 °C/.test(p)));
});

test("falha de queima: quedas de rotação com mistura normal; com mistura ruim, a causa é a mistura", () => {
  let r = run({ idleSd: 55, idleDips: 5 }, { lenta_rel: "oscila", vibra: "parado" });
  assert.match(get(r, "lenta").t, /falha de queima/);
  assert.equal(get(r, "lenta").sev, 2);
  assert.ok(get(r, "lenta").ev.some((e) => /não o coxim/.test(e)));
  r = run({ idleSd: 55, idleDips: 5, idleStft: 30, hiStft: 5 });
  assert.match(get(r, "lenta").pq, /mistura/);
  r = run({ idleSd: 20, dtc: ["P0302"], mil: true });
  assert.equal(get(r, "dtc_P0302").sev, 3); assert.equal(r.nivel, 3);
  // rotação firme + carro treme parado: aponta o coxim
  r = run({}, { vibra: "parado" });
  assert.ok(get(r, "coxim").ev.some((e) => /coxins/.test(e)));
});

test("superaquecimento manda parar; ventoinha tarde e termostato são graduais", () => {
  let r = run({ ectMax: 108, fanOn: undefined, fanDone: undefined });
  assert.equal(get(r, "superaquece").sev, 3); assert.match(r.rotulo, /Pare/);
  r = run({ fanOn: 104, ectMax: 104 }); assert.match(get(r, "ventoinha").t, /tarde/);
  r = run({ warmRate: 1.8 }); assert.equal(get(r, "termostato").conf, "baixa");
  r = run({ warmRate: 1.8, dtc: ["P0128"] }); assert.equal(get(r, "termostato").conf, "alta"); assert.equal(get(r, "termostato").acao, "trocar");
});

test("junta do cabeçote só aparece com sinais que se confirmam", () => {
  assert.ok(!get(run({}, { agua: "baixo" }), "junta"));
  assert.ok(!get(run({}, { fumaca: "branca" }), "junta"));
  const r = run({}, { oleo_asp: "leitoso", agua: "baixo", fumaca: "branca" });
  assert.equal(get(r, "junta").conf, "alta"); assert.equal(r.achados[0].sev, 3);
  assert.match(get(r, "junta").t, /suspeita/);
});

test("freio: pedal mole com fluido baixo é pare, com evidência cruzada", () => {
  const r = run({}, { freio: "esponja", freio_fluido: "baixo", luz_painel: ["freio"] });
  assert.equal(get(r, "freio_esponjoso").conf, "alta");
  assert.equal(get(r, "fluido_baixo").sev, 3);
  assert.equal(r.nivel, 3);
});

test("ordem: mais urgente primeiro, depois a maior confiança; cada achado traz evidência e o que fazer", () => {
  const r = run({ vRest: 12.1, idleStft: 28, hiStft: 6, ectMax: 108 }, { pneu_sulco: "careca", ar: "fraco", barulho: ["tec", "ronco"] });
  for (let i = 1; i < r.achados.length; i++) assert.ok(r.achados[i - 1].sev >= r.achados[i].sev);
  for (const a of r.achados) { assert.ok(a.ev.length, a.id); assert.ok(a.fz, a.id); assert.ok(a.pq, a.id); assert.ok(["Pode rodar", "Agende", "Pare"].includes(a.urg)); }
  assert.ok(get(r, "homocinetica") && get(r, "rolamento"));
  const t = texto(r);
  assert.match(t, /PROVÁVEL TROCA:/); assert.match(t, /VERIFICAR \/ INSPECIONAR:/); assert.match(t, /hipóteses a confirmar/);
  assert.ok(!/<b>/.test(t));
});

test("histórico: confirma o que foi medido, acrescenta o que não foi e não contradiz a medição de hoje", () => {
  const sis = (id, status, resumo = "ajuste alto parado") => ({ id, nome: id, status, resumo, fazer: "olhar" });
  // medido ruim hoje + histórico ruim = mais confiança
  let r = run({ idleStft: 18, hiStft: undefined }, {}, { insight: { sistemas: [sis("mistura", "bad")] } });
  assert.equal(get(r, "pobre").conf, "média");
  assert.ok(get(r, "pobre").ev.some((e) => /30 dias/.test(e)));
  // medido bom hoje + histórico ruim = vira nota (conserto recente), não achado
  r = run({}, {}, { insight: { sistemas: [sis("mistura", "bad"), sis("eletrica", "warn")] } });
  assert.deepEqual(ids(r), []);
  assert.equal(r.hist.filter((h) => /medição de hoje saiu normal/.test(h)).length, 2);
  // não medido hoje + histórico ruim = achado de histórico
  r = avaliar({ ts: NOW, ans: okAns(), m: {} }, { insight: { sistemas: [sis("arrefecimento", "bad", "chegou a 108 °C")] }, maint: [{ nome: "Óleo do motor", status: "bad", texto: "Vencido: passou 800 km" }, { nome: "Velas", status: "warn", texto: "faltam 900 km" }],
    alerts: [{ title: "Tensão baixa", severity: "warn", n: 3 }], dtc: { confirmed: [{ code: "P0420", desc: "Catalisador abaixo da eficiência" }], pending: [] },
    tests: [{ id: "mix", title: "Raio-X da mistura", verdict: "1 ponto(s) de atenção", ts: NOW - 864e5 }, { id: "cat", title: "Sondas", verdict: "tudo certo", ts: NOW - 864e5 }] }, NOW);
  assert.equal(get(r, "hist_arrefecimento").sev, 2);
  assert.match(get(r, "manutencao").t, /Óleo do motor/);
  assert.match(get(r, "dtc_P0420").t, /Catalisador abaixo/);
  assert.ok(r.hist.some((h) => /Tensão baixa \(3 vezes\)/.test(h)) && r.hist.some((h) => /Velas/.test(h)) && r.hist.some((h) => /Raio-X/.test(h)));
  assert.ok(!r.hist.some((h) => /Sondas/.test(h)));
  assert.equal(r.parcial, true);
  assert.match(r.resumo, /parcial/);
});

test("etapas e perguntas puladas aparecem como não avaliado; autotestes incompletos viram observação", () => {
  const r = run({ readyOk: 3 }, { oleo_nivel: "-" }, {}, ["Volta curta (pulada)"]);
  assert.ok(r.pulados.includes("Volta curta (pulada)"));
  assert.ok(r.pulados.some((p) => /Nível do óleo/.test(p)));
  assert.ok(r.obs.some((o) => /3 de 8 autotestes/.test(o)));
});

test("motor que não pega: achado crítico e nada de mistura", () => {
  const r = avaliar({ ts: NOW, ans: {}, m: { vRest: 11.6, vCrank: 7.9, noStart: true } }, {}, NOW);
  assert.equal(r.achados[0].id, "nao_pega"); assert.equal(r.nivel, 3);
  assert.match(get(r, "nao_pega").pq, /bateria não tem força/);
});

test("html do relatório: texto de fora nunca vira marcação (só <b> passa)", () => {
  const r = run({}, {}, { dtc: { confirmed: [{ code: "P0420", desc: '<img src=x onerror=alert(1)>' }], pending: [] } });
  const h = html(r);
  assert.ok(!/<img/.test(h)); assert.ok(/&lt;img/.test(h));
  assert.ok(/<b>/.test(html(run({}, { luz_painel: ["inj"] }))));
});

test("nuvem: o relatório público é cortado e tipado antes de ir para a fila", () => {
  assert.equal(cleanCheckup({}), null);
  assert.equal(cleanCheckup({ achados: [], rotulo: "  " }), null);
  const r = run({ vRest: 12.1 });
  const c = cleanCheckup({ ...r, extra: "x", rotulo: r.rotulo + "<script>alert(1)</script>", achados: [...r.achados, { t: "<a href=x>y</a><b>z</b>", sev: 99, ev: ["e".repeat(900)], pq: { a: 1 } }, null, "x"] });
  assert.ok(!("extra" in c) && !("mecanico" in c));
  assert.ok(!/<script/.test(c.rotulo));
  assert.equal(c.achados.length, 2);
  assert.equal(c.achados[1].t, "y<b>z</b>"); assert.equal(c.achados[1].sev, 3); assert.equal(c.achados[1].ev[0].length, 300);
  assert.equal(c.ts, NOW > Date.now() + 864e5 ? c.ts : NOW);
  assert.ok(JSON.stringify(cleanCheckup({ rotulo: "x", achados: Array(500).fill({ t: "a".repeat(5000) }) })).length < 40 * 1200);
});
