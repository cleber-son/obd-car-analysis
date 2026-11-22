// Delfos — compartilhar a viagem ao vivo (docs/GPS-ARQUITETURA.md, seção 9.21; F9).
//
// Quem recebe o link /s/<token> vê só a viagem em andamento: o carro andando no mapa, o trajeto desta viagem,
// velocidade, rotação, temperatura, consumo, tempo e km da viagem, o título de um alerta ativo e a chegada prevista
// ("chegada prevista 14:35"; o nome do destino só se o dono marcou). Nada de lugares, estacionamento, histórico,
// outras viagens nem o resto do painel. Sem senha e sem instalar nada.
//
// Segurança (falha fechada):
//   • token = 16 bytes aleatórios (base64url, 22 caracteres); só o SHA-256 fica no D1 (tabela share);
//   • cada pedido confere no D1: existe, não encerrado, não vencido (senão 404/410 e nada de dados);
//   • validade de 24 h ou "até eu cancelar" (decisão do dono: sem prazo, vale até ser encerrado no painel). O código
//     ainda aceita 1 h, 4 h e "até o fim da viagem" pela API, mas a tela não oferece;
//   • entre uma viagem e outra (motor desligado), a página mostra só "Carro desligado · última atualização HH:MM",
//     sem posição nem local de estacionamento; na viagem seguinte volta o trajeto dela e os dados ao vivo;
//   • o canal ao vivo do link é separado do painel: o Durable Object guarda a última posição e o trajeto da viagem
//     só enquanto há link ativo, apaga tudo quando a viagem termina e confere a validade de cada conexão a cada envio;
//   • depois de 90 s sem sinal do carro, nenhuma posição é mostrada (não revela onde o carro parou).
//
// Precisa da senha de localização (LOC_PASSWORD) na nuvem: sem ela o Pi nem envia posição e o Worker recusa criar.

export const SHARE_TOKEN_RE = /^[A-Za-z0-9_-]{22}$/;
export const SHARE_ID_RE = /^[0-9a-f]{8}$/;
const HASH_RE = /^[0-9a-f]{64}$/;
export const SHARE_MAX_MS = 24 * 3600e3;
export const FOREVER_TS = 253402300799000;      // "até eu cancelar": vence em 31/12/9999 (só com opts.m = "sempre")
const MIN_MS = 5 * 60e3;
export const SHARE_ACTIVE_MAX = 5;
export const SHARE_CREATE_PER_H = 10;
const MODES = { "24h": 1440, "1h": 60, "4h": 240, viagem: null, sempre: null };
const LABEL_MAX = 40;
export const SHARE_STALE_MS = 90_000;           // sem quadro do carro há 90 s: não mostra posição nem trajeto
export const SHARE_SILENT_END_MS = 15 * 60e3;   // "até o fim da viagem": 15 min sem sinal = a viagem acabou (Pi desligou)
const TRACK_STEP_M = 20, TRACK_MAX = 3000;
const POLL_PER_MIN = 60, WS_PER_MIN = 20, WS_PER_LINK = 10, WS_TOTAL = 50;
const LINKS_TTL_MS = 30_000;
const BODY_MAX = 1000, SYNC_BODY_MAX = 400_000;
export const CLOSE_GONE = 4410;

const enc = new TextEncoder();
const json = (data, status = 200, headers = {}) =>
  new Response(JSON.stringify(data), { status, headers: { "content-type": "application/json; charset=utf-8", "cache-control": "no-store", ...headers } });

