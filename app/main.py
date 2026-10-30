"""HOME RADAR 웹앱 — FastAPI.

  uvicorn app.main:app --reload --port 8777

수집(refresh)은 네트워크를 수십 번 때리므로 백그라운드 스레드에서 돌리고
/api/refresh/status 로 진행 상황을 본다. 그 외 조회는 전부 로컬 캐시라 즉시 응답한다.

개인정보는 이 프로세스 밖으로 나가지 않는다. 바인딩 기본값도 127.0.0.1 이다.
"""
from __future__ import annotations

import threading
import traceback
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse

from . import collect, profile, store
from .rules import (ASSET_GUIDE, MONTHLY_INCOME_BASE, RULE_SOURCES,
                    RULES_VERSION, SPECIAL_SUPPLY)
from .sources import applyhome_detail
from .util import SIDO, now_kst, today_kst

WEB = Path(__file__).resolve().parent / "web"

app = FastAPI(title="HOME RADAR", version="0.2.0")

# ---------------- 수집 작업 상태 ----------------
_job_lock = threading.Lock()
_job: dict = {"running": False, "started_at": None, "finished_at": None,
              "result": None, "error": None}


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
    view["rules_version"] = RULES_VERSION
    return view


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
            "derived": profile.derived(prof, today_kst())}


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
    with _job_lock:
        if _job["running"]:
            return JSONResponse({"started": False, "reason": "이미 수집 중입니다"},
                                status_code=409)
        _job.update({"running": True,
                     "started_at": now_kst().isoformat(timespec="seconds"),
                     "finished_at": None, "result": None, "error": None})
    threading.Thread(target=_run_refresh, args=(enrich,), daemon=True).start()
    return {"started": True}


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
    return {
        "source": raw.get("source"),
        "available": bool(rows),
        "location": raw.get("address", ""),
        "total_units": raw.get("households", ""),
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
    if not refresh:
        cached = store.load_detail(cache_key)
        if cached and not cached.get("error"):
            return {"cached": True, "available": True, **cached}
    d = applyhome_detail.fetch(house_manage_no, pblanc_no, house_secd)
    if d.get("error"):
        raise HTTPException(status_code=502, detail=d["error"])
    store.save_detail(cache_key, d)
    return {"cached": False, "available": True, **d}


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
