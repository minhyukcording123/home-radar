"""내 조건 프로필.

cheongyak-auto(MIT) 의 `cheong/profile.py` 스키마를 각색했다.
웹앱이므로 대화형 설문은 빼고, 파일 로드/저장 + 웹 UI 에서 쓰는 메타만 남겼다.
profile.json 은 .gitignore 에 들어 있다 — 개인정보라 커밋하지 않는다.
"""
from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path
from typing import Optional

from .util import parse_date, today_kst

ROOT = Path(__file__).resolve().parent.parent
PROFILE_FILE = ROOT / "profile.json"

DEFAULT: dict = {
    "birth": "",                  # YYYY-MM-DD 또는 YYYY
    "marital": "미혼",            # 미혼 | 예비신혼 | 신혼 | 기혼
    "household_size": 1,          # 가구원 수 (소득 기준 계산에 쓰인다)
    "children": 0,
    "newborn": False,             # 2세 미만 자녀 또는 임신 중
    "single_parent": False,

    "homeless": True,             # 본인 무주택
    "ever_owned": False,          # 과거 주택 소유 이력
    "household": "단독세대주",     # 단독세대주 | 부모님세대원 | 기타
    "household_homeless": True,   # 세대원 전원 무주택

    "employed": True,
    "student": False,
    "monthly_income": None,       # 세전 월소득 (만원)
    "assets": 0,                  # 총자산 (만원)
    "car": 0,                     # 자동차가액 (만원)
    "low_income": False,
    "disabled": False,
    "artist_or_startup": False,

    "account_years": 0,           # 청약통장 가입 기간 (년)
    "account_payments": 0,        # 납입 횟수
    "has_account": True,          # 청약통장 보유 여부

    "residence": "서울",
    "regions": ["서울", "경기", "인천"],
    "want": {
        "임대": True, "공공분양": True, "민영분양": True,
        "오피스텔": False, "민간임대": True,
    },
    "account_mode": "상관없음",    # 상관없음 | 가입만까지 | 불필요만
    "remind_days": 3,             # 마감 N일 전부터 알림
}


def load() -> dict:
    """PROFILE_JSON 환경변수 → profile.json → 기본값 순서."""
    data: dict = {}
    raw = os.environ.get("PROFILE_JSON")
    if raw:
        data = json.loads(raw)
    elif PROFILE_FILE.exists():
        data = json.loads(PROFILE_FILE.read_text(encoding="utf-8"))
    return merge(data)


def merge(data: Optional[dict]) -> dict:
    """기본값 위에 입력을 덮어쓴다. want 는 키 단위로 병합."""
    prof = json.loads(json.dumps(DEFAULT))
    if data:
        prof.update({k: v for k, v in data.items() if k in DEFAULT})
        prof["want"] = {**DEFAULT["want"], **(data.get("want") or {})}
    if not prof.get("regions"):
        prof["regions"] = []
    return prof


def save(data: dict) -> dict:
    prof = merge(data)
    PROFILE_FILE.write_text(
        json.dumps(prof, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return prof


def exists() -> bool:
    return PROFILE_FILE.exists() or bool(os.environ.get("PROFILE_JSON"))


def age(prof: dict, today: Optional[date] = None) -> Optional[int]:
    """만 나이. 연도만 주면 그 해 12-31 생으로 보수적으로 잡는다."""
    today = today or today_kst()
    b = str(prof.get("birth") or "").strip()
    if not b:
        return None
    d = parse_date(b)
    if d is None and len(b) >= 4 and b[:4].isdigit():
        d = date(int(b[:4]), 12, 31)
    if d is None:
        return None
    return today.year - d.year - ((today.month, today.day) < (d.month, d.day))
