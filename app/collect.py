"""수집 → 보강 → 중복제거 → 판정 → 알림 파이프라인.

역할을 둘로 나눴다.
  refresh()    : 바깥 세상에서 공고를 긁어 캐시에 넣는다 (느리다, 네트워크)
  build_view() : 캐시 + 내 프로필로 판정·알림·캘린더를 만든다 (빠르다, 로컬)

프로필을 바꿀 때마다 다시 긁지 않도록 판정은 항상 조회 시점에 한다.

수집원 하나가 죽어도 나머지는 살린다. 각 수집원의 마지막 성공 시각과 실패 사유는
store 에 남겨 화면에 그대로 보여준다 (= '원천 수집 오류 감지').
"""
from __future__ import annotations

import copy
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

import yaml

from . import dedup, store
from .eligibility import judge
from .models import Area, Event, Notice
from .rules import RULES_VERSION
from .sources import (applyhome_api, applyhome_cal, applyhome_detail, lh,
                      lh_supply, registry, sh, youth)
from .util import new_client, parse_date, today_kst

ROOT = Path(__file__).resolve().parent.parent
CONFIG_FILE = ROOT / "config.yaml"

DEFAULT_CONFIG = {
    "sources": {
        "applyhome": {"enabled": True, "back_months": 1, "ahead_months": 6,
                      "detail_limit": 40},
        # 공식 오픈API — 키가 있으면 켜진다. 같은 청약홈 범위를 권위 있는 값으로
        # 보강하고, 중복이면 dedup 이 공식 쪽(SOURCE_RANK 0)을 남긴다.
        "applyhome_api": {"enabled": True, "lookback_days": 60,
                          "per_page": 300, "max_pages": 10},
        "lh": {"enabled": True, "upp_types": ["05", "06", "13", "39"],
               "max_pages": 5, "detail_limit": 30, "supply_limit": 20},
        "sh": {"enabled": True, "pages": 3},
        "youth": {"enabled": True, "pages": 2},
        "ish": {"enabled": False, "pages": 2},
    }
}

# 상세 캐시 스키마 버전. 파서가 새 필드를 내놓기 시작하면 올린다.
# 올리면 예전 스키마로 저장된 캐시를 '오래됨'으로 보고 다시 받는다
# (캐시를 지우지 않아도 스스로 갱신된다).
DETAIL_SCHEMA = 4


def load_config() -> dict:
    """기본 설정 위에 config.yaml 을 덮어쓴다. 파일이 깨져 있어도 기본값으로 돈다."""
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    if CONFIG_FILE.exists():
        try:
            user = yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            user = {}
        for sid, scfg in (user.get("sources") or {}).items():
            cfg["sources"].setdefault(sid, {}).update(scfg or {})
    return cfg


def source_enabled(cfg: dict, sid: str) -> bool:
    """config.yaml 에 항목이 없으면 레지스트리의 기본값을 쓴다."""
    entry = cfg.get("sources", {}).get(sid)
    if entry is None:
        return bool((registry.BY_ID.get(sid) or {}).get("enabled", False))
    return bool(entry.get("enabled", False))


def source_cfg(cfg: dict, sid: str) -> dict:
    return cfg.get("sources", {}).get(sid) or {}


# ---------------------------------------------------------------- 상세 보강
def _apply_detail(n: Notice, d: dict) -> None:
    """상세에서 얻은 값으로 Notice 를 덮어쓴다. 상세가 더 정확하다."""
    if d.get("error"):
        return
    n.address = d.get("location") or n.address
    n.households = d.get("total_units") or n.households
    n.phone = d.get("phone") or n.phone
    n.price = d.get("price") or n.price
    if d.get("price_min") is not None:
        n.price_min = d["price_min"]
    if d.get("price_max") is not None:
        n.price_max = d["price_max"]
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
    if d.get("areas"):
        n.areas = [Area(name=a.get("name", ""), exclusive=a.get("exclusive", ""),
                        units=a.get("units", "")) for a in d["areas"]]
    n.detail_loaded = True


