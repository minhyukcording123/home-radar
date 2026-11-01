"""공통 유틸 — 시각, 날짜 파싱, HTTP.

cheongyak-auto(MIT) 의 util 설계를 참고해 httpx 기반으로 각색했다.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import httpx

KST = timezone(timedelta(hours=9))

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36")

# 청약홈은 세션 쿠키를 먼저 받아야 AJAX 호출을 받아준다
APPLYHOME_HOST = "https://www.applyhome.co.kr"
CALENDAR_PAGE = f"{APPLYHOME_HOST}/ai/aib/selectSubscrptCalenderView.do"

SIDO = ["서울", "부산", "대구", "인천", "광주", "대전", "울산", "세종",
        "경기", "강원", "충북", "충남", "전북", "전남", "경북", "경남", "제주"]

_SIDO_ALIAS = {
    "서울특별시": "서울", "부산광역시": "부산", "대구광역시": "대구",
    "인천광역시": "인천", "광주광역시": "광주", "대전광역시": "대전",
    "울산광역시": "울산", "세종특별자치시": "세종", "세종시": "세종",
    "경기도": "경기", "강원도": "강원", "강원특별자치도": "강원",
    "충청북도": "충북", "충청남도": "충남",
    "전라북도": "전북", "전북특별자치도": "전북", "전라남도": "전남",
    "경상북도": "경북", "경상남도": "경남",
    "제주도": "제주", "제주특별자치도": "제주",
}


def today_kst() -> date:
    return datetime.now(KST).date()


def now_kst() -> datetime:
    return datetime.now(KST)


# 수집원이 지역을 특정하지 못했을 때 쓰는 값들.
# **이건 지역 이름이 아니다.** 관심지역 필터가 이걸 '전국이라는 시도'로 다루면
# 전국 대상 공고가 숨겨져 버린다 (eligibility._check_region 참고).
NATIONWIDE = {"전국", "전지역", "전체"}


def to_sido(text: Optional[str]) -> str:
    """주소나 지역명에서 시도 2글자를 뽑아낸다."""
    if not text:
        return ""
    t = str(text).strip()
    for full, short in _SIDO_ALIAS.items():
        if t.startswith(full):
            return short
    for s in SIDO:
        if t.startswith(s) or s in t[:6]:
            return s
    return t.split()[0] if t.split() else ""


def sido_in_text(text: Optional[str]) -> str:
    """문자열 **어디에서든** 시도 이름을 찾는다. 못 찾으면 빈 문자열.

    `to_sido()` 는 앞 6글자만 본다. 그래서 LH 공고처럼 지역이 뒤쪽이나
    괄호 안에 있는 제목을 놓친다.

        '장애인 자립특화형 주택 여기가 입주자 모집공고(경기도 김포시)'  → 경기
        '[인천지역본부] 26년 2차 고령자 매입임대주택 예비입주자 모집'   → 인천
        '경남 거창군 신원면 다자녀 매입임대주택 예비입주자 모집공고'     → 경남

    **가장 앞에 나온 것**을 고른다. 공고 제목은 지역을 앞에 두는 편이고,
    단지명에 우연히 섞인 지역명보다 앞쪽 표기가 신뢰할 만하다.
    이 함수는 수집원이 지역을 못 준 경우의 **보조 수단**으로만 쓴다.
    """
    if not text:
        return ""
    t = str(text)
    best_at, best = len(t) + 1, ""
    # 정식 명칭(경기도·인천광역시…)을 먼저 본다 — 더 분명한 근거다
    for full, short in _SIDO_ALIAS.items():
        at = t.find(full)
        if at != -1 and at < best_at:
            best_at, best = at, short
    for s in SIDO:
        at = t.find(s)
        if at != -1 and at < best_at:
            best_at, best = at, s
    return best


# 구분자는 최대 2글자까지 허용한다.
# '2026-11-02' / '20261102' / '2026.11.02' 뿐 아니라
# 공고문에 흔한 '2026년 11월 2일'('년'+공백=2글자)도 읽어야 한다.
_DATE_RE = re.compile(r"(\d{4})\D{0,2}(\d{1,2})\D{0,2}(\d{1,2})")


def parse_date(value) -> Optional[date]:
    """'2026-11-02', '20261102', '2026.11.02' 등을 date 로."""
    if value in (None, ""):
        return None
    if isinstance(value, date):
        return value
    m = _DATE_RE.search(str(value))
    if not m:
        return None
    y, mo, d = (int(x) for x in m.groups())
    try:
        return date(y, mo, d)
    except ValueError:
        return None


def parse_period(text: str) -> tuple[Optional[date], Optional[date]]:
    """'2026.11.10 ~ 2026.11.12' 같은 기간 문자열에서 시작/끝을 뽑는다."""
    if not text:
        return None, None
    found = [parse_date(m.group(0)) for m in _DATE_RE.finditer(str(text))]
    found = [d for d in found if d]
    if not found:
        return None, None
    return found[0], found[-1]


def iso(d: Optional[date]) -> Optional[str]:
    return d.isoformat() if d else None


def new_client() -> httpx.Client:
    return httpx.Client(
        timeout=httpx.Timeout(20.0, connect=10.0),
        follow_redirects=True,
        headers={"User-Agent": UA},
    )
