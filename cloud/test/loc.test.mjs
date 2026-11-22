// Proteção da localização no painel online (docs/GPS-ARQUITETURA.md, seções 7.2 e 10.3): a regra única falha fechada.
// Roda com `npm test` (node --test test/). D1 e ASSETS falsos em memória; nenhuma rede.
import { test } from "node:test";
import assert from "node:assert/strict";
import worker, { isLoc, LOC_OPEN, LOC_QUEUE, docKey, normPath, makeLocCookie, locAuthorized, scrubLive } from "../src/index.js";

// ---------------------------------------------------------------- D1 falso (só as consultas que o Worker faz)
function fakeDB() {
  const docs = new Map(), cmds = [];
  let nextId = 1;
  const run = (sql, a) => {
    if (/^SELECT json, updated_ts FROM doc WHERE key=\?/.test(sql)) { const d = docs.get(a[0]); return { first: d ? { json: d.json, updated_ts: d.updated_ts } : null }; }
    if (/^SELECT key, json, updated_ts FROM doc WHERE key IN/.test(sql)) return { all: [...docs].filter(([k]) => k === "/api/live" || k === "_meta").map(([key, d]) => ({ key, ...d })) };
    if (/^INSERT INTO doc/.test(sql)) { docs.set(a[0], { json: a[1], updated_ts: a[2] }); return {}; }
    if (/^DELETE FROM doc WHERE key=\?/.test(sql)) { docs.delete(a[0]); return {}; }
    if (/^SELECT COUNT\(\*\) n FROM cmd WHERE done=0$/.test(sql)) return { first: { n: cmds.filter((c) => !c.done).length } };
    if (/^SELECT COUNT\(\*\) n, MAX\(ts\) last FROM cmd/.test(sql)) { const p = cmds.filter((c) => !c.done && c.path === "/api/fuel/fill"); return { first: { n: p.length, last: p.length ? Math.max(...p.map((c) => c.ts)) : null } }; }
    if (/^SELECT body FROM cmd WHERE done=0 AND path='\/api\/fuel\/fill' AND ts > \?/.test(sql)) return { all: cmds.filter((c) => !c.done && c.path === "/api/fuel/fill" && c.ts > a[0]) };
    if (/^SELECT ts, body FROM cmd WHERE done=0 AND path='\/api\/fuel\/fill'/.test(sql)) return { all: cmds.filter((c) => !c.done && c.path === "/api/fuel/fill") };
    if (/^INSERT INTO cmd/.test(sql)) { cmds.push({ id: nextId++, ts: a[0], method: a[1], path: a[2], body: a[3], done: 0 }); return {}; }
    if (/^SELECT id, ts, method, path, body FROM cmd WHERE done=0/.test(sql)) return { all: cmds.filter((c) => !c.done).slice(0, 50).map(({ id, ts, method, path, body }) => ({ id, ts, method, path, body })) };
    if (/^UPDATE cmd SET done=1/.test(sql)) { const c = cmds.find((x) => x.id === a[2]); if (c) c.done = 1; return {}; }
    throw new Error("SQL inesperado no teste: " + sql);
  };
  const stmt = (sql, a = []) => ({
    bind: (...b) => stmt(sql, b),
    first: async () => run(sql, a).first ?? null,
    all: async () => ({ results: run(sql, a).all ?? [] }),
    run: async () => (run(sql, a), { success: true }),
    _exec: () => run(sql, a),
  });
  return { docs, cmds, prepare: (sql) => stmt(sql.trim()), batch: async (list) => { for (const s of list) s._exec(); return []; } };
}

function mkEnv(extra = {}) {
  return {
    SYNC_TOKEN: "tok-sync", DB: fakeDB(), LOC_DELAY_MS: "0",
    ASSETS: { fetch: async (r) => new Response("asset " + new URL(r.url).pathname, { status: 200 }) },
    ...extra,
  };
}
const ORIGIN = "https://obd.exemplo";
const call = (env, path, init = {}) => worker.fetch(new Request(ORIGIN + path, init), env);
const withCookie = (c) => ({ headers: { cookie: c } });
const sync = (env, docs) => call(env, "/api/sync/docs", { method: "POST", headers: { authorization: "Bearer tok-sync", "content-type": "application/json" }, body: JSON.stringify({ docs }) });
const POS = JSON.stringify({ lat: -23.5, lon: -46.6, state: "ok", age_s: 1 });

