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