def _stale(cached: Optional[dict]) -> bool:
    """캐시가 지금 파서의 결과물이 아니면 True."""
    if not cached:
        return True
    return int(cached.get("schema") or 1) < DETAIL_SCHEMA


def _enrich_applyhome(notices: list[Notice], today: date, limit: int,
                      warnings: list[str]) -> None:
    """접수가 안 끝난 청약홈 공고를 마감 가까운 순으로 보강한다."""
    targets = [n for n in notices
               if n.source_id == "applyhome" and not n.is_closed(today)]
    targets.sort(key=lambda x: (x.end or date.max))
    targets = targets[:limit]
    if not targets:
        return

    with new_client() as client:
        client.get(applyhome_cal.CALENDAR_PAGE)   # 세션 한 번만 달군다
        for n in targets:
            cached = store.load_detail(n.key)
            if cached and not cached.get("error") and not _stale(cached):
                _apply_detail(n, cached)
                continue
            d = applyhome_detail.fetch(
                n.house_manage_no, n.pblanc_no, n.house_secd, client=client)
            if d.get("error"):
                warnings.append(f"[청약홈] {n.title} 상세 실패: {d['error']}")
            else:
                store.save_detail(n.key, {"schema": DETAIL_SCHEMA, **d})
                _apply_detail(n, d)


