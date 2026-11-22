// Link de compartilhamento da viagem ao vivo (docs/GPS-ARQUITETURA.md, seção 9.21; F9). Roda com `npm test`.
// D1, ASSETS e o Durable Object falsos em memória; nenhuma rede. O relógio do Durable Object é trocado nos testes
// de tempo (vencer, 90 s sem sinal, 15 min sem sinal); o do Worker é o real (as linhas do D1 são ajustadas).
import { test } from "node:test";
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import worker, { LiveHub, makeLocCookie } from "../src/index.js";
import { cleanFrame, parseCreate, linkState, SHARE_TOKEN_RE, CLOSE_GONE, FOREVER_TS } from "../src/share.js";

const sha = (s) => createHash("sha256").update(s).digest("hex");

// ---------------------------------------------------------------- D1 falso (só as consultas que o Worker faz)
function fakeDB({ noOpts = false } = {}) {
  const docs = new Map(), cmds = [], share = new Map();
  const q = { n: 0 };
  const rowOut = (r) => ({ ...r });
  const run = (sql, a) => {
    q.n++;
    if (noOpts && /share/.test(sql) && /opts/.test(sql)) throw new Error("no such column: opts");
    if (/^SELECT json, updated_ts FROM doc WHERE key=\?/.test(sql)) { const d = docs.get(a[0]); return { first: d ? { json: d.json, updated_ts: d.updated_ts } : null }; }
    if (/^SELECT key, json, updated_ts FROM doc WHERE key IN/.test(sql)) return { all: [...docs].filter(([k]) => k === "/api/live" || k === "_meta").map(([key, d]) => ({ key, ...d })) };
    if (/^INSERT INTO doc/.test(sql)) { docs.set(a[0], { json: a[1], updated_ts: a[2] }); return {}; }
    if (/^DELETE FROM doc WHERE key=\?/.test(sql)) { docs.delete(a[0]); return {}; }
    if (/^SELECT COUNT\(\*\) n FROM cmd WHERE done=0$/.test(sql)) return { first: { n: cmds.filter((c) => !c.done).length } };
    if (/^SELECT token_hash, created_ts, expires_ts, label, revoked, views, last_view_ts, opts FROM share WHERE token_hash=\?$/.test(sql)) { const r = share.get(a[0]); return { first: r ? rowOut(r) : null }; }
    if (/^SELECT token_hash, .* FROM share WHERE revoked=0 AND expires_ts>\?$/.test(sql)) return { all: [...share.values()].filter((r) => !r.revoked && r.expires_ts > a[0]).map(rowOut) };
    if (/^SELECT token_hash, .* FROM share WHERE created_ts>\? ORDER BY created_ts DESC$/.test(sql)) return { all: [...share.values()].filter((r) => r.created_ts > a[0]).sort((x, y) => y.created_ts - x.created_ts).map(rowOut) };
    if (/^INSERT OR IGNORE INTO share\(token_hash, created_ts, expires_ts, label, revoked, views, opts\) VALUES \(\?,\?,\?,\?,0,0,\?\)$/.test(sql)) {
      if (!share.has(a[0])) share.set(a[0], { token_hash: a[0], created_ts: a[1], expires_ts: a[2], label: a[3], revoked: 0, views: 0, last_view_ts: null, opts: a[4] });
      return {};
    }
    if (/^DELETE FROM share WHERE expires_ts<\?$/.test(sql)) { for (const [k, r] of share) if (r.expires_ts < a[0]) share.delete(k); return {}; }
    if (/^SELECT COUNT\(\*\) n FROM share WHERE revoked=0 AND expires_ts>\?$/.test(sql)) return { first: { n: [...share.values()].filter((r) => !r.revoked && r.expires_ts > a[0]).length } };
    if (/^SELECT COUNT\(\*\) n FROM share WHERE created_ts>\?$/.test(sql)) return { first: { n: [...share.values()].filter((r) => r.created_ts > a[0]).length } };
    if (/^UPDATE share SET revoked=1 WHERE substr\(token_hash,1,8\)=\? AND revoked=0$/.test(sql)) { for (const r of share.values()) if (r.token_hash.slice(0, 8) === a[0] && !r.revoked) r.revoked = 1; return {}; }
    if (/^UPDATE share SET opts=\? WHERE token_hash=\?$/.test(sql)) { const r = share.get(a[1]); if (r) r.opts = a[0]; return {}; }
    if (/^UPDATE share SET revoked=\? WHERE token_hash=\? AND revoked=0$/.test(sql)) { const r = share.get(a[1]); if (r && !r.revoked) r.revoked = a[0]; return {}; }
    if (/^UPDATE share SET views=views\+1, last_view_ts=\? WHERE token_hash=\?$/.test(sql)) { const r = share.get(a[1]); if (r) { r.views++; r.last_view_ts = a[0]; } return {}; }
    throw new Error("SQL inesperado no teste: " + sql);
  };
  const stmt = (sql, a = []) => ({
    bind: (...b) => stmt(sql, b),
    first: async () => run(sql, a).first ?? null,
    all: async () => ({ results: run(sql, a).all ?? [] }),
    run: async () => (run(sql, a), { success: true }),
    _exec: () => run(sql, a),
  });
  return { docs, cmds, share, q, prepare: (sql) => stmt(sql.trim()), batch: async (list) => { for (const s of list) s._exec(); return []; } };
}

