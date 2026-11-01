"""내 조건 프로필.

cheongyak-auto(MIT) 의 스키마에서 출발해 청약 자격 판정에 필요한 항목을 넓혔다.
  - 소득을 근로/사업으로 나눴다 (인정 방식이 다르다)
  - 자산에 부채를 더해 순자산을 볼 수 있게 했다
  - 거주기간을 넣었다 (해당지역 우선공급 요건)
  - 청약통장을 가입일·납입횟수·납입인정금액으로 쪼갰다
  - 희망 최대 분양가를 넣었다

profile.json 은 .gitignore 에 있다 — 생년·소득·자산이 들어가므로 커밋하지 않는다.

저장 계층 (2026-11-01 추가)
---------------------------
Render 무료 플랜은 영구 디스크가 없어 파일만으로는 재시작을 못 버틴다.
그래서 원격 저장소(`remote_store`)를 **첫 번째 자리**로 둔다.

  읽기  원격 DB → 로컬 파일 → PROFILE_JSON 환경변수 → 기본값
  쓰기  원격 DB + 로컬 파일 (원격이 켜져 있으면 원격 성공이 곧 저장 성공)

원격이 켜져 있으면 PC 와 아이폰이 같은 레코드를 보므로 한쪽에서 고치면
다른 쪽에도 바로 반영된다. 원격이 꺼져 있으면 예전 동작 그대로다.

**실패를 성공이라고 말하지 않는다.** 원격 쓰기가 실패하면 save() 가 예외를
던지고 화면은 실패를 표시한다 — '저장됐다'고 해놓고 재시작하면 사라지는 것이
가장 나쁘다.
"""
from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path
from typing import Optional

from . import remote_store, store
from .util import parse_date, today_kst

REMOTE_KEY = "profile"

ROOT = Path(__file__).resolve().parent.parent


def profile_file() -> Path:
    """내 조건을 저장할 파일 위치.

    우선순위
      1. `PROFILE_FILE` 환경변수 (경로를 직접 지정)
      2. `DATA_DIR` 이 설정돼 있으면 그 안의 profile.json
         (호스팅에 영구 디스크를 붙이면 자동으로 거기 쌓인다)
      3. 프로젝트 루트의 profile.json — 로컬에서 쓰던 그대로
    """
    raw = (os.environ.get("PROFILE_FILE") or "").strip()
    if raw:
        return Path(raw).expanduser()
    if (os.environ.get("DATA_DIR") or "").strip():
        return store.data_dir() / "profile.json"
    return ROOT / "profile.json"


PROFILE_FILE = profile_file()

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


def _read_raw() -> tuple[dict, str, str]:
    """저장된 원본과 출처를 찾는다.

    돌려주는 값: (원본 dict, 출처 이름, 경고 문구)
    출처는 "원격 DB" / "로컬 파일" / "환경변수" / "기본값".

    원격을 읽다 실패하면 로컬로 내려가되 **경고를 남긴다.** 조용히 기본값으로
    돌아가면, 사용자가 그 위에 저장을 눌렀을 때 원격의 진짜 값을 덮어쓴다.
    """
    warn = ""
    if remote_store.enabled():
        try:
            data = remote_store.get(REMOTE_KEY)
            if data:
                return data, "원격 DB", ""
        except remote_store.RemoteError as e:
            # 읽기 실패는 치명적이지 않다 — 화면은 떠야 한다. 대신 분명히 알린다.
            warn = str(e)

    path = profile_file()
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data:
                return data, "로컬 파일", warn
        except (json.JSONDecodeError, OSError):
            pass

    raw = os.environ.get("PROFILE_JSON")
    if raw:
        try:
            data = json.loads(raw)
            if data:
                return data, "환경변수", warn
        except json.JSONDecodeError:
            # 환경변수가 깨져 있어도 서버는 떠야 한다 — 기본값으로 간다
            pass

    return {}, "기본값", warn


def load() -> dict:
    """내 조건을 읽는다: 원격 DB → 로컬 파일 → 환경변수 → 기본값.

    원격이 켜져 있으면 그게 진실의 출처다 — PC 와 아이폰이 같은 값을 본다.
    원격이 꺼져 있거나 읽히지 않으면 예전 순서(파일 → 환경변수)로 내려간다.
    """
    data, _, _ = _read_raw()
    return merge(data)


def load_detailed() -> dict:
    """화면이 '어디서 읽었는지'와 '무엇이 기본값인지'까지 알아야 할 때.

    `provided` 는 **사용자가 실제로 저장한 키 목록**이다. 기본값과 저장값을
    화면에서 구분해 보여주기 위해 쓴다 (안 채운 칸을 채운 것처럼 보이면 안 된다).
    """
    data, origin, warn = _read_raw()
    prof = merge(data)
    provided = sorted(k for k in (data or {}) if k in DEFAULT)
    return {"profile": prof, "origin": origin, "warning": warn,
            "provided": provided, "saved": origin != "기본값"}


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


def _write_local(prof: dict) -> bool:
    """로컬 파일에도 써 둔다 (원격이 잠깐 안 될 때의 보조)."""
    try:
        path = profile_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(prof, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        tmp.replace(path)      # 쓰다가 죽어도 반쪽짜리 파일이 남지 않게
        return True
    except OSError:
        return False


def save(data: dict) -> dict:
    """내 조건을 저장한다.

    원격이 켜져 있으면 **원격 저장에 성공해야 성공이다.** 실패하면
    `remote_store.RemoteError` 가 그대로 올라가고 화면은 실패를 표시한다.
    로컬 파일에도 함께 쓰지만, 임시 디스크라 그것만으로는 보존됐다고 할 수 없다.
    """
    prof = merge(data)
    local_ok = _write_local(prof)

    if remote_store.enabled():
        remote_store.put(REMOTE_KEY, prof)      # 실패하면 여기서 예외
        return {"profile": prof, "persisted": "원격 DB",
                "local_ok": local_ok, "verified": True}

    if not local_ok:
        raise OSError("내 조건을 저장하지 못했습니다 (파일 쓰기 실패)")
    return {"profile": prof, "persisted": "로컬 파일",
            "local_ok": True, "verified": True,
            "warning": "원격 저장소가 설정되지 않아 서버 재시작 시 "
                       "사라질 수 있습니다"}


def clear() -> dict:
    """내 조건을 지우고 기본값으로 되돌린다."""
    removed = []
    if remote_store.enabled():
        remote_store.delete(REMOTE_KEY)         # 실패하면 예외
        removed.append("원격 DB")
    path = profile_file()
    if path.exists():
        try:
            path.unlink()
            removed.append("로컬 파일")
        except OSError:
            pass
    return {"cleared": removed, "profile": merge({})}


def exists() -> bool:
    """저장된 내 조건이 어딘가에 있는가 (원격 포함)."""
    _, origin, _ = _read_raw()
    return origin != "기본값"


def export_env_value() -> str:
    """지금 조건을 `PROFILE_JSON` 환경변수에 넣을 수 있는 한 줄 JSON 으로.

    무료 플랜(임시 디스크)에서 설정을 영구 보존하는 통로다 —
    화면에서 복사해 호스팅 대시보드의 환경변수에 붙여 넣는다.
    """
    return json.dumps(load(), ensure_ascii=False, separators=(",", ":"))


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
