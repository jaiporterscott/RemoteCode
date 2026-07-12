/* RemoteCode service worker — offline app-shell cache with version-based resync.
 *
 * __BUILD__ is replaced by the server (see /sw.js route) with the current build
 * version. Because the version is in the file body, any change to a shell asset
 * changes these bytes, the browser installs a new worker, and `activate` deletes
 * the previous cache — so a version bump transparently re-downloads and re-syncs.
 *
 * Strategy:
 *   - /api/**, /ws/**, websocket upgrades, non-GET  -> never touched (live data)
 *   - navigations & "/"                             -> network-first (get latest
 *       ?v= asset refs), fall back to cached shell when offline
 *   - /static/**                                    -> cache-first for instant
 *       loads, revalidated in the background (assets are fingerprinted or rare)
 */
const BUILD = '__BUILD__';
const CACHE = 'remotecode-' + BUILD;

const SHELL = [
  '/',
  '/static/style.css',
  '/static/app.js',
  '/static/manifest.webmanifest',
  '/static/vendor/xterm.css',
  '/static/vendor/xterm.js',
  '/static/vendor/addon-fit.js',
  '/static/vendor/highlight.min.js',
  '/static/vendor/hljs-dark.css',
  '/static/vendor/hljs-light.css',
  '/static/vendor/model-viewer.min.js',
];

self.addEventListener('install', (e) => {
  self.skipWaiting();
  e.waitUntil(
    caches.open(CACHE).then((c) =>
      // {cache:'reload'} bypasses the HTTP cache so we precache truly fresh copies
      c.addAll(SHELL.map((u) => new Request(u, { cache: 'reload' })))
    ).catch(() => {})
  );
});

self.addEventListener('activate', (e) => {
  e.waitUntil((async () => {
    const keys = await caches.keys();
    await Promise.all(
      keys.filter((k) => k.startsWith('remotecode-') && k !== CACHE)
          .map((k) => caches.delete(k))
    );
    await self.clients.claim();
  })());
});

function bypass(url, req) {
  if (req.method !== 'GET') return true;
  if (url.pathname.startsWith('/api/')) return true;   // sessions, streams, etc.
  if (url.pathname.startsWith('/ws/')) return true;    // terminal websocket
  if (url.pathname === '/sw.js') return true;          // let the browser manage it
  if (req.headers.get('upgrade') === 'websocket') return true;
  return false;
}

self.addEventListener('fetch', (e) => {
  const req = e.request;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;     // ignore cross-origin
  if (bypass(url, req)) return;                          // straight to network

  // App shell: network-first, cache fallback. Keeps the newest ?v= refs online,
  // still opens when offline.
  if (req.mode === 'navigate' || url.pathname === '/') {
    e.respondWith((async () => {
      try {
        const net = await fetch(req);
        const c = await caches.open(CACHE);
        c.put('/', net.clone());
        return net;
      } catch (_) {
        return (await caches.match('/')) || Response.error();
      }
    })());
    return;
  }

  // Static assets: cache-first (instant), revalidate in the background. ignoreSearch
  // so '?v=' variants hit the same entry — safe because a real change bumps BUILD,
  // which wipes this whole cache on the next activate.
  if (url.pathname.startsWith('/static/')) {
    e.respondWith((async () => {
      const c = await caches.open(CACHE);
      const hit = await c.match(req, { ignoreSearch: true });
      const net = fetch(req).then((r) => {
        if (r && r.ok) c.put(req, r.clone());
        return r;
      }).catch(() => null);
      return hit || (await net) || Response.error();
    })());
  }
});