// ---------------------------------------------------------------- Durable Object falso (WebSocket hibernável)
class FakeWS {
  constructor() { this.sent = []; this.closed = false; this.code = null; this.att = null; this.tags = []; }
  send(m) { if (this.closed) throw new Error("fechado"); this.sent.push(m); }
  close(code) { this.closed = true; this.code = code ?? 1000; }
  serializeAttachment(a) { this.att = structuredClone(a); }
  deserializeAttachment() { return this.att; }
  msgs() { return this.sent.map((x) => { try { return JSON.parse(x); } catch { return x; } }); }
}
globalThis.WebSocketPair = class { constructor() { this[0] = new FakeWS(); this[1] = new FakeWS(); this[0].peer = this[1]; } };
LiveHub.respond101 = (client) => { const r = new Response(null, { status: 200, headers: { "x-test-upgrade": "1" } }); r.server = client.peer; return r; };

function fakeCtx() {
  const socks = [], store = new Map();
  return {
    socks, store,
    acceptWebSocket(ws, tags = []) { ws.tags = tags; socks.push(ws); },
    getWebSockets(tag) { return socks.filter((w) => !w.closed && (tag === undefined || w.tags.includes(tag))); },
    storage: { get: async (k) => store.get(k), put: async (k, v) => { store.set(k, structuredClone(v)); }, delete: async (k) => store.delete(k) },
  };
}

function mkEnv(extra = {}, dbOpts = {}) {
  const env = {
    SYNC_TOKEN: "tok-sync", DB: fakeDB(dbOpts), LOC_DELAY_MS: "0",
    ASSETS: { fetch: async (r) => new Response("asset " + new URL(r.url).pathname, { status: 200 }) },
    ...extra,
  };
  const ctx = fakeCtx();
  const hub = new LiveHub(ctx, env);
  env.LIVE = { idFromName: () => "civic", get: () => ({ fetch: (u, init) => hub.fetch(u instanceof Request ? u : new Request(u, init)) }) };
  env._hub = hub; env._ctx = ctx;
  return env;
}
const ORIGIN = "https://obd.exemplo";
const LOCPW = { LOC_PASSWORD: "senha-de-teste-longa" };
const call = (env, path, init = {}) => worker.fetch(new Request(ORIGIN + path, init), env);
const PI = { authorization: "Bearer tok-sync", "content-type": "application/json" };
const locCookie = async (env) => "obd_loc=" + (await makeLocCookie(env));
async function create(env, body, cookie) {
  const r = await call(env, "/api/gps/share", { method: "POST", headers: { cookie: cookie ?? (await locCookie(env)), "content-type": "application/json" }, body: JSON.stringify(body) });
  return { r, j: await r.json() };
}
const tokenOf = (url) => url.split("/s/")[1];
const poll = async (env, tok, qs = "") => { const r = await call(env, `/api/share/${tok}${qs}`); return { r, j: await r.json() }; };
async function wsOpen(env, tok) {
  const r = await call(env, `/api/share/${tok}/ws`, { headers: { Upgrade: "websocket" } });
  return { r, ws: r.server };
}
const NOW = () => Date.now();
const live = (over = {}) => ({ ts: NOW(), state: "coletando", engine_on: true, trip_id: 7, values: { speed: 50, rpm: 2100 }, value_ts: {},
  gps: { state: "ok", sats: 9, speed: 49 }, ...over });
const frame = (over = {}) => ({ ts: NOW(), trip: 7, on: true, lat: -23.55, lon: -46.63, hdg: 90, gspd: 49, fix_ts: NOW(), spd: 50, rpm: 2100,
  ect: 90, ect_st: "ok", kml: 12.5, lh: 4, trip_s: 600, trip_km: 5.2, trip_kml: 11.9, alert: "Temperatura alta", alert_sev: "warn",
  eta: NOW() + 600e3, left_km: 6.1, dest: "Casa", ...over });
// o Pi manda a leitura ao vivo com o quadro do link em "_share" (só quando a nuvem diz que há link ativo)
const pushLive = (env, l = live(), f = frame()) => call(env, "/api/sync/live", { method: "POST", headers: PI, body: JSON.stringify(f ? { ...l, _share: f } : l) });
const syncShare = (env, body) => call(env, "/api/sync/share", { method: "POST", headers: PI, body: JSON.stringify(body) });
const SHARE_KEYS = ["ok", "state", "car", "data", "label", "mode", "expires_ts", "now", "track"];
const DATA_KEYS = ["on", "spd", "rpm", "ect", "ect_st", "kml", "lh", "trip_s", "trip_km", "trip_kml", "alert", "alert_sev", "eta", "left_km", "age_s"];

