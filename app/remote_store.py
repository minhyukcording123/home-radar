"""영속 저장소 — **GitHub 비공개 저장소**에 암호화된 작은 파일을 둔다.

왜 이걸 고르나
--------------
Render 무료 플랜은 영구 디스크가 없다. 재시작·슬립 해제·재배포마다 `data/` 와
`profile.json` 이 사라진다. 내 조건은 환경변수로 손수 복원할 수 있었지만
**즐겨찾기는 복구 수단이 아예 없었고**, PC 와 아이폰이 같은 값을 보지도 못했다.

저장할 것은 **암호화 후 약 1.3KB**, 쓰기는 주 몇 회다. 이 정도에 전용 DB 를
붙이면 계정·무료플랜 정책·휴면 규칙을 하나 더 떠안게 된다. 그래서 이미 가지고
있는 GitHub 를 쓴다.

  · 새 계정도, 새 무료 플랜도 필요 없다
  · 휴면·일시중지가 없다 (Supabase 무료는 7일, 다른 곳은 정책이 불명확하다)
  · **커밋 이력이 곧 백업이다** — 되돌리기가 공짜로 따라온다
  · Contents API 는 공식 문서가 있는 REST 라 httpx 만으로 충분하다 (새 의존성 0)

**코드 저장소와는 다른 저장소를 쓴다.** Render 가 지켜보는 저장소에 쓰면
저장할 때마다 재배포가 걸린다. 별도 저장소면 그 위험이 아예 없다.

무엇을 저장하나
---------------
단일 사용자 앱이라 복잡한 구조를 두지 않는다. 키 하나가 파일 하나다.

    data/profile.json     내 조건 (생년·소득·자산 …)   ← 암호화
    data/bookmarks.json   즐겨찾기 key 목록            ← 암호화

파일 안은 이런 봉투다 (암호문 자체는 `data` 안에 들어 있다):

    {"v": 1, "key_fp": "<키 지문>", "data": "<Fernet 토큰>"}

공고 캐시는 **여기 두지 않는다.** 공개 정보라 암호화할 이유가 없고 수백 건이라
저장소를 지저분하게 만든다. 캐시는 로컬 파일에 두고 없으면 다시 수집한다.

보안
----
- 값은 `app/crypto.py`(Fernet)로 **앱에서 암호화**한 뒤 올린다. GitHub 는 내용을 모른다.
- 토큰은 Render 환경변수에만 둔다. 어떤 API 응답·화면·로그에도 담기지 않는다.
- 토큰은 **그 저장소 하나에만** 권한을 주는 fine-grained PAT 를 쓴다.
- 실패를 **조용히 삼키지 않는다.** 저장이 안 됐는데 성공이라고 말하지 않는다.
"""
from __future__ import annotations

import base64
import json
import threading
import time
from typing import Any, Optional

import httpx

from . import crypto
from . import secrets as appsecrets

API = "https://api.github.com"
PATH_PREFIX = "data"
TIMEOUT = 10.0
ENVELOPE_VERSION = 1
_lock = threading.Lock()


class RemoteError(Exception):
    """원격 저장소 접근 실패 — 호출한 쪽이 사용자에게 알려야 한다."""


# ---------------------------------------------------------------- 설정
def repo() -> str:
    """`owner/repo` 형태. 코드 저장소가 **아닌** 데이터 전용 저장소."""
    return appsecrets.get("GITHUB_REPO").strip().strip("/")


def token() -> str:
    """fine-grained PAT (Contents: read and write). 서버에서만 쓴다."""
    return appsecrets.get("GITHUB_TOKEN")


def branch() -> str:
    return appsecrets.get("GITHUB_BRANCH") or "main"


def enabled() -> bool:
    """원격 저장소를 쓸 수 있는 상태인가."""
    return bool(repo() and token())


def status() -> dict:
    """설정 상태 — **비밀값은 담지 않는다** (화면에 보여줘도 되는 것만)."""
    return {
        "backend": "github",
        "enabled": enabled(),
        "encrypted": crypto.key_configured(),
        "repo": repo(),            # 저장소 이름은 비밀이 아니다
        "branch": branch(),
        "token_set": bool(token()),
        "key_fingerprint": crypto.fingerprint(),
    }


