"""HOME RADAR 웹앱 — FastAPI.

로컬:
  uvicorn app.main:app --reload --port 8777

외부 배포(HTTPS 호스팅):
  uvicorn app.main:app --host 0.0.0.0 --port $PORT --proxy-headers \
          --forwarded-allow-ips="*"

수집(refresh)은 네트워크를 수십 번 때리므로 백그라운드 스레드에서 돌리고
/api/refresh/status 로 진행 상황을 본다. 그 외 조회는 전부 로컬 캐시라 즉시 응답한다.

개인정보 취급
------------
생년·소득·자산·주거정보가 들어 있다. 로컬에서만 돌 때는 127.0.0.1 바인딩과
OS 계정이 울타리였지만 외부 주소에 올리면 그게 사라진다. 그래서
  - `APP_PASSWORD` 가 설정되면 **모든 경로가 로그인 뒤로 숨는다** (app/auth.py)
  - 배포 설정에서 `HR_REQUIRE_AUTH=1` 을 켜 두면 비밀번호 없이는 기동하지 않는다
  - robots.txt 로 전체 색인을 거부하고, 응답에 no-store 를 붙여 중간 캐시를 막는다
공개로 남겨 두는 것은 개인정보가 없는 것들뿐이다 — 로그인 화면, 아이콘,
manifest, 서비스워커, /healthz.
"""
from __future__ import annotations

import threading
import traceback
from pathlib import Path

from contextlib import asynccontextmanager
from urllib.parse import parse_qs

from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.responses import (FileResponse, JSONResponse, PlainTextResponse,
                               RedirectResponse, Response)

from . import auth, collect, profile, store
from .rules import (ASSET_GUIDE, MONTHLY_INCOME_BASE, RULE_SOURCES,
                    RULES_VERSION, SPECIAL_SUPPLY)
from .sources import applyhome_detail
from .util import SIDO, now_kst, today_kst

WEB = Path(__file__).resolve().parent / "web"
ICONS = WEB / "icons"

app = FastAPI(title="HOME RADAR", version="1.0.0",
              # 개인용 앱이라 공개 문서는 꺼 둔다 — 스키마가 곧 공격 지도다
              docs_url=None, redoc_url=None, openapi_url=None)

# 로그인 없이 열어 두는 경로. 개인정보가 전혀 없는 것만 올린다.
PUBLIC_PATHS = {
    "/login", "/logout", "/healthz", "/robots.txt",
    "/manifest.webmanifest", "/sw.js", "/favicon.ico",
    "/apple-touch-icon.png", "/apple-touch-icon-precomposed.png",
    "/api/cron/refresh",      # 토큰으로 따로 검사한다
}
PUBLIC_PREFIXES = ("/icons/",)

# 이 경로들은 캐시해도 안전하다 (개인정보 없음). 그 밖은 전부 no-store.
CACHEABLE_PREFIXES = ("/icons/",)


def _is_public(path: str) -> bool:
    return path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES)

# ---------------- 기동 점검 ----------------
# 비밀번호 없이 외부에 뜨는 사고를 막는다. import 시점에 터뜨려야
# 'HTTPS 주소는 살아 있는데 내용은 다 열려 있는' 상태가 생기지 않는다.
auth.check_config()


# ---------------- 수집 작업 상태 ----------------
_job_lock = threading.Lock()
_job: dict = {"running": False, "started_at": None, "finished_at": None,
              "result": None, "error": None, "trigger": None}


def _run_refresh(enrich: bool) -> None:
    try:
        p = collect.refresh(enrich=enrich)
        srcs = p.get("sources") or {}
        with _job_lock:
            _job["result"] = {
                "count": p.get("count"),
                "fetched_at": p.get("fetched_at"),
                "new_count": len(p.get("new_keys") or []),
                "revised_count": len(p.get("revised_keys") or []),
                "merged_count": len(p.get("merged") or []),
                "corrections_count": len(p.get("corrections") or []),
                "baseline_sources": p.get("baseline_sources") or [],
                "failed_sources": [sid for sid, st in srcs.items()
                                   if st.get("state") == "실패"],
                "skipped_sources": [sid for sid, st in srcs.items()
                                    if st.get("state") == "건너뜀"],
                "warnings": p.get("warnings") or [],
            }
            _job["error"] = None
    except Exception as e:  # noqa: BLE001
        with _job_lock:
            _job["error"] = f"{type(e).__name__}: {e}"
            _job["result"] = None
        traceback.print_exc()
    finally:
        with _job_lock:
            _job["running"] = False
            _job["finished_at"] = now_kst().isoformat(timespec="seconds")


