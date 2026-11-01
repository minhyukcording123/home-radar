"""실행 중인 서버에 대고 API 건강도·판정 정확성·키 유출을 점검한다.

    python scripts/verify_api.py [--base http://127.0.0.1:8777]

scripts/verify.ps1 이 이걸 호출한다. PowerShell 에 파이썬을 인라인으로 넣으면
따옴표가 인자 파싱에 깨지므로 파일로 분리했다.
"""
from __future__ import annotations

import argparse
import datetime as dt
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from app.sources import registry  # noqa: E402

fails: list[str] = []


def need(cond, msg: str) -> None:
    print(("  OK   " if cond else "  FAIL ") + msg)
    if not cond:
        fails.append(msg)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8777")
    args = ap.parse_args()
    B = args.base.rstrip("/")

    try:
        c = httpx.Client(timeout=180)
    except Exception as e:  # noqa: BLE001
        need(False, f"클라이언트 생성 실패: {e}")
        return 1

    try:
        print("--- API 건강도 ---")
        h = c.get(f"{B}/api/health").json()
        need(h.get("ok") is True,
             f"health · 캐시 {h.get('cached_count')}건 · 규칙 {h.get('rules_version')}")

        v = c.get(f"{B}/api/view").json()
        need(v.get("ready") is True,
             f"view · 수집 {v['stats'].get('collected')}건 → 표시 "
             f"{v['stats'].get('shown')}건 "
             f"(추천 {v['stats'].get('추천')} / 확인 {v['stats'].get('확인')} / "
             f"제외 {v['stats'].get('제외')})")
        rows = [r for b in v["buckets"].values() for r in b]

        for path in ("/api/sources", "/api/rules", "/api/profile",
                     "/api/revisions", "/api/bookmarks"):
            need(c.get(f"{B}{path}").status_code == 200, f"GET {path}")

        # 수집원별 상태
        ok_src = [s["name"] for s in v["sources"] if s["state"] == "정상"]
        bad_src = [(s["name"], s["last_error"][:40]) for s in v["sources"]
                   if s["state"] == "실패"]
        print(f"  정상 수집원: {', '.join(ok_src) or '없음'}")
        need(not bad_src, f"실패한 수집원 없음 {bad_src}")

        print("\n--- 판정 정확성 불변식 ---")
        # verdict.checks 는 응답에서 뺐다 (unmet/unknown/met/info 와 내용이
        # 똑같이 겹쳐 응답을 두 배로 불렸다). 네 갈래를 합치면 같은 집합이다.
        def all_checks(r):
            v = r["verdict"]
            return [*v.get("unmet", []), *v.get("unknown", []),
                    *v.get("met", []), *v.get("info", [])]

        bad = [(r["title"][:20], ch["name"]) for r in rows
               for ch in all_checks(r)
               if not ch["source"]["official"] and ch["status"] in ("충족", "불충족")]
        need(not bad, f"미확정 기준이 적격을 확정하지 않는다 (위반 {len(bad)})")

        bad = [r["title"] for r in rows
               if r["verdict"]["level"] == "추천"
               and r["verdict"]["counts"]["미확인"]]
        need(not bad, f"미확인이 있으면 추천하지 않는다 (위반 {len(bad)})")

        bad = [r["title"] for r in rows
               if r["verdict"]["level"] in ("추천", "확인")
               and r["verdict"]["counts"]["불충족"]]
        need(not bad, f"추천·확인에 불충족 없음 (위반 {len(bad)})")

        bad = [r["title"] for r in rows
               if r["verdict"]["level"] == "제외" and not r["verdict"]["unmet"]]
        need(not bad, f"제외는 전부 사유가 있다 (위반 {len(bad)})")

        # 알림은 응답에서 **키 목록**으로 온다 (공고 전체를 두 번 싣지 않으려고).
        # 공고 본문이 필요하면 buckets 쪽 rows 에서 키로 찾는다.
        by_key = {r["key"]: r for r in rows}

        def alert_rows(name):
            return [by_key[k] for k in v["alerts"].get(name, []) if k in by_key]

        newk = set(v["alerts"]["new"])
        basek = {r["key"] for r in rows if r["is_baseline"]}
        need(not (newk & basek),
             f"기준선 공고는 신규 알림에 없다 (겹침 {len(newk & basek)})")

        tdy = dt.date.fromisoformat(v["today"])
        mism = []
        for r in rows:
            if r["end"] and r["days_left"] != (
                    dt.date.fromisoformat(r["end"]) - tdy).days:
                mism.append(r["key"])
            if r["begin"] and r["days_to_begin"] != (
                    dt.date.fromisoformat(r["begin"]) - tdy).days:
                mism.append(r["key"])
        need(not mism, f"D-day 가 날짜와 일치 (불일치 {len(mism)})")

        bad = [r["key"] for r in rows
               if r["begin"] is None and r["phase"] == "접수중"]
        need(not bad, f"시작일 없이 '접수중'이라 하지 않는다 (위반 {len(bad)})")

        rd = v["stats"]["remind_days"]
        bad = [r["key"] for r in alert_rows("closing")
               if r["days_left"] is None or not (0 <= r["days_left"] <= rd)]
        need(not bad, f"마감 임박 알림이 0~{rd}일 이내 (위반 {len(bad)})")

        bad = [r["key"] for r in alert_rows("opening") if r["begin"] is None]
        need(not bad, f"접수 시작 알림에 시작일 없는 공고 없음 (위반 {len(bad)})")

        # 알림 키가 전부 목록 안에 있는 공고를 가리키는지 (끊긴 참조 방지).
        # 화면이 키로 공고를 찾으므로, 가리키는 대상이 없으면 알림이 빈다.
        dangling = {name: [k for k in keys if k not in by_key]
                    for name, keys in v["alerts"].items()}
        dangling = {n: ks for n, ks in dangling.items() if ks}
        need(not dangling, f"알림 키가 모두 목록의 공고를 가리킨다 ({dangling})")

        print("\n--- 인증키 유출 점검 ---")
        needles = set()
        for sid in ("lh", "applyhome_api"):
            k = registry.api_key(sid)
            if k and len(k) >= 16:
                needles.add(k)
        if not needles:
            print("  SKIP 설정된 키가 없어 생략")
        else:
            for path in ("/api/view", "/api/sources", "/api/profile",
                         "/api/rules", "/api/health", "/"):
                t = c.get(f"{B}{path}").text
                need(not any(n in t for n in needles), f"{path} 에 키 없음")
            leaked = []
            for p in (ROOT / "data").rglob("*.json"):
                if p.stat().st_size > 3_000_000:
                    continue
                t = p.read_text(encoding="utf-8", errors="replace")
                if any(n in t for n in needles):
                    leaked.append(str(p.relative_to(ROOT)))
            need(not leaked, f"디스크 캐시에 키 없음 {leaked[:3]}")
    except Exception as e:  # noqa: BLE001
        need(False, f"점검 중 예외: {type(e).__name__}: {e}")
    finally:
        c.close()

    print()
    if fails:
        print(f"실패 {len(fails)}건:")
        for f in fails:
            print("  -", f)
        return 1
    print("API 점검 전부 통과")
    return 0


if __name__ == "__main__":
    sys.exit(main())
