/* PIIPS service worker */
const CACHE = "piips-cache-v2";
const APP_SHELL = [
  "/", "/index.html", "/manifest.webmanifest",
  "/icon-192.png", "/icon-512.png",
];

self.addEventListener("install", (e) => {
  e.waitUntil(
    caches.open(CACHE).then((c) => c.addAll(APP_SHELL)).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (e) => {
  const req = e.request;
  const url = new URL(req.url);

  // only handle same-origin GETs; never cache the API
  if (req.method !== "GET" || url.origin !== location.origin) return;
  if (url.pathname.startsWith("/api/")) return;

  // pages: always a genuine network hit, never the browser's HTTP cache -
  // a stale index.html means every new deploy points nowhere useful, since
  // it references the previous build's content-hashed JS/CSS filenames.
  // Falls back to the cached shell only when the network is truly down.
  if (req.mode === "navigate") {
    e.respondWith(
      fetch(req, { cache: "no-store" }).catch(() => caches.match("/index.html"))
    );
    return;
  }

  // static assets: cache-first, then network (and cache the result)
  e.respondWith(
    caches.match(req).then((hit) =>
      hit ||
      fetch(req).then((res) => {
        const copy = res.clone();
        caches.open(CACHE).then((c) => c.put(req, copy)).catch(() => {});
        return res;
      }).catch(() => hit)
    )
  );
});