def _start_refresh(enrich: bool, trigger: str) -> bool:
    """수집을 백그라운드로 시작한다. 이미 돌고 있으면 False.

    사람이 누른 '새로고침', 외부 크론, 기동 직후 자동 수집, 캐시가 오래됐을 때의
    자동 수집이 **모두 이 문을 지난다** — 동시에 두 번 긁지 않게 하려고.
    """
    with _job_lock:
        if _job["running"]:
            return False
        _job.update({"running": True,
                     "started_at": now_kst().isoformat(timespec="seconds"),
                     "finished_at": None, "result": None, "error": None,
                     "trigger": trigger})
    threading.Thread(target=_run_refresh, args=(enrich,), daemon=True).start()
    return True


# ---------------- 자동 갱신 ----------------
def _stale_hours() -> float:
    """캐시가 몇 시간 지나면 '오래됐다'고 볼지. 0 이면 자동 갱신을 끈다."""
    raw = (auth.env("HR_STALE_HOURS") or "6").strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 6.0


def _cache_age_hours() -> float | None:
    """마지막 수집이 몇 시간 전인지. 수집 기록이 없으면 None."""
    cached = store.load_notices() or {}
    at = cached.get("fetched_at")
    if not at:
        return None
    from datetime import datetime
    try:
        then = datetime.fromisoformat(at)
    except ValueError:
        return None
    if then.tzinfo is None:
        from .util import KST
        then = then.replace(tzinfo=KST)
    return max(0.0, (now_kst() - then).total_seconds() / 3600.0)


def _maybe_auto_refresh(trigger: str) -> bool:
    """캐시가 비었거나 오래되면 조용히 수집을 깨운다.

    무료 호스팅은 요청이 없으면 인스턴스를 재웠다가 접속할 때 깨운다.
    그래서 **화면을 열 때마다 신선도를 확인하는** 이 방식이 서버 안의
    타이머보다 믿을 만하다 (잠든 서버에서는 타이머가 돌지 않는다).
    화면은 기존 캐시를 그대로 먼저 보여주고, 수집이 끝나면 새로고침하면 된다.
    """
    hours = _stale_hours()
    if hours <= 0:
        return False
    age = _cache_age_hours()
    if age is not None and age < hours:
        return False
    # 목록만 먼저(enrich=False) 긁으면 훨씬 빨라서 사용자가 덜 기다린다.
    # 상세 보강은 사람이 '새로고침'을 누를 때 한다.
    return _start_refresh(enrich=False, trigger=trigger)


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    """기동 직후 — 캐시가 없으면(임시 디스크라 날아갔으면) 바로 모으기 시작한다.

    `@app.on_event("startup")` 는 FastAPI 가 폐기 예정으로 표시했다.
    requirements 에 `<1.0` 상한을 걸어 뒀지만, 언젠가 사라질 API 를 쓰면
    업그레이드가 막히므로 lifespan 으로 쓴다.
    """
    if store.load_notices() is None:
        _start_refresh(enrich=False, trigger="startup")
    yield


app.router.lifespan_context = _lifespan


# ---------------- 보안 헤더 · 로그인 울타리 ----------------
@app.middleware("http")
async def _guard(request: Request, call_next):
    path = request.url.path
    secure = auth.is_secure_request(
        request.url.scheme, request.headers.get("x-forwarded-proto"))

    # 로그인이 켜져 있고 공개 경로가 아니면 세션 쿠키를 요구한다
    if auth.enabled() and not _is_public(path):
        if not auth.verify_token(request.cookies.get(auth.COOKIE_NAME)):
            if path.startswith("/api/"):
                # 화면 쪽 JS 가 401 을 보고 로그인 화면으로 보낸다
                resp: Response = JSONResponse(
                    {"detail": "로그인이 필요합니다", "login": "/login"},
                    status_code=401)
            else:
                resp = RedirectResponse("/login", status_code=303)
            return _harden(resp, path, secure)

    resp = await call_next(request)
    return _harden(resp, path, secure)


