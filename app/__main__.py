"""명령줄 진입점 — 매일 자동 수집(작업 스케줄러)에 쓴다.

  python -m app refresh              # 수집 + 상세 보강
  python -m app refresh --no-enrich  # 캘린더만 (빠름)
  python -m app show                 # 지금 판정 결과를 터미널로 확인
"""
from __future__ import annotations

import argparse
import sys

from . import collect, profile
from .util import today_kst


def cmd_refresh(args) -> int:
    payload = collect.refresh(back=args.back, ahead=args.ahead, enrich=not args.no_enrich)
    print(f"수집 완료: {payload['count']}건  ({payload['fetched_at']})")
    for w in payload.get("warnings") or []:
        print(f"  경고: {w}")
    return 0


def cmd_show(args) -> int:
    prof = profile.load()
    view = collect.build_view(prof, today_kst(), include_closed=args.closed)
    if not view["ready"]:
        print(view["message"])
        return 1
    s = view["stats"]
    print(f"[{view['today']}] 수집 {s['collected']}건 → 표시 {s['shown']}건"
          f" | 추천 {s['추천']} / 확인 {s['확인']} / 제외 {s['제외']}")
    a = view["alerts"]
    print(f"  새 공고 {len(a['new'])} · 마감 {s['remind_days']}일 이내 {len(a['closing'])}"
          f" · 오늘 접수 {len(a['today'])}")
    for level in ("추천", "확인"):
        rows = view["buckets"][level]
        if not rows:
            continue
        print(f"\n== {level} ({len(rows)}) ==")
        for r in rows[: args.limit]:
            dl = r["days_left"]
            print(f"  {'D-' + str(dl) if dl is not None else '  -':>5} "
                  f"{r['region']:3s} {r['verdict']['kind']:5s} {r['title'][:40]}")
            for why in (r["verdict"]["reasons"] + r["verdict"]["tips"])[:2]:
                print(f"         · {why}")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="app", description="HOME RADAR")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("refresh", help="청약홈에서 공고를 다시 긁는다")
    r.add_argument("--back", type=int, default=1, help="이전 몇 개월까지")
    r.add_argument("--ahead", type=int, default=6, help="이후 몇 개월까지")
    r.add_argument("--no-enrich", action="store_true", help="상세 보강 생략")
    r.set_defaults(func=cmd_refresh)

    s = sub.add_parser("show", help="현재 판정 결과를 출력한다")
    s.add_argument("--closed", action="store_true", help="마감 공고 포함")
    s.add_argument("--limit", type=int, default=12)
    s.set_defaults(func=cmd_show)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
