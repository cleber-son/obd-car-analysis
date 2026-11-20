/* Civic OBD: funciona como aplicativo e abre sem internet com a última cópia vista.
   Regra única: tenta a rede primeiro (dados sempre frescos); se não houver rede, entrega o que ficou guardado.
   Nunca guarda o fluxo ao vivo nem envios (POST). */
const CACHE = "civic-obd-v1";
const SHELL = ["/", "/dirigir", "/oficina", "/static/style.css", "/static/app.js", "/static/tests.js", "/static/extras.js", "/static/icon-192.png"];
self.addEventListener("install", (e) => { e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL).catch(() => { })).then(() => self.skipWaiting())); });
self.addEventListener("activate", (e) => { e.waitUntil(caches.keys().then((ks) => Promise.all(ks.filter((k) => k !== CACHE).map((k) => caches.delete(k)))).then(() => self.clients.claim())); });
self.addEventListener("fetch", (e) => {
  const req = e.request, url = new URL(req.url);
  if (req.method !== "GET" || url.origin !== location.origin) return;
  if (url.pathname === "/api/stream" || url.pathname === "/api/ws" || url.pathname.startsWith("/api/sync/") || url.pathname === "/api/export.csv") return;
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