// ---------------------------------------------------------------- token, hash e criação
test("token de 128 bits aleatórios; só o SHA-256 fica no D1; criar exige a senha de localização", async () => {
  const env = mkEnv(LOCPW);
  const toks = [];
  for (let i = 0; i < 3; i++) {
    const { r, j } = await create(env, { mode: "1h", label: "Para a Ana" });
    assert.equal(r.status, 200);
    assert.match(j.url, /^https:\/\/obd\.exemplo\/s\/[A-Za-z0-9_-]{22}$/);
    const tok = tokenOf(j.url);
    assert.ok(SHARE_TOKEN_RE.test(tok));
    assert.equal(Buffer.from(tok, "base64url").length, 16);
    const row = env.DB.share.get(sha(tok));
    assert.ok(row, "linha guardada pelo hash");
    assert.equal(j.id, sha(tok).slice(0, 8));
    assert.equal(row.expires_ts - row.created_ts, 3600e3);
    toks.push(tok);
  }
  assert.equal(new Set(toks).size, 3);
  const dump = JSON.stringify([...env.DB.share.values()]) + JSON.stringify([...env._ctx.store.values()]);
  for (const t of toks) assert.ok(!dump.includes(t), "o token não fica guardado em lugar nenhum");
  // 4 h, até o fim da viagem (24 h no máximo) e validade fora da faixa
  assert.equal((await create(env, { mode: "4h" })).j.expires_ts - NOW() > 4 * 3600e3 - 5000, true);
  const v = await create(env, { mode: "viagem" });
  const vr = env.DB.share.get(sha(tokenOf(v.j.url)));
  assert.equal(vr.expires_ts - vr.created_ts, 24 * 3600e3);
  assert.equal(JSON.parse(vr.opts).m, "viagem");
  assert.equal((await create(env, { minutes: 2000 })).r.status, 400);
  assert.equal((await create(env, { mode: "nunca" })).r.status, 400);
  // sem o cookie de localização: 401, nada criado
  const n = env.DB.share.size;
  const no = await call(env, "/api/gps/share", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ mode: "1h" }) });
  assert.equal(no.status, 401);
  assert.equal((await call(env, "/api/gps/share")).status, 401);
  assert.equal(env.DB.share.size, n);
  // a lista do dono não traz token nem hash inteiro
  const lst = await (await call(env, "/api/gps/share", { headers: { cookie: await locCookie(env) } })).json();
  assert.equal(lst.links.length, n);
  for (const t of toks) assert.ok(!JSON.stringify(lst).includes(t) && !JSON.stringify(lst).includes(sha(t)));
  // sem LOC_PASSWORD: nada de compartilhamento (nem pelo painel, nem pelo Pi)
  const env2 = mkEnv();
  assert.equal((await create(env2, { mode: "1h" }, "")).r.status, 401);
  assert.equal((await syncShare(env2, { create: [{ hash: sha("x"), created_ts: 1, expires_ts: 3600e3 + 1, mode: "tempo" }] })).status, 409);
  assert.equal(env2.DB.share.size, 0);
  const ping = await (await call(env2, "/api/sync/ping", { headers: PI })).json();
  assert.equal(ping.loc, false); assert.ok(!("share" in ping));
});

test("funções puras: parseCreate, cleanFrame (lista fechada) e linkState", () => {
  assert.deepEqual(parseCreate({ mode: "1h" }), { mode: "tempo", ms: 3600e3, label: "", dest: false });
  assert.deepEqual(parseCreate({ mode: "24h" }), { mode: "tempo", ms: 24 * 3600e3, label: "", dest: false });
  assert.deepEqual(parseCreate({ mode: "sempre" }), { mode: "sempre", ms: null, label: "", dest: false });
  assert.equal(parseCreate({ mode: "viagem", dest: true }).dest, true);
  assert.equal(parseCreate({ mode: "viagem", dest: "true" }).dest, false);
  assert.ok(parseCreate({}).error && parseCreate({ minutes: 1 }).error && parseCreate({ minutes: 1441 }).error);
  assert.equal(parseCreate({ mode: "1h", label: "<script>x</script>" + "a".repeat(80) }).label.includes("<"), false);
  const f = cleanFrame({ ...frame(), place: { name: "Casa" }, parked: { lat: 1, lon: 2 }, route: "Casa → Trabalho", values: { x: 1 }, lat: -23.551234567 });
  assert.equal(f.lat, -23.55123);
  for (const k of ["place", "parked", "route", "values", "dest"]) assert.ok(!(k in f), k);
  assert.equal(cleanFrame(frame(), true).dest, "Casa");
  assert.deepEqual(cleanFrame({ trip: null, lat: 1, lon: 2, spd: 3 }), { ts: null, trip: null });
  assert.equal(cleanFrame({ ...frame(), lat: 999 }).lat, undefined);
  assert.equal(cleanFrame({ ...frame(), rpm: "9e99" }).rpm, null);
  assert.equal(linkState(null).status, 404);
  assert.equal(linkState({ revoked: 1, created_ts: 1, expires_ts: NOW() + 1e6 }).status, 410);
  assert.equal(linkState({ revoked: 2, created_ts: 1, expires_ts: NOW() + 1e6 }).reason, "viagem_terminou");
  assert.equal(linkState({ revoked: 0, created_ts: NOW() - 1000, expires_ts: NOW() - 1 }).reason, "vencido");
  assert.equal(linkState({ revoked: 0, created_ts: NOW(), expires_ts: NOW() + 48 * 3600e3 }).reason, "vencido");   // além de 24 h: não vale
  assert.ok(linkState({ revoked: 0, created_ts: NOW(), expires_ts: NOW() + 3600e3, opts: "{\"m\":\"tempo\"}" }).ok);
});

