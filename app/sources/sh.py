"""SH(서울주택도시공사) 공고 — 서울주거포털 목록.

cheongyak-auto(MIT) 의 `cheong/sources/sh.py` 를 각색했다(bs4 유지, httpx 로 교체).

SH 는 공식 공고 API 가 없다. 서울주거포털(housing.seoul.go.kr)이 SH 공고를
표로 정리해 주므로 그것을 읽는다. 열 순서가 게시판마다 달라 **열 이름**으로 매핑한다.

  임대: 번호 | 청약유형 | 공고명 | 공고게시일 | 발표일 | 모집상태 | 담당부서 | 링크
  분양: 번호 | 청약유형 | 공고명 | 공고게시일 | 발표일 | 담당부서 | 링크

목록에는 **접수기간이 없다**(공고문 안에 있다) → 게시일·발표일·상태만 쓰고,
접수 시작/마감은 비워 둔다. 그래서 SH 공고는 D-day 가 뜨지 않고
Notice.is_closed 의 보수적 추정(게시 후 21일)으로 마감을 가늠한다.

robots.txt (2026-10-30 확인): `User-agent: * / Allow: /` — 전체 허용.
그래도 공개 화면이므로 페이지 사이에 간격을 두고 가볍게 읽는다.
"""
from __future__ import annotations

import re
import time
from datetime import date

from bs4 import BeautifulSoup

from ..models import Notice
from ..util import new_client, parse_date

BASE = "https://housing.seoul.go.kr"
BOARDS = {
    "publicLease": f"{BASE}/site/main/sh/publicLease/list",
    "publicSale": f"{BASE}/site/main/sh/publicSale/list",
}
BOARD_LABEL = {"publicLease": "공공임대", "publicSale": "공공분양"}

COLS = {"청약유형": "category", "공고명": "title", "공고게시일": "posted",
        "발표일": "announce", "모집상태": "status"}

POLITE_DELAY = 0.7   # 공개 화면이라 간격을 둔다 (초)


def parse_list(html: str, board: str) -> list[Notice]:
    soup = BeautifulSoup(html, "html.parser")
    table = soup.select_one(".board-list table") or soup.find("table")
    if table is None:
        return []

    heads = [th.get_text(strip=True) for th in table.select("thead th")]
    if not heads:
        return []

    out: list[Notice] = []
    for tr in table.select("tbody tr"):
        tds = tr.find_all("td")
        if len(tds) < len(heads):
            continue
        row = {COLS[h]: tds[i].get_text(" ", strip=True)
               for i, h in enumerate(heads) if h in COLS}
        title = (row.get("title") or "").strip()
        if not title:
            continue

        a = tr.select_one('a[href*="seq="]')
        href = a.get("href", "") if a else ""
        if href.startswith("/"):
            href = BASE + href
        m = re.search(r"seq=(\d+)", href)
        uid = m.group(1) if m else f"{board}:{title}"

        category = (row.get("category") or "").strip()
        out.append(Notice(
            source="SH",
            source_id="sh",
            uid=uid,
            title=title,
            category=" ".join(x for x in (BOARD_LABEL.get(board, ""), category) if x),
            region="서울",
            posted=parse_date(row.get("posted")),
            announce=parse_date(row.get("announce")),
            status=(row.get("status") or "").strip(),
            url=href,
            extra={"board": board},
        ))
    return out


def collect(today: date, pages: int = 3) -> tuple[list[Notice], list[str]]:
    warnings: list[str] = []
    out: list[Notice] = []
    seen: set[str] = set()

    with new_client() as client:
        for board, url in BOARDS.items():
            for cp in range(1, pages + 1):
                try:
                    r = client.get(url, params={"cp": cp, "supplyType": board})
                    r.raise_for_status()
                    rows = parse_list(r.text, board)
                except Exception as e:  # noqa: BLE001
                    warnings.append(f"SH {BOARD_LABEL.get(board, board)} p{cp} 실패: {e}")
                    break

                added = 0
                for n in rows:
                    if n.key in seen:
                        continue
                    seen.add(n.key)
                    out.append(n)
                    added += 1

                if not rows:
                    break
                if cp < pages:
                    time.sleep(POLITE_DELAY)

    if not out and not warnings:
        warnings.append("SH 목록을 읽었지만 공고가 0건이다 — 표 구조가 바뀐 것일 수 있다")
    return out, warnings
