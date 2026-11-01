"""원격 저장소 실연동 검증 — 실제 GitHub 저장소에 붙여 확인한다.

  python scripts/verify_store.py

확인하는 것
  1. 설정값이 있는가 (값은 출력하지 않는다)
  2. 저장소에 닿는가 · 비공개인가 · 코드 저장소와 분리됐는가
  3. 토큰 권한이 쓰기까지 되는가
  4. 가상값으로 저장 → 재조회 → 값이 같은가
  5. GitHub 에 **암호문만** 올라가는가 (평문 누출 점검)
  6. 동시 수정 충돌(sha 불일치)에서 복구되는가
  7. 커밋 이력이 쌓이는가 (= 백업)
  8. 잘못된 키로는 못 읽는가
  9. 뒷정리 — 테스트가 남긴 것을 지운다

안전장치
  - **`profile` / `bookmarks` 키는 건드리지 않는다.** 전용 키 `__verify__` 만 쓴다.
  - 실제 개인정보를 쓰지 않는다. 가상값만 넣는다.
  - 토큰·암호화 키 원문을 절대 출력하지 않는다.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import crypto, remote_store, secrets  # noqa: E402

# 운영 키와 절대 겹치지 않는 전용 키
TEST_KEY = "__verify__"
PROD_KEYS = ("profile", "bookmarks")

# 실제 개인정보 대신 쓰는 가상값 (누가 봐도 가짜인 값으로)
FAKE = {
    "birth": "1900-01-01",
    "household_size": 99,
    "income_work": 123456,
    "regions": ["테스트시", "가상도"],
    "marker": "HOME-RADAR-VERIFY-ONLY",
}

_fails: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    print(f"  {'OK  ' if ok else 'FAIL'} {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        _fails.append(name)
    return ok


def section(t: str) -> None:
    print(f"\n=== {t} ===")


def main() -> int:
    # ---------------------------------------------------------- 1. 설정
    section("1. 설정 확인 (값은 출력하지 않는다)")
    for name in ("GITHUB_REPO", "GITHUB_TOKEN", "ENCRYPTION_KEY"):
        st = secrets.status(name)
        check(f"{name} 설정됨", st["set"],
              f"출처 {st['source']} · {st['masked']}" if st["set"] else "비어 있음")
    if not remote_store.enabled() or not crypto.key_configured():
        print("\n설정이 비어 있어 더 진행할 수 없습니다.")
        print(".env 에 GITHUB_REPO / GITHUB_TOKEN / ENCRYPTION_KEY 를 넣어 주세요.")
        return 1

    repo = remote_store.repo()
    print(f"       대상 저장소: {repo} (branch={remote_store.branch()})")

    # ---------------------------------------------------------- 2. 접근
    section("2. 저장소 접근 · 분리 · 공개 범위")
    pg = remote_store.ping()
    if not check("저장소에 닿는다", bool(pg.get("ok")), str(pg.get("reason", ""))):
        print("\n접근하지 못했습니다. 아래를 확인하세요:")
        print("  · GITHUB_REPO 가 owner/repo 형태인가")
        print("  · 토큰이 그 저장소에 접근 권한이 있는가 (Only select repositories)")
        print("  · 토큰이 만료되지 않았는가")
        return 1
    check("데이터 저장소가 비공개", pg.get("private") is True,
          "공개 저장소입니다 — 즉시 Private 으로 바꾸세요"
          if pg.get("private") is False else "")
    check("코드 저장소와 분리됨", repo.split("/")[-1] != "home-radar",
          f"repo={repo} — 코드 저장소에 쓰면 저장마다 재배포가 걸립니다")
    dflt = pg.get("default_branch")
    check("브랜치 설정이 저장소와 맞음",
          not dflt or dflt == remote_store.branch(),
          f"설정 {remote_store.branch()} · 저장소 기본 {dflt}")

    # 운영 키의 현재 상태를 **읽기만** 해 둔다 (덮어쓰기 사고 방지용 기록)
    section("3. 운영 키 현재 상태 (읽기만 — 건드리지 않는다)")
    before: dict = {}
    for k in PROD_KEYS:
        try:
            v = remote_store.get(k, use_cache=False)
            before[k] = v
            if v is None:
                print(f"       {k:<10} 아직 저장된 적 없음 (빈 저장소)")
            else:
                size = len(json.dumps(v, ensure_ascii=False))
                print(f"       {k:<10} 이미 있음 — {size}B (내용은 출력하지 않음)")
        except remote_store.RemoteError as e:
            before[k] = "ERROR"
            print(f"       {k:<10} 읽기 실패: {e}")

    # ---------------------------------------------------------- 4. 왕복
    section("4. 가상값 저장 → 재조회 (전용 키 __verify__ 만 사용)")
    try:
        res = remote_store.put(TEST_KEY, FAKE)
        check("저장 성공 (쓰기 권한 있음)", bool(res.get("saved")))
        check("서버가 되읽어 확인함", bool(res.get("verified")))
    except remote_store.RemoteError as e:
        check("저장 성공 (쓰기 권한 있음)", False, str(e))
        print("\n쓰기에 실패했습니다. 토큰 권한에 "
              "Repository permissions → Contents → Read and write 가 있는지 "
              "확인하세요.")
        return 1

    remote_store.invalidate()
    back = remote_store.get(TEST_KEY, use_cache=False)
    check("재조회 값이 저장한 값과 같다", back == FAKE,
          "" if back == FAKE else f"{back}")

    # ---------------------------------------------------------- 5. 암호문
    section("5. GitHub 에 암호문만 올라가는가")
    import base64

    raw = remote_store._fetch(TEST_KEY)[0] or ""
    check("파일을 다시 받아옴", bool(raw))
    try:
        env = json.loads(raw)
    except json.JSONDecodeError as e:
        check("봉투 JSON 파싱", False, str(e))
        env = {}
    check("봉투 형식 v=1", env.get("v") == 1, str(env.get("v")))
    check("Fernet 토큰 형식", str(env.get("data", "")).startswith("gAAAA"),
          str(env.get("data", ""))[:12])
    check("키 지문 기록됨", bool(env.get("key_fp")), env.get("key_fp", ""))

    # 평문 누출 점검.
    #
    # **암호문·지문을 걷어낸 뒤** 본다. 짧은 값(예: "99")을 base64 암호문
    # 전체에서 찾으면 우연히 걸린다 — 228자 base64 에 특정 2글자가 들어갈
    # 확률은 결코 낮지 않아서, 그대로 두면 가끔 실패하는 깜빡이 검사가 된다.
    # 정작 중요한 것은 '봉투의 암호문 아닌 부분에 평문이 없는가' 다.
    token = str(env.get("data", ""))
    fp = str(env.get("key_fp", ""))
    structural = raw.replace(token, "<TOKEN>").replace(fp, "<FP>") if token else raw
    values = [str(v) for v in (FAKE["birth"], FAKE["household_size"],
                               FAKE["income_work"], FAKE["marker"],
                               FAKE["regions"][0])]
    found = [s for s in values if s in structural]
    check("봉투에 평문이 없다 (암호문·지문 제외)", not found,
          f"누출: {found}" if found else "v / key_fp / data 만 들어 있다")

    # 길고 특징적인 값은 파일 전체에서 찾아도 나오면 안 된다
    #   (우연히 겹칠 수 없는 길이라 이쪽은 전체 검색이 의미가 있다)
    distinctive = [s for s in values if len(s) >= 6]
    leaked = [s for s in distinctive if s in raw]
    check("특징적인 평문이 파일 어디에도 없다", not leaked,
          f"누출: {leaked}" if leaked else f"검사한 값 {len(distinctive)}개")

    # base64 로 한 꺼풀 싸인 경우까지 본다
    b64 = base64.b64encode(json.dumps(FAKE, ensure_ascii=False).encode()).decode()
    check("base64 평문도 아니다", b64 not in raw)

    # 복호화해야만 원래 값이 나오는지 (= 정말 암호화된 것인지)
    try:
        check("복호화해야 원래 값이 나온다",
              crypto.decrypt_json(token) == FAKE)
    except crypto.CryptoError as e:
        check("복호화해야 원래 값이 나온다", False, str(e))

    # ---------------------------------------------------------- 6. 충돌
    section("6. 동시 수정 충돌 복구 (sha 불일치)")
    hit = remote_store._cache.get(TEST_KEY)
    if hit:
        remote_store._cache[TEST_KEY] = (hit[0], hit[1], "0" * 40)  # 낡은 sha
    try:
        remote_store.put(TEST_KEY, {**FAKE, "round": 2})
        remote_store.invalidate()
        again = remote_store.get(TEST_KEY, use_cache=False)
        check("낡은 sha 로도 재시도해 저장된다", again.get("round") == 2,
              str(again.get("round")))
    except remote_store.RemoteError as e:
        check("낡은 sha 로도 재시도해 저장된다", False, str(e))

    # ---------------------------------------------------------- 7. 이력
    section("7. 커밋 이력 (= 백업)")
    try:
        hist = remote_store.history(TEST_KEY, 5)
        check("이력이 쌓인다", len(hist) >= 2, f"{len(hist)}건")
        if len(hist) >= 2:
            old = remote_store.get_at(TEST_KEY, hist[-1]["sha"])
            check("예전 시점 값을 복호화해 읽을 수 있다", old is not None,
                  f"round={old.get('round') if old else None}")
    except remote_store.RemoteError as e:
        check("이력이 쌓인다", False, str(e))

    # ---------------------------------------------------------- 8. 잘못된 키
    section("8. 다른 키로는 못 읽는다")
    import os
    from unittest import mock

    real_env = {k: os.environ.get(k) for k in
                ("GITHUB_REPO", "GITHUB_TOKEN", "ENCRYPTION_KEY")}
    with mock.patch.dict(os.environ, {**{k: v for k, v in real_env.items() if v},
                                      "ENCRYPTION_KEY": "완전히-다른-키-9999"}):
        # .env 가 우선순위에서 밀리도록 환경변수를 직접 덮는다
        crypto.reset_cache()
        remote_store.invalidate()
        try:
            remote_store.get(TEST_KEY, use_cache=False)
            check("다른 키로는 읽히지 않는다", False, "읽혀 버렸다 !!")
        except remote_store.RemoteError as e:
            check("다른 키로는 읽히지 않는다", True)
            check("키가 바뀌었다고 알려 준다", "ENCRYPTION_KEY" in str(e),
                  str(e)[:70])
    crypto.reset_cache()
    remote_store.invalidate()

    # ---------------------------------------------------------- 9. 뒷정리
    section("9. 뒷정리")
    try:
        remote_store.delete(TEST_KEY)
        remote_store.invalidate()
        check("테스트 키 삭제됨",
              remote_store.get(TEST_KEY, use_cache=False) is None)
    except remote_store.RemoteError as e:
        check("테스트 키 삭제됨", False, str(e))

    # 운영 키가 그대로인지 다시 확인한다
    same = True
    for k in PROD_KEYS:
        if before.get(k) == "ERROR":
            continue
        try:
            now = remote_store.get(k, use_cache=False)
        except remote_store.RemoteError:
            now = "ERROR"
        if now != before.get(k):
            same = False
    check("운영 키(profile·bookmarks)가 그대로다", same,
          "" if same else "!! 변경됨 — 즉시 확인 필요 !!")

    # ---------------------------------------------------------- 요약
    print("\n" + "=" * 58)
    if _fails:
        print(f"실패 {len(_fails)}건:")
        for f in _fails:
            print(f"  - {f}")
        return 1
    print("전부 통과 — 실제 GitHub 저장소 연동이 정상입니다")
    return 0


if __name__ == "__main__":
    sys.exit(main())
