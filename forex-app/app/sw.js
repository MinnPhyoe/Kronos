// Minimal service worker: makes the app installable on a phone. It never caches
// prices, forecasts or signals: those always come live from the API.
const SHELL = "fx-shell-v1";
const FILES = ["/", "/static/styles.css", "/static/app.js", "/static/chart.js", "/static/icon.svg",
  "/static/vendor/lightweight-charts.standalone.production.js"];
self.addEventListener("install", (e) => e.waitUntil(caches.open(SHELL).then((c) => c.addAll(FILES)).then(() => self.skipWaiting())));
self.addEventListener("activate", (e) => e.waitUntil(caches.keys().then((ks) => Promise.all(ks.filter((k) => k !== SHELL).map((k) => caches.delete(k)))).then(() => self.clients.claim())));
self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  const shell = e.request.method === "GET" && (url.pathname === "/" || url.pathname.startsWith("/static/"));
  if (!shell) return;                       // API calls go straight to the network
  e.respondWith(fetch(e.request).then((r) => {
    const copy = r.clone();
    caches.open(SHELL).then((c) => c.put(e.request, copy));
    return r;
  }).catch(() => caches.match(e.request)));
});
