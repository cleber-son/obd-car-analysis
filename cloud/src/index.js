// Civic OBD — painel online (Cloudflare Worker + D1).
//
// O Pi monta cada resposta da API e envia como "documento" (POST /api/sync/docs). Este Worker:
//   • protege tudo com senha (cookie de sessão assinado);
//   • serve o mesmo painel estático do Pi e responde GET /api/* com o documento correspondente;
//   • guarda ações feitas aqui (tanque cheio, anotações, editar viagem, pedir análise) numa fila que o Pi aplica ao conectar.
//
// Segredos (wrangler secret put): DASH_PASSWORD, SYNC_TOKEN, SESSION_SECRET.

const KEEP = {
  "/api/series": ["signals", "span"], "/api/alerts": ["days", "severity"], "/api/report": ["days"],
  "/api/report/period": ["kind", "offset"], "/api/summary": ["days"], "/api/throttle": ["days"],
  "/api/diagnostics": ["days"], "/api/trips": ["limit"], "/api/advice": ["rule"],
};
const QUEUEABLE = [/^\/api\/fuel\/fill$/, /^\/api\/annotations$/, /^\/api\/trips\/\d+$/, /^\/api\/ai\/analyze$/];
const SESSION_DAYS = 30;
const enc = new TextEncoder();

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
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover"><title>Civic OBD · Entrar</title>
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
<form method="post" action="/login"><div class="logo"><i></i></div><h1>Civic 1.8 Flex</h1>
<p>Painel online do OBD. Digite a senha para ver o histórico.</p>
<input type="password" name="password" placeholder="Senha" autocomplete="current-password" autofocus required aria-label="Senha">
${msg ? `<div class="err">${msg}</div>` : ""}<button type="submit">Entrar</button></form></body></html>`,
    { status: msg ? 401 : 200, headers: { "content-type": "text/html; charset=utf-8", "cache-control": "no-store" } });
}

async function getDoc(env, key) {
  const r = await env.DB.prepare("SELECT json, updated_ts FROM doc WHERE key=?").bind(key).first();
  return r ? { data: r.json, updated: r.updated_ts } : null;
}

function withAge(raw) {
  try {
    const d = JSON.parse(raw);
    if (d && d.ts) d.age_s = Math.round((Date.now() - d.ts) / 100) / 10;
    return JSON.stringify(d);
  } catch { return raw; }
}

async function envInfo(env) {
  const m = await getDoc(env, "_meta");
  const meta = m ? JSON.parse(m.data) : {};
  const p = await env.DB.prepare("SELECT COUNT(*) n FROM cmd WHERE done=0").first();
  return { cloud: true, last_sync_ts: meta.last_sync_ts || null, pi_version: meta.version || null, pending_cmds: p ? p.n : 0 };
}

// ---------------------------------------------------------------- sincronização (Pi → nuvem)
async function syncDocs(req, env) {
  const body = await req.json();
  const docs = Array.isArray(body.docs) ? body.docs : [];
  const now = Date.now();
  const stmts = docs.filter((d) => typeof d.key === "string" && (d.key.startsWith("/api/") || d.key === "_meta") && d.key.length < 512)
    .map((d) => env.DB.prepare("INSERT INTO doc(key, json, updated_ts) VALUES (?,?,?) ON CONFLICT(key) DO UPDATE SET json=excluded.json, updated_ts=excluded.updated_ts")
      .bind(d.key, typeof d.json === "string" ? d.json : JSON.stringify(d.json), now));
  for (let i = 0; i < stmts.length; i += 50) await env.DB.batch(stmts.slice(i, i + 50));
  if (Array.isArray(body.delete) && body.delete.length)
    await env.DB.batch(body.delete.map((k) => env.DB.prepare("DELETE FROM doc WHERE key=?").bind(k)));
  return json({ ok: true, stored: stmts.length });
}

async function syncCommands(req, env) {
  if (req.method === "GET") {
    const r = await env.DB.prepare("SELECT id, ts, method, path, body FROM cmd WHERE done=0 ORDER BY id LIMIT 50").all();
    return json({ commands: r.results });
  }
  const { ids = [], results = {} } = await req.json();
  if (ids.length)
    await env.DB.batch(ids.map((id) => env.DB.prepare("UPDATE cmd SET done=1, done_ts=?, result=? WHERE id=?")
      .bind(Date.now(), JSON.stringify(results[id] ?? null), id)));
  return json({ ok: true });
}

// ---------------------------------------------------------------- roteamento
export default {
  async fetch(req, env) {
    const url = new URL(req.url);
    const p = url.pathname;

    // Pi
    if (p.startsWith("/api/sync/")) {
      if (!syncAuthorized(req, env)) return json({ error: "token inválido" }, 401);
      if (p === "/api/sync/docs" && req.method === "POST") return syncDocs(req, env);
      if (p === "/api/sync/commands") return syncCommands(req, env);
      if (p === "/api/sync/ping") return json({ ok: true, ts: Date.now() });
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

    if (!(await validSession(req, env))) {
      if (p.startsWith("/api/")) return json({ error: "faça login" }, 401);
      return new Response(null, { status: 303, headers: { location: "/login" } });
    }

    // páginas
    if (p === "/" || p === "/index.html") return env.ASSETS.fetch(new Request(new URL("/index.html", url), req));
    if (p === "/dirigir") return env.ASSETS.fetch(new Request(new URL("/drive.html", url), req));
    if (!p.startsWith("/api/")) return env.ASSETS.fetch(req);

    // API
    if (p === "/api/env") return json(await envInfo(env));
    if (p === "/api/stream") {
      // um evento por conexão; o EventSource do navegador reconecta sozinho (retry 5 s)
      const d = await getDoc(env, "/api/live");
      const data = d ? withAge(d.data) : JSON.stringify({ state: "sem_dados", values: {}, alerts: [], dtc: {} });
      return new Response(`retry: 5000\ndata: ${data}\n\n`, { headers: { "content-type": "text/event-stream", "cache-control": "no-store" } });
    }
    if (req.method === "POST" || req.method === "DELETE") {
      if (QUEUEABLE.some((re) => re.test(p))) {
        const body = req.method === "POST" ? await req.text() : "";
        if (body.length > 20000) return json({ error: "conteúdo grande demais" }, 413);
        await env.DB.prepare("INSERT INTO cmd(ts, method, path, body) VALUES (?,?,?,?)").bind(Date.now(), req.method, p, body).run();
        return json({ ok: true, queued: true });
      }
      return json({ error: "Disponível só no painel do Pi (rede local)." }, 409);
    }
    if (p === "/api/export.csv") return json({ error: "Exportação CSV disponível no painel do Pi (rede local)." }, 404);
    const d = await getDoc(env, docKey(url));
    if (!d) return json({ error: "ainda não sincronizado pelo Pi" }, 404);
    return new Response(p === "/api/live" ? withAge(d.data) : d.data,
      { headers: { "content-type": "application/json; charset=utf-8", "cache-control": "no-store", "x-synced": String(d.updated) } });
  },
};