def _harden(resp: Response, path: str, secure: bool) -> Response:
    """보안 헤더를 붙인다.

    CSP 는 외부 호스트를 전부 막는다 — 이 앱은 자급자족이라(인라인 CSS/JS,
    외부 폰트·CDN 없음) 막아도 동작하고, 막으면 개인정보가 밖으로 새는
    경로 자체가 없어진다.
    """
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "font-src 'self' data:; "
        "connect-src 'self'; "
        "manifest-src 'self'; "
        "object-src 'none'; base-uri 'none'; "
        "form-action 'self'; frame-ancestors 'none'"
    )
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Cross-Origin-Opener-Policy"] = "same-origin"
    resp.headers["Permissions-Policy"] = (
        "geolocation=(), camera=(), microphone=(), payment=()")
    if secure:
        # HTTPS 로 들어온 요청에만 붙인다. 로컬 http 에 붙이면
        # 브라우저가 localhost 를 https 로만 열려고 해서 개발이 막힌다.
        resp.headers["Strict-Transport-Security"] = \
            "max-age=31536000; includeSubDomains"
    if path.startswith(CACHEABLE_PREFIXES):
        resp.headers["Cache-Control"] = "public, max-age=604800, immutable"
    elif path == "/sw.js":
        # 서비스워커는 캐시하지 않는다 — 안 그러면 새 버전이 안 내려간다
        resp.headers["Cache-Control"] = "no-cache"
    else:
        # 개인정보가 섞인 응답이 중간 캐시·브라우저 디스크에 남지 않게
        resp.headers["Cache-Control"] = "no-store, max-age=0"
    return resp


# ---------------- 로그인 ----------------
@app.get("/login", include_in_schema=False)
def login_page(request: Request):
    if not auth.enabled():
        return RedirectResponse("/", status_code=303)
    if auth.verify_token(request.cookies.get(auth.COOKIE_NAME)):
        return RedirectResponse("/", status_code=303)
    return FileResponse(WEB / "login.html")


@app.post("/login", include_in_schema=False)
async def login_submit(request: Request):
    """로그인 폼 처리.

    FastAPI 의 `Form()` 은 python-multipart 를 끌어온다. 입력이 비밀번호 한 칸뿐이라
    본문(application/x-www-form-urlencoded)을 직접 읽어 의존성을 늘리지 않는다.
    """
    if not auth.enabled():
        return RedirectResponse("/", status_code=303)

    raw = (await request.body())[:4096].decode("utf-8", "replace")
    fields = parse_qs(raw, keep_blank_values=True)
    password = (fields.get("password") or [""])[0]

    ip = auth.client_ip(request.headers,
                        request.client.host if request.client else "?")
    left = auth.locked_out(ip)
    if left:
        return RedirectResponse(f"/login?e=locked&s={left}", status_code=303)

    if not auth.check_password(password):
        auth.record_failure(ip)
        return RedirectResponse("/login?e=bad", status_code=303)

    auth.clear_failures(ip)
    secure = auth.is_secure_request(
        request.url.scheme, request.headers.get("x-forwarded-proto"))
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie(value=auth.make_token(), **auth.cookie_kwargs(secure))
    return resp


@app.post("/logout", include_in_schema=False)
@app.get("/logout", include_in_schema=False)
def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(auth.COOKIE_NAME, path="/")
    return resp


# ---------------- PWA (아이폰 홈 화면 설치) ----------------
@app.get("/manifest.webmanifest", include_in_schema=False)
def manifest():
    return FileResponse(WEB / "manifest.webmanifest",
                        media_type="application/manifest+json")


@app.get("/sw.js", include_in_schema=False)
def service_worker():
    return FileResponse(WEB / "sw.js", media_type="text/javascript")


@app.get("/icons/{name}", include_in_schema=False)
def icon(name: str):
    # 경로 탈출 방지 — 폴더 안의 png 만 내보낸다
    if "/" in name or "\\" in name or ".." in name or not name.endswith(".png"):
        raise HTTPException(status_code=404)
    path = ICONS / name
    if not path.is_file():
        raise HTTPException(status_code=404)
    return FileResponse(path, media_type="image/png")


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return FileResponse(ICONS / "icon-192.png", media_type="image/png")