// ---------------------------------------------------------------- o que quem recebe vê
test("link válido: só a viagem atual (carro, trajeto e dados), nada de lugares, histórico ou destino sem permissão", async () => {
  const env = mkEnv(LOCPW);
  // documentos privados que existem na nuvem: nunca podem aparecer no link
  for (const [k, v] of [["/api/gps/places", { places: [{ name: "Casa", lat: -23.6, lon: -46.7 }] }], ["/api/gps/parked", { lat: -23.611, lon: -46.711, name: "Casa" }],
    ["/api/gps/logbook?limit=1000", { items: [{ id: 3, route: "Casa → Trabalho" }] }], ["/api/gps/live", { lat: -23.55, lon: -46.63, place: { name: "Casa" }, parked: { lat: -23.611 } }]])
    env.DB.docs.set(k, { json: JSON.stringify(v), updated_ts: NOW() });
  const { j } = await create(env, { mode: "1h" });
  const tok = tokenOf(j.url);
  // antes de o Pi mandar a viagem: aguardando, sem posição
  let p = await poll(env, tok);
  assert.equal(p.r.status, 200);
  assert.equal(p.j.state, "desligado"); assert.equal(p.j.car, null); assert.equal(p.j.track, null);
  assert.equal(p.r.headers.get("cache-control"), "no-store");
  const lr = await (await pushLive(env)).json();
  assert.equal(lr.share, true, "a nuvem diz ao Pi que há link ativo");
  await pushLive(env, live(), frame({ lat: -23.5503, lon: -46.6303 }));
  p = await poll(env, tok);
  assert.equal(p.j.state, "ao_vivo");
  assert.deepEqual(Object.keys(p.j).sort(), SHARE_KEYS.slice().sort());
  assert.deepEqual(Object.keys(p.j.data).sort(), DATA_KEYS.slice().sort());
  assert.deepEqual(Object.keys(p.j.car).sort(), ["hdg", "lat", "lon"]);
  assert.equal(p.j.data.rpm, 2100); assert.equal(p.j.data.alert, "Temperatura alta"); assert.ok(p.j.data.eta > NOW());
  assert.equal(p.j.track.pts.length, 2);
  const txt = JSON.stringify(p.j);
  for (const bad of ["Casa", "Trabalho", "-23.611", "-46.711", "-23.6,", "logbook", "places", "parked", "values"]) assert.ok(!txt.includes(bad), bad);
  // o nome do destino só para o link que o dono marcou
  const d = await create(env, { mode: "1h", dest: true });
  await pushLive(env);
  assert.equal((await poll(env, tokenOf(d.j.url))).j.data.dest, "Casa");
  assert.ok(!("dest" in (await poll(env, tok)).j.data));
  // consulta incremental do trajeto (since/v) e versão trocada = tudo de novo
  await pushLive(env, live(), frame({ lat: -23.5520, lon: -46.6320 }));
  const full = (await poll(env, tok)).j.track;
  const inc = (await poll(env, tok, `?since=${full.n - 1}&v=${full.v}`)).j.track;
  assert.equal(inc.from, full.n - 1); assert.equal(inc.pts.length, 1);
  assert.equal((await poll(env, tok, `?since=${full.n - 1}&v=${full.v + 5}`)).j.track.from, 0);
  // conta quem abriu
  assert.ok(env.DB.share.get(sha(tok)).views >= 1);
});

test("trajeto desde a saída: o Pi manda os pontos da viagem aberta quando o link nasce no meio dela", async () => {
  const env = mkEnv(LOCPW);
  const tok = tokenOf((await create(env, { mode: "1h" })).j.url);
  await pushLive(env, live(), frame({ lat: -23.56, lon: -46.64, ts: NOW() }));
  const t0 = NOW() - 600e3;
  const pts = Array.from({ length: 30 }, (_, i) => [-23.50 - i * 0.002, -46.60 - i * 0.001, t0 + i * 10e3]);
  assert.equal((await syncShare(env, { track: { trip: 7, pts } })).status, 200);
  let tr = (await poll(env, tok)).j.track;
  assert.equal(tr.pts.length, 31);                                  // os 30 da viagem + o ponto ao vivo (mais novo)
  assert.deepEqual(tr.pts[0], [-23.5, -46.6]);
  // trajeto de outra viagem é ignorado
  await syncShare(env, { track: { trip: 99, pts: [[1, 1, NOW()]] } });
  tr = (await poll(env, tok)).j.track;
  assert.equal(tr.pts.length, 31); assert.equal(tr.trip, 7);
  // ponto fora da faixa é descartado
  await syncShare(env, { track: { trip: 7, pts: [[999, 1, 1], [-23.4, -46.5, NOW() - 1e6]] } });
  assert.ok((await poll(env, tok)).j.track.pts.every((p) => Math.abs(p[0]) <= 90));
});

// ---------------------------------------------------------------- vencer, encerrar, falha fechada
test("vencido ou encerrado: 410 sem dados, conexão ao vivo fechada (falha fechada)", async () => {
  const env = mkEnv(LOCPW);
  const c = await locCookie(env);
  const a = await create(env, { mode: "1h" }, c), b = await create(env, { mode: "1h" }, c);
  const ta = tokenOf(a.j.url), tb = tokenOf(b.j.url);
  await pushLive(env);
  const { r, ws } = await wsOpen(env, ta);
  assert.equal(r.headers.get("x-test-upgrade"), "1");
  const first = ws.msgs()[0];
  assert.equal(first.t, "s"); assert.equal(first.state, "ao_vivo"); assert.equal(first.car.lat, -23.55);
  await pushLive(env);
  assert.equal(ws.msgs().at(-1).t, "f");
  // o tempo passa além da validade: a próxima leitura fecha a conexão e não envia nada
  const hub = env._hub, real = hub.room.now;
  hub.room.now = () => NOW() + 2 * 3600e3;
  const before = ws.sent.length;
  await pushLive(env);
  assert.equal(ws.closed, true); assert.equal(ws.code, CLOSE_GONE);
  assert.equal(ws.sent.length, before);
  hub.room.now = real;
  // no D1 o link venceu: consulta 410 sem posição; WebSocket recusado
  env.DB.share.get(sha(ta)).expires_ts = NOW() - 1;
  const g = await poll(env, ta);
  assert.equal(g.r.status, 410); assert.equal(g.j.gone, true); assert.equal(g.j.reason, "vencido");
  assert.ok(!JSON.stringify(g.j).includes("-23.5"));
  assert.equal((await wsOpen(env, ta)).r.status, 410);
  // encerrar pelo dono: a conexão do outro link fecha na hora, a consulta passa a 410
  assert.equal(hub.room.frame, null, "sem link valendo, a posição foi esquecida");
  hub.room.linksAt = 0;
  await pushLive(env);
  const wb = (await wsOpen(env, tb)).ws;
  assert.equal(wb.msgs()[0].state, "ao_vivo");
  const rv = await call(env, `/api/gps/share/${b.j.id}`, { method: "POST", headers: { cookie: c, "content-type": "application/json" }, body: JSON.stringify({ revoke: true }) });
  assert.equal(rv.status, 200);
  assert.equal(wb.closed, true); assert.equal(wb.code, CLOSE_GONE);
  const gb = await poll(env, tb);
  assert.equal(gb.r.status, 410); assert.equal(gb.j.reason, "encerrado");
  // sem link ativo: o Durable Object esquece a posição e o trajeto e diz ao Pi para parar de mandar
  const lr = await (await pushLive(env)).json();
  assert.equal(lr.share, false);
  assert.equal(hub.room.frame, null); assert.equal(env._ctx.store.has("share"), false);
  // encerrar pede o cookie de localização
  assert.equal((await call(env, `/api/gps/share/${b.j.id}`, { method: "POST", body: JSON.stringify({ revoke: true }) })).status, 401);
});