// ---------------------------------------------------------------- a regra
test("isLoc e LOC_OPEN: todo /api/gps/* é protegido, menos as rotas só com números", () => {
  for (const p of ["/api/gps/live", "/api/gps/logbook", "/api/gps/places", "/api/gps/places/3", "/api/gps/parked", "/api/gps/alerts",
    "/api/gps/heat", "/api/gps/trips/12", "/api/gps/trips/12/track", "/api/gps/trips/12/export.gpx", "/api/gps/export.gpx",
    "/api/gps/routes", "/api/gps/segments/1", "/api/gps/events", "/api/gps/share", "/api/gps/qualquer-coisa-nova", "/api/gps",
    "/api/gps/status/", "/api/gps/status/x", "/API/GPS/LIVE", "/api//gps/live", "/api/%67ps/live", "/api%2Fgps%2Flive", "/api/gps/%2e%2e/live",
    "/api/gps/%E0%A4%A", "/api\\gps\\live"])
    assert.equal(isLoc(p), true, p);
  for (const p of ["/api/gps/status", "/api/gps/gears", "/api/gps/speedo", "/api/gps/score", "/api/gps/perf", "/api/gps/dyno",
    "/api/gps/terrain", "/api/gps/roadmix", "/api/live", "/api/trips", "/api/trips/3", "/api/alerts", "/", "/static/gps.js", "/api/gpsx"])
    assert.equal(isLoc(p), false, p);
  assert.ok(LOC_OPEN.test("/api/gps/status") && !LOC_OPEN.test("/api/gps/status2"));
  assert.equal(normPath("/API//Gps/%6Cive"), "/api/gps/live");
  assert.ok(LOC_QUEUE.some((r) => r.test("/api/gps/places")) && LOC_QUEUE.some((r) => r.test("/api/gps/places/4")) && !LOC_QUEUE.some((r) => r.test("/api/gps/live")));
});

test("docKey das rotas novas do GPS igual ao Pi (sync.py)", () => {
  const k = (u) => docKey(new URL("https://x" + u));
  assert.equal(k("/api/gps/logbook?limit=1000&offset=0"), "/api/gps/logbook?limit=1000");
  assert.equal(k("/api/gps/heat?days=30&signal=stft1"), "/api/gps/heat?days=30&signal=stft1");
  assert.equal(k("/api/gps/alerts?days=90&rule="), "/api/gps/alerts?days=90");
  assert.equal(k("/api/gps/events?days=30"), "/api/gps/events?days=30");
  assert.equal(k("/api/gps/trips/7/track?x=1"), "/api/gps/trips/7/track");
});

// ---------------------------------------------------------------- sem LOC_PASSWORD: localização indisponível
test("sem LOC_PASSWORD: nada de posição é servido nem guardado, e o ping diz loc=false", async () => {
  const env = mkEnv();
  const ping = await (await call(env, "/api/sync/ping", { headers: { authorization: "Bearer tok-sync" } })).json();
  assert.equal(ping.loc, false);
  const r = await (await sync(env, [{ key: "/api/gps/live", json: POS }, { key: "/api/gps/status", json: "{\"state\":\"ok\"}" },
    { key: "/api/gps/logbook?limit=1000", json: "{\"items\":[]}" }, { key: "/api/trips?limit=1000", json: "[]" }])).json();
  assert.equal(r.stored, 2);
  assert.deepEqual([...env.DB.docs.keys()].sort(), ["/api/gps/status", "/api/trips?limit=1000"]);
  env.DB.docs.set("/api/gps/live", { json: POS, updated_ts: Date.now() });          // mesmo se já houvesse cópia no banco
  const g = await call(env, "/api/gps/live");
  assert.equal(g.status, 401);
  const body = await g.json();
  assert.deepEqual(body, { error: "Localização protegida", locked: true, configured: false });
  assert.ok(!JSON.stringify(body).includes("-23.5"));
  assert.equal((await call(env, "/api/gps/status")).status, 200);                  // sem posição: aberto
  const u = await call(env, "/loc/unlock", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ password: "" }) });
  assert.equal(u.status, 503);
  assert.equal(u.headers.get("set-cookie"), null);
  // um cookie forjado com senha vazia não abre nada
  for (const fake of [{ LOC_PASSWORD: "", SYNC_TOKEN: "tok-sync" }, { SYNC_TOKEN: "tok-sync" }, { ...env }]) {
    const forged = await makeLocCookie(fake);
    assert.equal((await call(env, "/api/gps/live", withCookie("obd_loc=" + forged))).status, 401);
  }
  assert.equal((await (await call(env, "/api/env")).json()).loc, "desativada");
});

