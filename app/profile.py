"""내 조건 프로필.

cheongyak-auto(MIT) 의 스키마에서 출발해 청약 자격 판정에 필요한 항목을 넓혔다.
  - 소득을 근로/사업으로 나눴다 (인정 방식이 다르다)
  - 자산에 부채를 더해 순자산을 볼 수 있게 했다
  - 거주기간을 넣었다 (해당지역 우선공급 요건)
  - 청약통장을 가입일·납입횟수·납입인정금액으로 쪼갰다
  - 희망 최대 분양가를 넣었다

profile.json 은 .gitignore 에 있다 — 생년·소득·자산이 들어가므로 커밋하지 않는다.
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
    # ---- 기본 ----
    "birth": "",                   # YYYY-MM-DD 또는 YYYY
    "marital": "미혼",             # 미혼 | 예비신혼 | 신혼 | 기혼
    "marriage_date": "",           # 혼인신고일 (신혼 7년 이내 판단)
    "household_size": 1,           # 가구원 수
    "children": 0,                 # 미성년 자녀 수
    "newborn": False,              # 2세 미만 자녀 또는 임신 중
    "single_parent": False,

    # ---- 세대 ----
    "is_householder": True,        # 본인이 세대주인가
    "household": "단독세대주",      # 단독세대주 | 부모님세대원 | 기타
    "household_homeless": True,    # 세대원 전원 무주택

    # ---- 주택 보유 ----
    "homeless": True,              # 본인 무주택
    "homeless_since": "",          # 무주택 기간 기산일 (YYYY-MM)
    "ever_owned": False,           # 과거 주택 소유 이력
    "ever_won": False,             # 과거 청약 당첨 이력 (재당첨 제한)

    # ---- 거주 ----
    "residence": "서울",           # 현재 거주 시도
    "residence_since": "",         # 해당 지역 전입일 (YYYY-MM)

    # ---- 소득 (월, 만원) ----
    # 근로소득과 사업소득을 나눠 받는다. 청약 소득 심사에서 인정 방식이 다르다
    # (사업소득은 종합소득세 신고 기준) — 합쳐서 '소득 인정액'이라고 부르지 않는다.
    "income_work": None,           # 본인 근로소득 (세전 월평균)
    "income_business": None,       # 본인 사업·기타소득 (월 환산)
    "monthly_income": None,        # 구버전 호환 — 쓰지 않는다

    # ---- 배우자 소득 (월, 만원) ----
    # 신혼부부 유형은 외벌이/맞벌이 한도가 다르다(행복주택 100/120,
    # 신혼희망타운 130/200). 배우자 소득을 모르면 두 한도 사이가 '확인 필요'로
    # 남으므로, 입력하면 판정이 좁혀진다.
    "dual_income": False,          # 맞벌이 여부 (배우자 소득이 있는가)
    "spouse_income_work": None,    # 배우자 근로소득
    "spouse_income_business": None,  # 배우자 사업·기타소득

    # ---- 자산 (만원) ----
    "assets": None,                # 총자산 (부동산·예금·전세보증금 등)
    "debts": 0,                    # 총부채
    "car": 0,                      # 자동차가액
    # 특별공급·뉴:홈 일반형은 총자산이 아니라 **부동산가액**만 본다
    # (2026년 기준 2억 1,550만원). 분리 입력하지 않으면 '총자산 전체가 한도 이하'
    # 일 때만 충족을 확정할 수 있어서, 입력하면 판정이 정확해진다.
    "real_estate": None,           # 보유 부동산가액 (토지·건축물)

    # ---- 청약통장 ----
    "has_account": True,
    "account_opened": "",          # 가입일 (YYYY-MM-DD 또는 YYYY-MM)
    "account_payments": 0,         # 납입 횟수
    "account_recognized": None,    # 납입인정금액 (만원)

    # ---- 계층 ----
    "employed": True,
    "student": False,
    "low_income": False,           # 기초수급·차상위
    "disabled": False,
    "artist_or_startup": False,

    # ---- 희망 조건 ----
    "regions": ["서울", "경기", "인천"],
    "max_price": None,             # 희망 최대 분양가 (만원)
    "want": {
        "민영분양": True, "공공분양": True, "무순위": True,
        "공공임대": True, "민간임대": True, "오피스텔": False,
    },
    "account_mode": "상관없음",     # 상관없음 | 가입만까지 | 불필요만
    "remind_days": 3,
}

# 구버전 키 → 새 키
_MIGRATE = {"monthly_income": "income_work"}


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
        # 구버전 프로필: monthly_income 만 있으면 근로소득으로 옮긴다
        for old, new in _MIGRATE.items():
            if data.get(old) not in (None, "") and prof.get(new) in (None, ""):
                prof[new] = data[old]
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


# ---------------------------------------------------------------- 파생값
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


def _months_between(since: str, today: date) -> Optional[int]:
    d = parse_date(since)
    if d is None:
        # 'YYYY-MM' 형태 허용
        s = str(since or "").strip()
        if len(s) >= 7 and s[:4].isdigit() and s[5:7].isdigit():
            try:
                d = date(int(s[:4]), int(s[5:7]), 1)
            except ValueError:
                return None
        else:
            return None
    return (today.year - d.year) * 12 + (today.month - d.month)


def residence_months(prof: dict, today: Optional[date] = None) -> Optional[int]:
    """현재 거주 지역 거주기간(개월). 전입일 미입력이면 None."""
    return _months_between(prof.get("residence_since", ""), today or today_kst())


def account_months(prof: dict, today: Optional[date] = None) -> Optional[int]:
    """청약통장 가입기간(개월). 가입일 미입력이면 None."""
    return _months_between(prof.get("account_opened", ""), today or today_kst())


def account_years(prof: dict, today: Optional[date] = None) -> Optional[float]:
    m = account_months(prof, today)
    return None if m is None else round(m / 12, 1)


def marriage_years(prof: dict, today: Optional[date] = None) -> Optional[float]:
    m = _months_between(prof.get("marriage_date", ""), today or today_kst())
    return None if m is None else round(m / 12, 1)


def is_newlywed(prof: dict, today: Optional[date] = None) -> bool:
    """신혼부부 범위(혼인 7년 이내) 또는 예비신혼·신생아·한부모."""
    if prof.get("marital") == "예비신혼":
        return True
    if prof.get("newborn") or prof.get("single_parent"):
        return True
    if prof.get("marital") in ("신혼", "기혼"):
        y = marriage_years(prof, today)
        if y is None:
            return prof.get("marital") == "신혼"
        return y <= 7
    return False


def spouse_income(prof: dict) -> Optional[int]:
    """배우자 월소득 합계(만원). 둘 다 미입력이면 None."""
    w, b = prof.get("spouse_income_work"), prof.get("spouse_income_business")
    if w in (None, "") and b in (None, ""):
        return None
    return int(w or 0) + int(b or 0)


def is_dual_income(prof: dict) -> Optional[bool]:
    """맞벌이인가. 판단할 근거가 없으면 None (= 확정하지 않는다).

    근거 순서:
      1. 배우자 소득을 입력했다 → 0보다 크면 맞벌이, 0이면 외벌이로 확정
      2. 맞벌이 체크박스를 켰다 → 맞벌이로 확정
      3. 미혼이면 맞벌이가 성립하지 않는다 → 외벌이로 확정
      4. 그 밖에는 None — 신혼 유형에서 외벌이/맞벌이 한도 사이가
         '확인 필요'로 남는다 (함부로 유리하게 잡지 않는다)
    """
    sp = spouse_income(prof)
    if sp is not None:
        return sp > 0
    if prof.get("dual_income"):
        return True
    if prof.get("marital") == "미혼":
        return False
    return None


def derived(prof: dict, today: Optional[date] = None) -> dict:
    """화면에 같이 보여줄 파생값 모음."""
    today = today or today_kst()
    from .rules import income_base, income_ratio, net_assets

    return {
        "age": age(prof, today),
        "spouse_income": spouse_income(prof),
        "is_dual_income": is_dual_income(prof),
        "residence_months": residence_months(prof, today),
        "account_months": account_months(prof, today),
        "account_years": account_years(prof, today),
        "marriage_years": marriage_years(prof, today),
        "is_newlywed": is_newlywed(prof, today),
        "income_total": (None if prof.get("income_work") in (None, "")
                         and prof.get("income_business") in (None, "")
                         else int(prof.get("income_work") or 0)
                         + int(prof.get("income_business") or 0)),
        "income_base_manwon": income_base(prof.get("household_size", 1)) // 10_000,
        "income_ratio": income_ratio(prof),
        "net_assets": net_assets(prof),
    }
