// オフラインでも前回の画面を出すための仕組み（Service Worker）。
// ・画面（HTML）と data.json などは、毎回ネットを先に見る（更新がすぐ届くように）。
//   つながらない時だけ、前回保存した物を出す。
// ・アイコン等の変わらない物は保存した物を先に使う。
const CACHE = "us-v1";
const SHELL = ["./", "index.html", "manifest.webmanifest", "apple-touch-icon.png", "icon-192.png"];
self.addEventListener("install", e => { e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting())); });
self.addEventListener("activate", e => {
  e.waitUntil(caches.keys().then(ks => Promise.all(ks.filter(k => k !== CACHE).map(k => caches.delete(k)))).then(() => self.clients.claim()));
});
self.addEventListener("fetch", e => {
  const u = new URL(e.request.url);
  if (e.request.method !== "GET" || u.origin !== location.origin) return;
  const key = e.request.mode === "navigate" ? "index.html" : u.pathname.replace(/^.*\//, "") || "index.html";
  if (e.request.mode === "navigate" || /\.(json|html)$/.test(u.pathname)) {
    e.respondWith(fetch(e.request, {cache: "no-store"}).then(r => {
      if (r.ok) { const copy = r.clone(); caches.open(CACHE).then(c => c.put(key, copy)); }
      return r;
    }).catch(() => caches.match(key)));
    return;
  }
  e.respondWith(caches.match(e.request).then(hit => hit || fetch(e.request)));
});