@app.get("/apple-touch-icon.png", include_in_schema=False)
@app.get("/apple-touch-icon-precomposed.png", include_in_schema=False)
def apple_icon():
    """iOS 가 manifest 를 못 읽었을 때 루트에서 찾는 경로."""
    return FileResponse(ICONS / "apple-touch-icon.png", media_type="image/png")


@app.get("/robots.txt", include_in_schema=False)
def robots():
    """검색엔진 색인 전면 거부 — 개인 대시보드다."""
    return PlainTextResponse("User-agent: *\nDisallow: /\n")


@app.get("/healthz", include_in_schema=False)
def healthz():
    """호스팅 헬스체크용. 개인정보를 담지 않는다 (공개 경로다)."""
    return {"ok": True}


# ---------------- 화면 ----------------
@app.get("/", include_in_schema=False)
def index():
    return FileResponse(WEB / "index.html")


# ---------------- 조회 ----------------
@app.get("/api/view")
def api_view(include_closed: bool = Query(False, description="마감된 공고도 포함")):
    # 캐시가 비었거나 오래됐으면 조용히 수집을 깨운다. 응답을 막지는 않는다 —
    # 있는 캐시를 바로 보여주고, 끝나면 화면이 새로 불러온다.
    auto = _maybe_auto_refresh("stale")

    prof = profile.load()
    view = collect.build_view(prof, today_kst(), include_closed=include_closed)
    view["profile_saved"] = profile.exists()
    view["rules_version"] = RULES_VERSION
    with _job_lock:
        view["refreshing"] = bool(_job["running"])
    view["auto_refresh_started"] = auto
    view["cache_age_hours"] = _cache_age_hours()
    view["deploy"] = _deploy_info()
    return view


def _deploy_info() -> dict:
    """배포 환경 정보 — 화면이 '설정이 재시작 시 초기화된다' 같은 안내를
    띄울지 판단하는 데 쓴다. **비밀값은 담지 않는다.**
    """
    return {
        "auth": auth.enabled(),
        "ephemeral": auth.ephemeral_storage(),
        "stale_hours": _stale_hours(),
        "data_dir_set": bool(auth.env("DATA_DIR")),
        "profile_seeded": bool(auth.env("PROFILE_JSON")),
    }


@app.get("/api/sources")
def api_sources():
    """수집원별 설정·키·robots 확인 결과·마지막 성공 시각."""
    return {"sources": collect.source_overview(),
            "config": collect.load_config()}


@app.get("/api/rules")
def api_rules():
    """판정에 쓰는 규칙의 출처·기준일·공식 여부 — 화면에서 근거를 보여주기 위해."""
    return {
        "version": RULES_VERSION,
        "sources": RULE_SOURCES,
        "income_base": MONTHLY_INCOME_BASE,
        "special_supply": SPECIAL_SUPPLY,
        "asset_guide": ASSET_GUIDE,
        "disclaimer": ("official=false 인 기준은 샘플 값이다. "
                       "HOME RADAR 는 이 값으로 적격·부적격을 확정하지 않고 "
                       "'미확인' 신호로만 쓴다."),
    }


# ---------------- 내 조건 ----------------
@app.get("/api/profile")
def api_profile_get():
    prof = profile.load()
    return {
        "profile": prof,
        "saved": profile.exists(),
        "derived": profile.derived(prof, today_kst()),
        "meta": {
            "sido": SIDO,
            "marital": ["미혼", "예비신혼", "신혼", "기혼"],
            "household": ["단독세대주", "부모님세대원", "기타"],
            "account_mode": ["상관없음", "가입만까지", "불필요만"],
            "want_kinds": list(profile.DEFAULT["want"].keys()),
            "income_base": MONTHLY_INCOME_BASE,
            "special_supply": SPECIAL_SUPPLY,
            "rules_version": RULES_VERSION,
        },
    }


@app.put("/api/profile")
def api_profile_put(payload: dict = Body(...)):
    prof = profile.save(payload)
    return {"profile": prof, "saved": True,
            "derived": profile.derived(prof, today_kst()),
            "deploy": _deploy_info()}


@app.get("/api/profile/export")
def api_profile_export():
    """지금 조건을 `PROFILE_JSON` 환경변수용 한 줄 JSON 으로 돌려준다.

    무료 플랜(임시 디스크)에서 설정을 영구 보존하는 통로다. 호스팅 대시보드의
    환경변수에 붙여 넣으면 재시작·재배포 후에도 그 값으로 시작한다.

    **개인정보가 담긴 응답이다** — 로그인 뒤에만 열리고 no-store 가 붙는다.
    """
    return {"name": "PROFILE_JSON", "value": profile.export_env_value(),
            "deploy": _deploy_info()}


