"""HOME RADAR 웹앱 — FastAPI.

  uvicorn app.main:app --reload --port 8000

수집(refresh)은 네트워크를 수십 번 때리므로 백그라운드 스레드에서 돌리고
/api/refresh/status 로 진행 상황을 본다. 그 외 조회는 전부 로컬 캐시라 즉시 응답한다.
"""
from __future__ import annotations

import threading
import traceback
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse

from . import collect, profile, store
from .rules import MONTHLY_INCOME_BASE, SPECIAL_SUPPLY, income_base
from .sources import applyhome_detail
from .util import SIDO, now_kst, today_kst

WEB = Path(__file__).resolve().parent / "web"

app = FastAPI(title="HOME RADAR", version="0.1.0")

# ---------------- 수집 작업 상태 ----------------
_job_lock = threading.Lock()
_job: dict = {"running": False, "started_at": None, "finished_at": None,
              "result": None, "error": None}


def _run_refresh(back: int, ahead: int, enrich: bool) -> None:
    try:
        payload = collect.refresh(back=back, ahead=ahead, enrich=enrich)
        with _job_lock:
            _job["result"] = {
                "count": payload.get("count"),
                "fetched_at": payload.get("fetched_at"),
                "warnings": payload.get("warnings") or [],
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


# ---------------- 화면 ----------------
@app.get("/", include_in_schema=False)
def index():
    return FileResponse(WEB / "index.html")


# ---------------- 조회 ----------------
@app.get("/api/view")
def api_view(include_closed: bool = Query(False, description="마감된 공고도 포함")):
    prof = profile.load()
    view = collect.build_view(prof, today_kst(), include_closed=include_closed)
    view["profile_saved"] = profile.exists()
    return view


@app.get("/api/profile")
def api_profile_get():
    return {
        "profile": profile.load(),
        "saved": profile.exists(),
        "meta": {
            "sido": SIDO,
            "marital": ["미혼", "예비신혼", "신혼", "기혼"],
            "household": ["단독세대주", "부모님세대원", "기타"],
            "account_mode": ["상관없음", "가입만까지", "불필요만"],
            "want_kinds": list(profile.DEFAULT["want"].keys()),
            "income_base": MONTHLY_INCOME_BASE,
            "special_supply": SPECIAL_SUPPLY,
        },
    }


@app.put("/api/profile")
def api_profile_put(payload: dict = Body(...)):
    prof = profile.save(payload)
    return {"profile": prof, "saved": True,
            "income_base": income_base(prof.get("household_size", 1))}


# ---------------- 수집 ----------------
@app.post("/api/refresh")
def api_refresh(back: int = Query(1, ge=0, le=6),
                ahead: int = Query(6, ge=0, le=12),
                enrich: bool = Query(True)):
    with _job_lock:
        if _job["running"]:
            return JSONResponse({"started": False, "reason": "이미 수집 중입니다"},
                                status_code=409)
        _job.update({"running": True, "started_at": now_kst().isoformat(timespec="seconds"),
                     "finished_at": None, "result": None, "error": None})
    threading.Thread(target=_run_refresh, args=(back, ahead, enrich), daemon=True).start()
    return {"started": True}


@app.get("/api/refresh/status")
def api_refresh_status():
    with _job_lock:
        return dict(_job)


# ---------------- 공고 상세 ----------------
@app.get("/api/detail")
def api_detail(house_manage_no: str, pblanc_no: str, house_secd: str = "01",
               refresh: bool = False):
    key = f"청약홈:{house_manage_no}-{pblanc_no}"
    if not refresh:
        cached = store.load_detail(key)
        if cached and not cached.get("error"):
            return {"cached": True, **cached}
    d = applyhome_detail.fetch(house_manage_no, pblanc_no, house_secd)
    if d.get("error"):
        raise HTTPException(status_code=502, detail=d["error"])
    store.save_detail(key, d)
    return {"cached": False, **d}


@app.get("/api/health")
def api_health():
    cached = store.load_notices()
    return {
        "ok": True,
        "today": today_kst().isoformat(),
        "cached_count": (cached or {}).get("count", 0),
        "fetched_at": (cached or {}).get("fetched_at"),
        "profile_saved": profile.exists(),
    }