test("90 s sem sinal do carro: nenhuma posição nem trajeto (não mostra onde ele parou)", async () => {
  const env = mkEnv(LOCPW);
  const tok = tokenOf((await create(env, { mode: "4h" })).j.url);
  await pushLive(env);
  assert.equal((await poll(env, tok)).j.state, "ao_vivo");
  env._hub.room.now = () => NOW() + 91e3;
  const p = (await poll(env, tok)).j;
  assert.equal(p.state, "sem_sinal"); assert.equal(p.car, null); assert.equal(p.data, null); assert.equal(p.track, null);
  assert.ok(!JSON.stringify(p).includes("-23.55"));
});

test("até o fim da viagem: encerra quando a viagem termina (aviso do Pi, viagem nova ou 15 min sem sinal); o de prazo fixo continua sem posição", async () => {
  const env = mkEnv(LOCPW);
  const c = await locCookie(env);
  await pushLive(env, live(), null);                              // viagem 7 em andamento (estado ao vivo, sem link ainda)
  env.DB.docs.set("/api/live", { json: JSON.stringify({ trip_id: 7 }), updated_ts: NOW() });
  const v = await create(env, { mode: "viagem" }, c), t = await create(env, { mode: "4h" }, c);
  const tv = tokenOf(v.j.url), tt = tokenOf(t.j.url);
  assert.equal(JSON.parse(env.DB.share.get(sha(tv)).opts).trip, 7, "ligado à viagem em andamento");
  await pushLive(env);
  const wv = (await wsOpen(env, tv)).ws;
  assert.equal((await poll(env, tv)).j.state, "ao_vivo");
  // o Pi avisa que a viagem 7 acabou
  assert.equal((await syncShare(env, { trip_end: 7 })).status, 200);
  assert.equal(env.DB.share.get(sha(tv)).revoked, 2);
  const g = await poll(env, tv);
  assert.equal(g.r.status, 410); assert.equal(g.j.reason, "viagem_terminou");
  assert.equal(wv.closed, true); assert.equal(wv.code, CLOSE_GONE);
  // o de prazo fixo continua valendo, mas sem a posição de onde o carro parou
  const pt = (await poll(env, tt)).j;
  assert.equal(pt.state, "desligado"); assert.equal(pt.car, null); assert.equal(pt.track, null); assert.ok(pt.last_ts > 0);
  assert.deepEqual(Object.keys(env._ctx.store.get("share")), ["lastSeen"]);          // guardada só a hora, nada de posição
  // criado com o carro parado: liga-se à próxima viagem; uma viagem nova sem aviso do fim encerra
  env.DB.docs.delete("/api/live");
  const w = await create(env, { mode: "viagem" }, c);
  const tw = tokenOf(w.j.url);
  assert.equal(JSON.parse(env.DB.share.get(sha(tw)).opts).trip, null);
  assert.equal((await poll(env, tw)).j.state, "desligado");
  await pushLive(env, live({ trip_id: 9 }), frame({ trip: 9 }));
  assert.equal(JSON.parse(env.DB.share.get(sha(tw)).opts).trip, 9);
  assert.equal((await poll(env, tw)).j.state, "ao_vivo");
  await pushLive(env, live({ trip_id: 10 }), frame({ trip: 10, lat: -22.9, lon: -43.2 }));
  assert.equal((await poll(env, tw)).r.status, 410);
  const p10 = (await poll(env, tt)).j;                              // o de prazo fixo vê a viagem nova, só ela
  assert.equal(p10.state, "ao_vivo"); assert.equal(p10.track.pts.length, 1); assert.equal(p10.car.lat, -22.9);
  // o Pi desliga junto com o carro (sem aviso): 15 min sem sinal encerra o "até o fim da viagem"
  const x = await create(env, { mode: "viagem" }, c);
  const tx = tokenOf(x.j.url);
  await pushLive(env, live({ trip_id: 10 }), frame({ trip: 10 }));
  assert.equal(JSON.parse(env.DB.share.get(sha(tx)).opts).trip, 10);
  env._hub.room.now = () => NOW() + 16 * 60e3;
  const gx = await poll(env, tx);
  assert.equal(gx.r.status, 410);
  assert.equal(env.DB.share.get(sha(tx)).revoked, 2);
  assert.equal((await poll(env, tt)).j.car, null);
  // sem viagem aberta (o Pi manda trip: null): nada de posição
  env._hub.room.now = () => Date.now();
  await pushLive(env, live({ trip_id: 11 }), frame({ trip: 11 }));
  await pushLive(env, live({ trip_id: null }), { ts: NOW(), trip: null, lat: -1, lon: -1 });
  const pn = (await poll(env, tt)).j;
  assert.equal(pn.state, "desligado"); assert.ok(!JSON.stringify(pn).includes("-1,"));
});