# ---------------- 북마크 ----------------
@app.post("/api/bookmark")
def api_bookmark(payload: dict = Body(...)):
    key = (payload or {}).get("key")
    if not key:
        raise HTTPException(status_code=400, detail="key 가 필요합니다")
    return store.toggle_bookmark(str(key))


@app.get("/api/bookmarks")
def api_bookmarks():
    return {"keys": store.load_bookmarks()}


# ---------------- 수집 ----------------
@app.post("/api/refresh")
def api_refresh(enrich: bool = Query(True)):
    if not _start_refresh(enrich, trigger="manual"):
        return JSONResponse({"started": False, "reason": "이미 수집 중입니다"},
                            status_code=409)
    return {"started": True}


@app.post("/api/cron/refresh", include_in_schema=False)
@app.get("/api/cron/refresh", include_in_schema=False)
def api_cron_refresh(token: str = Query(""), enrich: bool = Query(False)):
    """외부 스케줄러가 주기적으로 수집을 깨우는 입구.

    무료 플랜은 접속이 없으면 인스턴스를 재운다. 그래서 '서버 안의 타이머'는
    믿을 수 없고, **밖에서 두드려 깨우는** 방식이 필요하다
    (.github/workflows/refresh.yml 참고).

    로그인 쿠키 대신 `CRON_TOKEN` 으로 검사한다 — 스케줄러는 쿠키가 없다.
    토큰이 설정돼 있지 않으면 이 입구는 아예 닫혀 있다.
    """
    if not auth.check_cron_token(token):
        # 토큰이 틀렸는지 미설정인지 구분해 주지 않는다
        raise HTTPException(status_code=404)
    started = _start_refresh(enrich, trigger="cron")
    return {"started": started,
            "reason": "" if started else "이미 수집 중입니다",
            "cache_age_hours": _cache_age_hours()}


@app.get("/api/refresh/status")
def api_refresh_status():
    with _job_lock:
        return dict(_job)


# ---------------- 공고 상세 ----------------
def _notice_by_key(key: str) -> dict | None:
    cached = store.load_notices() or {}
    for raw in cached.get("notices") or []:
        if raw.get("key") == key:
            return raw
    return None


def _lh_detail(raw: dict) -> dict:
    """LH 공고의 공급정보를 상세 화면이 쓰는 모양으로 맞춘다.

    공급정보 API 의 금액은 **원** 단위다. price_min/max 는 만원이라
    collect 가 이미 환산해 뒀고, 보증금·월임대료는 분양가가 아니라서
    supply_rows 에 원 단위 그대로 둔다 — 화면이 라벨을 붙여 보여준다.
    """
    supply = (raw.get("extra") or {}).get("supply") or {}
    rows = supply.get("rows") or []

    # 상세 API 가 미승인이면 주소·세대수가 비어 있다. 그럴 때는 공급정보에서
    # 끌어와 요약을 채운다 — 단지명/호별 주소와 세대수 합계는 거기 있다.
    location = raw.get("address") or ""
    if not location and rows:
        names = []
        for r in rows:
            nm = r.get("complex") or r.get("address") or ""
            if nm and nm not in names:
                names.append(nm)
        if names:
            location = names[0] if len(names) == 1 else \
                f"{names[0]} 외 {len(names) - 1}곳"

    total_units = raw.get("households") or ""
    if not total_units and rows:
        nums = [r["units"] for r in rows if isinstance(r.get("units"), int)]
        if nums:
            total_units = f"{sum(nums):,}세대 (공급정보 합계)"
        elif any(r.get("address") for r in rows):
            # 매입임대는 세대수 대신 호별 목록으로 온다 — 행 수가 곧 호수다
            total_units = f"{len(rows):,}호 (공급정보 목록)"

    return {
        "source": raw.get("source"),
        "available": bool(rows),
        "location": location,
        "total_units": total_units,
        "posted": raw.get("posted") or "",
        "announce": raw.get("announce") or "",
        "begin": raw.get("begin") or "",
        "end": raw.get("end") or "",
        "status": raw.get("status") or "",
        "price": raw.get("price", ""),
        "price_min": raw.get("price_min"),
        "price_max": raw.get("price_max"),
        "areas": raw.get("areas") or [],
        "receptions": [],
        "doc_url": raw.get("doc_url") or raw.get("url", ""),
        # LH 고유 — 보증금·월임대료는 원 단위
        "supply_rows": rows,
        "supply_available": supply.get("available") or [],
        "supply_missing": supply.get("missing") or [],
        "amount_unit": "원",
        # 상세 API(15057999)로 받은 서류·계약 일정과 단지 정보
        "lh_schedule": (raw.get("extra") or {}).get("lh_schedule") or {},
        "lh_site": (raw.get("extra") or {}).get("lh_site") or {},
        # 어느 수집원에서 합쳐졌는지 (출처 표시용)
        "merged_from": (raw.get("extra") or {}).get("merged_from") or [],
        "detail_loaded": bool(raw.get("detail_loaded")),
        "reason": "" if rows else "이 공고의 공급정보가 제공되지 않습니다",
    }


