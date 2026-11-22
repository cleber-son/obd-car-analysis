// Delfos — painel online público (Cloudflare Worker + D1), em https://obd.cleberson.social.br
//
// O Pi monta cada resposta da API e envia como "documento" (POST /api/sync/docs, com token). Este Worker:
//   • serve o mesmo painel estático do Pi e responde GET /api/* com o documento correspondente (público, só leitura);
//   • marca o carro como desligado quando o Pi para de enviar (Pi sem energia/sem internet);
//   • se DASH_PASSWORD estiver definido, exige senha (desligado por padrão, a pedido do dono);
//   • a fila de ações (tanque cheio, editar viagem…) só fica ativa com senha; sem senha, o painel online é só leitura.
//
// Segredos (wrangler secret put): SYNC_TOKEN (obrigatório); DASH_PASSWORD e SESSION_SECRET (opcionais);
// LOC_PASSWORD (opcional): senha de localização. Sem ela, nada com posição ou nome de lugar é servido nem guardado.
//
// F9: link para compartilhar a viagem ao vivo (/s/<token>): ver src/share.js. Público e sem senha, mas só mostra a
// viagem em andamento enquanto o link vale; exige LOC_PASSWORD (sem ela o Pi nem envia posição).

import { ShareRoom, ownerShare, syncShare, pingShare, sharePage, shareApi } from "./share.js";

const KEEP = {
  "/api/series": ["signals", "span"], "/api/alerts": ["days", "severity"], "/api/report": ["days"],
  "/api/report/period": ["kind", "offset", "day", "trip"], "/api/summary": ["days"], "/api/throttle": ["days"],
  "/api/diagnostics": ["days"], "/api/trips": ["limit"], "/api/advice": ["rule"], "/api/ai/analysis": ["trip"],
  // GPS (docs/GPS-ARQUITETURA.md, seção 7.2): igual ao KEEP de src/obdpi/sync.py
  "/api/gps/logbook": ["limit"], "/api/gps/alerts": ["days"], "/api/gps/events": ["days"], "/api/gps/heat": ["signal", "days"],
  "/api/gps/traffic": ["days"], "/api/gps/terrain": ["days"], "/api/gps/roadmix": ["days"], "/api/gps/score": ["days"],
};
const SHARE_STATIC = new Set(["/static/leaflet.js", "/static/leaflet.css"]);
const QUEUEABLE = [/^\/api\/fuel\/fill$/, /^\/api\/annotations$/, /^\/api\/trips\/\d+$/, /^\/api\/ai\/analyze$/];
const SESSION_DAYS = 30;
const enc = new TextEncoder();

// ---------------------------------------------------------------- localização (seção 7.2): regra única, falha fechada
// Todo /api/gps/* leva posição ou nome de lugar, menos estas rotas só com números. O caminho é normalizado antes
// (minúsculas, %xx decodificado, barras repetidas) para nenhuma grafia alternativa escapar; erro ao decodificar = protegido.
export const LOC_OPEN = /^\/api\/gps\/(status|gears|speedo|score|perf|dyno|terrain|roadmix)$/;
export function normPath(p) {
  let x = String(p || "");
  for (let i = 0; i < 3 && /%[0-9a-f]{2}/i.test(x); i++) { try { x = decodeURIComponent(x); } catch { return "/api/gps/?"; } }
  return x.toLowerCase().replace(/\\/g, "/").replace(/\/{2,}/g, "/");
}
export function isLoc(p) {
  const n = normPath(p);
  return (n === "/api/gps" || n.startsWith("/api/gps/")) && !LOC_OPEN.test(n);
}
// ações com localização que entram na fila do Pi (só com a localização liberada)
export const LOC_QUEUE = [/^\/api\/gps\/places(\/\d+)?$/, /^\/api\/gps\/segments(\/\d+)?$/, /^\/api\/gps\/routes\/\d+$/, /^\/api\/gps\/fuelstop\/\d+$/];
const LOC_DAYS = 30, LOC_BODY_MAX = 4000, LOC_PENDING_MAX = 30;
const locConfigured = (env) => typeof env.LOC_PASSWORD === "string" && env.LOC_PASSWORD.length > 0;
// a assinatura do cookie depende da própria senha: trocar a LOC_PASSWORD invalida todos os cookies emitidos
const locSecret = (env) => `loc|${env.LOC_PASSWORD}|${env.SESSION_SECRET || env.SYNC_TOKEN || ""}`;

export async function makeLocCookie(env, now = Date.now()) {
  const exp = now + LOC_DAYS * 86400e3;
  return `${exp}.${await hmac(locSecret(env), "loc" + exp)}`;
}

export async function locAuthorized(req, env) {
  if (!locConfigured(env)) return false;                     // sem senha: localização indisponível para todos
  const m = (req.headers.get("cookie") || "").match(/(?:^|;\s*)obd_loc=([^;]+)/);
  if (!m) return false;
  const [exp, sig] = m[1].split(".");
  if (!exp || !sig || !/^\d+$/.test(exp) || +exp < Date.now() || +exp > Date.now() + (LOC_DAYS + 1) * 86400e3) return false;
  return safeEqual(sig, await hmac(locSecret(env), "loc" + exp));
}

