"""로그인 — 비밀번호 1개 + 서명된 세션 쿠키.

HOME RADAR 는 생년·소득·자산·주거정보를 다룬다. 로컬(127.0.0.1)에서만 돌 때는
OS 계정이 울타리였지만, 외부 HTTPS 주소에 올리는 순간 그 울타리가 사라진다.
그래서 **외부에 올릴 때는 반드시 비밀번호를 요구한다.**

설계
----
- 비밀번호는 환경변수 `APP_PASSWORD` 로만 받는다. 코드·저장소에 두지 않는다.
- 세션은 서버에 저장하지 않는다(무료 플랜은 재시작이 잦아 서버 세션이 날아간다).
  대신 `exp|hmac` 형태로 **서명한 쿠키**를 쓴다. 위조하려면 서명 키가 필요하다.
- 서명 키는 `APP_SECRET` 이 있으면 그것을, 없으면 **비밀번호에서 유도**한다.
  유도해서 쓰면 환경변수를 하나만 넣어도 되고, 재시작해도 로그인이 유지되며,
  비밀번호를 바꾸면 기존 세션이 전부 무효가 된다 (원하는 동작이다).
- 비교는 전부 `secrets.compare_digest` — 타이밍 공격으로 한 글자씩 맞히지 못한다.

여기서 비밀번호 원문이나 서명 키를 **로그·응답·화면에 내보내지 않는다.**
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets as pysecrets
import time
from typing import Optional

from . import secrets as appsecrets

COOKIE_NAME = "hr_session"
DEFAULT_TTL_DAYS = 30

# 서명 키를 비밀번호에서 유도할 때 쓰는 고정 salt.
# 공개돼도 무해하다 — 비밀번호를 모르면 키를 만들 수 없다.
_KDF_SALT = b"home-radar/session-signing-key/v1"
_KDF_ROUNDS = 200_000

# 로그인 시도 제한 (IP 단위, 프로세스 메모리).
# 무료 플랜은 인스턴스가 하나라서 메모리만으로 충분하다.
MAX_FAILURES = 10
LOCKOUT_SECONDS = 600
_failures: dict[str, list[float]] = {}


# ---------------------------------------------------------------- 설정 읽기
def password() -> str:
    """로그인 비밀번호. 설정돼 있지 않으면 빈 문자열."""
    return appsecrets.get("APP_PASSWORD")


def enabled() -> bool:
    """로그인을 요구하는가 — `APP_PASSWORD` 가 있으면 켜진다.

    로컬 개발(127.0.0.1)에서는 비밀번호를 안 넣고 쓰던 흐름을 유지한다.
    대신 외부 배포에서는 `require_auth()` 가 비밀번호 없는 기동을 막는다.
    """
    return bool(password())


def require_auth() -> bool:
    """비밀번호가 **반드시** 있어야 하는 환경인가.

    `HR_REQUIRE_AUTH=1` 을 켜 두면(배포 설정에서 켠다) 비밀번호를 빼먹은 채
    기동하는 사고를 막는다 — 개인정보가 열린 채 떠 있는 것보다 안 뜨는 게 낫다.
    """
    return _truthy(appsecrets.get("HR_REQUIRE_AUTH"))


def _truthy(v: str) -> bool:
    return str(v or "").strip().lower() in ("1", "true", "yes", "on")


def check_config() -> None:
    """기동 시 설정 점검. 외부 배포인데 비밀번호가 없으면 띄우지 않는다."""
    if require_auth() and not password():
        raise RuntimeError(
            "APP_PASSWORD 가 설정되지 않았습니다. "
            "외부에 공개되는 배포에서는 로그인 비밀번호가 반드시 필요합니다 "
            "(개인 소득·자산 정보가 들어 있습니다). "
            "호스팅 대시보드의 환경변수에 APP_PASSWORD 를 추가하세요."
        )


def _signing_key() -> bytes:
    """쿠키 서명 키. APP_SECRET 우선, 없으면 비밀번호에서 유도한다."""
    explicit = appsecrets.get("APP_SECRET")
    if explicit:
        return hashlib.sha256(explicit.encode("utf-8")).digest()
    pw = password()
    if not pw:
        # 로그인이 꺼진 로컬 모드 — 서명할 일이 없다. 프로세스 한정 임의값.
        return _ephemeral_key()
    return hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"),
                               _KDF_SALT, _KDF_ROUNDS)


_eph: Optional[bytes] = None


def _ephemeral_key() -> bytes:
    global _eph
    if _eph is None:
        _eph = pysecrets.token_bytes(32)
    return _eph


# ---------------------------------------------------------------- 토큰
def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def _sign(payload: str) -> str:
    mac = hmac.new(_signing_key(), payload.encode("ascii"), hashlib.sha256)
    return _b64e(mac.digest())


def make_token(ttl_days: int = DEFAULT_TTL_DAYS) -> str:
    """`만료시각.서명` 형태의 세션 토큰."""
    exp = int(time.time()) + int(ttl_days) * 86_400
    payload = str(exp)
    return f"{payload}.{_sign(payload)}"


def verify_token(token: Optional[str]) -> bool:
    """서명과 만료를 확인한다. 조금이라도 어긋나면 False."""
    if not token or "." not in token:
        return False
    payload, _, sig = token.rpartition(".")
    if not payload.isdigit():
        return False
    if not hmac.compare_digest(sig, _sign(payload)):
        return False
    return int(payload) > int(time.time())


def check_password(given: Optional[str]) -> bool:
    """비밀번호 확인 — 길이가 달라도 타이밍이 새지 않게 compare_digest 를 쓴다."""
    expected = password()
    if not expected:
        return False
    return hmac.compare_digest(
        hashlib.sha256((given or "").encode("utf-8")).digest(),
        hashlib.sha256(expected.encode("utf-8")).digest(),
    )


# ---------------------------------------------------------------- 시도 제한
def _prune(ip: str, now: float) -> list[float]:
    hits = [t for t in _failures.get(ip, []) if now - t < LOCKOUT_SECONDS]
    if hits:
        _failures[ip] = hits
    else:
        _failures.pop(ip, None)
    return hits


def locked_out(ip: str) -> int:
    """남은 잠금 시간(초). 0 이면 잠겨 있지 않다."""
    now = time.time()
    hits = _prune(ip, now)
    if len(hits) < MAX_FAILURES:
        return 0
    return max(0, int(LOCKOUT_SECONDS - (now - min(hits))) + 1)


def record_failure(ip: str) -> None:
    now = time.time()
    _prune(ip, now)
    _failures.setdefault(ip, []).append(now)


def clear_failures(ip: str) -> None:
    _failures.pop(ip, None)


def reset_state() -> None:
    """테스트용 — 시도 기록과 임시 키를 비운다."""
    global _eph
    _failures.clear()
    _eph = None


# ---------------------------------------------------------------- 쿠키
def cookie_kwargs(secure: bool, ttl_days: int = DEFAULT_TTL_DAYS) -> dict:
    """세션 쿠키 설정.

    httponly  : 자바스크립트가 못 읽는다 (XSS 로 세션을 훔치기 어렵게)
    samesite  : lax — 다른 사이트에서 폼으로 쏘는 요청에 쿠키가 안 붙는다
    secure    : HTTPS 일 때만 붙인다. 로컬 http 에서 켜면 쿠키가 저장되지 않는다.
    """
    return {
        "key": COOKIE_NAME,
        "httponly": True,
        "samesite": "lax",
        "secure": bool(secure),
        "max_age": int(ttl_days) * 86_400,
        "path": "/",
    }


def is_secure_request(scheme: str, forwarded_proto: Optional[str] = None) -> bool:
    """HTTPS 요청인가 — 프록시 뒤에서는 X-Forwarded-Proto 를 본다.

    Render·Fly 같은 호스팅은 TLS 를 앞단에서 끝내고 앱에는 http 로 넘긴다.
    그걸 그대로 믿으면 Secure 쿠키를 못 붙인다.
    """
    if (forwarded_proto or "").split(",")[0].strip().lower() == "https":
        return True
    return str(scheme or "").lower() == "https"


# ---------------------------------------------------------------- 수집 크론
def cron_token() -> str:
    return appsecrets.get("CRON_TOKEN")


def check_cron_token(given: Optional[str]) -> bool:
    """외부 스케줄러(크론)가 수집을 깨울 때 쓰는 토큰."""
    expected = cron_token()
    if not expected:
        return False
    return hmac.compare_digest(
        hashlib.sha256((given or "").encode("utf-8")).digest(),
        hashlib.sha256(expected.encode("utf-8")).digest(),
    )


def ephemeral_storage() -> bool:
    """재시작하면 저장이 날아가는 환경인가 (무료 플랜의 임시 디스크).

    화면에서 '내 조건은 재시작 시 초기화된다'를 안내할지 결정하는 데 쓴다.
    """
    return _truthy(appsecrets.get("HR_EPHEMERAL"))


def client_ip(headers: dict, fallback: str = "?") -> str:
    """프록시 뒤에서의 클라이언트 IP — 시도 제한 집계에만 쓴다."""
    fwd = headers.get("x-forwarded-for") or ""
    if fwd:
        return fwd.split(",")[0].strip() or fallback
    return headers.get("x-real-ip") or fallback


# 환경변수를 직접 읽는 곳이 없도록 한 번 감싸 둔다 (테스트에서 바꿔 끼우기 쉽다)
def env(name: str) -> str:
    return (os.environ.get(name) or "").strip()
