// Service worker de Veilleuse — SERVI À LA RACINE (/sw.js) : enregistré depuis
// /static/, sa portée n'aurait couvert que /static/ et la page « / » n'aurait
// jamais été contrôlée — ready en attente infinie, push mort-né. C'était le bug.
// Rôles : installation écran d'accueil, cache de secours, et surtout les
// notifications poussées, seul canal qui survive à une page fermée.
const CACHE = "veilleuse-v5";
const SHELL = ["/", "/static/app.css", "/static/app.js", "/static/icon.svg", "/static/manifest.webmanifest"];
self.addEventListener("install", (e) => { e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL))); self.skipWaiting(); });
self.addEventListener("activate", (e) => { e.waitUntil(caches.keys().then((ks) => Promise.all(ks.filter((k) => k !== CACHE).map((k) => caches.delete(k))))); self.clients.claim(); });

// Une URL qui contient le code ne prouve rien : l'accueil peut garder le fragment,
// la page peut être déconnectée ou sans son, l'écran peut être un émetteur. On
// DEMANDE à chaque fenêtre visible si elle est un récepteur armé et connecté, et
// dans le doute (pas de réponse à temps), on affiche : mieux vaut une notification
// de trop qu'une alerte avalée.
async function someoneIsWatching(code) {
  if (!code) return false;
  const wins = (await self.clients.matchAll({ type: "window", includeUncontrolled: true }))
    .filter((c) => c.visibilityState === "visible");
  const answers = await Promise.all(wins.map((c) => new Promise((res) => {
    try {
      const mc = new MessageChannel();
      const timer = setTimeout(() => res(false), 400);
      mc.port1.onmessage = (ev) => { clearTimeout(timer); res(!!(ev.data && ev.data.watching)); };
      c.postMessage({ type: "watching?", code }, [mc.port2]);
    } catch { res(false); }
  })));
  return answers.some(Boolean);
}

self.addEventListener("push", (e) => {
  let d = {};
  try { d = e.data.json(); } catch { /* payload vide */ }
  e.waitUntil((async () => {
    if (!d.test && await someoneIsWatching(d.code)) return;   // l'essai s'affiche toujours
    return self.registration.showNotification(d.title || "Veilleuse", {
      body: d.body || "", tag: d.tag || "veilleuse", renotify: true,
      vibrate: [400, 150, 400, 150, 800],
      icon: "/static/icon-192.png", badge: "/static/icon-192.png",
      data: { code: d.code || "" },
    });
  })());
});

// Un appui sur la notification ramène à la BONNE soirée, même app fermée — sans
// jamais détourner un onglet existant : un émetteur ou une autre soirée reste
// intact, on focalise ce qui correspond ou on ouvre une fenêtre neuve.
self.addEventListener("notificationclick", (e) => {
  e.notification.close();
  const code = e.notification.data && e.notification.data.code;
  e.waitUntil(self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((list) => {
    const right = code && list.find((c) => c.url.includes(code));
    if (right) return right.focus();
    return self.clients.openWindow(code ? `/?src=notif#${code}` : "/");
  }));
});

self.addEventListener("fetch", (e) => {
  if (e.request.method !== "GET" || new URL(e.request.url).pathname.startsWith("/api/")) return;
  e.respondWith(fetch(e.request).then((r) => { const copy = r.clone(); caches.open(CACHE).then((c) => c.put(e.request, copy)); return r; }).catch(() => caches.match(e.request)));
});
