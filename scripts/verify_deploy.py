"""배포 검증 — 로컬이든 실제 HTTPS 주소든 같은 잣대로 확인한다.

  python scripts/verify_deploy.py http://127.0.0.1:8899 --password ... --cron-token ...
  python scripts/verify_deploy.py https://home-radar.onrender.com --password ...

확인하는 것
  1. 살아 있는가 (/healthz)
  2. 로그인 전에 개인정보가 새지 않는가  ← 가장 중요하다
  3. 로그인이 되는가 · 틀린 비밀번호는 막히는가
  4. 로그인 뒤 공고 조회·판정이 정상인가
  5. 보안 헤더가 붙는가 (HSTS·CSP·no-store)
  6. PWA 파일(manifest·서비스워커·아이콘)이 제대로 나오는가
  7. robots 가 색인을 막는가
  8. 크론 입구가 토큰 없이는 안 열리는가

종료코드 0 = 전부 통과.
"""
from __future__ import annotations

import argparse
import json
import sys

import httpx

OK, BAD = "OK  ", "FAIL"
_fails: list[str] = []


def check(name: str, passed: bool, detail: str = "") -> bool:
    print(f"  {OK if passed else BAD} {name}" + (f" — {detail}" if detail else ""))
    if not passed:
        _fails.append(name)
    return passed


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("base", help="예: https://home-radar.onrender.com")
    ap.add_argument("--password", default="", help="APP_PASSWORD")
    ap.add_argument("--cron-token", default="", help="CRON_TOKEN")
    ap.add_argument("--timeout", type=float, default=90.0,
                    help="무료 플랜은 잠든 서버를 깨우는 데 30초 넘게 걸린다")
    args = ap.parse_args()
    base = args.base.rstrip("/")
    https = base.startswith("https://")

    # 리다이렉트를 자동으로 따라가면 '로그인으로 튕겼는지'를 못 본다
    client = httpx.Client(timeout=args.timeout, follow_redirects=False)

    # ---------------------------------------------------------- 1. 생존
    section("1. 서버 생존")
    try:
        r = client.get(f"{base}/healthz")
        check("/healthz 200", r.status_code == 200, f"{r.status_code}")
        check("/healthz 본문", r.json().get("ok") is True, r.text[:60])
    except Exception as e:  # noqa: BLE001
        check("/healthz 접속", False, f"{type(e).__name__}: {e}")
        print("\n서버에 닿지 못했습니다. 주소와 배포 상태를 확인하세요.")
        return 1

    # ---------------------------------------------------------- 2. 비인증 차단
    section("2. 로그인 전 차단 (개인정보 보호)")
    guarded = {
        "/": (303, 307),
        "/api/view": (401,),
        "/api/profile": (401,),
        "/api/profile/export": (401,),
        "/api/health": (401,),
        "/api/rules": (401,),
        "/api/sources": (401,),
    }
    auth_on = True
    for path, expect in guarded.items():
        r = client.get(f"{base}{path}")
        ok = r.status_code in expect
        if path == "/" and r.status_code == 200:
            # 비밀번호가 설정되지 않은 서버 (로컬 개발 모드)
            auth_on = False
        check(f"{path} → {'/'.join(map(str, expect))}", ok, f"{r.status_code}")

    if not auth_on:
        print("\n  ! APP_PASSWORD 가 설정되지 않은 서버입니다 — 로그인 검사를 건너뜁니다.")
        print("    외부에 공개하는 배포라면 반드시 APP_PASSWORD 를 설정하세요.")

    if auth_on:
        # 개인정보가 담긴 문자열이 비인증 응답에 섞여 있지 않은지
        body = client.get(f"{base}/api/view").text
        leaked = any(k in body for k in
                     ("income_work", "assets", "birth", "monthly_income",
                      "profile", "추천"))
        check("비인증 응답에 개인정보 흔적 없음", not leaked, body[:80])

    # ---------------------------------------------------------- 3. 로그인
    section("3. 로그인")
    if not auth_on:
        check("로그인 검사", True, "건너뜀 (비밀번호 미설정)")
    elif not args.password:
        check("로그인 검사", False, "--password 를 주지 않아 확인하지 못했습니다")
    else:
        r = client.get(f"{base}/login")
        check("로그인 화면 200", r.status_code == 200, f"{r.status_code}")

        r = client.post(f"{base}/login", data={"password": "틀린비밀번호"})
        bad_ok = (r.status_code == 303
                  and "e=bad" in (r.headers.get("location") or ""))
        check("틀린 비밀번호 거부", bad_ok,
              f"{r.status_code} {r.headers.get('location')}")
        check("거부 시 쿠키 없음", "set-cookie" not in r.headers)

        r = client.post(f"{base}/login", data={"password": args.password})
        good = r.status_code == 303 and "hr_session" in (
            r.headers.get("set-cookie") or "")
        check("올바른 비밀번호 로그인", good,
              f"{r.status_code} {(r.headers.get('set-cookie') or '')[:40]}")
        if not good:
            print("\n  비밀번호가 맞지 않습니다. 이후 검사를 할 수 없습니다.")
            return 1

        cookie = r.headers.get("set-cookie") or ""
        check("쿠키 HttpOnly", "HttpOnly" in cookie)
        check("쿠키 SameSite=lax", "SameSite=lax" in cookie.lower()
              or "samesite=lax" in cookie.lower())
        if https:
            check("쿠키 Secure (HTTPS)", "Secure" in cookie, cookie[:70])
        client.cookies.update(r.cookies)

    # ---------------------------------------------------------- 4. 기능
    section("4. 공고 조회 · 판정")
    r = client.get(f"{base}/api/view")
    check("/api/view 200", r.status_code == 200, f"{r.status_code}")
    if r.status_code == 200:
        v = r.json()
        stats = v.get("stats") or {}
        buckets = v.get("buckets") or {}
        if not v.get("ready"):
            check("수집 데이터 존재", False,
                  "아직 수집 전입니다 — 잠시 뒤 다시 확인하세요 "
                  f"(refreshing={v.get('refreshing')})")
        else:
            check("수집 데이터 존재", True,
                  f"수집 {stats.get('collected')}건 · 표시 {stats.get('shown')}건")
            check("판정 3단계 존재",
                  all(k in buckets for k in ("추천", "확인", "제외")),
                  f"추천 {len(buckets.get('추천', []))} / "
                  f"확인 {len(buckets.get('확인', []))} / "
                  f"제외 {len(buckets.get('제외', []))}")
            check("규칙 버전 표기", bool(v.get("rules_version")),
                  str(v.get("rules_version")))
            srcs = v.get("sources") or []
            live = [s for s in srcs if s.get("state") == "정상"]
            check("수집원 하나 이상 정상", bool(live),
                  ", ".join(f"{s['id']}:{s.get('last_success_count')}건"
                            for s in live) or "없음")
            # 공식 API 키가 필요한 수집원의 상태를 알려 준다 (실패해도 치명적이진 않다)
            for s in srcs:
                if s.get("needs_key"):
                    print(f"       · {s['id']}: 키 {'있음' if s.get('has_key') else '없음'}"
                          f" · 상태 {s.get('state')}")

        r2 = client.get(f"{base}/api/profile")
        check("/api/profile 200", r2.status_code == 200, f"{r2.status_code}")

    # ---------------------------------------------------------- 5. 보안 헤더
    section("5. 보안 헤더")
    r = client.get(f"{base}/api/view" if auth_on else f"{base}/")
    h = {k.lower(): v for k, v in r.headers.items()}
    check("Content-Security-Policy", "content-security-policy" in h,
          (h.get("content-security-policy") or "")[:60])
    check("CSP 가 외부 접속 차단",
          "connect-src 'self'" in (h.get("content-security-policy") or ""))
    check("X-Content-Type-Options: nosniff",
          h.get("x-content-type-options") == "nosniff")
    check("X-Frame-Options: DENY", h.get("x-frame-options") == "DENY")
    check("Referrer-Policy: no-referrer", h.get("referrer-policy") == "no-referrer")
    check("Cache-Control: no-store",
          "no-store" in (h.get("cache-control") or ""), h.get("cache-control"))
    if https:
        check("Strict-Transport-Security", "strict-transport-security" in h,
              h.get("strict-transport-security"))
    else:
        check("HSTS 는 http 에서 붙이지 않는다",
              "strict-transport-security" not in h)

    # ---------------------------------------------------------- 6. PWA
    section("6. 아이폰 홈 화면 설치 (PWA)")
    r = client.get(f"{base}/manifest.webmanifest")
    man_ok = r.status_code == 200
    check("manifest 200 (로그인 없이)", man_ok, f"{r.status_code}")
    if man_ok:
        try:
            m = json.loads(r.text)
            check("manifest name/short_name",
                  bool(m.get("name") and m.get("short_name")),
                  m.get("short_name"))
            check("manifest display=standalone",
                  m.get("display") == "standalone", m.get("display"))
            check("manifest start_url", m.get("start_url") == "/", m.get("start_url"))
            icons = m.get("icons") or []
            sizes = {i.get("sizes") for i in icons}
            check("아이콘 192·512 둘 다", {"192x192", "512x512"} <= sizes,
                  ", ".join(sorted(s for s in sizes if s)))
            check("maskable 아이콘",
                  any("maskable" in (i.get("purpose") or "") for i in icons))
            for ic in icons:
                ri = client.get(f"{base}{ic['src']}")
                check(f"아이콘 {ic['src']}",
                      ri.status_code == 200
                      and ri.headers.get("content-type") == "image/png",
                      f"{ri.status_code} {len(ri.content)}B")
        except json.JSONDecodeError as e:
            check("manifest JSON 파싱", False, str(e))

    r = client.get(f"{base}/sw.js")
    sw_ok = r.status_code == 200
    check("서비스워커 200 (로그인 없이)", sw_ok, f"{r.status_code}")
    if sw_ok:
        check("서비스워커 자바스크립트 타입",
              "javascript" in (r.headers.get("content-type") or ""),
              r.headers.get("content-type"))
        check("서비스워커가 /api 를 캐시하지 않는다",
              "/api/" in r.text and "isPrivate" in r.text)

    r = client.get(f"{base}/apple-touch-icon.png")
    check("apple-touch-icon 200", r.status_code == 200, f"{r.status_code}")

    # ---------------------------------------------------------- 7. robots
    section("7. 색인 차단")
    r = client.get(f"{base}/robots.txt")
    check("robots.txt Disallow: /",
          r.status_code == 200 and "Disallow: /" in r.text, r.text.strip()[:40])

    # ---------------------------------------------------------- 8. 크론
    section("8. 수집 크론 입구")
    r = client.post(f"{base}/api/cron/refresh?token=아무거나틀린값")
    check("틀린 토큰 거부(404)", r.status_code == 404, f"{r.status_code}")
    if args.cron_token:
        r = client.post(f"{base}/api/cron/refresh?token={args.cron_token}")
        check("올바른 토큰 수락", r.status_code == 200,
              f"{r.status_code} {r.text[:80]}")
    else:
        print("       · --cron-token 미지정 — 정상 동작 확인은 건너뜁니다")

    # ---------------------------------------------------------- 요약
    print("\n" + "=" * 58)
    if _fails:
        print(f"실패 {len(_fails)}건:")
        for f in _fails:
            print(f"  - {f}")
        return 1
    print("전부 통과")
    return 0


if __name__ == "__main__":
    sys.exit(main())