// ---------------------------------------------------------------- decisão do dono: 24 h (padrão) ou "até eu cancelar"
test("até eu cancelar: vale de uma viagem para outra, sem posição com o carro desligado; encerrar corta na hora", async () => {
  const env = mkEnv(LOCPW);
  const c = await locCookie(env);
  const s = await create(env, { mode: "sempre", label: "Família" }, c);
  assert.equal(s.r.status, 200); assert.equal(s.j.mode, "sempre"); assert.equal(s.j.expires_ts, null);
  const tok = tokenOf(s.j.url), row = env.DB.share.get(sha(tok));
  assert.equal(row.expires_ts, FOREVER_TS);
  assert.equal((await create(env, { mode: "24h" }, c)).j.expires_ts - NOW() > 24 * 3600e3 - 5000, true);
  // viagem 21: trajeto e dados ao vivo
  for (let i = 0; i < 3; i++) await pushLive(env, live({ trip_id: 21 }), frame({ trip: 21, lat: -23.55 + i * 0.001, lon: -46.63 }));
  let p = (await poll(env, tok)).j;
  assert.equal(p.state, "ao_vivo"); assert.equal(p.track.pts.length, 3); assert.equal(p.mode, "sempre"); assert.equal(p.expires_ts, null);
  const ws = (await wsOpen(env, tok)).ws;
  // a viagem acaba (aviso do Pi): carro desligado, só a hora; nada de posição nem de onde parou
  const tEnd = NOW();
  await syncShare(env, { trip_end: 21 });
  p = (await poll(env, tok)).j;
  assert.equal(p.state, "desligado"); assert.equal(p.car, null); assert.equal(p.data, null); assert.equal(p.track, null);
  assert.ok(p.last_ts >= tEnd - 5000 && p.last_ts <= NOW());
  assert.ok(!JSON.stringify(p).includes("-23.5") && !JSON.stringify(p).includes("-46.6"));
  assert.equal(ws.closed, false); assert.equal(ws.msgs().at(-1).state, "desligado");
  assert.ok(!JSON.stringify([...env._ctx.store.values()]).includes("-23.5"), "nada de posição guardada entre as viagens");
  // dias depois (o Pi desligou junto com o carro, sem aviso): continua valendo, sem posição
  env._hub.room.now = () => NOW() + 3 * 86400e3;
  p = (await poll(env, tok)).j;
  assert.equal(p.state, "desligado"); assert.equal(p.car, null);
  env._hub.room.now = () => Date.now();
  // viagem nova: só o trajeto dela
  await pushLive(env, live({ trip_id: 22 }), frame({ trip: 22, lat: -22.9, lon: -43.2 }));
  p = (await poll(env, tok)).j;
  assert.equal(p.state, "ao_vivo"); assert.equal(p.track.trip, 22); assert.deepEqual(p.track.pts, [[-22.9, -43.2]]);
  // lista do dono: o sem prazo aparece mesmo criado há dias, marcado
  env.DB.share.get(sha(tok)).created_ts = NOW() - 10 * 86400e3;
  const lst = await (await call(env, "/api/gps/share", { headers: { cookie: c } })).json();
  const it = lst.links.find((x) => x.id === s.j.id);
  assert.ok(it && it.mode === "sempre" && it.expires_ts === null && it.state === "ativo");
  // encerrar: conexão fechada na hora, 410 dali em diante, e o Pi para de mandar posição se não houver outro link
  await call(env, `/api/gps/share/${s.j.id}`, { method: "POST", headers: { cookie: c, "content-type": "application/json" }, body: JSON.stringify({ revoke: true }) });
  assert.equal(ws.closed, true); assert.equal(ws.code, CLOSE_GONE);
  const g = await poll(env, tok);
  assert.equal(g.r.status, 410); assert.ok(!JSON.stringify(g.j).includes("-22.9"));
  // sem prazo só com as duas marcas juntas: data-sentinela sem o modo (ou o modo sem a data) não vale
  assert.equal(linkState({ revoked: 0, created_ts: NOW(), expires_ts: FOREVER_TS, opts: "{\"m\":\"tempo\"}" }).reason, "vencido");
  assert.equal(linkState({ revoked: 0, created_ts: NOW(), expires_ts: FOREVER_TS, opts: "lixo" }).reason, "vencido");
  assert.equal(linkState({ revoked: 0, created_ts: NOW() - 2000, expires_ts: NOW() - 1000, opts: "{\"m\":\"sempre\"}" }).reason, "vencido");
  assert.ok(linkState({ revoked: 0, created_ts: NOW() - 400 * 86400e3, expires_ts: FOREVER_TS, opts: "{\"m\":\"sempre\"}" }).ok);
  assert.equal(linkState({ revoked: 1, created_ts: NOW(), expires_ts: FOREVER_TS, opts: "{\"m\":\"sempre\"}" }).status, 410);
  // pelo Pi: "sempre" sem prazo; validade comum continua limitada a 24 h
  const r = await (await syncShare(env, { create: [{ hash: sha("pi-sempre"), created_ts: 1, expires_ts: null, mode: "sempre" }] })).json();
  assert.deepEqual(r.created, [sha("pi-sempre").slice(0, 8)]);
  assert.equal(env.DB.share.get(sha("pi-sempre")).expires_ts, FOREVER_TS);
});