def _headers() -> dict:
    t = token()
    # HTTP 헤더는 ASCII 만 담을 수 있다. 토큰을 잘못 붙여 넣어 한글·줄바꿈이
    # 섞이면 httpx 안쪽에서 터지는데 그 메시지로는 원인을 알 수 없다.
    try:
        t.encode("ascii")
    except UnicodeEncodeError:
        raise RemoteError(
            "GITHUB_TOKEN 에 ASCII 가 아닌 문자가 들어 있습니다 — "
            "복사할 때 다른 문자가 섞였는지 확인하세요") from None
    return {
        "Authorization": f"Bearer {t}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "home-radar",
    }


def _url(key: str) -> str:
    return f"{API}/repos/{repo()}/contents/{PATH_PREFIX}/{key}.json"


# ---------------------------------------------------------------- 캐시
# 단일 사용자라 메모리 캐시 하나면 충분하다. 화면을 열 때마다 네트워크를 타면
# 무료 인스턴스에서 첫 응답이 눈에 띄게 느려진다.
# sha 도 같이 들고 있어야 덮어쓸 때 한 번 더 조회하지 않는다.
_cache: dict[str, tuple[float, Any, Optional[str]]] = {}
CACHE_TTL = 30.0


def invalidate(key: Optional[str] = None) -> None:
    with _lock:
        if key is None:
            _cache.clear()
        else:
            _cache.pop(key, None)


def _remember(key: str, value, sha: Optional[str]) -> None:
    with _lock:
        _cache[key] = (time.monotonic(), value, sha)


def _cached_sha(key: str) -> Optional[str]:
    hit = _cache.get(key)
    return hit[2] if hit else None


# ---------------------------------------------------------------- 저수준
def _request(method: str, url: str, **kw) -> httpx.Response:
    try:
        return httpx.request(method, url, headers=_headers(),
                             timeout=TIMEOUT, **kw)
    except httpx.HTTPError as e:
        raise RemoteError(f"원격 저장소 접속 실패: {type(e).__name__}") from e


def _explain(r: httpx.Response) -> str:
    """GitHub 오류를 사람이 읽을 수 있게. **토큰은 절대 담지 않는다.**"""
    try:
        msg = (r.json() or {}).get("message", "")[:160]
    except ValueError:
        msg = (r.text or "")[:160]
    if r.status_code == 401:
        return "인증 거부(401) — GITHUB_TOKEN 이 틀렸거나 만료됐습니다"
    if r.status_code == 403:
        if "rate limit" in msg.lower():
            return "요청 한도 초과(403) — 잠시 뒤 다시 시도하세요"
        return ("권한 없음(403) — 토큰에 이 저장소의 "
                "Contents: Read and write 권한이 있는지 확인하세요")
    if r.status_code == 404:
        return (f"저장소나 경로를 찾을 수 없습니다(404) — GITHUB_REPO='{repo()}' "
                f"브랜치='{branch()}' 가 맞는지, 토큰이 그 저장소에 접근할 수 "
                "있는지 확인하세요")
    if r.status_code == 409:
        return "충돌(409) — 다른 곳에서 먼저 저장했습니다. 다시 시도하세요"
    if r.status_code == 422:
        return f"요청이 거부됐습니다(422): {msg}"
    return f"원격 저장소 오류({r.status_code}): {msg}"


def _fetch(key: str) -> tuple[Optional[str], Optional[str]]:
    """(봉투 문자열, sha). 파일이 없으면 (None, None)."""
    r = _request("GET", _url(key), params={"ref": branch()})
    if r.status_code == 404:
        return None, None
    if r.status_code >= 400:
        raise RemoteError(_explain(r))
    body = r.json()
    raw = body.get("content") or ""
    try:
        text = base64.b64decode(raw).decode("utf-8")
    except (ValueError, UnicodeDecodeError) as e:
        raise RemoteError(f"'{key}' 내용을 읽을 수 없습니다: {e}") from e
    return text, body.get("sha")