def _enrich_lh_supply(notices: list[Notice], limit: int,
                      warnings: list[str]) -> None:
    """LH 공고에 공급정보(주택형·면적·세대수·금액)를 붙인다.

    LH 공급정보 API 의 금액은 **원** 단위인데 Notice.price_min/max 는 만원이다.
    그대로 넣으면 화면에 1만 배로 찍히므로 여기서 환산한다.
    보증금·월임대료는 분양가가 아니라서 price_* 에 넣지 않고
    extra["supply"] 에 그대로 두어 상세 화면이 따로 보여준다.
    """
    if limit <= 0:
        return
    try:
        warnings.extend(lh_supply.enrich_supply(notices, limit=limit))
    except Exception as e:  # noqa: BLE001 — 보강 실패가 수집을 세우면 안 된다
        warnings.append(f"[LH] 공급정보 보강 실패: {type(e).__name__}: {e}")
        return

    for n in notices:
        supply = (n.extra or {}).get("supply") or {}
        rows = supply.get("rows") or []
        if not rows:
            continue
        # '분양가'(price)만 금액 범위로 쓴다 — 원 → 만원
        prices = [r["price"] for r in rows
                  if isinstance(r.get("price"), (int, float)) and r["price"] > 0]
        if not prices:
            continue
        lo, hi = int(min(prices) // 10_000), int(max(prices) // 10_000)
        if lo <= 0:
            continue
        # 다른 경로(청약홈 상세)가 이미 채웠으면 건드리지 않는다
        if n.price_min is None:
            n.price_min = lo
        if n.price_max is None:
            n.price_max = hi


# ---------------------------------------------------------------- 수집
def refresh(today: Optional[date] = None, enrich: bool = True,
            only: Optional[list[str]] = None) -> dict:
    """모든 수집원에서 공고를 긁어 캐시를 갱신한다."""
    today = today or today_kst()
    cfg = load_config()
    warnings: list[str] = []
    all_notices: list[Notice] = []
    source_results: dict[str, dict] = {}

    def run(sid: str, fn) -> None:
        """수집원 하나를 돌리고 상태를 기록한다. 예외는 여기서 흡수한다."""
        if only and sid not in only:
            return
        info = registry.describe(sid, cfg_enabled=source_enabled(cfg, sid))
        if not info["ready"]:
            source_results[sid] = store.record_source(
                sid, ok=False, skipped=True, note=info["blocked_by"])
            return
        try:
            rows, warns = fn()
            all_notices.extend(rows)
            warnings.extend(warns)
            source_results[sid] = store.record_source(
                sid, ok=True, count=len(rows),
                note=f"경고 {len(warns)}건" if warns else "")
        except Exception as e:  # noqa: BLE001 — 한 수집원 실패가 전체를 죽이지 않게
            msg = f"{type(e).__name__}: {e}"
            warnings.append(f"[{info.get('name', sid)}] 수집 실패: {msg}")
            source_results[sid] = store.record_source(sid, ok=False, error=msg)

    ac = source_cfg(cfg, "applyhome")
    run("applyhome", lambda: applyhome_cal.collect(
        today, back=int(ac.get("back_months", 1)),
        ahead=int(ac.get("ahead_months", 6))))

    aac = source_cfg(cfg, "applyhome_api")
    run("applyhome_api", lambda: applyhome_api.collect(
        today, lookback_days=int(aac.get("lookback_days", 60)),
        per_page=int(aac.get("per_page", 300)),
        max_pages=int(aac.get("max_pages", 10))))

    lc = source_cfg(cfg, "lh")
    run("lh", lambda: lh.collect(
        today, upp_types=lc.get("upp_types"),
        max_pages=int(lc.get("max_pages", 5)),
        detail_limit=int(lc.get("detail_limit", 30))))

    run("sh", lambda: sh.collect(
        today, pages=int(source_cfg(cfg, "sh").get("pages", 3))))
    run("youth", lambda: youth.collect(
        today, pages=int(source_cfg(cfg, "youth").get("pages", 2))))

    # 아직 수집기를 붙이지 않은 수집원 — 상태만 정직하게 남긴다.
    #   ish : robots.txt 가 AI 에이전트를 차단해 구현하지 않았다
    for sid, reason in (
        ("ish", "robots.txt 가 AI 에이전트를 명시 차단 — 미구현"),
    ):
        if only and sid not in only:
            continue
        info = registry.describe(sid, cfg_enabled=source_enabled(cfg, sid))
        source_results[sid] = store.record_source(
            sid, ok=False, skipped=True,
            note=f"{info['blocked_by']} / {reason}" if info["blocked_by"] else reason)

    # 청약홈 상세 보강
    if enrich:
        _enrich_applyhome(all_notices, today,
                          int(ac.get("detail_limit", 40)), warnings)
        _enrich_lh_supply(all_notices, int(lc.get("supply_limit", 20)), warnings)

    # 중복 제거 → 정정 공고 연결
    kept, merged = dedup.dedupe(all_notices)
    corrections = dedup.link_corrections(kept)

    rows = [n.to_dict() for n in kept]

    # 최초 관측 기록 — 수집원별 첫 배치는 baseline 으로 찍어 '신규'에서 뺀다
    seen_result = store.mark_seen([(n.key, n.source_id) for n in kept], today)
    # 공고 수정사항 추적
    revisions = store.track_revisions(rows)

    payload = store.save_notices(rows, warnings, sources=source_results,
                                 merged=merged, corrections=corrections)
    payload["new_keys"] = seen_result["new"]
    payload["baselined"] = seen_result["baselined"]
    payload["baseline_sources"] = seen_result["baseline_sources"]
    payload["revised_keys"] = list(revisions)
    return payload


# ---------------------------------------------------------------- 되살리기
def _rehydrate(raw: dict) -> Notice:
    n = Notice(
        source=raw.get("source", "청약홈"),
        source_id=raw.get("source_id", "applyhome"),
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
        price_min=raw.get("price_min"),
        price_max=raw.get("price_max"),
        move_in=raw.get("move_in", ""),
        status=raw.get("status", ""),
        detail_loaded=bool(raw.get("detail_loaded")),
        dup_of=raw.get("dup_of", ""),
        correction_of=raw.get("correction_of", ""),
        extra=raw.get("extra") or {},
    )
    n.events = [Event(date=parse_date(e.get("date")), code=e.get("code", ""),
                      label=e.get("label", ""))
                for e in (raw.get("events") or []) if parse_date(e.get("date"))]
    n.areas = [Area(name=a.get("name", ""), exclusive=a.get("exclusive", ""),
                    units=a.get("units", "")) for a in (raw.get("areas") or [])]
    return n


# ---------------------------------------------------------------- 뷰
def source_overview(cfg: Optional[dict] = None) -> list[dict]:
    """수집원별 설정·키·마지막 성공 시각 — 화면 상단에 그대로 쓴다."""
    cfg = cfg or load_config()
    status = store.load_source_status()
    out = []
    for s in registry.SOURCES:
        sid = s["id"]
        info = registry.describe(sid, cfg_enabled=source_enabled(cfg, sid))
        st = status.get(sid) or {}
        out.append({
            "id": sid,
            "name": info.get("name", sid),
            "access": info.get("access", ""),
            "covers": info.get("covers", ""),
            "robots": info.get("robots", ""),
            "note": info.get("note", ""),
            "signup": info.get("signup", ""),
            "enabled": info["enabled"],
            "needs_key": info["needs_key"],
            "has_key": info["has_key"],
            # 키 상태는 설정 여부·출처·마스킹된 값만 담긴다 (원문 키는 들어가지 않는다)
            "key": info.get("key") or {},
            "ready": info["ready"],
            "blocked_by": info["blocked_by"],
            "state": st.get("state", "미실행"),
            "last_success_at": st.get("last_success_at"),
            "last_success_count": st.get("last_success_count"),
            "last_attempt_at": st.get("last_attempt_at"),
            "last_error": st.get("last_error", ""),
            "consecutive_failures": st.get("consecutive_failures", 0),
        })
    return out


def build_view(prof: dict, today: Optional[date] = None,
               include_closed: bool = False) -> dict:
    """캐시 + 프로필 → 버킷 + 알림 + 캘린더 + 수집원 상태."""
    today = today or today_kst()
    cached = store.load_notices()
    overview = source_overview()

    if not cached:
        return {
            "ready": False,
            "message": "아직 수집된 공고가 없습니다. 먼저 새로고침을 실행하세요.",
            "today": today.isoformat(),
            "buckets": {"추천": [], "확인": [], "제외": []},
            "alerts": {"new": [], "closing": [], "opening": [], "today": [],
                       "revised": []},
            "stats": {}, "sources": overview, "calendar": [], "facets": {},
            "baselines": {}, "runs": 0, "warnings": [],
            "rules_version": RULES_VERSION,
        }

    seen = store.load_seen()
    revisions = store.load_revisions()
    bookmarks = set(store.load_bookmarks())
    seen_info = store.seen_meta()

    buckets: dict[str, list[dict]] = {"추천": [], "확인": [], "제외": []}
    rows: list[dict] = []
    ignored = 0

    for raw in cached.get("notices") or []:
        n = _rehydrate(raw)
        closed = n.is_closed(today)
        if closed and not include_closed:
            continue
        v = judge(n, prof, today)
        if v.level == "무시":
            ignored += 1
            continue

        seen_rec = seen.get(n.key) or {}
        row = n.to_dict()
        row["verdict"] = v.to_dict()
        row["closed"] = closed
        row["phase"] = n.phase(today)
        row["days_left"] = n.days_left(today)
        row["days_to_begin"] = n.days_to_begin(today)
        row["days_to_announce"] = n.days_to_announce(today)
        row["first_seen"] = seen_rec.get("first")
        row["is_baseline"] = bool(seen_rec.get("baseline"))
        row["bookmarked"] = n.key in bookmarks
        row["revisions"] = (revisions.get(n.key) or {}).get("history") or []
        rows.append(row)
        buckets[v.level].append(row)

    # 급한 것이 위로 — 접수중(마감 가까운 순) → 예정(시작 가까운 순) → 나머지
    def urgency(r):
        # '마감전(시작일 미상)'은 지금 판단해야 할 수 있으니 접수중 바로 뒤에 둔다
        phase_rank = {"접수중": 0, "마감전(시작일 미상)": 1, "예정": 2,
                      "발표대기": 3, "미정": 4, "마감": 5}
        dl = r.get("days_left")
        db = r.get("days_to_begin")
        primary = dl if dl is not None else (db if db is not None else 9999)
        return (phase_rank.get(r.get("phase"), 9), primary, r.get("title") or "")

    for b in buckets.values():
        b.sort(key=urgency)

    remind = int(prof.get("remind_days") or 3)
    stamp = today.isoformat()
    actionable = [r for r in rows
                  if r["verdict"]["level"] in ("추천", "확인") and not r["closed"]]

    alerts = {
        # 오늘 처음 관측 — 단, 수집원별 최초 배치(baseline)는 신규가 아니다
        "new": [r for r in actionable
                if r.get("first_seen") == stamp and not r.get("is_baseline")],
        # 접수 마감 N일 이내
        "closing": [r for r in actionable
                    if r.get("days_left") is not None
                    and 0 <= r["days_left"] <= remind],
        # 접수 시작 N일 이내
        "opening": [r for r in actionable
                    if r.get("days_to_begin") is not None
                    and 0 <= r["days_to_begin"] <= remind],
        # 오늘 일정이 있는 공고
        "today": [r for r in actionable
                  if any(e.get("date") == stamp for e in (r.get("events") or []))
                  or r.get("begin") == stamp or r.get("end") == stamp],
        # 공고 내용이 바뀐 것
        "revised": [r for r in actionable if r.get("revisions")],
    }

    return {
        "ready": True,
        "fetched_at": cached.get("fetched_at"),
        "warnings": cached.get("warnings") or [],
        "today": stamp,
        # 어떤 규칙 판본으로 판정했는지 — CLI·화면·테스트가 같은 값을 본다
        "rules_version": RULES_VERSION,
        # 수집원별로 언제 기준선을 잡았는지 — '신규'의 기준을 화면에 설명하기 위해
        "baselines": seen_info.get("baselines") or {},
        "runs": seen_info.get("runs", 0),
        "buckets": buckets,
        "alerts": alerts,
        "sources": overview,
        "calendar": _calendar(rows),
        "facets": _facets(rows),
        "stats": {
            "collected": cached.get("count", 0),
            "shown": len(rows),
            "추천": len(buckets["추천"]),
            "확인": len(buckets["확인"]),
            "제외": len(buckets["제외"]),
            "무시": ignored,
            "bookmarked": sum(1 for r in rows if r["bookmarked"]),
            "remind_days": remind,
        },
    }


def _calendar(rows: list[dict]) -> list[dict]:
    """날짜별 일정 — 캘린더와 주간 타임라인이 같은 데이터를 쓴다."""
    by_date: dict[str, list[dict]] = {}

    def put(d: Optional[str], kind: str, r: dict) -> None:
        if not d:
            return
        by_date.setdefault(d, []).append({
            "key": r["key"], "title": r["title"], "region": r.get("region", ""),
            "kind": r["verdict"]["kind"], "level": r["verdict"]["level"],
            "event": kind,
        })

    for r in rows:
        put(r.get("begin"), "접수시작", r)
        put(r.get("end"), "접수마감", r)
        put(r.get("announce"), "당첨발표", r)
        for e in r.get("events") or []:
            put(e.get("date"), e.get("label") or "일정", r)

    return [{"date": d, "items": items} for d, items in sorted(by_date.items())]


def _facets(rows: list[dict]) -> dict:
    """필터 후보 — 지역, 공급 유형, 태그, 수집원, 단계."""
    def census(values):
        c: dict[str, int] = {}
        for v in values:
            if v:
                c[v] = c.get(v, 0) + 1
        return [{"value": k, "count": n} for k, n in
                sorted(c.items(), key=lambda kv: (-kv[1], kv[0]))]

    return {
        "regions": census(r.get("region") for r in rows),
        "kinds": census(r["verdict"]["kind"] for r in rows),
        "tags": census(t for r in rows for t in r["verdict"]["tags"]),
        "sources": census(r.get("source") for r in rows),
        "phases": census(r.get("phase") for r in rows),
    }


def week_timeline(rows: list[dict], today: Optional[date] = None,
                  days: int = 14) -> list[dict]:
    """오늘부터 N일간 날짜별 일정 (주간 타임라인용)."""
    today = today or today_kst()
    cal = {c["date"]: c["items"] for c in _calendar(rows)}
    out = []
    for i in range(days):
        d = today + timedelta(days=i)
        s = d.isoformat()
        out.append({"date": s, "dow": d.weekday(), "items": cal.get(s, [])})
    return out
