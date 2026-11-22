/* Delfos: funciona como aplicativo e abre sem internet com a última cópia vista.
   Regra única: tenta a rede primeiro (dados sempre frescos); se não houver rede, entrega o que ficou guardado.
   Nunca guarda o fluxo ao vivo nem envios (POST). Nada com posição fica guardado no aparelho: /api/gps/*, /api/share/*,
   /s/* e /loc/* vão direto à rede (sem cópia), para a localização não sobrar depois que a senha vence.
   Os ladrilhos do mapa são de outra origem (tile.openstreetmap.org) e não passam por aqui. */
const CACHE = "delfos-v4";
const SHELL = ["/", "/dirigir", "/oficina", "/static/style.css", "/static/app.js", "/static/tests.js", "/static/extras.js", "/static/icon-192.png",
  "/static/leaflet.js", "/static/leaflet.css", "/static/gpsmap.js", "/static/gps.js", "/static/voice.js"];
const NO_CACHE = (p) => p === "/api/stream" || p === "/api/ws" || p.startsWith("/api/sync/") || p === "/api/export.csv"
  || p.toLowerCase().startsWith("/api/gps") || p.startsWith("/api/share/") || p.startsWith("/s/") || p.startsWith("/loc/") || /%[0-9a-f]{2}/i.test(p);
self.addEventListener("install", (e) => { e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL).catch(() => { })).then(() => self.skipWaiting())); });
self.addEventListener("activate", (e) => { e.waitUntil(caches.keys().then((ks) => Promise.all(ks.filter((k) => k !== CACHE).map((k) => caches.delete(k)))).then(() => self.clients.claim())); });
self.addEventListener("fetch", (e) => {
  const req = e.request, url = new URL(req.url);
  if (req.method !== "GET" || url.origin !== location.origin) return;
  if (NO_CACHE(url.pathname)) return;
  e.respondWith((async () => {
    const cache = await caches.open(CACHE);
    try {
      const res = await fetch(req);
      if (res.ok && res.status === 200) cache.put(req, res.clone()).catch(() => { });
      return res;
    } catch (err) {
      const hit = await cache.match(req, { ignoreSearch: url.pathname.startsWith("/static/") });
      if (hit) return hit;
      if (url.pathname === "/api/live") return new Response(JSON.stringify({ state: "pi_offline", engine_on: false, values: {}, alerts: [], dtc: {}, offline: true }), { headers: { "content-type": "application/json" } });
      throw err;
    }
  })());
});