# ---------------------------------------------------------------- 읽기/쓰기
def get(key: str, *, use_cache: bool = True) -> Optional[Any]:
    """레코드 하나를 읽어 복호화한다. 없으면 None.

    복호화에 실패하면 **예외를 던진다.** 조용히 None 을 돌려주면 호출한 쪽이
    '아직 저장한 적 없음'으로 오해하고 기본값으로 덮어써서 원본이 날아간다.
    """
    if not enabled():
        return None
    if use_cache:
        hit = _cache.get(key)
        if hit and (time.monotonic() - hit[0]) < CACHE_TTL:
            return hit[1]

    text, sha = _fetch(key)
    if text is None:
        _remember(key, None, None)
        return None

    try:
        env = json.loads(text)
    except json.JSONDecodeError as e:
        raise RemoteError(f"'{key}' 형식이 올바르지 않습니다: {e}") from e

    blob, fp = env.get("data"), env.get("key_fp") or ""
    if not blob:
        _remember(key, None, sha)
        return None

    try:
        value = crypto.decrypt_json(blob)
    except crypto.CryptoError as e:
        now_fp = crypto.fingerprint()
        if fp and now_fp and fp != now_fp:
            raise RemoteError(
                f"'{key}' 를 복호화하지 못했습니다 — 저장할 때와 "
                f"ENCRYPTION_KEY 가 다릅니다 (저장 당시 {fp}, 지금 {now_fp}). "
                "예전 키를 되돌리면 그대로 읽힙니다. "
                "지금 저장하면 예전 내용을 덮어쓰게 되니 주의하세요.") from e
        raise RemoteError(f"'{key}' 복호화 실패: {e}") from e

    _remember(key, value, sha)
    return value


def put(key: str, value: Any) -> dict:
    """레코드를 암호화해 저장하고 **읽어서 확인까지** 한다.

    쓰기 응답만 믿지 않고 다시 읽어 같은 값인지 본다 — '저장됐다고 했는데
    사실은 안 된' 상황을 사용자에게 성공이라고 말하지 않기 위해서다.
    """
    if not enabled():
        raise RemoteError("원격 저장소가 설정되지 않았습니다 "
                          "(GITHUB_REPO / GITHUB_TOKEN)")
    if not crypto.key_configured():
        raise RemoteError("ENCRYPTION_KEY 가 없어 개인정보를 외부에 "
                          "저장하지 않습니다")

    envelope = json.dumps({
        "v": ENVELOPE_VERSION,
        "key_fp": crypto.fingerprint(),
        "data": crypto.encrypt_json(value),
    }, ensure_ascii=False, indent=2)
    content = base64.b64encode(envelope.encode("utf-8")).decode("ascii")

    sha = _cached_sha(key)
    for attempt in (1, 2):
        payload = {"message": f"chore: {key} 갱신", "content": content,
                   "branch": branch()}
        if sha:
            payload["sha"] = sha      # 기존 파일을 덮어쓸 때만 필요하다
        r = _request("PUT", _url(key), json=payload)
        if r.status_code < 400:
            break
        # sha 가 낡았다 — 다른 곳에서 먼저 저장했다. 한 번만 다시 읽어 재시도.
        if r.status_code in (409, 422) and attempt == 1:
            _, sha = _fetch(key)
            continue
        raise RemoteError(_explain(r))

    invalidate(key)
    # 되읽어 확인 — 여기서 어긋나면 저장 실패로 본다
    back = get(key, use_cache=False)
    if back != value:
        raise RemoteError(f"'{key}' 저장을 확인하지 못했습니다 "
                          "(쓴 값과 읽은 값이 다릅니다)")
    return {"saved": True, "key": key, "verified": True}