test("DASH_PASSWORD sozinha não libera a localização", async () => {
  const env = mkEnv({ DASH_PASSWORD: "painel-123" });
  const login = await call(env, "/login", { method: "POST", body: new URLSearchParams({ password: "painel-123" }) });
  const sess = login.headers.get("set-cookie").split(";")[0];
  assert.equal((await call(env, "/api/trips", withCookie(sess))).status, 404);       // logado (doc ainda não existe)
  env.DB.docs.set("/api/gps/live", { json: POS, updated_ts: Date.now() });
  const r = await call(env, "/api/gps/live", withCookie(sess));
  assert.equal(r.status, 401);
  assert.equal((await r.json()).configured, false);
});

// ---------------------------------------------------------------- com LOC_PASSWORD
test("com LOC_PASSWORD: trancada sem cookie, aberta com a senha; cookie inválido, vencido ou de outra senha não vale", async () => {
  const env = mkEnv({ LOC_PASSWORD: "senha-de-teste-longa" });
  assert.equal((await (await call(env, "/api/sync/ping", { headers: { authorization: "Bearer tok-sync" } })).json()).loc, true);
  await sync(env, [{ key: "/api/gps/live", json: POS }, { key: "/api/gps/logbook?limit=1000", json: "{\"items\":[1]}" }]);
  assert.ok(env.DB.docs.has("/api/gps/live"));
  const locked = await call(env, "/api/gps/live");
  assert.equal(locked.status, 401);
  assert.deepEqual(await locked.json(), { error: "Localização protegida", locked: true, configured: true });
  assert.equal((await (await call(env, "/api/env")).json()).loc, "trancada");
  for (const p of ["/API/GPS/LIVE", "/api//gps/live", "/api/%67ps/live", "/api/gps/logbook?limit=1000"])
    assert.equal((await call(env, p)).status, 401, p);
  // senha errada
  const bad = await call(env, "/loc/unlock", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ password: "errada" }) });
  assert.equal(bad.status, 401);
  assert.equal(bad.headers.get("set-cookie"), null);
  // senha certa (JSON e formulário)
  const ok = await call(env, "/loc/unlock", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ password: "senha-de-teste-longa" }) });
  assert.equal(ok.status, 200);
  const sc = ok.headers.get("set-cookie");
  assert.match(sc, /^obd_loc=\d+\.[0-9a-f]{64}; Path=\/; Max-Age=2592000; HttpOnly; Secure; SameSite=Lax$/);
  const form = await call(env, "/loc/unlock", { method: "POST", body: new URLSearchParams({ password: "senha-de-teste-longa" }) });
  assert.equal(form.status, 200);
  const cookie = sc.split(";")[0];
  const g = await call(env, "/api/gps/live", withCookie("outro=1; " + cookie));
  assert.equal(g.status, 200);
  const gd = await g.json();
  assert.equal(gd.lat, -23.5);
  assert.equal(g.headers.get("cache-control"), "no-store");
  assert.equal((await (await call(env, "/api/env", withCookie(cookie))).json()).loc, "aberta");
  assert.equal((await call(env, "/api/gps/logbook?limit=1000", withCookie(cookie))).status, 200);
  // cookie adulterado, sem assinatura, vencido, longe demais no futuro, de outra senha
  const [exp, sig] = cookie.slice("obd_loc=".length).split(".");
  const flip = sig.slice(0, -1) + (sig.endsWith("0") ? "1" : "0");
  const old = await makeLocCookie(env, Date.now() - 31 * 86400e3);
  const far = await makeLocCookie(env, Date.now() + 400 * 86400e3);
  const other = await makeLocCookie({ ...env, LOC_PASSWORD: "outra-senha-qualquer" });
  for (const c of [`obd_loc=${exp}.${flip}`, `obd_loc=${exp}`, `obd_loc=${exp}.`, `obd_loc=abc.${sig}`, `obd_loc=${old}`, `obd_loc=${far}`, `obd_loc=${other}`, `xobd_loc=${exp}.${sig}`])
    assert.equal((await call(env, "/api/gps/live", withCookie(c))).status, 401, c);
  // trocar a senha invalida os cookies já emitidos
  const env2 = { ...env, LOC_PASSWORD: "senha-nova-de-teste" };
  assert.equal(await locAuthorized(new Request(ORIGIN + "/api/gps/live", withCookie(cookie)), env2), false);
  // trancar de novo
  const lock = await call(env, "/loc/lock", { method: "POST" });
  assert.match(lock.headers.get("set-cookie"), /^obd_loc=; Path=\/; Max-Age=0/);
});

