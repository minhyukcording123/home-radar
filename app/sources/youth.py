"""서울 청년안심주택(구 역세권청년주택) 모집공고.

cheongyak-auto(MIT) 의 `cheong/sources/youth.py` 를 각색했다.

목록 화면(list.do)은 빈 표만 주고 JS 가 bbsListJson.json 을 POST 로 불러 채운다.
그래서 JSON 엔드포인트를 직접 읽는다. 필드(페이지 JS 기준):
  boardId 글번호 · nttSj 공고명 · optn1 공고게시일 · optn4 청약신청일
  optn3 담당부서/사업자 · optn2 1=공공 2=민간 · optn5 1=최초 2=추가
접수 마감일은 본문(content)의 '청약신청 : A ~ B' 줄에서 읽는다.

robots.txt (2026-10-30 확인): /robots.txt 가 403 으로 조회되지 않는다.
RFC 9309 는 robots.txt 가 4xx 면 제한 없음으로 보도록 정하고 있다.
"""
from __future__ import annotations

import re
import time
from datetime import date
from typing import Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ..models import Notice
from ..util import new_client, parse_date

LIST_URL = "https://soco.seoul.go.kr/youth/bbs/BMSR00015/list.do"
JSON_URL = "https://soco.seoul.go.kr/youth/pgm/home/yohome/bbsListJson.json"
BBS_ID = "BMSR00015"

_KIND = {"1": "공공", "2": "민간"}
_ROUND = {"1": "최초", "2": "추가"}
POLITE_DELAY = 0.7

# '‘26. 10. 11.' / '2026-10-11' / '10월 15일' 을 모두 받아낸다
_DT = re.compile(r"(?:(20\d{2}|\d{2})\s*[.\-/년]\s*)?(\d{1,2})\s*[.\-/월]\s*(\d{1,2})")


def apply_period(content: Optional[str], begin: Optional[date] = None
                 ) -> tuple[Optional[date], Optional[date]]:
    """본문의 '청약신청 : ‘26. 10. 11. (일) 09:00 ~ 10. 15. (목) 23:00' → (시작, 마감).

    마감에 연도가 없으면 시작 연도를 쓴다. 이상한 값(마감<시작, 60일 초과)은 버린다.
    """
    if not content:
        return begin, None
    text = BeautifulSoup(content, "html.parser").get_text("\n")
    for line in text.splitlines():
        if "청약신청" not in line or "~" not in line or "페이지" in line:
            continue
        found = []
        for m in _DT.finditer(line.split(":", 1)[-1] if ":" in line else line):
            yy, mm, dd = m.groups()
            year = (2000 + int(yy)) if yy and len(yy) == 2 else int(yy) if yy else None
            found.append((year, int(mm), int(dd)))
        if len(found) < 2:
            continue
        (y1, m1, d1), (y2, m2, d2) = found[0], found[-1]
        y1 = y1 or (begin.year if begin else None)
        if not y1:
            continue
        y2 = y2 or (y1 + 1 if m2 < m1 else y1)
        try:
            b, e = date(y1, m1, d1), date(y2, m2, d2)
        except ValueError:
            continue
        if b <= e and (e - b).days <= 60:
            return begin or b, e
    return begin, None


def _s(v) -> str:
    return "" if v is None else str(v).strip()


def from_json(it: dict) -> Optional[Notice]:
    bid = _s(it.get("boardId"))
    if not bid:
        return None
    begin, end = apply_period(it.get("content"), parse_date(_s(it.get("optn4"))))
    labels = " ".join(x for x in (_KIND.get(_s(it.get("optn2")), ""),
                                  _ROUND.get(_s(it.get("optn5")), "")) if x)
    return Notice(
        source="청년안심",
        source_id="youth",
        uid=bid,
        title=_s(it.get("nttSj")),
        category=f"청년안심주택 {labels}".strip(),
        region="서울",
        posted=parse_date(_s(it.get("optn1"))),
        begin=begin,
        end=end,
        url=urljoin(LIST_URL,
                    f"view.do?optn1={_s(it.get('optn1'))}&boardId={bid}&menuNo=400008"),
        extra={
            "supplier": _s(it.get("optn3")),
            # 공공/민간과 최초/추가를 **구조화해서** 둔다.
            # category 문자열만으로는 화면에서 가려내기 어렵다.
            "youth_kind": _KIND.get(_s(it.get("optn2")), ""),    # 공공 | 민간
            "youth_round": _ROUND.get(_s(it.get("optn5")), ""),  # 최초 | 추가
        },
    )


def parse_json(body) -> list[Notice]:
    rows = (body or {}).get("resultList") or []
    return [n for n in (from_json(it) for it in rows) if n]


def collect(today: date, pages: int = 6) -> tuple[list[Notice], list[str]]:
    """모집공고 게시판을 읽는다.

    **pages 를 넉넉히 둬야 하는 이유**: 이 게시판은 '추가모집'이 압도적으로 많아
    (실측 80건 중 73건) 최신 2페이지(20건)를 긁으면 전부 추가모집이고
    **최초모집 공고가 통째로 빠진다.** 6페이지면 60건까지 덮어 최초모집이 들어온다.

    공공/민간 구분: 요청에 `optn2=""` 를 보내 둘 다 받는다. 다만 2026-11-01
    실측으로는 이 게시판에 **민간임대(공공지원민간임대)만 올라온다** — 80건 전부.
    공공임대분은 여기 올라오지 않으므로, 0건일 때 '수집 실패'로 오해하지 않도록
    아래에서 내역을 경고(정보)로 남긴다.
    """
    warnings: list[str] = []
    out: list[Notice] = []
    seen: set[str] = set()

    with new_client() as client:
        for p in range(1, pages + 1):
            try:
                r = client.post(JSON_URL, data={
                    "bbsId": BBS_ID, "pageIndex": p, "searchAdresGu": "",
                    "searchCondition": "", "searchKeyword": "",
                    "optn2": "", "optn5": ""})
                r.raise_for_status()
                rows = parse_json(r.json())
            except Exception as e:  # noqa: BLE001
                warnings.append(f"청년안심 p{p} 실패: {e}")
                break

            for n in rows:
                if n.key in seen:
                    continue
                seen.add(n.key)
                out.append(n)

            if not rows:
                break
            if p < pages:
                time.sleep(POLITE_DELAY)

    # 무엇이 몇 건 들어왔는지 남긴다 — '0건'이 수집 실패인지 원래 없는 것인지
    # 구분할 수 있어야 한다.
    kinds: dict[str, int] = {}
    rounds: dict[str, int] = {}
    for n in out:
        k = (n.extra or {}).get("youth_kind") or "미상"
        rd = (n.extra or {}).get("youth_round") or "미상"
        kinds[k] = kinds.get(k, 0) + 1
        rounds[rd] = rounds.get(rd, 0) + 1
    if out:
        warnings.append(
            "[정보] 청년안심 수집: "
            + " · ".join(f"{k} {c}건" for k, c in sorted(kinds.items()))
            + " / " + " · ".join(f"{k} {c}건" for k, c in sorted(rounds.items())))
        if not kinds.get("공공"):
            warnings.append(
                "[정보] 청년안심 공공임대 공고 0건 — 수집 실패가 아니라 "
                "이 모집공고 게시판에 민간임대(공공지원민간임대)만 올라온다")

    return out, warnings