def delete(key: str) -> dict:
    """레코드를 지운다 (내 조건 초기화).

    커밋 이력에는 예전 **암호문**이 남는다. 열쇠가 없으면 읽을 수 없지만,
    완전히 없애려면 저장소 자체를 지우면 된다 — HANDOFF 에 적어 뒀다.
    """
    if not enabled():
        raise RemoteError("원격 저장소가 설정되지 않았습니다")
    _, sha = _fetch(key)
    if sha is None:
        invalidate(key)
        return {"deleted": False, "key": key, "reason": "저장된 값이 없습니다"}
    r = _request("DELETE", _url(key),
                 json={"message": f"chore: {key} 삭제", "sha": sha,
                       "branch": branch()})
    if r.status_code >= 400:
        raise RemoteError(_explain(r))
    invalidate(key)
    return {"deleted": True, "key": key}


def ping() -> dict:
    """연결 확인 — 저장소에 닿는지, 토큰 권한이 맞는지 본다."""
    if not enabled():
        return {"ok": False, "reason": "미설정"}
    try:
        r = _request("GET", f"{API}/repos/{repo()}")
    except RemoteError as e:
        return {"ok": False, "reason": str(e)}
    if r.status_code >= 400:
        return {"ok": False, "reason": _explain(r)}
    body = r.json()
    return {"ok": True, "private": bool(body.get("private")),
            "default_branch": body.get("default_branch")}


def history(key: str, limit: int = 10) -> list[dict]:
    """이 레코드의 변경 이력 — **커밋 이력이 곧 백업이다.**

    되돌리려면 해당 커밋의 파일을 받아 복호화해 다시 저장하면 된다.
    (scripts/restore.py 참고)
    """
    if not enabled():
        return []
    r = _request("GET", f"{API}/repos/{repo()}/commits",
                 params={"path": f"{PATH_PREFIX}/{key}.json",
                         "sha": branch(), "per_page": limit})
    if r.status_code >= 400:
        raise RemoteError(_explain(r))
    return [{"sha": c.get("sha", "")[:7],
             "at": (c.get("commit") or {}).get("committer", {}).get("date"),
             "message": (c.get("commit") or {}).get("message", "")}
            for c in (r.json() or [])]


def get_at(key: str, sha: str) -> Optional[Any]:
    """특정 커밋 시점의 값을 복호화해 돌려준다 (복구용, 쓰기는 하지 않는다)."""
    if not enabled():
        return None
    r = _request("GET", _url(key), params={"ref": sha})
    if r.status_code == 404:
        return None
    if r.status_code >= 400:
        raise RemoteError(_explain(r))
    text = base64.b64decode((r.json() or {}).get("content") or "").decode("utf-8")
    env = json.loads(text)
    return crypto.decrypt_json(env["data"])


# 사용자가 따라 할 설정 안내. 코드에 담아 두면 문서와 실제가 어긋나지 않는다.
SETUP_GUIDE = """\
HOME RADAR 영속 저장소 설정 (GitHub 비공개 저장소)

1) 데이터 전용 저장소를 만든다 — 코드 저장소와 **다른** 저장소여야 한다.
   github.com → New repository
     이름        home-radar-data   (아무 이름이나 가능)
     공개 범위   Private           ← 반드시 비공개
     초기화      'Add a README file' 체크   ← main 브랜치가 생겨야 한다

2) 토큰을 만든다 (그 저장소 하나에만 권한을 준다).
   github.com → Settings → Developer settings
     → Personal access tokens → Fine-grained tokens → Generate new token
     Repository access   Only select repositories → home-radar-data
     Permissions         Repository permissions → Contents → Read and write
     Expiration          원하는 기간 (만료되면 다시 만들어 교체한다)

3) Render → 서비스 → Environment 에 3개를 넣는다.
     GITHUB_REPO     <사용자명>/home-radar-data
     GITHUB_TOKEN    위에서 만든 토큰 (github_pat_... )
     ENCRYPTION_KEY  아래 명령으로 만든 값 — 분실하면 복구 불가, 따로 보관할 것
                     python -c "import secrets; print(secrets.token_urlsafe(32))"

4) 저장되면 data/profile.json · data/bookmarks.json 이 그 저장소에 생긴다.
   내용은 암호화돼 있어 GitHub 에서 열어도 읽을 수 없다.
   커밋 이력이 곧 백업이다.
"""