export async function sha256hex(s) {
  const d = await crypto.subtle.digest("SHA-256", enc.encode(String(s)));
  return [...new Uint8Array(d)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

export function newToken() {
  const b = crypto.getRandomValues(new Uint8Array(16));
  return btoa(String.fromCharCode(...b)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

export function cleanLabel(s) {
  return String(s ?? "").replace(/[\x00-\x1f\x7f<>]/g, "").trim().slice(0, LABEL_MAX);
}

// {mode: "1h"|"4h"|"viagem"} ou {minutes}; {label?, dest?} → {mode, ms, label, dest} ou {error}
export function parseCreate(b) {
  b = b && typeof b === "object" ? b : {};
  const mode = String(b.mode || "").toLowerCase();
  let ms;
  if (mode === "sempre") return { mode: "sempre", ms: null, label: cleanLabel(b.label), dest: b.dest === true };
  if (Object.prototype.hasOwnProperty.call(MODES, mode)) ms = (MODES[mode] ?? 1440) * 60e3;
  else if (b.minutes !== undefined && b.minutes !== null && b.minutes !== "") {
    const m = Math.floor(Number(b.minutes));
    if (!Number.isFinite(m) || m * 60e3 < MIN_MS || m * 60e3 > SHARE_MAX_MS) return { error: "Validade de 5 min a 24 h." };
    ms = m * 60e3;
  } else return { error: "Escolha a validade: 24 horas ou até você cancelar." };
  return { mode: mode === "viagem" ? "viagem" : "tempo", ms: Math.min(ms, SHARE_MAX_MS), label: cleanLabel(b.label), dest: b.dest === true };
}

const num = (v, lo, hi, nd = 1) => {
  if (v === null || v === undefined || v === "" || typeof v === "boolean") return null;
  const x = Number(v);
  if (!Number.isFinite(x) || x < lo || x > hi) return null;
  const k = 10 ** nd;
  return Math.round(x * k) / k;
};
const str = (v, n) => (typeof v === "string" && v ? cleanLabel(v).slice(0, n) || null : null);

// Quadro que o Pi manda (seção 9.21): lista fechada de campos, cada um conferido. Qualquer outra chave é descartada.
export function cleanFrame(f, keepDest = false) {
  if (!f || typeof f !== "object") return null;
  const trip = num(f.trip, 1, 1e12, 0);
  const out = { ts: num(f.ts, 0, 1e14, 0), trip };
  if (trip === null) return out;
  const lat = num(f.lat, -90, 90, 5), lon = num(f.lon, -180, 180, 5);
  if (lat !== null && lon !== null) {
    out.lat = lat; out.lon = lon;
    out.hdg = num(f.hdg, 0, 360, 0); out.gspd = num(f.gspd, 0, 400, 1); out.fix_ts = num(f.fix_ts, 0, 1e14, 0);
  }
  out.on = f.on === true;
  out.spd = num(f.spd, 0, 400, 0); out.rpm = num(f.rpm, 0, 12000, 0); out.ect = num(f.ect, -40, 200, 0);
  out.ect_st = ["ok", "warn", "crit"].includes(f.ect_st) ? f.ect_st : null;
  out.kml = num(f.kml, 0, 100, 1); out.lh = num(f.lh, 0, 100, 2);
  out.trip_s = num(f.trip_s, 0, 7 * 86400, 0); out.trip_km = num(f.trip_km, 0, 5000, 1); out.trip_kml = num(f.trip_kml, 0, 100, 1);
  out.alert = str(f.alert, 60); out.alert_sev = out.alert && ["warn", "crit"].includes(f.alert_sev) ? f.alert_sev : null;
  out.eta = num(f.eta, 0, 1e14, 0); out.left_km = num(f.left_km, 0, 5000, 1);
  if (keepDest && out.eta !== null) out.dest = str(f.dest, LABEL_MAX);
  return out;
}

function haversine(a, b, c, d) {
  const R = 6371000, r = Math.PI / 180, dl = (c - a) * r, dn = (d - b) * r;
  const x = Math.sin(dl / 2) ** 2 + Math.cos(a * r) * Math.cos(c * r) * Math.sin(dn / 2) ** 2;
  return 2 * R * Math.asin(Math.sqrt(x));
}

// ---------------------------------------------------------------- D1 (tabela share + coluna opts da migração da F9)
const COLS = "token_hash, created_ts, expires_ts, label, revoked, views, last_view_ts, opts";
const modeOf = (o) => (o.m === "viagem" || o.m === "sempre" ? o.m : "tempo");
const optsOf = (row) => { try { const o = JSON.parse(row.opts || "{}"); return o && typeof o === "object" ? o : {}; } catch { return {}; } };

export async function getShare(env, hash) {
  return env.DB.prepare(`SELECT ${COLS} FROM share WHERE token_hash=?`).bind(hash).first();
}
async function activeRows(env, now) {
  return (await env.DB.prepare(`SELECT ${COLS} FROM share WHERE revoked=0 AND expires_ts>?`).bind(now).all()).results || [];
}

// estado de um link no D1: válido, ou 404/410 (falha fechada: qualquer coisa estranha = encerrado)
export function linkState(row, now = Date.now()) {
  if (!row) return { gone: true, status: 404, reason: "nao_encontrado" };
  if (row.revoked) return { gone: true, status: 410, reason: row.revoked === 2 ? "viagem_terminou" : "encerrado" };
  const o = optsOf(row);
  // sem prazo só com as duas marcas juntas (modo "sempre" e a data-sentinela); qualquer outra coisa segue o teto de 24 h
  const forever = o.m === "sempre" && row.expires_ts === FOREVER_TS;
  if (!(row.created_ts > 0) || (!forever && (!(row.expires_ts > now) || row.expires_ts - row.created_ts > SHARE_MAX_MS + 60e3))) return { gone: true, status: 410, reason: "vencido" };
  return { ok: true, opts: o };
}

const view = (row, now) => {
  const o = optsOf(row), st = linkState(row, now);
  return { id: row.token_hash.slice(0, 8), created_ts: row.created_ts, expires_ts: o.m === "sempre" ? null : row.expires_ts, mode: modeOf(o),
           label: row.label || "", dest: !!o.dest, src: o.src === "pi" ? "pi" : "nuvem", views: row.views || 0, last_view_ts: row.last_view_ts || null,
           state: st.ok ? (o.m === "viagem" && o.trip == null ? "aguardando a viagem" : "ativo") : st.reason === "vencido" ? "vencido" : st.reason === "viagem_terminou" ? "viagem terminou" : "encerrado" };
};

// links ativos (inclusive os sem prazo, de qualquer data) + os criados nas últimas 24 h
export async function listShares(env, now = Date.now()) {
  const r = await env.DB.prepare(`SELECT ${COLS} FROM share WHERE created_ts>? ORDER BY created_ts DESC`).bind(now - 86400e3).all();
  const rows = new Map((r.results || []).map((x) => [x.token_hash, x]));
  for (const x of await activeRows(env, now)) if (!rows.has(x.token_hash)) rows.set(x.token_hash, x);
  return [...rows.values()].sort((a, b) => b.created_ts - a.created_ts).map((x) => view(x, now));
}

async function insertShare(env, hash, now, p, src, trip) {
  const opts = JSON.stringify({ m: p.mode, trip: p.mode === "viagem" ? trip ?? null : null, dest: p.dest ? 1 : 0, src });
  await env.DB.prepare("INSERT OR IGNORE INTO share(token_hash, created_ts, expires_ts, label, revoked, views, opts) VALUES (?,?,?,?,0,0,?)")
    .bind(hash, now, p.mode === "sempre" ? FOREVER_TS : now + p.ms, p.label || null, opts).run();
}

// limites de criação: no máximo 5 ativos e 10 criados por hora; apaga os vencidos há mais de 7 dias
async function canCreate(env, now) {
  await env.DB.prepare("DELETE FROM share WHERE expires_ts<?").bind(now - 7 * 86400e3).run();
  const act = await env.DB.prepare("SELECT COUNT(*) n FROM share WHERE revoked=0 AND expires_ts>?").bind(now).first();
  if (act && act.n >= SHARE_ACTIVE_MAX) return `Já há ${SHARE_ACTIVE_MAX} links ativos; encerre um antes.`;
  const hr = await env.DB.prepare("SELECT COUNT(*) n FROM share WHERE created_ts>?").bind(now - 3600e3).first();
  if (hr && hr.n >= SHARE_CREATE_PER_H) return "Muitos links criados na última hora. Tente mais tarde.";
  return null;
}

async function revokeById(env, id) {
  await env.DB.prepare("UPDATE share SET revoked=1 WHERE substr(token_hash,1,8)=? AND revoked=0").bind(id).run();
}

// viagem atual (para "até o fim da viagem" criado no painel online): o canal ao vivo, ou a cópia do estado se recente
async function currentTrip(env, hubStub) {
  try { const r = await (await hubStub.fetch("https://hub/share-info")).json(); if (r.trip) return r.trip; } catch { }
  try {
    const d = await env.DB.prepare("SELECT json, updated_ts FROM doc WHERE key=?").bind("/api/live").first();
    if (d && Date.now() - d.updated_ts < 60_000) { const t = JSON.parse(d.json).trip_id; if (Number.isInteger(t) && t > 0) return t; }
  } catch { }
  return null;
}

const reload = (hubStub) => hubStub.fetch("https://hub/share-reload", { method: "POST" }).catch(() => null);

async function readBody(req, max) {
  const t = await req.text();
  if (t.length > max) return { error: json({ error: "conteúdo grande demais" }, 413) };
  try { return { body: t ? JSON.parse(t) : {} }; } catch { return { error: json({ error: "dados inválidos" }, 400) }; }
}

// ---------------------------------------------------------------- dono (painel online, só com a localização liberada)
// GET /api/gps/share · POST /api/gps/share {mode, label?, dest?} · POST /api/gps/share/<id> {revoke: true}
// Quem chama já passou por isLoc + locAuthorized no index.js.
export async function ownerShare(req, env, hubStub, path, origin) {
  const now = Date.now();
  try {
    if (path === "/api/gps/share") {
      if (req.method === "GET" || req.method === "HEAD") return json({ links: await listShares(env, now), max: SHARE_ACTIVE_MAX });
      if (req.method !== "POST") return json({ error: "método não permitido" }, 405);
      const { body, error } = await readBody(req, BODY_MAX);
      if (error) return error;
      const p = parseCreate(body);
      if (p.error) return json({ error: p.error }, 400);
      const lim = await canCreate(env, now);
      if (lim) return json({ error: lim }, 429);
      const token = newToken(), hash = await sha256hex(token);
      await insertShare(env, hash, now, p, "nuvem", p.mode === "viagem" ? await currentTrip(env, hubStub) : null);
      await reload(hubStub);
      return json({ ok: true, url: `${origin}/s/${token}`, id: hash.slice(0, 8), expires_ts: p.mode === "sempre" ? null : now + p.ms, mode: p.mode });
    }
    const m = path.match(/^\/api\/gps\/share\/([0-9a-f]{8})$/);
    if (m) {
      if (req.method !== "POST") return json({ error: "método não permitido" }, 405);
      const { body, error } = await readBody(req, BODY_MAX);
      if (error) return error;
      if (body.revoke !== true) return json({ error: "nada a fazer" }, 400);
      await revokeById(env, m[1]);
      await reload(hubStub);
      return json({ ok: true });
    }
    return json({ error: "rota desconhecida" }, 404);
  } catch (e) {
    return json({ error: "Compartilhamento indisponível (a tabela share precisa da migração da F9).", detail: String(e && e.message || e).slice(0, 120) }, 503);
  }
}

// ---------------------------------------------------------------- Pi (POST /api/sync/share, com o SYNC_TOKEN)
// {create: [{hash, created_ts, expires_ts, mode, label, dest, trip}], revoke: [id], trip_end: N, track: {trip, pts}}
export async function syncShare(req, env, hubStub, locOk) {
  if (!locOk) return json({ error: "Localização desativada na nuvem (sem LOC_PASSWORD): nada de compartilhamento.", loc: false }, 409);
  const { body, error } = await readBody(req, SYNC_BODY_MAX);
  if (error) return error;
  const now = Date.now(), res = { ok: true, created: [], refused: [] };
  try {
    for (const c of Array.isArray(body.create) ? body.create.slice(0, SHARE_ACTIVE_MAX) : []) {
      const h = String(c && c.hash || "");
      // o relógio do Pi pode estar errado (sem RTC): vale a hora da nuvem e a duração pedida
      const forever = c && c.mode === "sempre";
      const ms = forever ? null : Number(c && c.expires_ts) - Number(c && c.created_ts);
      if (!HASH_RE.test(h) || (!forever && (!Number.isFinite(ms) || ms < MIN_MS || ms > SHARE_MAX_MS + 60e3))) { res.refused.push(h.slice(0, 8)); continue; }
      const lim = await canCreate(env, now);
      if (lim) { res.refused.push(h.slice(0, 8)); res.error = lim; continue; }
      const trip = Number.isInteger(c.trip) && c.trip > 0 ? c.trip : null;
      await insertShare(env, h, now, { mode: forever ? "sempre" : c.mode === "viagem" ? "viagem" : "tempo", ms: forever ? null : Math.min(ms, SHARE_MAX_MS),
        label: cleanLabel(c.label), dest: c.dest === true }, "pi", trip);
      res.created.push(h.slice(0, 8));
    }
    for (const id of Array.isArray(body.revoke) ? body.revoke.slice(0, 20) : []) if (SHARE_ID_RE.test(String(id))) await revokeById(env, String(id));
    const extra = {};
    if (Number.isInteger(body.trip_end) && body.trip_end > 0) extra.trip_end = body.trip_end;
    if (body.track && typeof body.track === "object" && Number.isInteger(body.track.trip) && Array.isArray(body.track.pts)) extra.track = body.track;
    await hubStub.fetch("https://hub/share-reload", { method: "POST", body: JSON.stringify(extra) });
    res.links = await listShares(env, now);
    return json(res);
  } catch (e) {
    return json({ error: "Compartilhamento indisponível (a tabela share precisa da migração da F9).", detail: String(e && e.message || e).slice(0, 120) }, 503);
  }
}

// resumo para o ping do Pi: os links ativos (sem hash inteiro, sem token)
export async function pingShare(env) {
  try { return { links: (await listShares(env)).filter((x) => x.state === "ativo" || x.state === "aguardando a viagem") }; }
  catch { return { links: [], error: "migração da F9 pendente" }; }
}

// ---------------------------------------------------------------- quem recebe o link (público, sem senha)
const PAGE_HEADERS = (host) => ({
  "content-type": "text/html; charset=utf-8", "cache-control": "no-store", "x-robots-tag": "noindex, nofollow",
  "referrer-policy": "strict-origin", "x-content-type-options": "nosniff", "x-frame-options": "DENY",
  "content-security-policy": `default-src 'none'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data: https://tile.openstreetmap.org; connect-src 'self' wss://${host}; base-uri 'none'; form-action 'none'; frame-ancestors 'none'`,
});

export async function sharePage(req, env, url) {
  const a = await env.ASSETS.fetch(new Request(new URL("/share.html", url), { method: "GET" }));
  return new Response(a.body, { status: a.ok ? 200 : 404, headers: PAGE_HEADERS(url.host) });
}

// GET /api/share/<token> (consulta; reserva do WebSocket) e GET /api/share/<token>/ws
export async function shareApi(req, env, hubStub, rest, locOk) {
  const m = rest.match(/^([A-Za-z0-9_-]+)(\/ws)?$/);
  const gone = (status, reason) => json({ error: status === 404 ? "Link não encontrado." : "Este link foi encerrado.", gone: true, reason }, status);
  if (!m || !SHARE_TOKEN_RE.test(m[1])) return gone(404, "nao_encontrado");         // nem consulta o banco
  if (!locOk) return gone(410, "encerrado");             // a senha de localização saiu da nuvem: nenhum link vale mais
  if (req.method !== "GET") return json({ error: "método não permitido" }, 405);
  const hash = await sha256hex(m[1]);
  let row;
  try { row = await getShare(env, hash); } catch { return json({ error: "Indisponível agora." }, 503); }
  const st = linkState(row);
  if (st.gone) return gone(st.status, st.reason);
  const q = new URLSearchParams({ h: hash, exp: String(row.expires_ts), dest: st.opts.dest ? "1" : "0", label: row.label || "", mode: modeOf(st.opts) });
  if (m[2]) {
    if (req.headers.get("Upgrade") !== "websocket") return json({ error: "websocket esperado" }, 426);
    return hubStub.fetch(new Request("https://hub/share-ws?" + q, req));
  }
  const sp = new URL(req.url).searchParams;
  for (const k of ["since", "v"]) if (sp.get(k) !== null) q.set(k, String(Math.max(0, Math.floor(Number(sp.get(k)) || 0))));
  const r = await hubStub.fetch("https://hub/share-state?" + q);
  return new Response(r.body, { status: r.status, headers: { "content-type": "application/json; charset=utf-8", "cache-control": "no-store", "x-robots-tag": "noindex" } });
}

// ---------------------------------------------------------------- dentro do Durable Object (LiveHub)
// Última posição e trajeto da viagem atual, só enquanto houver link ativo; conexões de cada link com a validade.
export class ShareRoom {
  constructor(ctx, env, nowFn = () => Date.now()) {
    this.ctx = ctx; this.env = env; this.now = nowFn;
    this.links = new Map(); this.linksAt = 0;
    this.frame = null; this.frameAt = 0; this.track = { trip: null, pts: [], v: 0 };
    this.lastSeen = 0;                                   // hora do último quadro (só a hora: "última atualização HH:MM")
    this.loaded = false; this.savedAt = 0; this.rates = new Map(); this.viewAt = new Map();
  }

  async load() {
    if (this.loaded) return;
    this.loaded = true;
    try {
      const s = await this.ctx.storage.get("share");
      if (s && typeof s === "object") { this.frame = s.frame || null; this.frameAt = s.frameAt || 0; this.track = s.track || { trip: null, pts: [], v: 0 }; this.lastSeen = s.lastSeen || 0; }
    } catch { }
  }

  async refresh(force = false) {
    const now = this.now();
    if (!force && now - this.linksAt < LINKS_TTL_MS) return;
    this.linksAt = now;
    const m = new Map();
    try {
      for (const r of await activeRows(this.env, now)) {
        const o = optsOf(r);
        if (linkState(r, now).ok) m.set(r.token_hash, { exp: r.expires_ts, m: o.m === "viagem" ? "viagem" : "tempo", trip: o.trip ?? null, dest: !!o.dest, opts: o });
      }
    } catch { /* sem a tabela (migração pendente) ou D1 fora: nenhum link vale */ }
    this.links = m;
  }

  valid(h, now = this.now()) { const l = this.links.get(h); return !!l && l.exp > now; }
  hasActive(now = this.now()) { for (const l of this.links.values()) if (l.exp > now) return true; return false; }
  wantDest() { for (const l of this.links.values()) if (l.dest) return true; return false; }

  rate(key, max) {
    const now = this.now(), r = this.rates.get(key);
    if (!r || now - r.t >= 60e3) { this.rates.set(key, { t: now, n: 1 }); if (this.rates.size > 500) this.rates.clear(); return true; }
    return ++r.n <= max;
  }

  // esquece posição e trajeto; keepSeen guarda só a hora do último sinal (sem posição) para "Carro desligado · HH:MM"
  async wipe(keepSeen = false) {
    this.lastSeen = keepSeen ? Math.max(this.lastSeen || 0, this.frameAt || 0) : 0;
    this.frame = null; this.frameAt = 0; this.track = { trip: null, pts: [], v: (this.track.v || 0) + 1 };
    try { if (keepSeen && this.lastSeen) await this.ctx.storage.put("share", { lastSeen: this.lastSeen }); else await this.ctx.storage.delete("share"); } catch { }
  }

  async save(force = false) {
    const now = this.now();
    if (!force && now - this.savedAt < 10_000) return;
    this.savedAt = now;
    try { await this.ctx.storage.put("share", { frame: this.frame, frameAt: this.frameAt, track: this.track, lastSeen: this.lastSeen }); } catch { }
  }

  async setOpts(h, l) {
    try { await this.env.DB.prepare("UPDATE share SET opts=? WHERE token_hash=?").bind(JSON.stringify({ ...l.opts, trip: l.trip }), h).run(); } catch { }
  }
  async revoke(h, code) {
    this.links.delete(h);
    try { await this.env.DB.prepare("UPDATE share SET revoked=? WHERE token_hash=? AND revoked=0").bind(code, h).run(); } catch { }
  }

  // a viagem T terminou (o Pi avisou, outra viagem começou, ou 15 min sem sinal): encerra os links "até o fim da
  // viagem" dela e apaga a posição e o trajeto (não revela onde o carro parou)
  async tripEnded(T) {
    for (const [h, l] of [...this.links]) if (l.m === "viagem" && l.trip != null && (T == null || l.trip === T)) await this.revoke(h, 2);
    await this.wipe(true);
  }

  async checkSilent() {
    const now = this.now();
    if (this.frameAt && now - this.frameAt > SHARE_SILENT_END_MS) await this.tripEnded(this.track.trip ?? this.frame?.trip ?? null);
  }

  // quadro novo do Pi (junto com a leitura ao vivo). Devolve se ainda há link ativo (o Pi para de mandar posição se não).
  async ingest(raw) {
    await this.load(); await this.refresh();
    if (!this.hasActive()) {                                     // nenhum link vale: esquece tudo e fecha as conexões que sobraram
      if (this.frame || this.track.pts.length) await this.wipe();
      this.broadcastState();
      return false;
    }
    await this.checkSilent();
    const f = cleanFrame(raw, this.wantDest());
    if (!f) return this.hasActive();
    const now = this.now();
    let changed = false;
    if (f.trip === null) {                                        // sem viagem aberta: nada de posição
      if (this.track.trip != null || this.frame) { await this.tripEnded(this.track.trip ?? this.frame?.trip ?? null); changed = true; }
    } else {
      if (this.track.trip !== f.trip) {
        if (this.track.trip != null) await this.tripEnded(this.track.trip);
        this.track = { trip: f.trip, pts: [], v: (this.track.v || 0) + 1 }; changed = true;
      }
      for (const [h, l] of [...this.links]) {
        if (l.m !== "viagem") continue;
        if (l.trip == null) { l.trip = f.trip; await this.setOpts(h, l); }          // criado antes de a viagem começar
        else if (l.trip !== f.trip) await this.revoke(h, 2);                          // viagem antiga que acabou sem aviso
      }
      this.frame = f; this.frameAt = now; this.lastSeen = now;
      let pt = null;
      if (f.lat != null) {
        const p = this.track.pts, last = p[p.length - 1];
        if (!last || haversine(last[0], last[1], f.lat, f.lon) >= TRACK_STEP_M) {
          p.push([f.lat, f.lon, f.ts || now]); pt = [f.lat, f.lon];
          if (p.length > TRACK_MAX) {                               // metade dos pontos (mantendo a ponta); os aparelhos recebem tudo de novo
            const lp = p[p.length - 1]; this.track.pts = p.filter((_, i) => i % 2 === 0);
            if (this.track.pts[this.track.pts.length - 1] !== lp) this.track.pts.push(lp);
            this.track.v = (this.track.v || 0) + 1; changed = true;
          }
        }
      }
      if (!changed) this.broadcast((h, att) => ({ t: "f", ...this.live(att, now), pt, v: this.track.v || 0, n: this.track.pts.length }));
    }
    if (changed) this.broadcastState();
    await this.save(changed);
    return this.hasActive();
  }

  // trajeto da viagem aberta desde a saída (o Pi manda quando o link nasce no meio da viagem)
  async setTrack(t) {
    await this.load(); await this.refresh();
    if (!this.hasActive() || !Number.isInteger(t.trip)) return;
    if (this.frame && this.frame.trip !== t.trip) return;          // trajeto de outra viagem: ignora
    const pts = [];
    for (const x of (t.pts || []).slice(-TRACK_MAX)) {
      const la = num(x && x[0], -90, 90, 5), lo = num(x && x[1], -180, 180, 5), ts = num(x && x[2], 0, 1e14, 0);
      if (la !== null && lo !== null) pts.push([la, lo, ts || 0]);
    }
    const lastTs = pts.length ? pts[pts.length - 1][2] : 0;
    const newer = this.track.trip === t.trip ? this.track.pts.filter((p) => p[2] > lastTs) : [];
    this.track = { trip: t.trip, pts: pts.concat(newer).slice(-TRACK_MAX), v: (this.track.v || 0) + 1 };
    await this.save(true);
    this.broadcastState();
  }

  // o que um link vê agora (consulta e WebSocket). Sem quadro recente: nada de posição nem de trajeto.
  live(att, now = this.now()) {
    const f = this.frame;
    // sem viagem: só "carro desligado" e a hora do último sinal (sem posição nem lugar de estacionamento)
    if (!f || f.trip == null || !this.frameAt) return { state: "desligado", car: null, data: null, last_ts: this.lastSeen || null };
    if (now - this.frameAt > SHARE_STALE_MS) return { state: "sem_sinal", car: null, data: null, last_ts: this.frameAt };
    const data = {};
    for (const k of ["on", "spd", "rpm", "ect", "ect_st", "kml", "lh", "trip_s", "trip_km", "trip_kml", "alert", "alert_sev", "eta", "left_km"]) data[k] = f[k] ?? null;
    if (att.dest && f.dest) data.dest = f.dest;
    data.age_s = Math.round((now - this.frameAt) / 100) / 10;
    const car = f.lat != null ? { lat: f.lat, lon: f.lon, hdg: f.hdg ?? null } : null;
    return { state: "ao_vivo", car, data };
  }

  // since/v: o aparelho já tem os 'since' primeiros pontos da versão 'v' do trajeto (consulta incremental)
  snapshot(att, since = 0, v = null) {
    const now = this.now(), l = this.live(att, now);
    const show = l.state === "ao_vivo", n = this.track.pts.length, tv = this.track.v || 0;
    const from = since > 0 && since <= n && v === tv ? since : 0;
    const pts = show ? this.track.pts.slice(from).map((p) => [p[0], p[1]]) : [];
    return { ok: true, ...l, label: att.label || "", mode: att.mode, expires_ts: att.mode === "sempre" ? null : att.exp, now,
             track: show ? { trip: this.track.trip, v: tv, from, n, pts } : null };
  }

  sockets() { try { return this.ctx.getWebSockets("share"); } catch { return []; } }

  // envia a cada conexão o que o link dela pode ver; conexão de link vencido ou encerrado é fechada
  broadcast(msgFor) {
    const now = this.now();
    for (const ws of this.sockets()) {
      let att = null; try { att = ws.deserializeAttachment(); } catch { }
      if (!att || !(att.exp > now) || !this.valid(att.h, now)) { try { ws.close(CLOSE_GONE, "encerrado"); } catch { } continue; }
      try { ws.send(JSON.stringify(msgFor(att.h, att))); } catch { }
    }
  }
  broadcastState() { this.broadcast((h, att) => ({ t: "s", ...this.snapshot(att) })); }

  async countView(h) {
    const now = this.now();
    if (now - (this.viewAt.get(h) || 0) < 30e3) return;
    this.viewAt.set(h, now);
    try { await this.env.DB.prepare("UPDATE share SET views=views+1, last_view_ts=? WHERE token_hash=?").bind(now, h).run(); } catch { }
  }

  // rotas internas do Durable Object
  async handle(req, url, upgrade) {
    const p = url.pathname;
    if (p === "/share-info") { await this.load(); return json({ trip: this.frame && this.now() - this.frameAt < SHARE_STALE_MS ? this.frame.trip : null }); }
    if (p === "/share-reload") {
      await this.load(); await this.refresh(true);
      let b = {}; try { b = JSON.parse((await req.text()) || "{}"); } catch { }
      if (Number.isInteger(b.trip_end)) await this.tripEnded(b.trip_end);
      if (!this.hasActive()) await this.wipe();
      if (b.track) await this.setTrack(b.track);
      this.broadcast((h, att) => ({ t: "s", ...this.snapshot(att) }));     // fecha as conexões de link encerrado
      return json({ ok: true, active: this.hasActive() });
    }
    const att = { h: url.searchParams.get("h") || "", exp: +url.searchParams.get("exp") || 0, dest: url.searchParams.get("dest") === "1",
                  label: cleanLabel(url.searchParams.get("label")), mode: ["viagem", "sempre"].includes(url.searchParams.get("mode")) ? url.searchParams.get("mode") : "tempo" };
    if (!HASH_RE.test(att.h)) return json({ error: "link inválido" }, 400);
    await this.load(); await this.refresh();
    await this.checkSilent();
    if (!this.valid(att.h)) { await this.refresh(true); if (!this.valid(att.h)) return json({ error: "Este link foi encerrado.", gone: true }, 410); }
    if (p === "/share-state") {
      if (!this.rate("p:" + att.h, POLL_PER_MIN)) return json({ error: "Muitas consultas; aguarde um pouco." }, 429, { "retry-after": "10" });
      const since = url.searchParams.get("since"), v = url.searchParams.get("v");
      if (since === null) await this.countView(att.h);
      return json(this.snapshot(att, since === null ? 0 : Math.floor(+since) || 0, v === null ? null : Math.floor(+v)));
    }
    if (p === "/share-ws") {
      if (!this.rate("w:" + att.h, WS_PER_MIN)) return json({ error: "Muitas conexões; aguarde um pouco." }, 429);
      const all = this.sockets();
      let mine = 0; for (const ws of all) { try { if (ws.deserializeAttachment()?.h === att.h) mine++; } catch { } }
      if (mine >= WS_PER_LINK || all.length >= WS_TOTAL) return json({ error: "Muitas pessoas vendo agora." }, 429);
      await this.countView(att.h);
      return upgrade((server) => {
        this.ctx.acceptWebSocket(server, ["share"]);
        server.serializeAttachment(att);
        server.send(JSON.stringify({ t: "s", ...this.snapshot(att) }));
      });
    }
    return json({ error: "rota desconhecida" }, 404);
  }
}