// ---------------------------------------------------------------- limites
test("limites: consultas por minuto, links ativos, criações por hora, tamanho, token malformado nem consulta o banco", async () => {
  const env = mkEnv(LOCPW);
  const c = await locCookie(env);
  const tok = tokenOf((await create(env, { mode: "1h" }, c)).j.url);
  let last;
  for (let i = 0; i < 61; i++) last = await call(env, `/api/share/${tok}`);
  assert.equal(last.status, 429);
  for (let i = 0; i < 4; i++) assert.equal((await create(env, { mode: "1h" }, c)).r.status, 200);
  const six = await create(env, { mode: "1h" }, c);
  assert.equal(six.r.status, 429); assert.match(six.j.error, /5 links ativos/);
  for (const r of env.DB.share.values()) r.revoked = 1;
  for (let i = 0; i < 5; i++) assert.equal((await create(env, { mode: "1h" }, c)).r.status, 200);
  for (const r of env.DB.share.values()) r.revoked = 1;
  const eleven = await create(env, { mode: "1h" }, c);
  assert.equal(eleven.r.status, 429); assert.match(eleven.j.error, /última hora/);
  const big = await call(env, "/api/gps/share", { method: "POST", headers: { cookie: c }, body: JSON.stringify({ mode: "1h", label: "x".repeat(2000) }) });
  assert.equal(big.status, 413);
  assert.equal((await call(env, "/api/sync/share", { method: "POST", headers: PI, body: "x".repeat(400_001) })).status, 413);
  const n = env.DB.q.n;
  for (const bad of ["abc", "x".repeat(23), "a".repeat(21) + "!", "%2e%2e%2fapi%2fgps%2flive", "../gps/live"]) {
    const r = await call(env, `/api/share/${bad}`);
    assert.ok(r.status === 404 || r.status === 401, bad + " " + r.status);          // "../gps/live" vira /api/gps/live: protegido
    assert.ok(!(await r.text()).includes("-23.5"), bad);
  }
  assert.equal((await call(env, `/api/share/${"A".repeat(22)}/ws`, { headers: { Upgrade: "websocket" } })).status, 404);
  assert.equal(env.DB.q.n - n, 1, "só o token bem formado consulta o banco");
  assert.equal((await call(env, `/api/share/${tok}`, { method: "POST" })).status, 405);
  // conexões ao vivo por link
  env.DB.share.clear();
  const env2 = mkEnv(LOCPW);
  const t2 = tokenOf((await create(env2, { mode: "1h" })).j.url);
  for (let i = 0; i < 10; i++) assert.equal((await wsOpen(env2, t2)).r.headers.get("x-test-upgrade"), "1");
  assert.equal((await wsOpen(env2, t2)).r.status, 429);
});

// ---------------------------------------------------------------- o painel continua sem posição
test("estado ao vivo do painel nunca leva coordenada; quem tem o link nunca recebe o estado do painel", async () => {
  const env = mkEnv(LOCPW);
  const tok = tokenOf((await create(env, { mode: "1h" })).j.url);
  const pub = await call(env, "/api/ws", { headers: { Upgrade: "websocket" } });
  const pws = pub.server;
  const sws = (await wsOpen(env, tok)).ws;
  const l = live({ gps: { state: "ok", sats: 9, lat: -23.55, lon: -46.63, place: { name: "Casa" }, dest: { name: "Casa" } } });
  const r = await pushLive(env, l, frame());
  assert.equal(r.status, 200);
  const pm = pws.sent.at(-1);
  assert.ok(pm && !pm.includes("_share") && !pm.includes("-23.55") && !pm.includes("-46.63") && !pm.includes("Casa"), pm);
  assert.equal(JSON.parse(pm).values.rpm, 2100);
  for (const m of sws.sent) { const o = JSON.parse(m); assert.ok(o.t === "s" || o.t === "f"); assert.ok(!("values" in o) && !("dtc" in o) && !("status" in o)); }
  assert.equal(sws.msgs().at(-1).car.lat, -23.55);
  // cópia no banco (/api/live) e SSE: sem posição
  await call(env, "/api/sync/docs", { method: "POST", headers: PI, body: JSON.stringify({ docs: [{ key: "/api/live", json: JSON.stringify(l) }, { key: "_meta", json: JSON.stringify({ last_sync_ts: NOW() }) }] }) });
  const d = await (await call(env, "/api/live")).json();
  assert.ok(!JSON.stringify(d).includes("-23.55") && !("_share" in d));
  // sem LOC_PASSWORD o quadro do link é descartado e o Pi ouve "share: false"
  const env2 = mkEnv();
  const p2 = (await call(env2, "/api/ws", { headers: { Upgrade: "websocket" } })).server;
  const r2 = await (await pushLive(env2)).json();
  assert.equal(r2.share, false);
  assert.ok(!p2.sent.at(-1).includes("-23.55"));
  assert.equal(env2._hub.room.frame, null);
});