test("exportação GPX/KML: 404 na nuvem (mesmo com a localização liberada)", async () => {
  const env = mkEnv({ LOC_PASSWORD: "senha-de-teste-longa" });
  const c = "obd_loc=" + (await makeLocCookie(env));
  for (const p of ["/api/gps/trips/3/export.gpx", "/api/gps/trips/3/export.kml", "/api/gps/export.gpx?from=1&to=2"]) {
    assert.equal((await call(env, p)).status, 401, p);
    const r = await call(env, p, withCookie(c));
    assert.equal(r.status, 404, p);
    assert.match((await r.json()).error, /painel do Pi/);
  }
});

test("fila de ações com posição: só com a localização liberada, corpo limitado, e só para o Pi que pede cap=gps", async () => {
  const env = mkEnv({ LOC_PASSWORD: "senha-de-teste-longa" });
  const c = "obd_loc=" + (await makeLocCookie(env));
  const body = JSON.stringify({ name: "Casa", kind: "casa", lat: -23.5, lon: -46.6 });
  assert.equal((await call(env, "/api/gps/places", { method: "POST", body })).status, 401);
  assert.equal(env.DB.cmds.length, 0);
  const r = await call(env, "/api/gps/places", { method: "POST", body, headers: { cookie: c } });
  assert.deepEqual(await r.json(), { ok: true, queued: true });
  assert.equal((await call(env, "/api/gps/places/2", { method: "POST", body: "x".repeat(4001), headers: { cookie: c } })).status, 413);
  assert.equal((await call(env, "/api/gps/places/2", { method: "POST", body: "{nao é json", headers: { cookie: c } })).status, 400);
  assert.equal((await call(env, "/api/gps/live", { method: "POST", body: "{}", headers: { cookie: c } })).status, 409);
  const get = async (cap) => (await (await call(env, "/api/sync/commands" + (cap ? "?cap=" + cap : ""), { headers: { authorization: "Bearer tok-sync" } })).json()).commands;
  assert.equal((await get("total,maint")).length, 0);                      // Pi sem cap=gps não recebe
  const got = await get("total,maint,gps");
  assert.equal(got.length, 1);
  assert.equal(got[0].path, "/api/gps/places");
  for (let i = 0; i < 29; i++) env.DB.cmds.push({ id: 100 + i, ts: 1, method: "POST", path: "/api/annotations", body: "{}", done: 0 });
  assert.equal((await call(env, "/api/gps/places", { method: "POST", body, headers: { cookie: c } })).status, 429);
});

test("estado ao vivo público nunca leva posição (defesa extra no Worker)", async () => {
  const env = mkEnv({ LOC_PASSWORD: "senha-de-teste-longa" });
  const live = { state: "coletando", ts: Date.now(), values: {}, gps: { state: "ok", sats: 9, speed: 30, lat: -23.5, lon: -46.6, place: { name: "Casa" } } };
  await sync(env, [{ key: "/api/live", json: JSON.stringify(live) }, { key: "_meta", json: JSON.stringify({ last_sync_ts: Date.now() }) }]);
  const d = await (await call(env, "/api/live")).json();
  assert.equal(d.gps.sats, 9);
  assert.ok(!("lat" in d.gps) && !("lon" in d.gps) && !("place" in d.gps));
  assert.ok(!JSON.stringify(d).includes("-23.5") && !JSON.stringify(d).includes("Casa"));
  assert.deepEqual(scrubLive({ gps: { state: "ok", latitude: 1, longitude: 2, parked: {}, dest: {} } }), { gps: { state: "ok" } });
});

