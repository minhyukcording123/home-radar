/* HOME RADAR 서비스워커 — 홈 화면 앱의 껍데기만 캐시한다.
 *
 * 핵심 규칙: **개인정보는 절대 캐시하지 않는다.**
 * /api/* 응답에는 생년·소득·자산과 판정 결과가 들어 있다. 그걸 기기 캐시에
 * 남기면 로그아웃해도 디스크에 남는다. 그래서 API 와 로그인 경로는
 * 서비스워커가 아예 손대지 않고 네트워크로 그대로 흘려보낸다.
 *
 * 캐시하는 것은 개인정보가 없는 껍데기뿐이다 — HTML 뼈대, 아이콘, manifest.
 * 덕분에 지하철처럼 신호가 끊기는 곳에서도 앱이 열리고,
 * 데이터는 연결되는 순간 받아온다.
 */

const VERSION = 'home-radar-v1';
const SHELL = [
  '/',
  '/manifest.webmanifest',
  '/icons/icon-192.png',
  '/icons/icon-512.png',
  '/icons/apple-touch-icon.png',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(VERSION)
      // 한 장이라도 실패하면 설치 전체가 실패하는 걸 막는다
      .then((cache) => Promise.allSettled(SHELL.map((u) => cache.add(u))))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(
        keys.filter((k) => k !== VERSION).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

/* 로그아웃할 때 화면이 보내는 신호 — 캐시를 통째로 비운다 */
self.addEventListener('message', (event) => {
  if (event.data === 'clear-cache') {
    event.waitUntil(caches.keys().then(
      (keys) => Promise.all(keys.map((k) => caches.delete(k)))));
  }
});

function isPrivate(url) {
  return url.pathname.startsWith('/api/')
      || url.pathname === '/login'
      || url.pathname === '/logout';
}

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;

  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;

  // 개인정보 경로는 건드리지 않는다 (캐시도, 가로채기도 없음)
  if (isPrivate(url)) return;

  // 아이콘·manifest — 캐시 우선 (바뀔 일이 거의 없고 즉시 뜬다)
  if (url.pathname.startsWith('/icons/')
      || url.pathname === '/manifest.webmanifest') {
    event.respondWith(
      caches.match(req).then((hit) => hit || fetch(req).then((res) => {
        if (res.ok) {
          const copy = res.clone();
          caches.open(VERSION).then((c) => c.put(req, copy));
        }
        return res;
      }))
    );
    return;
  }

  // 화면 이동 — 네트워크 우선, 끊기면 캐시된 껍데기로
  if (req.mode === 'navigate') {
    event.respondWith(
      fetch(req).then((res) => {
        // 200 짜리 HTML 만 갱신한다. 로그인 화면으로 가는 리다이렉트(303)를
        // 캐시하면 로그인한 뒤에도 계속 로그인 화면이 뜬다.
        if (res.ok && res.type === 'basic'
            && (res.headers.get('content-type') || '').includes('text/html')) {
          const copy = res.clone();
          caches.open(VERSION).then((c) => c.put('/', copy));
        }
        return res;
      }).catch(() => caches.match('/').then(
        (hit) => hit || new Response(
          '<!doctype html><meta charset="utf-8">'
          + '<meta name="viewport" content="width=device-width,initial-scale=1">'
          + '<body style="font:16px/1.6 -apple-system,sans-serif;padding:40px;'
          + 'text-align:center;color:#1b222b">'
          + '<h1 style="font-size:19px">연결할 수 없습니다</h1>'
          + '<p style="color:#4d5765">네트워크가 돌아오면 다시 열립니다.</p>',
          { status: 503, headers: { 'Content-Type': 'text/html; charset=utf-8' } })))
    );
  }
});