test("quadro do Pi conferido campo a campo; senha de localização removida: links antigos param na hora", async () => {
  const env = mkEnv(LOCPW);
  const tok = tokenOf((await create(env, { mode: "1h" })).j.url);
  await pushLive(env, live(), { ...frame(), rpm: "9e99", ect: 900, alert: "<b>Óleo</b>\u0000" + "x".repeat(200), lat: 999, place: { name: "Casa" }, parked: { lat: -23.611 }, extra: "z" });
  const p = (await poll(env, tok)).j;
  assert.equal(p.data.rpm, null); assert.equal(p.data.ect, null); assert.equal(p.car, null);
  assert.ok(!p.data.alert.includes("<") && !p.data.alert.includes("\u0000") && p.data.alert.length <= 60);
  const stored = JSON.stringify([...env._ctx.store.values()]) + JSON.stringify(env._hub.room.frame);
  for (const bad of ["Casa", "-23.611", "extra", "9e99"]) assert.ok(!stored.includes(bad), bad);
  // LOC_PASSWORD tirada do Worker com um link ainda no D1: o link não serve mais nada e o quadro do Pi é descartado
  const env2 = mkEnv();
  for (const [k, r] of env.DB.share) env2.DB.share.set(k, { ...r });
  const r2 = await (await pushLive(env2)).json();
  assert.equal(r2.share, false); assert.equal(env2._hub.room.frame, null);
  const g = await poll(env2, tok);
  assert.equal(g.r.status, 410); assert.ok(!JSON.stringify(g.j).includes("-23.5"));
  assert.equal((await wsOpen(env2, tok)).r.status, 410);
});

test("/s/<token>: página pública sem senha (nem com DASH_PASSWORD), sem cache, noindex; o resto do painel continua fechado", async () => {
  const env = mkEnv({ ...LOCPW, DASH_PASSWORD: "painel-123" });
  const login = await call(env, "/login", { method: "POST", body: new URLSearchParams({ password: "painel-123" }) });
  const sess = login.headers.get("set-cookie").split(";")[0];
  assert.equal((await create(env, { mode: "1h" })).r.status, 401);           // só a senha de localização não basta: precisa do login
  const tok = tokenOf((await create(env, { mode: "1h" }, `${sess}; ${await locCookie(env)}`)).j.url);
  const pg = await call(env, `/s/${tok}`);
  assert.equal(pg.status, 200);
  assert.equal(await pg.text(), "asset /share.html");
  assert.equal(pg.headers.get("cache-control"), "no-store");
  assert.match(pg.headers.get("x-robots-tag"), /noindex/);
  assert.equal(pg.headers.get("referrer-policy"), "strict-origin");
  assert.match(pg.headers.get("content-security-policy"), /frame-ancestors 'none'/);
  assert.equal((await call(env, `/s/qualquer`)).status, 200);              // a página diz "link não encontrado"
  assert.equal((await call(env, `/api/share/${tok}`)).status, 200);
  assert.equal((await call(env, "/static/leaflet.js")).status, 200);
  for (const p of ["/api/live", "/api/gps/share", "/api/gps/live", "/api/trips", "/static/app.js"]) {
    const r = await call(env, p);
    assert.ok(r.status === 401 || r.status === 303, p + " " + r.status);
  }
});

// ---------------------------------------------------------------- Pi
test("Pi: link criado no painel do Pi (só o hash), relógio do Pi errado não estende a validade, lista no ping sem hash", async () => {
  const env = mkEnv(LOCPW);
  const tok = "AbCdEfGhIjKlMnOpQrStUv", h = sha(tok);
  const r = await (await syncShare(env, { create: [{ hash: h, created_ts: 946684800000, expires_ts: 946684800000 + 3600e3, mode: "viagem", label: "Ana", dest: true, trip: 31 },
    { hash: "zz", created_ts: 1, expires_ts: 2 }, { hash: sha("y"), created_ts: 1, expires_ts: 1 + 48 * 3600e3 }] })).json();
  assert.deepEqual(r.created, [h.slice(0, 8)]);
  assert.equal(r.refused.length, 2);
  const row = env.DB.share.get(h);
  assert.ok(Math.abs(row.created_ts - NOW()) < 5000 && row.expires_ts - row.created_ts === 3600e3);
  assert.deepEqual(JSON.parse(row.opts), { m: "viagem", trip: 31, dest: 1, src: "pi" });
  const ping = await (await call(env, "/api/sync/ping", { headers: PI })).json();
  assert.equal(ping.loc, true);
  assert.equal(ping.share.links.length, 1);
  assert.equal(ping.share.links[0].id, h.slice(0, 8));
  assert.ok(!JSON.stringify(ping).includes(h) && !JSON.stringify(ping).includes(tok));
  await pushLive(env, live({ trip_id: 31 }), frame({ trip: 31 }));
  assert.equal((await poll(env, tok)).j.data.dest, "Casa");
  await syncShare(env, { revoke: [h.slice(0, 8), "nao-e-id"] });
  assert.equal((await poll(env, tok)).r.status, 410);
  assert.equal((await call(env, "/api/sync/share", { method: "POST", body: "{}" })).status, 401);      // sem o token do Pi
});

test("sem a migração da F9 (coluna opts): compartilhamento indisponível, nada de posição; o painel não muda", async () => {
  const env = mkEnv(LOCPW, { noOpts: true });
  const { r } = await create(env, { mode: "1h" });
  assert.equal(r.status, 503);
  assert.equal(env.DB.share.size, 0);
  const lr = await (await pushLive(env)).json();
  assert.equal(lr.share, false);
  assert.equal(env._hub.room.frame, null);
  assert.equal((await call(env, `/api/share/${"A".repeat(22)}`)).status, 503);
  assert.equal((await call(env, "/api/sync/ping", { headers: PI })).status, 200);
});
