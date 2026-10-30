"""청약홈 청약일정 캘린더 수집 — API 키가 필요 없는 1차 수집원.

공개 웹 화면(selectSubscrptCalenderView.do)이 쓰는 AJAX 엔드포인트를 그대로 호출한다.
세션 쿠키를 먼저 받아야 응답을 주기 때문에 달력 페이지를 한 번 GET 한 뒤 POST 한다.

호출 방식은 analyst-sooin/apply-home 의 공개 동작을 참고해 확인했으나
코드는 복사하지 않고 httpx 로 독자 구현했다 (NOTICE.md 참고).

캘린더가 주는 것은 '일정 행'이다. 같은 공고가 특별공급·1순위·2순위로 날짜마다
따로 내려오므로, (HOUSE_MANAGE_NO, PBLANC_NO) 로 묶어 공고 단위 Notice 를 만들고
접수 시작/마감을 일정의 최소/최대 날짜로 도출한다.
"""
from __future__ import annotations

from datetime import date
from typing import Iterable

import httpx

from ..models import RCEPT_SE, Event, Notice
from ..util import CALENDAR_PAGE, APPLYHOME_HOST, new_client, to_sido

API_URL = f"{APPLYHOME_HOST}/ai/aib/selectSubscrptCalender.do"

AJAX_HEADERS = {
    "Content-Type": "application/json",
    "Referer": CALENDAR_PAGE,
    "gvPgmId": "AIB01M01",
    "ajaxAt": "Y",
    "X-Requested-With": "XMLHttpRequest",
}


def month_range(today: date, back: int = 1, ahead: int = 6) -> list[str]:
    """'YYYYMM' 목록. 기본은 지난달 ~ 6개월 뒤."""
    out = []
    for off in range(-back, ahead + 1):
        y, m = today.year, today.month + off
        while m < 1:
            y -= 1
            m += 12
        while m > 12:
            y += 1
            m -= 12
        out.append(f"{y}{m:02d}")
    return out


def _warm_session(client: httpx.Client) -> None:
    """달력 페이지를 한 번 열어 세션 쿠키를 받는다."""
    client.get(CALENDAR_PAGE)


def fetch_month(client: httpx.Client, year_month: str) -> list[dict]:
    r = client.post(
        API_URL,
        headers=AJAX_HEADERS,
        json={"reqData": {"inqirePd": year_month}},
    )
    r.raise_for_status()
    body = r.json()
    if body.get("exception"):
        raise RuntimeError(str(body["exception"]))
    return body.get("schdulList") or []


def _row_date(year_month: str, in_day) -> date | None:
    try:
        return date(int(year_month[:4]), int(year_month[4:6]), int(in_day))
    except (TypeError, ValueError):
        return None


def _is_noise(row: dict) -> bool:
    """RCEPT_SE '12' 등 공고가 붙지 않은 빈 행과 휴일 행을 버린다."""
    if row.get("RESTDE_AT") == "Y":
        return True
    if not row.get("HOUSE_NM") or not row.get("HOUSE_MANAGE_NO"):
        return True
    if str(row.get("RCEPT_SE") or "") not in RCEPT_SE:
        return True
    return False


def collect(today: date, back: int = 1, ahead: int = 6,
            months: Iterable[str] | None = None) -> tuple[list[Notice], list[str]]:
    """공고 단위 Notice 목록과 (월별) 수집 경고 메시지를 돌려준다."""
    months = list(months) if months else month_range(today, back, ahead)
    warnings: list[str] = []
    grouped: dict[tuple[str, str], Notice] = {}
    event_seen: set[tuple[str, str, str, date]] = set()

    with new_client() as client:
        _warm_session(client)
        for ym in months:
            try:
                rows = fetch_month(client, ym)
            except Exception as e:  # 한 달이 실패해도 전체를 죽이지 않는다
                warnings.append(f"{ym} 수집 실패: {e}")
                # 세션이 끊긴 경우를 대비해 다시 달군다
                try:
                    _warm_session(client)
                except Exception:
                    pass
                continue

            for row in rows:
                if _is_noise(row):
                    continue
                d = _row_date(ym, row.get("IN_DAY"))
                if d is None:
                    continue

                hmn = str(row["HOUSE_MANAGE_NO"])
                pbn = str(row.get("PBLANC_NO") or hmn)
                code = str(row["RCEPT_SE"])
                gkey = (hmn, pbn)

                n = grouped.get(gkey)
                if n is None:
                    n = Notice(
                        source="청약홈",
                        uid=f"{hmn}-{pbn}",
                        title=str(row.get("HOUSE_NM") or "").strip(),
                        region=to_sido(row.get("SUBSCRPT_AREA_CODE_NM")),
                        house_secd=str(row.get("HOUSE_SECD") or "01"),
                        house_manage_no=hmn,
                        pblanc_no=pbn,
                    )
                    grouped[gkey] = n

                ekey = (hmn, pbn, code, d)
                if ekey not in event_seen:
                    event_seen.add(ekey)
                    n.events.append(Event(date=d, code=code, label=RCEPT_SE[code]))

    notices = list(grouped.values())
    for n in notices:
        _finalize(n)
    notices.sort(key=lambda x: (x.begin or date.max, x.title))
    return notices, warnings


def _finalize(n: Notice) -> None:
    """일정 모음에서 접수 시작/마감과 유형 라벨을 도출한다."""
    n.events.sort(key=lambda e: (e.date, e.code))
    days = [e.date for e in n.events]
    if days:
        n.begin, n.end = min(days), max(days)
    labels: list[str] = []
    for e in n.events:
        if e.label not in labels:
            labels.append(e.label)
    n.category = " ".join(labels)
    n.url = (f"{APPLYHOME_HOST}/ai/aib/selectSubscrptCalenderView.do")
