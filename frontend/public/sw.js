// PIIPS service worker: caches the static app shell so it installs and
// loads offline. Never caches API calls (/api/*) - invoice/job data must
// always come from the network, since a stale cached response here could
// show a wrong dashboard state or silently drop a change.

const CACHE = "piips-shell-v2";
// "/" (index.html) is deliberately NOT precached/cache-first (see the fetch
// handler below) - unlike the hashed JS/CSS it references, index.html's own
// content changes every build (to point at that build's new hashes), so
// cache-first here was serving an indefinitely stale shell - a rebuild
// never took effect for an already-installed user until they manually
// cleared site data, since the SAME cache name meant "activate" never saw
// it as stale enough to evict.
const SHELL = ["/manifest.webmanifest", "/icon-192.png", "/icon-512.png", "/icon-maskable-512.png"];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE).then((cache) => cache.addAll(SHELL)).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const { request } = event;
  if (request.method !== "GET") return;

  const url = new URL(request.url);
  if (url.pathname.startsWith("/api/")) return; // always network, never cached

  // Navigations (the "/" document itself) are network-first: always fetch
  // the latest index.html (and cache it as the offline fallback) so a new
  // build's hashed asset references take effect immediately for anyone
  // online, instead of being stuck on whatever shell first got installed.
  if (request.mode === "navigate") {
    event.respondWith(
      fetch(request)
        .then((response) => {
          if (response && response.ok) {
            const copy = response.clone();
            caches.open(CACHE).then((cache) => cache.put(request, copy));
          }
          return response;
        })
        .catch(() => caches.match(request))
    );
    return;
  }

  // Everything else (hashed /assets/*.js|css, icons, manifest) is safe to
  // cache-first - a new build gets new filenames, so stale content is
  // never served once a fresh index.html (above) starts referencing them.
  event.respondWith(
    caches.match(request).then((cached) => {
      if (cached) return cached;
      return fetch(request)
        .then((response) => {
          if (response && response.ok) {
            const copy = response.clone();
            caches.open(CACHE).then((cache) => cache.put(request, copy));
          }
          return response;
        })
        .catch(() => cached);
    })
  );
});
