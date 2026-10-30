"""로컬 저장소 — 수집 결과 캐시, 상세 캐시, '처음 본 날' 기록.

외부 DB 없이 JSON 파일만 쓴다. 개인용 단일 사용자 앱이라 이 정도로 충분하고,
data/ 는 .gitignore 에 들어 있다.

'처음 본 날'(seen.json)이 매일 새 공고 알림의 근거다. 공고에는 '언제 올라왔는지'가
항상 붙어 있지 않아서, 우리가 처음 관측한 날짜를 기준으로 삼는다.
"""
from __future__ import annotations

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

_SAFE = re.compile(r"[^0-9A-Za-z._-]")


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
def save_notices(notices: list[dict], warnings: list[str]) -> dict:
    payload = {
        "fetched_at": now_kst().isoformat(timespec="seconds"),
        "count": len(notices),
        "warnings": warnings,
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


# ---------------- 처음 본 날 ----------------
def load_seen() -> dict[str, str]:
    return _read(SEEN_FILE, {}) or {}


def mark_seen(keys: list[str], today: Optional[date] = None) -> list[str]:
    """아직 기록이 없는 키를 오늘 날짜로 등록하고, 새로 등록된 키를 돌려준다."""
    today = today or today_kst()
    seen = load_seen()
    fresh = [k for k in keys if k not in seen]
    if fresh:
        stamp = today.isoformat()
        for k in fresh:
            seen[k] = stamp
        _write(SEEN_FILE, seen)
    return fresh


def first_seen(key: str) -> Optional[str]:
    return load_seen().get(key)