// ---------------------------------------------------------------- trajeto da viagem aberta (mapa do Ao vivo)
const syncTrack = (env, body) => call(env, "/api/sync/track", { method: "POST", headers: { authorization: "Bearer tok-sync", "content-type": "application/json" }, body: JSON.stringify(body) });

test("trajeto da viagem aberta: só com LOC_PASSWORD e token, 401 sem a senha de localização, servido com ela, nunca no /api/live", async () => {
  const T = 1_800_000_000_000, P = (i) => [-23.5 + i * 1e-4, -46.6, T + i * 1000];
  // sem LOC_PASSWORD: não guarda nada
  const env0 = mkEnv();
  assert.deepEqual(await (await syncTrack(env0, { trip: 7, start_ts: T - 1, pts: [P(0)] })).json(), { ok: false, loc: false });
  assert.equal(env0.DB.docs.has("/api/gps/current_track"), false);
  const env = mkEnv({ LOC_PASSWORD: "senha-de-teste-longa" });
  // sem o token do Pi: recusado
  assert.equal((await call(env, "/api/sync/track", { method: "POST", body: JSON.stringify({ trip: 7, pts: [P(0)] }) })).status, 401);
  // viagem nova: completo; depois só acrescenta (pontos velhos, repetidos ou inválidos não entram)
  assert.equal((await (await syncTrack(env, { trip: 7, start_ts: T - 1, pts: [P(0), P(1), ["x", 1, 2]] })).json()).full, true);
  assert.equal((await (await syncTrack(env, { trip: 7, append: [P(1), P(2), [91, 0, T + 9e6], P(3)] })).json()).added, 2);
  assert.deepEqual((await (await syncTrack(env, { trip: 8, append: [P(4)] })).json()), { ok: false, reset: true });   // outra viagem: Pi manda tudo
  assert.equal((await syncTrack(env, { trip: 7, append: "x" })).status, 400);
  // GET: trancado sem o cookie (com qualquer grafia), aberto com ele
  for (const p of ["/api/gps/current_track", "/API/GPS/CURRENT_TRACK", "/api//gps/current_track", "/api/gps/current%5Ftrack"]) {
    const r = await call(env, p);
    assert.equal(r.status, 401, p);
    assert.ok(!(await r.text()).includes("-23.5"), p);
  }
  const ok = await call(env, "/loc/unlock", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ password: "senha-de-teste-longa" }) });
  const cookie = ok.headers.get("set-cookie").split(";")[0];
  const g = await call(env, "/api/gps/current_track", withCookie(cookie));
  assert.equal(g.status, 200);
  assert.equal(g.headers.get("cache-control"), "no-store");
  const j = await g.json();
  assert.deepEqual(j, { trip: 7, start_ts: T - 1, pts: [P(0), P(1), P(2), P(3)] });
  // o /api/live público nunca leva o trajeto
  await sync(env, [{ key: "/api/live", json: JSON.stringify({ state: "coletando", engine_on: true, trip_id: 7, gps: { state: "ok" } }) },
    { key: "_meta", json: JSON.stringify({ last_sync_ts: Date.now() }) }]);
  for (const c of [{}, withCookie(cookie)]) {
    const live = await (await call(env, "/api/live", c)).text();
    assert.ok(!live.includes("-23.5") && !live.includes("pts") && !live.includes("current_track"), live);
  }
  // teto de pontos: rareia mantendo a saída e a ponta atual
  await syncTrack(env, { trip: 9, start_ts: T, pts: Array.from({ length: 3500 }, (_, i) => P(i)) });
  const big = JSON.parse(env.DB.docs.get("/api/gps/current_track").json);
  assert.ok(big.pts.length <= 3000 && big.pts[0][2] === T && big.pts.at(-1)[2] === T + 3499 * 1000);
  // fim da viagem: apaga
  assert.equal((await (await syncTrack(env, { end: 9 })).json()).ended, true);
  assert.equal(env.DB.docs.has("/api/gps/current_track"), false);
  assert.equal((await call(env, "/api/gps/current_track", withCookie(cookie))).status, 404);
});
