"""수집 → 표시 깔때기 진단 — 어디서 몇 건이 왜 빠지는지 수집원별로 센다.

  python scripts/funnel.py                  # 전체
  python scripts/funnel.py --source lh      # LH 만
  python scripts/funnel.py --source youth --list   # 탈락 공고 제목까지

네트워크를 쓰지 않는다. 이미 받아 둔 data/notices.json 만 읽는다.
그래서 '수집이 안 된 것'과 '수집은 됐는데 안 보이는 것'을 구분할 수 있다.

단계
  1 원본 수집    수집원이 돌려준 건수 (source_status 의 마지막 성공 건수)
  2 중복 흡수    dedupe 가 다른 공고에 합친 건수
  3 캐시 적재    notices.json 에 남은 건수
  4 마감 제외    is_closed → 기본 화면에서 숨김
  5 모집 아님    '당첨자 발표' 같은 후속 안내 (judge 의 무시)
  6 내 설정      관심 지역·유형·통장 모드 필터
  7 자격 판정    추천 / 확인 / 제외
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter, defaultdict
from pathlib import Path

# scripts/ 에서 바로 실행해도 app 패키지를 찾게 한다
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import collect, profile, store
from app.checks import FILTER, LEVEL_IGNORE, UNMET
from app.eligibility import judge
from app.util import parse_date, today_kst

# 화면에 쓰는 수집원 이름
NAMES = {"applyhome": "청약홈", "applyhome_api": "청약홈API", "lh": "LH",
         "sh": "SH", "youth": "청년안심", "ish": "SH게시판"}


def _filter_reason(v) -> str:
    """내 설정 필터에 걸린 이유 한 줄."""
    for c in v.checks:
        if c.category == FILTER and c.status == UNMET:
            return c.name
    return "기타"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", help="수집원 id (lh, youth, sh, applyhome …)")
    ap.add_argument("--today", help="기준일 YYYY-MM-DD (기본: 오늘)")
    ap.add_argument("--list", action="store_true", help="탈락 공고 제목도 출력")
    ap.add_argument("--limit", type=int, default=12)
    args = ap.parse_args()

    today = parse_date(args.today) or today_kst()
    cached = store.load_notices()
    if not cached:
        print("캐시가 없습니다. 먼저 `python -m app refresh` 를 돌리세요.")
        return 1

    prof = profile.load()
    status = store.load_source_status()
    merged = cached.get("merged") or []
    raw_rows = cached.get("notices") or []

    # 중복으로 흡수된 건수 — dropped key 앞부분이 수집원 이름이다
    merged_by_src: Counter = Counter()
    for m in merged:
        dropped = m.get("dropped") or ""
        merged_by_src[dropped.split(":")[0]] += 1

    stages: dict[str, Counter] = defaultdict(Counter)
    examples: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))

    for raw in raw_rows:
        n = collect._rehydrate(raw)
        sid = n.source_id
        stages[sid]["캐시"] += 1

        v = judge(n, prof, today)

        if v.level == LEVEL_IGNORE:
            stages[sid]["모집아님"] += 1
            examples[sid]["모집아님"].append(n.title)
            continue

        if n.is_closed(today):
            stages[sid]["마감"] += 1
            examples[sid]["마감"].append(f"{n.title}  (마감 {n.end})")
            continue

        stages[sid]["표시대상"] += 1
        if v.level == "제외" and any(c.category == FILTER and c.status == UNMET
                                     for c in v.checks):
            reason = _filter_reason(v)
            stages[sid]["내설정"] += 1
            stages[sid][f"내설정:{reason}"] += 1
            examples[sid][f"내설정:{reason}"].append(n.title)
        stages[sid][v.level] += 1
        if v.level == "제외":
            examples[sid]["제외"].append(
                f"{n.title}  ← {v.summary}")

    sids = [args.source] if args.source else [
        s for s in ("applyhome", "applyhome_api", "lh", "sh", "youth", "ish")
        if s in stages or s in status]

    print(f"기준일 {today} · 캐시 {cached.get('count')}건 "
          f"(수집 {cached.get('fetched_at')})\n")

    head = (f"{'수집원':<12}{'원본':>6}{'중복흡수':>9}{'캐시':>7}"
            f"{'모집아님':>9}{'마감':>7}{'표시':>7}{'추천':>7}{'확인':>7}{'제외':>7}")
    print(head)
    print("-" * len(head.encode("euc-kr", "replace")))

    total = Counter()
    for sid in sids:
        s = stages.get(sid, Counter())
        st = status.get(sid) or {}
        raw_n = st.get("last_success_count") or 0
        row = (f"{NAMES.get(sid, sid):<12}{raw_n:>6}{merged_by_src.get(NAMES.get(sid, sid), 0):>9}"
               f"{s['캐시']:>7}{s['모집아님']:>9}{s['마감']:>7}"
               f"{s['표시대상']:>7}{s['추천']:>7}{s['확인']:>7}{s['제외']:>7}")
        print(row)
        for k, val in s.items():
            total[k] += val
        total["원본"] += raw_n

    print("-" * len(head.encode("euc-kr", "replace")))
    print(f"{'합계':<12}{total['원본']:>6}{sum(merged_by_src.values()):>9}"
          f"{total['캐시']:>7}{total['모집아님']:>9}{total['마감']:>7}"
          f"{total['표시대상']:>7}{total['추천']:>7}{total['확인']:>7}"
          f"{total['제외']:>7}")

    # ---- 내 설정 필터로 빠진 것 ----
    print("\n[내 설정 필터로 제외된 건수]")
    any_filter = False
    for sid in sids:
        s = stages.get(sid, Counter())
        reasons = {k.split(":", 1)[1]: v for k, v in s.items()
                   if k.startswith("내설정:")}
        if not reasons:
            continue
        any_filter = True
        detail = " · ".join(f"{r} {c}건" for r, c in
                            sorted(reasons.items(), key=lambda kv: -kv[1]))
        print(f"  {NAMES.get(sid, sid):<12} {s['내설정']:>4}건  — {detail}")
    if not any_filter:
        print("  없음")

    # ---- 수집 실패·건너뜀 ----
    print("\n[수집원 상태]")
    for sid in sids:
        st = status.get(sid) or {}
        state = st.get("state", "미실행")
        line = f"  {NAMES.get(sid, sid):<12} {state}"
        if st.get("last_success_at"):
            line += f" · 마지막 성공 {st['last_success_at'][:16]}"
        if st.get("note"):
            line += f" · {st['note'][:60]}"
        if st.get("last_error"):
            line += f" · 오류 {st['last_error'][:60]}"
        print(line)

    # ---- 제목 나열 ----
    if args.list:
        for sid in sids:
            ex = examples.get(sid) or {}
            if not ex:
                continue
            print(f"\n===== {NAMES.get(sid, sid)} 탈락 내역 =====")
            for bucket, items in ex.items():
                if not items:
                    continue
                print(f"\n  [{bucket}] {len(items)}건")
                for t in items[:args.limit]:
                    print(f"    - {t[:98]}")
                if len(items) > args.limit:
                    print(f"    … 외 {len(items) - args.limit}건")

    return 0


if __name__ == "__main__":
    sys.exit(main())
