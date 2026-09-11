// Service worker de Veilleuse — SERVI À LA RACINE (/sw.js) : enregistré depuis
// /static/, sa portée n'aurait couvert que /static/ et la page « / » n'aurait
// jamais été contrôlée — ready en attente infinie, push mort-né. C'était le bug.
// Rôles : installation écran d'accueil, cache de secours, et surtout les
// notifications poussées, seul canal qui survive à une page fermée.
const CACHE = "veilleuse-v4";
const SHELL = ["/", "/static/app.css", "/static/app.js", "/static/icon.svg", "/static/manifest.webmanifest"];
self.addEventListener("install", (e) => { e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL))); self.skipWaiting(); });
self.addEventListener("activate", (e) => { e.waitUntil(caches.keys().then((ks) => Promise.all(ks.filter((k) => k !== CACHE).map((k) => caches.delete(k))))); self.clients.claim(); });

self.addEventListener("push", (e) => {
  let d = {};
  try { d = e.data.json(); } catch { /* payload vide */ }
  e.waitUntil(self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((list) => {
    // On ne se tait que si la BONNE soirée est sous les yeux : une page visible
    // sur l'accueil ou sur une autre soirée ne sonne pas, elle ne doit pas
    // absorber la notification. Une notification d'essai s'affiche toujours.
    const watching = list.some((c) => c.visibilityState === "visible" && d.code && c.url.includes(d.code));
    if (watching && !d.test) return;
    return self.registration.showNotification(d.title || "Veilleuse", {
      body: d.body || "", tag: d.tag || "veilleuse", renotify: true,
      vibrate: [400, 150, 400, 150, 800],
      icon: "/static/icon-192.png", badge: "/static/icon-192.png",
      data: { code: d.code || "" },
    });
  }));
});

// Un appui sur la notification ramène à la BONNE soirée, même app fermée.
self.addEventListener("notificationclick", (e) => {
  e.notification.close();
  const code = e.notification.data && e.notification.data.code;
  const target = code ? "/#" + code : "/";
  e.waitUntil(self.clients.matchAll({ type: "window", includeUncontrolled: true }).then(async (list) => {
    const right = code && list.find((c) => c.url.includes(code));
    if (right) return right.focus();
    const any = list[0];
    if (any) {
      // même origine : on peut re-router la fenêtre existante vers la soirée
      try { await any.navigate(target); } catch { /* tant pis */ }
      return any.focus();
    }
    return self.clients.openWindow(target);
  }));
});

self.addEventListener("fetch", (e) => {
  if (e.request.method !== "GET" || new URL(e.request.url).pathname.startsWith("/api/")) return;
  e.respondWith(fetch(e.request).then((r) => { const copy = r.clone(); caches.open(CACHE).then((c) => c.put(e.request, copy)); return r; }).catch(() => caches.match(e.request)));
});
