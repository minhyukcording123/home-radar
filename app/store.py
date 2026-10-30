"""로컬 저장소 — 수집 캐시, 상세 캐시, 최초 관측 기록, 변경 추적, 수집원 상태.

외부 DB 없이 JSON 파일만 쓴다. 개인용 단일 사용자 앱이고 data/ 는 .gitignore 에 있다.

여기서 다루는 까다로운 문제 두 가지:

1. **첫 수집을 '신규 공고'로 보지 않기**
   공고에 게시일이 항상 붙어 있지 않아 '우리가 처음 본 날'을 신규 기준으로 쓴다.
   그런데 그러면 맨 처음 수집한 날은 216건 전부가 신규로 잡힌다.
   그래서 최초 1회 수집분은 baseline 으로 표시해 신규 알림에서 빼고,
   그 다음 수집부터 들어온 공고만 신규로 센다.

2. **공고 수정사항 추적**
   접수 마감이 밀리거나 발표일이 바뀌는 일이 잦다. 핵심 필드의 해시를 들고 있다가
   값이 달라지면 변경 이력으로 쌓는다.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import date
from pathlib import Path
from typing import Optional

from .util import now_kst, today_kst

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DETAILS = DATA / "details"

NOTICES_FILE = DATA / "notices.json"
SEEN_FILE = DATA / "seen.json"
REVISIONS_FILE = DATA / "revisions.json"
SOURCE_STATUS_FILE = DATA / "source_status.json"
BOOKMARKS_FILE = DATA / "bookmarks.json"

_SAFE = re.compile(r"[^0-9A-Za-z._-]")

# 변경을 추적할 필드 — 일정과 규모가 바뀌면 사용자가 알아야 한다.
#
# 금액은 **숫자**(price_min/price_max)로 추적한다. 사람이 읽는 price 문자열을
# 추적하면 우리 쪽 표시 형식만 바꿔도 '공고가 변경됨'으로 잡힌다
# (실제로 '82,530만원' → '8억 2,530만원' 으로 바꿨을 때 15건이 오탐으로 떴다).
TRACKED_FIELDS = ("begin", "end", "announce", "posted", "households",
                  "price_min", "price_max", "address", "status", "title",
                  "doc_url")


def _ensure() -> None:
    DETAILS.mkdir(parents=True, exist_ok=True)


def _read(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default


def _write(path: Path, payload) -> None:
    _ensure()
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


# ---------------- 공고 목록 캐시 ----------------
def save_notices(notices: list[dict], warnings: list[str],
                 sources: Optional[dict] = None,
                 merged: Optional[list] = None,
                 corrections: Optional[list] = None) -> dict:
    """수집 결과를 캐시에 쓴다.

    중복 흡수(merged)·정정 연결(corrections) 결과도 함께 남긴다 —
    예전에는 refresh() 가 반환값에만 담아서, 수집이 끝난 뒤에는
    "무엇이 왜 합쳐졌는지" 확인할 방법이 없었다.
    """
    payload = {
        "fetched_at": now_kst().isoformat(timespec="seconds"),
        "count": len(notices),
        "warnings": warnings,
        "sources": sources or {},
        "merged": merged or [],
        "corrections": corrections or [],
        "notices": notices,
    }
    _write(NOTICES_FILE, payload)
    return payload


def load_notices() -> Optional[dict]:
    return _read(NOTICES_FILE, None)


# ---------------- 상세 캐시 ----------------
def detail_path(key: str) -> Path:
    return DETAILS / (_SAFE.sub("_", key) + ".json")


def load_detail(key: str) -> Optional[dict]:
    return _read(detail_path(key), None)


def save_detail(key: str, detail: dict) -> None:
    _write(detail_path(key), {"cached_at": now_kst().isoformat(timespec="seconds"),
                              **detail})


# ---------------- 최초 관측 기록 ----------------
def _load_seen_raw() -> dict:
    raw = _read(SEEN_FILE, None)
    if raw is None:
        return {"_meta": {"baselines": {}, "runs": 0}, "keys": {}}
    # 구버전({key: "YYYY-MM-DD"}) 마이그레이션 — 전부 baseline 으로 본다
    if "keys" not in raw:
        return {
            "_meta": {"baselines": {"applyhome": min(raw.values())} if raw else {},
                      "baseline_at": min(raw.values()) if raw else None,
                      "runs": 1, "migrated": True},
            "keys": {k: {"first": v, "baseline": True,
                         "source_id": "applyhome"} for k, v in raw.items()},
        }
    raw.setdefault("_meta", {}).setdefault("baselines", {})
    return raw


def load_seen() -> dict[str, dict]:
    return _load_seen_raw()["keys"]


def seen_meta() -> dict:
    return _load_seen_raw()["_meta"]


def mark_seen(items: list[tuple[str, str]],
              today: Optional[date] = None) -> dict:
    """처음 보는 키를 기록한다. items 는 (key, source_id) 목록.

    baseline 은 **수집원별로** 잡는다. 전역으로 잡으면 나중에 수집원을 하나
    추가했을 때 그 수집원의 공고 전부가 '오늘의 신규'로 쏟아진다.
    수집원마다 첫 배치를 baseline 으로 찍어 신규에서 빼면 그 문제가 사라진다.

    돌려주는 값:
      {"new": [진짜 신규 키], "baselined": {source_id: 건수}, "baseline_sources": [...]}
    """
    today = today or today_kst()
    raw = _load_seen_raw()
    meta, store_keys = raw["_meta"], raw["keys"]
    baselines: dict = meta.setdefault("baselines", {})
    stamp = today.isoformat()

    # 이번 수집에서 처음 등장한 수집원 = 아직 baseline 이 없는 수집원
    sources_now = {sid for _, sid in items}
    baseline_sources = {sid for sid in sources_now if sid not in baselines}

    new_keys: list[str] = []
    baselined: dict[str, int] = {}

    for key, sid in items:
        if key in store_keys:
            continue
        is_base = sid in baseline_sources
        store_keys[key] = {"first": stamp, "baseline": is_base, "source_id": sid}
        if is_base:
            baselined[sid] = baselined.get(sid, 0) + 1
        else:
            new_keys.append(key)

    for sid in baseline_sources:
        baselines[sid] = stamp

    meta["runs"] = int(meta.get("runs") or 0) + 1
    meta["last_run_at"] = now_kst().isoformat(timespec="seconds")

    _write(SEEN_FILE, {"_meta": meta, "keys": store_keys})
    return {"new": new_keys, "baselined": baselined,
            "baseline_sources": sorted(baseline_sources)}


def first_seen(key: str) -> Optional[str]:
    rec = load_seen().get(key)
    return rec.get("first") if rec else None


def is_baseline(key: str) -> bool:
    rec = load_seen().get(key)
    return bool(rec and rec.get("baseline"))


# ---------------- 공고 수정사항 추적 ----------------
def _fingerprint(notice: dict) -> tuple[str, dict]:
    fields = {f: notice.get(f) for f in TRACKED_FIELDS}
    blob = json.dumps(fields, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16], fields


def track_revisions(notices: list[dict]) -> dict[str, list[dict]]:
    """이전 수집과 비교해 바뀐 필드를 이력으로 쌓는다.

    돌려주는 값: {key: [이번에 생긴 변경들]} — 변경이 있는 공고만 담긴다.
    """
    store = _read(REVISIONS_FILE, {}) or {}
    at = now_kst().isoformat(timespec="seconds")
    changed_now: dict[str, list[dict]] = {}

    for n in notices:
        key = n.get("key")
        if not key:
            continue
        fp, fields = _fingerprint(n)
        prev = store.get(key)
        if prev is None:
            store[key] = {"hash": fp, "fields": fields, "history": []}
            continue
        if prev.get("hash") == fp:
            continue
        diffs = [
            {"at": at, "field": f,
             "from": (prev.get("fields") or {}).get(f), "to": fields.get(f)}
            for f in TRACKED_FIELDS
            if (prev.get("fields") or {}).get(f) != fields.get(f)
        ]
        if diffs:
            history = (prev.get("history") or []) + diffs
            store[key] = {"hash": fp, "fields": fields, "history": history[-40:]}
            changed_now[key] = diffs

    _write(REVISIONS_FILE, store)
    return changed_now


def load_revisions() -> dict:
    return _read(REVISIONS_FILE, {}) or {}


def revision_history(key: str) -> list[dict]:
    return (load_revisions().get(key) or {}).get("history") or []


# ---------------- 수집원 상태 ----------------
def load_source_status() -> dict:
    return _read(SOURCE_STATUS_FILE, {}) or {}


def record_source(source_id: str, *, ok: bool, count: int = 0,
                  error: str = "", note: str = "", skipped: bool = False) -> dict:
    """수집원별 마지막 성공/실패 시각과 건수를 남긴다."""
    store = load_source_status()
    at = now_kst().isoformat(timespec="seconds")
    rec = store.get(source_id) or {}

    rec["last_attempt_at"] = at
    rec["last_count"] = count
    rec["note"] = note
    rec["skipped"] = skipped
    if skipped:
        rec["state"] = "건너뜀"
    elif ok:
        rec["state"] = "정상"
        rec["last_success_at"] = at
        rec["last_success_count"] = count
        rec["last_error"] = ""
        rec["consecutive_failures"] = 0
    else:
        rec["state"] = "실패"
        rec["last_error"] = error
        rec["consecutive_failures"] = int(rec.get("consecutive_failures") or 0) + 1

    store[source_id] = rec
    _write(SOURCE_STATUS_FILE, store)
    return rec


# ---------------- 북마크 ----------------
def load_bookmarks() -> list[str]:
    return _read(BOOKMARKS_FILE, []) or []


def toggle_bookmark(key: str) -> dict:
    marks = load_bookmarks()
    if key in marks:
        marks.remove(key)
        on = False
    else:
        marks.append(key)
        on = True
    _write(BOOKMARKS_FILE, marks)
    return {"key": key, "bookmarked": on, "count": len(marks)}
