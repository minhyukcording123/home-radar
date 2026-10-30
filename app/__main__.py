"""명령줄 진입점 — 매일 자동 수집(작업 스케줄러)에 쓴다.

  python -m app refresh              # 전체 수집 + 상세 보강
  python -m app refresh --no-enrich  # 목록만 (빠름)
  python -m app refresh --only sh    # 특정 수집원만
  python -m app show                 # 지금 판정 결과를 터미널로 확인
  python -m app sources              # 수집원 상태 (키·robots·마지막 성공)

수집 범위(기간·페이지 수)는 config.yaml 에서 바꾼다.
"""
from __future__ import annotations

import argparse
import sys

from . import collect, profile
from .util import today_kst


def cmd_refresh(args) -> int:
    p = collect.refresh(enrich=not args.no_enrich, only=args.only)
    print(f"수집 완료: {p['count']}건  ({p['fetched_at']})")
    print(f"  새 공고 {len(p['new_keys'])} · 변경 {len(p['revised_keys'])}"
          f" · 중복 흡수 {len(p['merged'])} · 정정 {len(p['corrections'])}")
    if p.get("baseline_sources"):
        print(f"  기준선 수립(신규로 세지 않음): {', '.join(p['baseline_sources'])}"
              f" {p.get('baselined')}")
    print("  수집원:")
    for sid, st in (p.get("sources") or {}).items():
        line = f"    {sid:15s} {st.get('state', '?'):5s} {st.get('last_count', 0):>4}건"
        if st.get("note"):
            line += f"  — {st['note']}"
        if st.get("last_error"):
            line += f"  ! {st['last_error'][:70]}"
        print(line)
    for w in (p.get("warnings") or [])[:10]:
        print(f"  경고: {w[:120]}")
    return 0


def cmd_show(args) -> int:
    prof = profile.load()
    view = collect.build_view(prof, today_kst(), include_closed=args.closed)
    if not view["ready"]:
        print(view["message"])
        return 1
    s = view["stats"]
    print(f"[{view['today']}] 수집 {s['collected']}건 → 표시 {s['shown']}건"
          f" | 추천 {s['추천']} / 확인 {s['확인']} / 제외 {s['제외']}"
          f" | 규칙 {view['rules_version']}")
    a = view["alerts"]
    print(f"  새 공고 {len(a['new'])} · 마감 {s['remind_days']}일 이내 {len(a['closing'])}"
          f" · 접수 시작 {len(a['opening'])} · 오늘 일정 {len(a['today'])}"
          f" · 변경 {len(a['revised'])}")

    for level in ("추천", "확인"):
        rows = view["buckets"][level]
        if not rows:
            continue
        print(f"\n== {level} ({len(rows)}) ==")
        for r in rows[: args.limit]:
            dl, db = r["days_left"], r["days_to_begin"]
            when = (f"마감 D-{dl}" if dl is not None and dl >= 0
                    else (f"시작 D-{db}" if db is not None and db >= 0 else r["phase"]))
            print(f"  {when:>10} {r['region']:3s} {r['verdict']['kind']:5s}"
                  f" {r['title'][:38]}")
            c = r["verdict"]["counts"]
            print(f"             충족 {c['충족']} · 미확인 {c['미확인']}"
                  f" · 불충족 {c['불충족']}")
            for ch in (r["verdict"]["unmet"] + r["verdict"]["unknown"])[:2]:
                mark = "" if ch["source"]["official"] else " [샘플]"
                print(f"             - {ch['status']} {ch['name']}: "
                      f"{ch['detail'][:56]}{mark}")
    return 0


def cmd_sources(args) -> int:
    for s in collect.source_overview():
        flag = "O" if s["ready"] else "X"
        print(f"[{flag}] {s['name']}  ({s['access']})")
        print(f"     상태: {s['state']}"
              + (f" · 마지막 성공 {s['last_success_at']}"
                 f" ({s['last_success_count']}건)" if s["last_success_at"] else ""))
        if s["blocked_by"]:
            print(f"     막힘: {s['blocked_by']}")
        if s["needs_key"]:
            print(f"     키: {'설정됨' if s['has_key'] else '없음'}"
                  + (f" · 신청 {s['signup']}" if s.get("signup") else ""))
        if s.get("robots"):
            print(f"     robots: {s['robots']}")
        if s["last_error"]:
            print(f"     오류: {s['last_error'][:100]}")
        print(f"     범위: {s['covers']}")
        print()
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="app", description="HOME RADAR")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("refresh", help="모든 수집원에서 공고를 다시 긁는다")
    r.add_argument("--no-enrich", action="store_true", help="상세 보강 생략")
    r.add_argument("--only", nargs="*", help="특정 수집원만 (applyhome lh sh youth)")
    r.set_defaults(func=cmd_refresh)

    s = sub.add_parser("show", help="현재 판정 결과를 출력한다")
    s.add_argument("--closed", action="store_true", help="마감 공고 포함")
    s.add_argument("--limit", type=int, default=10)
    s.set_defaults(func=cmd_show)

    v = sub.add_parser("sources", help="수집원 상태를 출력한다")
    v.set_defaults(func=cmd_sources)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