const locLocked = (env) => json({ error: "Localização protegida", locked: true, configured: locConfigured(env) }, 401);
const LOC_COOKIE = (v, age) => `obd_loc=${v}; Path=/; Max-Age=${age}; HttpOnly; Secure; SameSite=Lax`;

async function locUnlock(req, env) {
  if (!locConfigured(env)) return json({ error: "Localização desativada: defina LOC_PASSWORD no Worker.", configured: false }, 503);
  let pw = "";
  try {
    const ct = req.headers.get("content-type") || "";
    if (ct.includes("application/json")) pw = String((JSON.parse((await req.text()).slice(0, 2000)) || {}).password || "");
    else pw = String((await req.formData()).get("password") || "");
  } catch { pw = ""; }
  // comparação de tamanho fixo (HMAC das duas), para o tempo não revelar nada da senha
  const k = locSecret(env);
  if (pw && safeEqual(await hmac(k, "pw" + pw), await hmac(k, "pw" + env.LOC_PASSWORD)))
    return json({ ok: true, loc: "aberta" }, 200, { "set-cookie": LOC_COOKIE(await makeLocCookie(env), LOC_DAYS * 86400) });
  await new Promise((r) => setTimeout(r, env.LOC_DELAY_MS !== undefined ? +env.LOC_DELAY_MS : 800));   // freia tentativas
  return json({ error: "Senha incorreta.", locked: true, configured: true }, 401);
}

async function locQueue(req, env, p) {
  const body = (await req.text());
  if (body.length > LOC_BODY_MAX) return json({ error: "conteúdo grande demais" }, 413);
  try { if (body) JSON.parse(body); } catch { return json({ error: "dados inválidos" }, 400); }
  const pend = await env.DB.prepare("SELECT COUNT(*) n FROM cmd WHERE done=0").first();
  if (pend && pend.n >= LOC_PENDING_MAX) return json({ error: "Há muitas ações esperando o Pi conectar. Tente depois." }, 429);
  await env.DB.prepare("INSERT INTO cmd(ts, method, path, body) VALUES (?,?,?,?)").bind(Date.now(), "POST", p, body || "{}").run();
  return json({ ok: true, queued: true });
}

// defesa extra: o estado ao vivo (público) nunca leva posição; se um Pi com defeito mandar, sai daqui sem ela
export function scrubLive(d) {
  if (d && typeof d === "object" && d.gps && typeof d.gps === "object") {
    for (const k of ["lat", "lon", "latitude", "longitude", "place", "parked", "dest"]) delete d.gps[k];
  }
  return d;
}

// mesma normalização que src/obdpi/sync.py (doc_key)
export function docKey(url) {
  const keep = KEEP[url.pathname] || [];
  const parts = keep.filter((k) => url.searchParams.has(k)).sort().map((k) => `${k}=${url.searchParams.get(k)}`);
  return url.pathname + (parts.length ? "?" + parts.join("&") : "");
}

const json = (data, status = 200, headers = {}) =>
  new Response(JSON.stringify(data), { status, headers: { "content-type": "application/json; charset=utf-8", "cache-control": "no-store", ...headers } });

