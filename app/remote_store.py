"""영속 저장소 — Supabase(PostgREST) 에 암호화된 키-값 레코드를 둔다.

왜 이게 필요한가
----------------
Render 무료 플랜은 영구 디스크가 없다. 재시작·슬립 해제·재배포마다 `data/` 와
`profile.json` 이 사라진다. 내 조건은 `PROFILE_JSON` 환경변수로 손수 복원할 수
있었지만 **즐겨찾기는 복구 수단이 아예 없었고**, PC 와 아이폰이 같은 값을
보지도 못했다. 그래서 바깥에 작은 저장소를 하나 둔다.

왜 드라이버를 안 쓰나
---------------------
Supabase 는 PostgREST 로 **HTTP** 접근을 제공한다. 이미 쓰고 있는 httpx 로
충분해서 `psycopg`/`sqlalchemy` 같은 네이티브 의존성을 추가하지 않는다.
무료 인스턴스의 빌드 시간과 메모리를 아끼는 쪽이 낫다.

무엇을 저장하나
---------------
단일 사용자 앱이라 표를 여러 개 만들지 않는다. 키-값 한 장이면 된다.

    home_radar_kv(k text primary key, v text, key_fp text, updated_at timestamptz)

    k = "profile"    내 조건 (생년·소득·자산 …)   ← 암호화해서 넣는다
    k = "bookmarks"  즐겨찾기 key 목록            ← 암호화해서 넣는다
    k = "settings"   화면 설정                    ← 암호화해서 넣는다

공고 캐시는 **여기 두지 않는다.** 공개 정보라 암호화할 이유가 없고, 수백 건이라
무료 DB 용량과 요청 수를 잡아먹는다. 캐시는 지금처럼 로컬 파일에 두고
없으면 다시 수집한다.

보안
----
- 값은 `app/crypto.py` 로 **앱에서 암호화**한 뒤 올린다. DB 는 의미를 모른다.
- 접근키는 Render 환경변수에만 둔다. **service_role 키를 화면으로 내보내지 않는다**
  — 이 모듈은 서버에서만 import 되고, 어떤 API 응답에도 키가 담기지 않는다.
- 실패를 **조용히 삼키지 않는다.** 저장이 안 됐는데 성공이라고 말하지 않는다.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Optional

import httpx

from . import crypto
from . import secrets as appsecrets

TABLE = "home_radar_kv"
TIMEOUT = 8.0          # 무료 DB 가 느릴 때 화면이 통째로 멎지 않게 짧게 끊는다
_lock = threading.Lock()


class RemoteError(Exception):
    """원격 저장소 접근 실패 — 호출한 쪽이 사용자에게 알려야 한다."""


# ---------------------------------------------------------------- 설정
def base_url() -> str:
    """https://<project>.supabase.co 형태."""
    return appsecrets.get("SUPABASE_URL").rstrip("/")


def api_key() -> str:
    """service_role 키 (서버 전용). 절대 화면으로 내보내지 않는다."""
    return appsecrets.get("SUPABASE_KEY")


def enabled() -> bool:
    """원격 저장소를 쓸 수 있는 상태인가."""
    return bool(base_url() and api_key())


def status() -> dict:
    """설정 상태 — **비밀값은 담지 않는다** (화면에 보여줘도 되는 것만)."""
    return {
        "enabled": enabled(),
        "encrypted": crypto.key_configured(),
        "url_set": bool(base_url()),
        "key_set": bool(api_key()),
        "key_fingerprint": crypto.fingerprint(),
    }