@app.get("/api/detail")
def api_detail(key: str | None = None,
               house_manage_no: str | None = None,
               pblanc_no: str | None = None,
               house_secd: str = "01",
               refresh: bool = False):
    """공고 상세. `key`(= row.key)로 부르면 수집원에 맞는 경로를 골라 준다.

    구버전 호출(house_manage_no + pblanc_no)도 그대로 받는다.
    """
    if key:
        raw = _notice_by_key(key)
        if raw is None:
            raise HTTPException(status_code=404, detail="공고를 찾을 수 없습니다")
        sid = raw.get("source_id")
        if sid == "lh":
            return _lh_detail(raw)
        if sid != "applyhome":
            # SH·청년안심은 목록만 제공한다 — 상세 API 가 없다
            return {
                "source": raw.get("source"),
                "available": False,
                "reason": f"{raw.get('source')} 는 공고 목록만 제공합니다 — "
                          "자세한 내용은 공고문에서 확인하세요",
                "doc_url": raw.get("doc_url") or raw.get("url", ""),
                "location": raw.get("address", ""),
                "total_units": raw.get("households", ""),
                "areas": [], "receptions": [],
                "price_min": raw.get("price_min"),
                "price_max": raw.get("price_max"),
            }
        house_manage_no = raw.get("house_manage_no") or house_manage_no
        pblanc_no = raw.get("pblanc_no") or pblanc_no
        house_secd = raw.get("house_secd") or house_secd

    if not house_manage_no or not pblanc_no:
        raise HTTPException(status_code=400,
                            detail="key 또는 house_manage_no+pblanc_no 가 필요합니다")

    cache_key = f"청약홈:{house_manage_no}-{pblanc_no}"
    extra = {}
    if key:
        raw = _notice_by_key(key) or {}
        extra = {
            "merged_from": (raw.get("extra") or {}).get("merged_from") or [],
            "source": raw.get("source"),
            "detail_loaded": bool(raw.get("detail_loaded")),
        }
    if not refresh:
        cached = store.load_detail(cache_key)
        if cached and not cached.get("error"):
            return {"cached": True, "available": True, **extra, **cached}
    d = applyhome_detail.fetch(house_manage_no, pblanc_no, house_secd)
    if d.get("error"):
        raise HTTPException(status_code=502, detail=d["error"])
    store.save_detail(cache_key, d)
    return {"cached": False, "available": True, **extra, **d}


@app.get("/api/revisions")
def api_revisions(key: str | None = None):
    """공고 수정 이력."""
    if key:
        return {"key": key, "history": store.revision_history(key)}
    store_all = store.load_revisions()
    return {"count": len(store_all),
            "changed": {k: v.get("history", [])[-3:]
                        for k, v in store_all.items() if v.get("history")}}


@app.get("/api/health")
def api_health():
    cached = store.load_notices()
    meta = store.seen_meta()
    return {
        "ok": True,
        "today": today_kst().isoformat(),
        "cached_count": (cached or {}).get("count", 0),
        "fetched_at": (cached or {}).get("fetched_at"),
        "profile_saved": profile.exists(),
        "rules_version": RULES_VERSION,
        "runs": meta.get("runs", 0),
        "baselines": meta.get("baselines", {}),
    }