async function hmac(secret, msg) {
  const key = await crypto.subtle.importKey("raw", enc.encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const sig = await crypto.subtle.sign("HMAC", key, enc.encode(msg));
  return [...new Uint8Array(sig)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

function safeEqual(a, b) {
  if (typeof a !== "string" || typeof b !== "string" || a.length !== b.length) return false;
  let r = 0;
  for (let i = 0; i < a.length; i++) r |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return r === 0;
}

const secretOf = (env) => env.SESSION_SECRET || `${env.DASH_PASSWORD}|${env.SYNC_TOKEN}`;

async function makeSession(env) {
  const exp = Date.now() + SESSION_DAYS * 86400e3;
  return `${exp}.${await hmac(secretOf(env), "s" + exp)}`;
}

async function validSession(req, env) {
  const m = (req.headers.get("cookie") || "").match(/(?:^|;\s*)obd_s=([^;]+)/);
  if (!m) return false;
  const [exp, sig] = m[1].split(".");
  if (!exp || !sig || +exp < Date.now()) return false;
  return safeEqual(sig, await hmac(secretOf(env), "s" + exp));
}

function syncAuthorized(req, env) {
  const h = req.headers.get("authorization") || "";
  return !!env.SYNC_TOKEN && safeEqual(h, `Bearer ${env.SYNC_TOKEN}`);
}

function loginPage(msg = "") {
  return new Response(`<!doctype html><html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover"><title>Delfos · Entrar</title>
<meta name="theme-color" content="#0d1117"><style>
:root{--bg:#0d1117;--panel:#151b24;--line:#263041;--fg:#e8edf4;--muted:#8592a6;--ok:#2ee59d;--bad:#ff4d5e;color-scheme:dark}
*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--bg);color:var(--fg);
font:15px/1.5 -apple-system,"Segoe UI",system-ui,sans-serif;padding:24px max(16px,env(safe-area-inset-left))}
form{width:min(380px,100%);background:var(--panel);border:1px solid var(--line);border-radius:20px;padding:28px;display:grid;gap:14px}
.logo{width:44px;height:44px;border-radius:13px;background:conic-gradient(from 200deg,#ff6b3d,#ffb020,#2ee59d,#2ec8ff,#8b7bff,#ff4fa3,#ff6b3d);display:grid;place-items:center}
.logo i{width:28px;height:28px;border-radius:9px;background:var(--panel)}h1{margin:0;font-size:20px}p{margin:0;color:var(--muted);font-size:13px}
input{font:16px inherit;padding:14px;border-radius:12px;border:1px solid var(--line);background:#1c2430;color:var(--fg)}
button{font:700 16px inherit;padding:14px;border:0;border-radius:12px;background:var(--ok);color:#06140d;cursor:pointer}
.err{color:var(--bad);font-size:13px}</style></head><body>
<form method="post" action="/login"><div class="logo"><i></i></div><h1>Delfos</h1>
<p>o motor fala, Delfos traduz.<br>Painel online do Civic. Digite a senha para ver o histórico.</p>
<input type="password" name="password" placeholder="Senha" autocomplete="current-password" autofocus required aria-label="Senha">
${msg ? `<div class="err">${msg}</div>` : ""}<button type="submit">Entrar</button></form></body></html>`,
    { status: msg ? 401 : 200, headers: { "content-type": "text/html; charset=utf-8", "cache-control": "no-store" } });
}

// posição atual: a idade conta também o tempo desde que a nuvem recebeu (o Pi pode ter desligado)
function gpsLiveView(raw, updated) {
  try {
    const d = JSON.parse(raw);
    const since = Math.max(0, (Date.now() - updated) / 1000);
    d.age_s = Math.round(((+d.age_s || 0) + since) * 10) / 10;
    d.cloud = true;
    if (since > OFFLINE_AFTER_MS / 1000) { d.engine_on = false; d.pi_offline = true; }
    return JSON.stringify(d);
  } catch { return raw; }
}

async function getDoc(env, key) {
  const r = await env.DB.prepare("SELECT json, updated_ts FROM doc WHERE key=?").bind(key).first();
  return r ? { data: r.json, updated: r.updated_ts } : null;
}

const OFFLINE_AFTER_MS = 60_000;   // o Pi envia o estado ao vivo a cada 1 s (motor ligado) ou 10 s (desligado)

// estado ao vivo como o painel deve ver: se o Pi parou de enviar, o carro está desligado (ou sem internet)
function liveView(raw, meta) {
  try {
    const d = JSON.parse(raw);
    const last = Math.max(meta.last_sync_ts || 0, meta.live_updated || 0) || d.ts || 0;
    if (Date.now() - last > OFFLINE_AFTER_MS) {
      d.pi_last_seen = last;
      d.last_state = d.state;
      d.state = "pi_offline";
      d.engine_on = false;
      d.trip_id = null; d.trip_start = null; d.trip = null;
      d.values = {}; d.value_ts = {}; d.pedal_norm = null;          // não mostrar a última leitura como se fosse ao vivo
    }
    // idade pela hora em que a nuvem recebeu: o Pi não tem relógio próprio e fica minutos atrasado logo depois de ligar
    const seen = meta.live_updated || d.ts;
    if (seen) d.age_s = Math.max(0, Math.round((Date.now() - seen) / 100) / 10);
    d.cloud = true;
    return JSON.stringify(scrubLive(d));
  } catch { return raw; }
}

// estado ao vivo + "último sinal do Pi" numa consulta só
async function liveNow(env) {
  const r = await env.DB.prepare("SELECT key, json, updated_ts FROM doc WHERE key IN ('/api/live', '_meta')").all();
  const live = r.results.find((x) => x.key === "/api/live"), m = r.results.find((x) => x.key === "_meta");
  let mt = {}; try { mt = m ? JSON.parse(m.json) : {}; } catch { }
  if (m && mt.last_sync_ts) mt.last_sync_ts = m.updated_ts;
  mt.live_updated = live ? live.updated_ts : 0;
  return { stamp: `${live ? live.updated_ts : 0}:${Date.now() - Math.max(mt.last_sync_ts || 0, mt.live_updated) > OFFLINE_AFTER_MS}`,
           data: live ? liveView(live.json, mt) : JSON.stringify({ state: "pi_offline", engine_on: false, values: {}, alerts: [], dtc: {}, cloud: true }) };
}

// SSE de verdade: confere o banco 1x/s e envia quando o Pi mandou algo novo. Cada conexão dura ~40 s
// (limite de consultas por requisição no plano gratuito); o navegador reconecta sozinho em seguida.
function liveStream(env) {
  const te = new TextEncoder();
  let stop = false;
  const body = new ReadableStream({
    async start(c) {
      try {
        c.enqueue(te.encode("retry: 500\n\n"));
        let last = null;
        for (let i = 0; i < 40 && !stop; i++) {
          const n = await liveNow(env);
          if (n.stamp !== last) { last = n.stamp; c.enqueue(te.encode(`data: ${n.data}\n\n`)); }
          else if (i % 15 === 14) c.enqueue(te.encode(": ping\n\n"));
          await new Promise((r) => setTimeout(r, 1000));
        }
      } catch { }
      try { c.close(); } catch { }
    },
    cancel() { stop = true; },
  });
  return new Response(body, { headers: { "content-type": "text/event-stream", "cache-control": "no-store" } });
}

async function meta(env) {
  const m = await getDoc(env, "_meta");
  // "último sinal do Pi" = hora em que a nuvem recebeu, não a hora que o Pi informou (relógio dele atrasa após ligar)
  try { const o = m ? JSON.parse(m.data) : {}; if (o.last_sync_ts) o.last_sync_ts = m.updated; return o; } catch { return {}; }
}

async function envInfo(req, env) {
  const m = await meta(env);
  const p = await env.DB.prepare("SELECT COUNT(*) n FROM cmd WHERE done=0").first();
  const loc = !locConfigured(env) ? "desativada" : (await locAuthorized(req, env)) ? "aberta" : "trancada";
  return { cloud: true, readonly: !env.DASH_PASSWORD, last_sync_ts: m.last_sync_ts || null, pi_version: m.version || null,
           pi_online: !!m.last_sync_ts && Date.now() - m.last_sync_ts < OFFLINE_AFTER_MS, pending_cmds: p ? p.n : 0, loc };
}

// ---------------------------------------------------------------- sincronização (Pi → nuvem)
async function syncDocs(req, env) {
  const body = await req.json();
  const docs = Array.isArray(body.docs) ? body.docs : [];
  const now = Date.now();
  const loc = locConfigured(env);       // sem senha de localização: documentos de posição nem são guardados
  const stmts = docs.filter((d) => typeof d.key === "string" && (d.key.startsWith("/api/") || d.key === "_meta") && d.key.length < 512 && (loc || !isLoc(d.key)))
    .map((d) => env.DB.prepare("INSERT INTO doc(key, json, updated_ts) VALUES (?,?,?) ON CONFLICT(key) DO UPDATE SET json=excluded.json, updated_ts=excluded.updated_ts")
      .bind(d.key, typeof d.json === "string" ? d.json : JSON.stringify(d.json), now));
  for (let i = 0; i < stmts.length; i += 50) await env.DB.batch(stmts.slice(i, i + 50));
  if (Array.isArray(body.delete) && body.delete.length)
    await env.DB.batch(body.delete.map((k) => env.DB.prepare("DELETE FROM doc WHERE key=?").bind(k)));
  return json({ ok: true, stored: stmts.length, dropped: docs.length - stmts.length });
}

// Trajeto da viagem aberta (mapa do Ao vivo): o Pi manda tudo desde a saída quando a viagem começa ({trip, start_ts,
// pts}) e depois só os pontos novos a cada ~30 s ({trip, append}); {end: trip} apaga. Documento de localização: só
// guardado com LOC_PASSWORD e servido por isLoc (GET /api/gps/current_track só com o cookie da senha).
export const TRACK_KEY = "/api/gps/current_track";
export const TRACK_MAX = 3000;
const TRACK_BODY_MAX = 200_000;
function cleanPts(list) {
  const out = [];
  for (const p of Array.isArray(list) ? list : []) {
    if (!Array.isArray(p) || p.length < 3) continue;
    const [la, lo, ts] = p.map(Number);
    if (![la, lo, ts].every(Number.isFinite) || Math.abs(la) > 90 || Math.abs(lo) > 180 || ts <= 0) continue;
    if (out.length && ts <= out[out.length - 1][2]) continue;            // só para a frente no tempo
    out.push([Math.round(la * 1e5) / 1e5, Math.round(lo * 1e5) / 1e5, Math.round(ts)]);
  }
  return out;
}
function capPts(pts) {                                                   // metade dos pontos, mantendo a ponta atual
  while (pts.length > TRACK_MAX) { const last = pts[pts.length - 1]; pts = pts.filter((_, i) => i % 2 === 0); if (pts[pts.length - 1] !== last) pts.push(last); }
  return pts;
}
async function syncTrack(req, env) {
  const text = await req.text();
  if (!locConfigured(env)) return json({ ok: false, loc: false });    // sem senha de localização: nada é guardado
  if (text.length > TRACK_BODY_MAX) return json({ error: "grande demais" }, 413);
  let b; try { b = JSON.parse(text); } catch { return json({ error: "dados inválidos" }, 400); }
  if (!b || typeof b !== "object") return json({ error: "dados inválidos" }, 400);
  const put = (d) => env.DB.prepare("INSERT INTO doc(key, json, updated_ts) VALUES (?,?,?) ON CONFLICT(key) DO UPDATE SET json=excluded.json, updated_ts=excluded.updated_ts")
    .bind(TRACK_KEY, JSON.stringify(d), Date.now()).run();
  if (b.end !== undefined) {
    await env.DB.prepare("DELETE FROM doc WHERE key=?").bind(TRACK_KEY).run();
    return json({ ok: true, ended: true });
  }
  const trip = Number(b.trip);
  if (!Number.isInteger(trip) || trip <= 0) return json({ error: "viagem inválida" }, 400);
  if (Array.isArray(b.pts)) {                                            // viagem nova: substitui
    const st = Number(b.start_ts);
    await put({ trip, start_ts: Number.isFinite(st) && st > 0 ? st : null, pts: capPts(cleanPts(b.pts)) });
    return json({ ok: true, full: true });
  }
  if (!Array.isArray(b.append)) return json({ error: "dados inválidos" }, 400);
  const cur = await getDoc(env, TRACK_KEY);
  let d = null; try { d = cur ? JSON.parse(cur.data) : null; } catch { d = null; }
  if (!d || d.trip !== trip || !Array.isArray(d.pts)) return json({ ok: false, reset: true });   // Pi manda tudo de novo
  const last = d.pts.length ? d.pts[d.pts.length - 1][2] : 0;
  const add = cleanPts(b.append).filter((p) => p[2] > last);
  if (add.length) { d.pts = capPts(d.pts.concat(add)); await put(d); }
  return json({ ok: true, added: add.length });
}

// abastecimentos registrados no painel online que o Pi ainda não buscou: aparecem na hora no histórico, marcados
async function withPendingFills(env, raw) {
  try {
    const r = await env.DB.prepare("SELECT ts, body FROM cmd WHERE done=0 AND path='/api/fuel/fill' ORDER BY ts DESC").all();
    if (!r.results.length) return raw;
    const f = JSON.parse(raw);
    const last = (f.fills || [])[0];
    const pend = r.results.map((c) => { const b = JSON.parse(c.body || "{}");
      return { id: null, pending: true, ts: b.ts || c.ts, fuel: b.fuel || (last ? last.fuel : "gasolina"), total: b.total ?? null, liters: b.liters ?? null, price_l: b.price_l ?? null, full: 1 }; });
    f.fills = pend.concat(f.fills || []);
    return JSON.stringify(f);
  } catch { return raw; }
}

async function publicFill(req, env) {
  let b; try { b = JSON.parse((await req.text()).slice(0, 2000)); } catch { return json({ error: "dados inválidos" }, 400); }
  const num = (v, lo, hi) => { if (v === undefined || v === null || v === "") return null; const x = Number(String(v).replace(",", ".")); return Number.isFinite(x) && x >= lo && x <= hi ? x : NaN; };
  const total = num(b.total, 1, 5000), price = num(b.price_l, 0.5, 30), liters = num(b.liters, 0.5, 80);
  if (total === null || [total, price, liters].some(Number.isNaN)) return json({ error: "Informe o valor pago (R$)." }, 400);
  if (b.fuel !== undefined && !["gasolina", "etanol"].includes(b.fuel)) return json({ error: "combustível inválido" }, 400);
  const pend = await env.DB.prepare("SELECT COUNT(*) n, MAX(ts) last FROM cmd WHERE done=0 AND path='/api/fuel/fill'").first();
  if (pend && pend.n >= 5) return json({ error: "Já há abastecimentos esperando o Pi conectar. Tente depois." }, 429);
  if (pend && pend.last && Date.now() - pend.last < 20_000) return json({ error: "Abastecimento registrado há instantes; aguarde um pouco." }, 429);
  const dup = await env.DB.prepare("SELECT body FROM cmd WHERE done=0 AND path='/api/fuel/fill' AND ts > ?").bind(Date.now() - 3600_000).all();
  if (dup.results.some((c) => { try { return JSON.parse(c.body).total === total; } catch { return false; } }))
    return json({ ok: true, queued: true, duplicate: true });          // mesmo valor já registrado há menos de 1 h: não duplica
  const clean = { total, full: true, ts: Date.now() };
  if (price !== null) clean.price_l = price;
  if (liters !== null) clean.liters = liters;
  if (b.fuel) clean.fuel = b.fuel;
  await env.DB.prepare("INSERT INTO cmd(ts, method, path, body) VALUES (?,?,?,?)").bind(Date.now(), "POST", "/api/fuel/fill", JSON.stringify(clean)).run();
  return json({ ok: true, queued: true });
}

// Outras anotações que o dono faz pelo painel público: serviço feito, hodômetro, item de manutenção.
// Cada uma aceita só os campos esperados, com limites, e a fila tem teto. O Pi aplica quando conecta.
// Relatório do "Diagnóstico geral" feito pelo painel público: só texto e números, tudo com tamanho limitado e sem HTML
// (só <b>). O Pi valida de novo antes de gravar (analytics/checkup.py) e é ele quem escreve a linha do diário.
export function cleanCheckup(b) {
  if (!b || !Array.isArray(b.achados) || typeof b.rotulo !== "string" || !b.rotulo.trim()) return null;
  const s = (v, n) => String(v ?? "").replace(/<(?!\/?b>)[^>]*>/g, "").slice(0, n);
  const l = (v, n, z) => (Array.isArray(v) ? v : []).slice(0, n).map((x) => s(x, z));
  const m = {}; for (const [k, v] of Object.entries(b.m && typeof b.m === "object" ? b.m : {}).slice(0, 60)) if (typeof v === "number" || typeof v === "boolean") m[s(k, 24)] = v;
  const ts = Number(b.ts) > 9e11 && Number(b.ts) <= Date.now() + 864e5 ? Number(b.ts) : Date.now();
  return { ts, rotulo: s(b.rotulo, 80), resumo: s(b.resumo, 500), parcial: !!b.parcial, bons: l(b.bons, 30, 200), pulados: l(b.pulados, 30, 200), obs: l(b.obs, 6, 400), hist: l(b.hist, 12, 400), m,
    achados: b.achados.slice(0, 40).filter((a) => a && typeof a === "object").map((a) => ({ id: s(a.id, 40), sis: s(a.sis, 40), sev: Math.max(0, Math.min(3, Math.trunc(Number(a.sev)) || 0)),
      conf: s(a.conf, 8), acao: s(a.acao, 12), t: s(a.t, 200), pq: s(a.pq, 600), ev: l(a.ev, 6, 300), fz: s(a.fz, 500), teste: s(a.teste, 12) })) };
}
const PUBLIC_NOTES = {
  "/api/checkup": cleanCheckup,
  "/api/annotations": (b) => { const text = String(b.text || "").trim().slice(0, 200); if (text.length < 3) return null;
    return { text, kind: ["manutencao", "teste"].includes(b.kind) ? b.kind : "manutencao", ts: Date.now() }; },
  "/api/maintenance/odometer": (b) => { const km = Number(b.km); return km >= 1 && km <= 999999 ? { km, ts: Date.now() } : null; },
  "/api/maintenance/done": (b) => { if (!/^[a-z_]{3,20}$/.test(String(b.key || ""))) return null;
    const out = { key: b.key, ts: Number(b.ts) > 9e11 && Number(b.ts) <= Date.now() + 864e5 ? Number(b.ts) : Date.now() };
    const odo = Number(b.odo); if (b.odo !== "" && b.odo != null && odo >= 0 && odo <= 999999) out.odo = odo; return out; },
};
async function publicNote(req, env, path) {
  let b; try { b = JSON.parse((await req.text()).slice(0, path === "/api/checkup" ? 40000 : 2000)); } catch { return json({ error: "dados inválidos" }, 400); }
  const clean = PUBLIC_NOTES[path](b || {});
  if (!clean) return json({ error: "Preencha o campo corretamente." }, 400);
  const pend = await env.DB.prepare("SELECT COUNT(*) n FROM cmd WHERE done=0").first();
  if (pend && pend.n >= 30) return json({ error: "Há muitas ações esperando o Pi conectar. Tente depois." }, 429);
  if ((path === "/api/annotations" && clean.kind === "teste") || path === "/api/checkup") {            // resultados de teste: não deixa repetir em menos de 30 s
    const last = await env.DB.prepare("SELECT MAX(ts) t FROM cmd WHERE path=? AND done=0").bind(path).first();
    if (last && last.t && Date.now() - last.t < 30_000) return json({ ok: true, queued: true, duplicate: true });
  }
  await env.DB.prepare("INSERT INTO cmd(ts, method, path, body) VALUES (?,?,?,?)").bind(Date.now(), "POST", path, JSON.stringify(clean)).run();
  return json({ ok: true, queued: true });
}

async function syncCommands(req, env) {
  if (req.method === "GET") {
    const r = await env.DB.prepare("SELECT id, ts, method, path, body FROM cmd WHERE done=0 ORDER BY id LIMIT 50").all();
    // abastecimentos "só com o valor pago" esperam o Pi já atualizado (uma versão antiga gravaria sem o valor)
    const cap = new URL(req.url).searchParams.get("cap") || "";
    return json({ commands: r.results.filter((c) => (cap.includes("total") || !(c.path === "/api/fuel/fill" && /"total"/.test(c.body || "")))
      && (cap.includes("maint") || !c.path.startsWith("/api/maintenance/"))
      && (cap.includes("gps") || !isLoc(c.path))) });
  }
  const { ids = [], results = {} } = await req.json();
  if (ids.length)
    await env.DB.batch(ids.map((id) => env.DB.prepare("UPDATE cmd SET done=1, done_ts=?, result=? WHERE id=?")
      .bind(Date.now(), JSON.stringify(results[id] ?? null), id)));
  return json({ ok: true });
}

// ---------------------------------------------------------------- roteamento
// Canal ao vivo: o Pi envia cada leitura (POST /api/sync/live) e este objeto repassa na hora, por WebSocket,
// para quem está com o painel aberto. Sem banco no caminho: o atraso fica em frações de segundo.
// F9: o mesmo objeto atende o link de compartilhamento (ShareRoom, em share.js) com conexões marcadas "share",
// separadas das do painel ("live"): quem tem o link nunca recebe o estado ao vivo do painel, e o painel nunca recebe posição.
export class LiveHub {
  constructor(ctx, env) { this.ctx = ctx; this.env = env; this.last = null; this.lastAt = 0; this.room = new ShareRoom(ctx, env); }
  static respond101(client) { return new Response(null, { status: 101, webSocket: client }); }    // os testes trocam (Node não aceita 101)
  upgrade(setup) { const pair = new WebSocketPair(); setup(pair[1]); return LiveHub.respond101(pair[0]); }
  async fetch(req) {
    const url = new URL(req.url);
    if (url.pathname === "/push") {
      let live = await req.text(), share;
      if (req.headers.get("x-share") === "1") {               // {live: "<estado ao vivo>", share: {quadro do link}}
        try { const o = JSON.parse(live); live = typeof o.live === "string" ? o.live : ""; share = o.share; } catch { return new Response("dados inválidos", { status: 400 }); }
      }
      this.last = live; this.lastAt = Date.now();
      for (const ws of this.ctx.getWebSockets("live")) { try { ws.send(this.last); } catch { } }
      let on = false;
      try { on = share !== undefined ? await this.room.ingest(share) : (await this.room.load(), await this.room.refresh(), await this.room.checkSilent(), this.room.hasActive()); } catch { on = false; }
      return Response.json({ ok: true, share: on });
    }
    if (url.pathname.startsWith("/share-")) return this.room.handle(req, url, (setup) => this.upgrade(setup));
    if (req.headers.get("Upgrade") !== "websocket") return new Response("websocket esperado", { status: 426 });
    return this.upgrade((server) => {
      this.ctx.acceptWebSocket(server, ["live"]);            // hibernável: não cobra tempo parado
      if (this.last && Date.now() - this.lastAt < 15_000) server.send(this.last);
    });
  }
  webSocketMessage() { }
  webSocketClose(ws, code) { try { ws.close(code === 1005 ? 1000 : code, "fim"); } catch { } }
  webSocketError() { }
}
const hub = (env) => env.LIVE.get(env.LIVE.idFromName("civic"));

async function syncLive(req, env) {
  const body = req.headers.get("content-encoding") === "gzip" ? req.body.pipeThrough(new DecompressionStream("gzip")) : req.body;
  const text = await new Response(body).text();
  if (text.length > 60_000) return json({ error: "grande demais" }, 413);
  let out = text, share;
  if (/"(lat|lon|latitude|longitude|parked|dest|_share)"\s*:/.test(text)) {   // raro: só reescreve se aparecer algo suspeito
    try {
      const d = JSON.parse(text);
      // F9: o quadro do link de compartilhamento vem junto, em "_share"; sai daqui antes de o estado ir para o painel
      if (d && typeof d === "object" && "_share" in d) { share = d._share; delete d._share; }
      out = JSON.stringify(scrubLive(d));
    } catch { return json({ error: "dados inválidos" }, 400); }
  }
  const loc = locConfigured(env);
  if (!loc) share = undefined;                         // sem senha de localização: posição não entra nem no link
  const r = await hub(env).fetch("https://hub/push", share !== undefined
    ? { method: "POST", body: JSON.stringify({ live: out, share }), headers: { "x-share": "1" } } : { method: "POST", body: out });
  let on = false; try { on = !!(await r.json()).share; } catch { }
  return json({ ok: true, share: loc && on });         // share: há link ativo (o Pi manda a posição junto só então)
}

export default {
  async fetch(req, env) {
    const url = new URL(req.url);
    const p = url.pathname;

    // Pi
    if (p.startsWith("/api/sync/")) {
      if (!syncAuthorized(req, env)) return json({ error: "token inválido" }, 401);
      if (p === "/api/sync/docs" && req.method === "POST") return syncDocs(req, env);
      if (p === "/api/sync/live" && req.method === "POST") return syncLive(req, env);
      if (p === "/api/sync/commands") return syncCommands(req, env);
      if (p === "/api/sync/share" && req.method === "POST") return syncShare(req, env, hub(env), locConfigured(env));
      if (p === "/api/sync/track" && req.method === "POST") return syncTrack(req, env);
      // loc: a nuvem tem senha de localização; o Pi só envia posição se vier true. share: links ativos (F9)
      if (p === "/api/sync/ping") return json({ ok: true, ts: Date.now(), loc: locConfigured(env), ...(locConfigured(env) ? { share: await pingShare(env) } : {}) });
      return json({ error: "rota desconhecida" }, 404);
    }

    // login
    if (p === "/login") {
      if (req.method === "POST") {
        const form = await req.formData();
        if (env.DASH_PASSWORD && safeEqual(String(form.get("password") || ""), env.DASH_PASSWORD)) {
          return new Response(null, { status: 303, headers: {
            location: "/", "set-cookie": `obd_s=${await makeSession(env)}; Path=/; Max-Age=${SESSION_DAYS * 86400}; HttpOnly; Secure; SameSite=Lax` } });
        }
        await new Promise((r) => setTimeout(r, 800));   // freia tentativas de adivinhar a senha
        return loginPage("Senha incorreta.");
      }
      return loginPage();
    }
    if (p === "/logout")
      return new Response(null, { status: 303, headers: { location: "/login", "set-cookie": "obd_s=; Path=/; Max-Age=0; HttpOnly; Secure; SameSite=Lax" } });
    if (p === "/manifest.webmanifest" || p === "/favicon.ico") return env.ASSETS.fetch(req);

    // F9: link de compartilhamento — público, sem senha (nem a do painel); só a viagem atual enquanto o link vale
    if (p.startsWith("/s/")) return sharePage(req, env, url);
    if (p.startsWith("/api/share/")) return shareApi(req, env, hub(env), p.slice("/api/share/".length), locConfigured(env));
    if (SHARE_STATIC.has(p)) return env.ASSETS.fetch(req);             // o mapa da página do link (arquivos sem dados)

    if (env.DASH_PASSWORD && !(await validSession(req, env))) {
      if (p.startsWith("/api/")) return json({ error: "faça login" }, 401);
      return new Response(null, { status: 303, headers: { location: "/login" } });
    }

    // senha de localização (cookie próprio, 30 dias)
    if (p === "/loc/unlock" && req.method === "POST") return locUnlock(req, env);
    if (p === "/loc/lock" && req.method === "POST") return json({ ok: true, loc: locConfigured(env) ? "trancada" : "desativada" }, 200, { "set-cookie": LOC_COOKIE("", 0) });
    if (p.startsWith("/loc/")) return json({ error: "rota desconhecida" }, 404);

    // localização: antes de qualquer outra rota da API (falha fechada)
    if (isLoc(p)) {
      if (!(await locAuthorized(req, env))) return locLocked(env);
      if (/\/export\.(gpx|kml)$/.test(normPath(p))) return json({ error: "Exportação GPX/KML disponível no painel do Pi (rede local)." }, 404);
      const np = normPath(p);
      if (np === "/api/gps/share" || /^\/api\/gps\/share\/[0-9a-f]{8}$/.test(np)) return ownerShare(req, env, hub(env), np, url.origin);   // F9
      if (req.method === "POST") {
        if (LOC_QUEUE.some((re) => re.test(p))) return locQueue(req, env, p);
        return json({ error: "Faça isso no painel do Pi (rede local)." }, 409);
      }
      if (req.method !== "GET" && req.method !== "HEAD") return json({ error: "método não permitido" }, 405);
    }

    // páginas
    if (p === "/" || p === "/index.html") return env.ASSETS.fetch(new Request(new URL("/index.html", url), req));
    if (p === "/dirigir") return env.ASSETS.fetch(new Request(new URL("/drive.html", url), req));
    if (p === "/oficina") return env.ASSETS.fetch(new Request(new URL("/oficina.html", url), req));
    if (!p.startsWith("/api/")) return env.ASSETS.fetch(req);

    // API
    if (p === "/api/env") return json(await envInfo(req, env));
    if (p === "/api/stream") return liveStream(env);
    if (p === "/api/ws") return hub(env).fetch(req);
    if (req.method === "POST" || req.method === "DELETE") {
      // "Tanque cheio" funciona no painel público (pedido do dono): só esse formulário, validado e com limite
      if (!env.DASH_PASSWORD && p === "/api/fuel/fill" && req.method === "POST") return publicFill(req, env);
      if (!env.DASH_PASSWORD && req.method === "POST" && PUBLIC_NOTES[p]) return publicNote(req, env, p);
      if (env.DASH_PASSWORD && QUEUEABLE.some((re) => re.test(p))) {
        const body = req.method === "POST" ? await req.text() : "";
        if (body.length > 20000) return json({ error: "conteúdo grande demais" }, 413);
        await env.DB.prepare("INSERT INTO cmd(ts, method, path, body) VALUES (?,?,?,?)").bind(Date.now(), req.method, p, body).run();
        return json({ ok: true, queued: true });
      }
      return json({ error: "O painel online é só de visualização. Faça isso no painel do Pi (no carro, pelo hotspot: http://172.20.10.14)." }, 409);
    }
    if (p === "/api/export.csv") return json({ error: "Exportação CSV disponível no painel do Pi (rede local)." }, 404);
    const d = await getDoc(env, docKey(url));
    if (!d) return json({ error: "ainda não sincronizado pelo Pi" }, 404);
    if (p === "/api/fuel") d.data = await withPendingFills(env, d.data);
    if (p === "/api/gps/live") d.data = gpsLiveView(d.data, d.updated);
    return new Response(p === "/api/live" ? liveView(d.data, { ...(await meta(env)), live_updated: d.updated }) : d.data,
      { headers: { "content-type": "application/json; charset=utf-8", "cache-control": "no-store", "x-synced": String(d.updated) } });
  },
};
