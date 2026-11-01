"""내 조건·즐겨찾기 백업과 복구.

  python scripts/backup.py save                 # 지금 값을 파일로 받아 둔다
  python scripts/backup.py save --out my.json
  python scripts/backup.py list                 # 원격 저장소의 변경 이력
  python scripts/backup.py show <sha>           # 그 시점의 값을 보기만 한다
  python scripts/backup.py restore --file my.json   # 파일에서 되돌린다
  python scripts/backup.py restore --sha <sha>      # 예전 커밋으로 되돌린다

백업이 왜 두 겹인가
-------------------
원격 저장소를 쓰면 **커밋 이력 자체가 백업**이다 (`list` / `restore --sha`).
그래도 로컬 파일 백업(`save`)을 따로 두는 이유는, 저장소나 토큰을 잃는 상황
까지 대비하기 위해서다. 둘은 서로를 대신하지 못한다.

받아 둔 파일은 **평문**이다 — 생년·소득·자산이 그대로 들어 있다.
안전한 곳에 두고, 저장소에 커밋하지 않는다 (`*.backup.json` 은 .gitignore 에 있다).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import profile, remote_store, store
from app.util import now_kst


def _collect() -> dict:
    return {
        "saved_at": now_kst().isoformat(timespec="seconds"),
        "profile": profile.load(),
        "bookmarks": store.load_bookmarks(),
    }


def cmd_save(args) -> int:
    data = _collect()
    out = Path(args.out) if args.out else Path(
        f"home-radar-backup-{now_kst():%Y%m%d-%H%M}.backup.json")
    if out.exists() and not args.force:
        print(f"이미 있습니다: {out}  (--force 로 덮어쓰기)")
        return 1
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    print(f"백업 저장: {out}")
    print(f"  내 조건   {len([k for k, v in data['profile'].items() if v not in (None, '', [], 0)])}개 항목")
    print(f"  즐겨찾기  {len(data['bookmarks'])}건")
    print("\n⚠ 이 파일은 평문입니다 — 생년·소득·자산이 그대로 들어 있습니다.")
    print("  안전한 곳에 보관하고 저장소에 올리지 마세요.")
    return 0


def cmd_list(args) -> int:
    if not remote_store.enabled():
        print("원격 저장소가 설정되지 않았습니다 (GITHUB_REPO / GITHUB_TOKEN).")
        print("로컬 파일만 쓰는 중이라 변경 이력이 없습니다 — `save` 로 받아 두세요.")
        return 1
    for key in ("profile", "bookmarks"):
        try:
            hist = remote_store.history(key, args.limit)
        except remote_store.RemoteError as e:
            print(f"[{key}] 이력을 읽지 못했습니다: {e}")
            continue
        print(f"\n=== {key} ({len(hist)}건) ===")
        if not hist:
            print("  아직 저장된 적이 없습니다")
        for h in hist:
            at = (h.get("at") or "")[:16].replace("T", " ")
            print(f"  {h['sha']}  {at}  {h.get('message', '')[:50]}")
    print("\n되돌리기:  python scripts/backup.py restore --sha <sha>")
    return 0


def cmd_show(args) -> int:
    if not remote_store.enabled():
        print("원격 저장소가 설정되지 않았습니다.")
        return 1
    try:
        val = remote_store.get_at(args.key, args.sha)
    except remote_store.RemoteError as e:
        print(f"읽지 못했습니다: {e}")
        return 1
    if val is None:
        print("그 시점에는 저장된 값이 없습니다.")
        return 1
    print(json.dumps(val, ensure_ascii=False, indent=2))
    return 0


def cmd_restore(args) -> int:
    if not args.file and not args.sha:
        print("--file 또는 --sha 중 하나가 필요합니다.")
        return 1

    if args.file:
        path = Path(args.file)
        if not path.exists():
            print(f"파일이 없습니다: {path}")
            return 1
        data = json.loads(path.read_text(encoding="utf-8"))
        prof, marks = data.get("profile"), data.get("bookmarks")
        src = str(path)
    else:
        if not remote_store.enabled():
            print("원격 저장소가 설정되지 않아 --sha 복구를 쓸 수 없습니다.")
            return 1
        prof = remote_store.get_at("profile", args.sha)
        marks = remote_store.get_at("bookmarks", args.sha)
        src = f"커밋 {args.sha}"
        if prof is None and marks is None:
            print("그 시점에는 저장된 값이 없습니다.")
            return 1

    print(f"복구 원본: {src}")
    cur = _collect()
    print(f"  지금 내 조건 생년={cur['profile'].get('birth')!r} "
          f"가구원={cur['profile'].get('household_size')} "
          f"즐겨찾기 {len(cur['bookmarks'])}건")
    if prof is not None:
        print(f"  복구 후   생년={prof.get('birth')!r} "
              f"가구원={prof.get('household_size')} ", end="")
    print(f"즐겨찾기 {len(marks or [])}건")

    if not args.yes:
        ans = input("\n덮어쓸까요? 되돌릴 수 없습니다 [y/N] ").strip().lower()
        if ans != "y":
            print("취소했습니다.")
            return 1

    try:
        if prof is not None:
            profile.save(prof)
            print("  내 조건 복구 완료")
        if marks is not None:
            store.save_bookmarks(list(marks))
            print(f"  즐겨찾기 {len(marks)}건 복구 완료")
    except remote_store.RemoteError as e:
        print(f"복구 실패: {e}")
        return 1
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="backup", description="HOME RADAR 백업·복구")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("save", help="지금 값을 로컬 파일로 받는다")
    s.add_argument("--out")
    s.add_argument("--force", action="store_true")
    s.set_defaults(func=cmd_save)

    l = sub.add_parser("list", help="원격 저장소의 변경 이력")
    l.add_argument("--limit", type=int, default=15)
    l.set_defaults(func=cmd_list)

    sh = sub.add_parser("show", help="특정 시점의 값을 보기만 한다")
    sh.add_argument("sha")
    sh.add_argument("--key", default="profile", choices=["profile", "bookmarks"])
    sh.set_defaults(func=cmd_show)

    r = sub.add_parser("restore", help="파일 또는 예전 커밋에서 되돌린다")
    r.add_argument("--file")
    r.add_argument("--sha")
    r.add_argument("--yes", action="store_true", help="확인 없이 진행")
    r.set_defaults(func=cmd_restore)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
