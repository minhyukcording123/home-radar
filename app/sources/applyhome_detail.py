"""청약홈 공고 상세 보강 — 역시 API 키가 필요 없다.

캘린더는 '언제'만 주고 '무엇을'은 주지 않는다. 공급위치·공급규모·공급금액·
모집공고문 PDF 는 상세 화면에만 있으므로 필요할 때 공고 단위로 긁어 캐시한다.

상세 화면이 주택 구분(HOUSE_SECD)마다 다르다는 점은 apply-home 의 공개 동작에서
확인했으나, HTML 파싱은 HOME RADAR 가 필요한 항목만 추출하도록 새로 썼다.
"""
from __future__ import annotations

import re
from typing import Optional

import httpx

from ..util import APPLYHOME_HOST, CALENDAR_PAGE, new_client, parse_date, parse_period

APT_DETAIL = "/ai/aia/selectAPTLttotPblancDetail.do"
REMNDR_DETAIL = "/ai/aia/selectAPTRemndrLttotPblancDetailView.do"
PRMO_DETAIL = "/ai/aia/selectPRMOLttotPblancDetailView.do"

RECEPT_WORDS = ("특별공급", "일반공급", "1순위", "2순위", "무순위", "임의공급")


def detail_path(house_secd: str) -> str:
    s = str(house_secd or "01")
    if s in ("01", "09"):
        return APT_DETAIL
    if s in ("04", "06", "11"):
        return REMNDR_DETAIL
    return PRMO_DETAIL


_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"[\s ]+")


def _text(html: str) -> str:
    """태그를 떼고 공백을 정리한다."""
    if not html:
        return ""
    s = html.replace("<br>", " ").replace("<br/>", " ").replace("<br />", " ")
    return _WS.sub(" ", _TAG.sub("", s)).strip()


def _field(html: str, label: str) -> str:
    """<th>라벨</th><td>값</td> 에서 값을 꺼낸다."""
    m = re.search(
        r"<t[hd][^>]*>\s*" + re.escape(label) + r"\s*</t[hd]>\s*<td[^>]*>(.*?)</td>",
        html, re.S)
    return _text(m.group(1)) if m else ""


def _href_near(html: str, word: str) -> str:
    """특정 단어가 붙은 링크의 href."""
    m = re.search(r'href="([^"]+)"[^>]*>[^<]*' + re.escape(word), html, re.S)
    if not m:
        m = re.search(r'<a[^>]*>[^<]*' + re.escape(word) + r'[^<]*</a>', html, re.S)
        if not m:
            return ""
        m2 = re.search(r'href="([^"]+)"', m.group(0))
        return m2.group(1) if m2 else ""
    url = m.group(1)
    if url.startswith("/"):
        url = APPLYHOME_HOST + url
    return url


def _receptions(html: str) -> list[dict]:
    """접수 유형별 기간을 뽑는다. 해당지역/기타지역으로 나뉘는 표도 처리."""
    start = html.find("청약접수")
    if start < 0:
        start = 0
    end = html.find("당첨자", start)
    section = html[start: end if end > start else len(html)]

    out: list[dict] = []
    for m in re.finditer(r"<tr[^>]*>(.*?)</tr>", section, re.S):
        cells = [_text(c) for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", m.group(1), re.S)]
        if not cells:
            continue
        kind = next((c for c in cells[:2] if c in RECEPT_WORDS), "")
        if not kind:
            continue
        periods = []
        for c in cells:
            if c == kind:
                continue
            b, e = parse_period(c)
            if b:
                periods.append({"text": c, "begin": b.isoformat(), "end": (e or b).isoformat()})
        if periods:
            out.append({"type": kind, "periods": periods})
    return out


def _supply_total(html: str) -> str:
    for label in ("공급규모", "공급세대수", "총세대수"):
        v = _field(html, label)
        if v:
            return v
    return ""


def _price(html: str) -> str:
    """공급금액 표에서 대표 금액 범위를 한 줄로 요약한다 — 어디까지나 참고값이다.

    같은 표에 전용면적·연도 같은 숫자가 섞여 있어 하한이 오염되기 쉽다.
    그래서 분양가로 볼 수 있는 구간(5천만원 이상)만 남긴다.
    """
    start = html.find("공급금액")
    if start < 0:
        return ""
    end = html.find("기타사항", start)
    section = html[start: end if end > start else start + 20000]
    nums = []
    for m in re.finditer(r"(\d{1,3}(?:,\d{3})+)", _text(section)):
        try:
            v = int(m.group(1).replace(",", ""))
        except ValueError:
            continue
        # 만원 단위. 5천만원 ~ 50억 밖은 분양가가 아니라고 본다
        if 5_000 <= v <= 500_000:
            nums.append(v)
    if not nums:
        return ""
    lo, hi = min(nums), max(nums)
    return f"{lo:,}만원" if lo == hi else f"{lo:,}~{hi:,}만원"


def fetch(house_manage_no: str, pblanc_no: str, house_secd: str = "01",
          client: Optional[httpx.Client] = None) -> dict:
    """공고 상세를 dict 로. 실패하면 {'error': ...}."""
    own = client is None
    c = client or new_client()
    try:
        if own or not c.cookies:
            c.get(CALENDAR_PAGE)  # 세션 쿠키 — 넘겨받은 클라이언트는 이미 달궈져 있다
        r = c.post(
            APPLYHOME_HOST + detail_path(house_secd),
            headers={
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "Referer": CALENDAR_PAGE,
            },
            content=(f"houseManageNo={house_manage_no}"
                     f"&pblancNo={pblanc_no}&houseSecd={house_secd}").encode(),
        )
        r.raise_for_status()
        html = r.text
        if "에러페이지" in html or "관리자에게 문의" in html:
            return {"error": "상세 정보를 찾을 수 없습니다"}

        posted = _field(html, "모집공고일")
        announce_raw = _field(html, "당첨자 발표일") or _field(html, "당첨자발표일")
        receptions = _receptions(html)

        # 접수기간 전체에서 시작/마감을 다시 계산 (캘린더보다 정확하다)
        begins, ends = [], []
        for rc in receptions:
            for p in rc["periods"]:
                begins.append(p["begin"])
                ends.append(p["end"])

        return {
            "location": _field(html, "공급위치"),
            "total_units": _supply_total(html),
            "phone": _field(html, "문의처"),
            "posted": (parse_date(posted).isoformat() if parse_date(posted) else ""),
            "announce": (parse_date(announce_raw).isoformat()
                         if parse_date(announce_raw) else ""),
            "move_in": _field(html, "입주예정월"),
            "price": _price(html),
            "receptions": receptions,
            "begin": min(begins) if begins else "",
            "end": max(ends) if ends else "",
            "doc_url": _href_near(html, "모집공고문"),
        }
    except Exception as e:  # noqa: BLE001 — 보강 실패가 목록 조회를 막으면 안 된다
        return {"error": f"{type(e).__name__}: {e}"}
    finally:
        if own:
            c.close()
