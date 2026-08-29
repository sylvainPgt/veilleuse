// Service worker minimal : rend l'app installable ; réseau d'abord, cache en secours pour la coquille.
// Porte aussi les notifications : sur Android, seules celles du service worker existent.
const CACHE = "veilleuse-v3";

// Web Push : le serveur pousse l'alerte jusqu'ici, même app fermée ou téléphone
// verrouillé. C'est le seul chemin qui ne dépende pas d'une page ouverte.
self.addEventListener("push", (e) => {
  let d = {};
  try { d = e.data.json(); } catch { /* payload vide */ }
  e.waitUntil(self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((list) => {
    // l'app est sous les yeux : elle sonne déjà, pas besoin de doubler
    if (list.some((c) => c.visibilityState === "visible")) return;
    return self.registration.showNotification(d.title || "Veilleuse", {
      body: d.body || "", tag: d.tag || "veilleuse", renotify: true,
      vibrate: [400, 150, 400, 150, 800],
      icon: "/static/icon-192.png", badge: "/static/icon-192.png",
    });
  }));
});
const SHELL = ["/", "/static/app.css", "/static/app.js", "/static/icon.svg", "/static/manifest.webmanifest"];
self.addEventListener("install", (e) => { e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL))); self.skipWaiting(); });
self.addEventListener("activate", (e) => { e.waitUntil(caches.keys().then((ks) => Promise.all(ks.filter((k) => k !== CACHE).map((k) => caches.delete(k))))); self.clients.claim(); });
// Un appui sur la notification ramène à l'app plutôt que de ne rien faire.
self.addEventListener("notificationclick", (e) => {
  e.notification.close();
  e.waitUntil(self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((list) => {
    for (const c of list) if ("focus" in c) return c.focus();
    return self.clients.openWindow("/");
  }));
});
self.addEventListener("fetch", (e) => {
  if (e.request.method !== "GET" || new URL(e.request.url).pathname.startsWith("/api/")) return;
  e.respondWith(fetch(e.request).then((r) => { const copy = r.clone(); caches.open(CACHE).then((c) => c.put(e.request, copy)); return r; }).catch(() => caches.match(e.request)));
});
