/* TimeScribe service worker.

   Its ONLY job is to make the dashboard installable as a PWA: Edge/Chrome
   only surface "Install as app" for a page that registers a service worker
   with a fetch handler. TimeScribe is a live local app (127.0.0.1), so we
   deliberately do NOT cache API responses -- every request passes straight
   through to the network. Offline caching would just serve stale data. */

self.addEventListener('install', (e) => {
  self.skipWaiting();
});

self.addEventListener('activate', (e) => {
  e.waitUntil(self.clients.claim());
});

self.addEventListener('fetch', (e) => {
  // Network passthrough. Having this handler at all is what makes the app
  // installable; we intentionally don't cache the live dashboard/API.
  e.respondWith(fetch(e.request));
});
