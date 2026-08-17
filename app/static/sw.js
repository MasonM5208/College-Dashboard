// Service worker for the installed iPhone app.
//
// IT CACHES NOTHING, ON PURPOSE. Please do not add caching here.
//
// Two reasons, both from SPEC.md:
//
//   * Offline capability is an explicit non-goal (§1). The dashboard is useless
//     offline anyway, because everything it shows lives on the server.
//   * §4 says fail loudly, and warns that stale data is worse than an error: "a
//     sync that silently stops is worse than one that crashes — the owner will
//     trust stale data." A cache-first service worker does exactly the forbidden
//     thing. It would serve yesterday's deadlines, from a page that looks
//     perfectly current, at the moment the server is unreachable.
//
// So why have one at all? Because iOS needs a registered service worker for the
// page to behave as an installed application, and because web push in M4 is
// delivered through this file's 'push' event.

self.addEventListener('install', function (event) {
  // Take over immediately rather than waiting for every tab to close.
  self.skipWaiting();
});

self.addEventListener('activate', function (event) {
  event.waitUntil(self.clients.claim());
});

self.addEventListener('fetch', function (event) {
  // Straight to the network, every time. Present only because iOS wants a fetch
  // handler registered; it adds no behaviour of its own.
  return;
});
