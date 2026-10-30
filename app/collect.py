"""수집 → 보강 → 판정 → 알림 파이프라인.

역할을 둘로 나눴다.
  refresh()    : 바깥 세상에서 공고를 긁어와 캐시에 넣는다 (느리다, 네트워크)
  build_view() : 캐시 + 내 프로필로 추천/확인/제외와 알림을 만든다 (빠르다, 로컬)

프로필을 바꿀 때마다 다시 긁지 않아도 되도록 판정은 항상 조회 시점에 한다.
"""
from __future__ import annotations

from datetime import date
from typing import Optional

from . import store
from .eligibility import judge
from .models import Notice
from .sources import applyhome_cal, applyhome_detail
from .util import new_client, parse_date, today_kst

# 상세까지 보강할 공고 수 상한 — 접수 중/임박 공고만 보강하면 충분하다
DETAIL_LIMIT = 40


def _apply_detail(n: Notice, d: dict) -> None:
    """상세에서 얻은 값으로 Notice 를 덮어쓴다. 상세가 더 정확하다."""
    if d.get("error"):
        return
    n.address = d.get("location") or n.address
    n.households = d.get("total_units") or n.households
    n.phone = d.get("phone") or n.phone
    n.price = d.get("price") or n.price
    n.move_in = d.get("move_in") or n.move_in
    n.doc_url = d.get("doc_url") or n.doc_url
    if d.get("posted"):
        n.posted = parse_date(d["posted"]) or n.posted
    if d.get("announce"):
        n.announce = parse_date(d["announce"]) or n.announce
    # 접수기간은 상세에 있을 때만 교체한다 (오피스텔·무순위 상세엔 표가 없다)
    if d.get("begin"):
        n.begin = parse_date(d["begin"]) or n.begin
    if d.get("end"):
        n.end = parse_date(d["end"]) or n.end
    if d.get("receptions"):
        n.extra["receptions"] = d["receptions"]
    n.detail_loaded = True


def refresh(today: Optional[date] = None, back: int = 1, ahead: int = 6,
            enrich: bool = True, detail_limit: int = DETAIL_LIMIT) -> dict:
    """청약홈에서 공고를 다시 긁어 캐시를 갱신한다."""
    today = today or today_kst()
    notices, warnings = applyhome_cal.collect(today, back=back, ahead=ahead)

    # 처음 본 날 기록 — 매일 새 공고 알림의 근거
    store.mark_seen([n.key for n in notices], today)

    if enrich:
        # 접수가 안 끝난 공고를 마감 가까운 순으로 보강한다
        targets = [n for n in notices if not n.is_closed(today)]
        targets.sort(key=lambda x: (x.end or date.max))
        targets = targets[:detail_limit]

        with new_client() as client:
            client.get(applyhome_cal.CALENDAR_PAGE)  # 세션 한 번만 달군다
            for n in targets:
                cached = store.load_detail(n.key)
                if cached and not cached.get("error"):
                    _apply_detail(n, cached)
                    continue
                d = applyhome_detail.fetch(
                    n.house_manage_no, n.pblanc_no, n.house_secd, client=client)
                if d.get("error"):
                    warnings.append(f"{n.title} 상세 실패: {d['error']}")
                else:
                    store.save_detail(n.key, d)
                    _apply_detail(n, d)

    payload = store.save_notices([n.to_dict() for n in notices], warnings)
    return payload


def _rehydrate(raw: dict) -> Notice:
    """캐시된 dict 를 판정에 쓸 수 있는 Notice 로 되돌린다."""
    from .models import Event

    n = Notice(
        source=raw.get("source", "청약홈"),
        uid=raw.get("uid", ""),
        title=raw.get("title", ""),
        category=raw.get("category", ""),
        region=raw.get("region", ""),
        address=raw.get("address", ""),
        house_secd=raw.get("house_secd", "01"),
        house_manage_no=raw.get("house_manage_no", ""),
        pblanc_no=raw.get("pblanc_no", ""),
        posted=parse_date(raw.get("posted")),
        begin=parse_date(raw.get("begin")),
        end=parse_date(raw.get("end")),
        announce=parse_date(raw.get("announce")),
        households=raw.get("households", ""),
        url=raw.get("url", ""),
        doc_url=raw.get("doc_url", ""),
        phone=raw.get("phone", ""),
        price=raw.get("price", ""),
        move_in=raw.get("move_in", ""),
        status=raw.get("status", ""),
        detail_loaded=bool(raw.get("detail_loaded")),
        extra=raw.get("extra") or {},
    )
    n.events = [
        Event(date=parse_date(e.get("date")), code=e.get("code", ""),
              label=e.get("label", ""))
        for e in (raw.get("events") or [])
        if parse_date(e.get("date"))
    ]
    return n


def build_view(prof: dict, today: Optional[date] = None,
               include_closed: bool = False) -> dict:
    """캐시 + 프로필 → 버킷(추천/확인/제외) + 알림."""
    today = today or today_kst()
    cached = store.load_notices()
    if not cached:
        return {
            "ready": False,
            "message": "아직 수집된 공고가 없습니다. 먼저 새로고침을 실행하세요.",
            "buckets": {"추천": [], "확인": [], "제외": []},
            "alerts": {"new": [], "closing": [], "today": []},
            "stats": {},
        }

    seen = store.load_seen()
    buckets: dict[str, list[dict]] = {"추천": [], "확인": [], "제외": []}
    ignored = 0
    rows: list[dict] = []

    for raw in cached.get("notices") or []:
        n = _rehydrate(raw)
        closed = n.is_closed(today)
        if closed and not include_closed:
            continue
        v = judge(n, prof, today)
        if v.level == "무시":
            ignored += 1
            continue
        row = n.to_dict()
        row["verdict"] = v.to_dict()
        row["closed"] = closed
        row["days_left"] = n.days_left(today)
        row["first_seen"] = seen.get(n.key)
        rows.append(row)
        buckets[v.level].append(row)

    # 마감 임박 순 → 추천 안에서도 급한 것이 위로
    def urgency(r):
        dl = r.get("days_left")
        return (dl if dl is not None else 9999, r.get("title") or "")

    for b in buckets.values():
        b.sort(key=urgency)

    remind = int(prof.get("remind_days") or 3)
    stamp = today.isoformat()
    actionable = [r for r in rows if r["verdict"]["level"] in ("추천", "확인")
                  and not r["closed"]]

    alerts = {
        # 오늘 처음 관측한 공고
        "new": [r for r in actionable if r.get("first_seen") == stamp],
        # 접수 마감 N일 이내
        "closing": [r for r in actionable
                    if r.get("days_left") is not None and 0 <= r["days_left"] <= remind],
        # 오늘이 접수일인 공고
        "today": [r for r in actionable
                  if any(e.get("date") == stamp for e in (r.get("events") or []))],
    }

    return {
        "ready": True,
        "fetched_at": cached.get("fetched_at"),
        "warnings": cached.get("warnings") or [],
        "today": stamp,
        "buckets": buckets,
        "alerts": alerts,
        "stats": {
            "collected": cached.get("count", 0),
            "shown": len(rows),
            "추천": len(buckets["추천"]),
            "확인": len(buckets["확인"]),
            "제외": len(buckets["제외"]),
            "무시": ignored,
            "remind_days": remind,
        },
    }