def _headers() -> dict:
    k = api_key()
    # HTTP 헤더는 ASCII 만 담을 수 있다. 키를 잘못 붙여 넣어 한글·공백이 섞이면
    # httpx 안쪽에서 UnicodeEncodeError 로 터지는데, 그 메시지로는 원인을 알 수
    # 없다. 여기서 먼저 잡아 무엇이 잘못됐는지 말해 준다. (키 값은 안 보여준다.)
    try:
        k.encode("ascii")
    except UnicodeEncodeError:
        raise RemoteError(
            "SUPABASE_KEY 에 ASCII 가 아닌 문자가 들어 있습니다 — "
            "복사할 때 다른 문자가 섞였는지 확인하세요") from None
    return {
        "apikey": k,
        "Authorization": f"Bearer {k}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _url() -> str:
    return f"{base_url()}/rest/v1/{TABLE}"


# ---------------------------------------------------------------- 캐시
# 단일 사용자라 메모리 캐시 하나면 충분하다. 화면을 열 때마다 DB 를 때리면
# 무료 인스턴스에서 첫 응답이 눈에 띄게 느려진다.
_cache: dict[str, tuple[float, Any]] = {}
CACHE_TTL = 30.0


def invalidate(key: Optional[str] = None) -> None:
    with _lock:
        if key is None:
            _cache.clear()
        else:
            _cache.pop(key, None)


def _cached(key: str):
    hit = _cache.get(key)
    if hit and (time.monotonic() - hit[0]) < CACHE_TTL:
        return True, hit[1]
    return False, None


def _remember(key: str, value) -> None:
    with _lock:
        _cache[key] = (time.monotonic(), value)


# ---------------------------------------------------------------- 읽기/쓰기
def get(key: str, *, use_cache: bool = True) -> Optional[Any]:
    """레코드 하나를 읽어 복호화한다. 없으면 None.

    복호화에 실패하면 **예외를 던진다.** 조용히 None 을 돌려주면 호출한 쪽이
    '아직 저장한 적 없음'으로 오해하고 기본값으로 덮어써서 원본이 날아간다.
    """
    if not enabled():
        return None
    if use_cache:
        ok, val = _cached(key)
        if ok:
            return val

    try:
        r = httpx.get(_url(), headers=_headers(), timeout=TIMEOUT,
                      params={"k": f"eq.{key}", "select": "v,key_fp", "limit": 1})
    except httpx.HTTPError as e:
        raise RemoteError(f"원격 저장소 접속 실패: {type(e).__name__}") from e
    if r.status_code >= 400:
        raise RemoteError(_explain(r))

    rows = r.json() or []
    if not rows:
        _remember(key, None)
        return None

    row = rows[0]
    blob, fp = row.get("v"), row.get("key_fp") or ""
    if blob in (None, ""):
        _remember(key, None)
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

    _remember(key, value)
    return value


def put(key: str, value: Any) -> dict:
    """레코드를 암호화해 저장하고 **읽어서 확인까지** 한다.

    쓰기 응답만 믿지 않고 다시 읽어 같은 값인지 본다 — '저장됐다고 했는데
    사실은 안 된' 상황을 사용자에게 성공이라고 말하지 않기 위해서다.
    """
    if not enabled():
        raise RemoteError("원격 저장소가 설정되지 않았습니다 "
                          "(SUPABASE_URL / SUPABASE_KEY)")
    if not crypto.key_configured():
        raise RemoteError("ENCRYPTION_KEY 가 없어 개인정보를 외부에 "
                          "저장하지 않습니다")

    payload = {"k": key, "v": crypto.encrypt_json(value),
               "key_fp": crypto.fingerprint()}
    try:
        r = httpx.post(
            _url(), headers={**_headers(),
                             # 같은 k 가 있으면 갱신 (upsert)
                             "Prefer": "resolution=merge-duplicates,return=minimal"},
            params={"on_conflict": "k"}, json=payload, timeout=TIMEOUT)
    except httpx.HTTPError as e:
        raise RemoteError(f"원격 저장소 쓰기 실패: {type(e).__name__}") from e
    if r.status_code >= 400:
        raise RemoteError(_explain(r))

    invalidate(key)
    # 되읽어 확인 — 여기서 어긋나면 저장 실패로 본다
    back = get(key, use_cache=False)
    if back != value:
        raise RemoteError(f"'{key}' 저장을 확인하지 못했습니다 "
                          "(쓴 값과 읽은 값이 다릅니다)")
    return {"saved": True, "key": key, "verified": True}


def delete(key: str) -> dict:
    """레코드를 지운다 (내 조건 초기화)."""
    if not enabled():
        raise RemoteError("원격 저장소가 설정되지 않았습니다")
    try:
        r = httpx.delete(_url(), headers=_headers(), timeout=TIMEOUT,
                         params={"k": f"eq.{key}"})
    except httpx.HTTPError as e:
        raise RemoteError(f"원격 저장소 삭제 실패: {type(e).__name__}") from e
    if r.status_code >= 400:
        raise RemoteError(_explain(r))
    invalidate(key)
    return {"deleted": True, "key": key}


def ping() -> dict:
    """연결 확인 — 표가 만들어졌는지, 키가 맞는지 본다.

    시간당 크론이 이걸 한 번씩 불러 **무료 DB 가 휴면에 들어가지 않게** 한다
    (Supabase 무료 프로젝트는 일정 기간 활동이 없으면 일시중지된다).
    """
    if not enabled():
        return {"ok": False, "reason": "미설정"}
    try:
        r = httpx.get(_url(), headers=_headers(), timeout=TIMEOUT,
                      params={"select": "k", "limit": 1})
    except httpx.HTTPError as e:
        return {"ok": False, "reason": f"{type(e).__name__}"}
    if r.status_code >= 400:
        return {"ok": False, "reason": _explain(r)}
    return {"ok": True, "rows": len(r.json() or [])}


def _explain(r: httpx.Response) -> str:
    """PostgREST 오류를 사람이 읽을 수 있게. **키는 절대 담지 않는다.**"""
    try:
        body = r.json()
        msg = body.get("message") or body.get("hint") or str(body)[:160]
    except ValueError:
        msg = (r.text or "")[:160]
    if r.status_code in (401, 403):
        return f"인증 거부({r.status_code}) — SUPABASE_KEY 를 확인하세요"
    if r.status_code == 404 and TABLE in msg:
        return (f"표 '{TABLE}' 가 없습니다 — Supabase SQL 편집기에서 "
                "생성 SQL을 먼저 실행하세요")
    return f"원격 저장소 오류({r.status_code}): {msg}"


# 표 생성 SQL — 사용자가 Supabase SQL 편집기에 붙여 넣는다.
# 코드에 담아 두면 HANDOFF 와 실제 스키마가 어긋나지 않는다.
CREATE_TABLE_SQL = f"""
-- HOME RADAR 영속 저장소 (단일 사용자)
create table if not exists public.{TABLE} (
  k          text primary key,
  v          text not null,            -- 앱에서 암호화한 값 (평문 아님)
  key_fp     text,                     -- 암호화 키 지문 (키 교체 감지용)
  updated_at timestamptz not null default now()
);

-- 갱신 시각 자동 기록
create or replace function public.home_radar_touch()
returns trigger language plpgsql as $$
begin new.updated_at = now(); return new; end $$;

drop trigger if exists home_radar_kv_touch on public.{TABLE};
create trigger home_radar_kv_touch before update on public.{TABLE}
  for each row execute function public.home_radar_touch();

-- 행 수준 보안을 켜고 정책을 두지 않는다.
-- anon/authenticated 키로는 한 줄도 읽거나 쓸 수 없고,
-- 서버가 쓰는 service_role 키만 RLS 를 통과한다.
alter table public.{TABLE} enable row level security;
""".strip()
