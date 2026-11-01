"""개인정보를 외부 DB에 두기 전에 **앱에서 암호화한다.**

왜 필요한가
-----------
생년·소득·자산·주거정보가 Supabase 같은 외부 DB로 나간다. 전송구간은 HTTPS 로
보호되지만, 그것만으로는 **DB 안에 평문으로 쌓인다.** DB 운영사·백업본·접근키가
새는 순간 소득과 자산이 그대로 읽힌다. 그래서 저장하기 전에 앱이 암호화하고,
DB 에는 의미를 알 수 없는 덩어리만 올린다. 열쇠는 Render 환경변수에만 있다.

구성 — 표준 조합만 쓴다 (새 의존성 없음)
----------------------------------------
암호 알고리즘을 직접 설계하지 않는다. 파이썬 표준 라이브러리의 검증된 원시요소를
**교과서적인 방식으로 조합**한다.

  1. 키 유도   scrypt(ENCRYPTION_KEY) → 마스터 키 32바이트
               (사람이 고른 문자열을 그대로 키로 쓰지 않는다)
  2. 레코드별 부분키
               enc_key = HMAC-SHA256(master, "enc" || nonce)
               mac_key = HMAC-SHA256(master, "mac" || nonce)
               → 레코드마다 키가 달라져 nonce 재사용 사고가 번지지 않는다
  3. 암호화   HMAC-SHA256 을 PRF 로 쓴 카운터 모드 키스트림과 XOR
  4. 인증     encrypt-then-MAC — HMAC-SHA256(mac_key, 버전 || nonce || 암호문)
               복호화 전에 **먼저 태그를 검증**한다. 한 바이트라도 손대면 거부된다.

`secrets.compare_digest` 로 비교해 타이밍 정보가 새지 않게 한다.

저장 형식 (문자열 한 줄)
    v1.<base64url nonce>.<base64url 암호문>.<base64url 태그>

열쇠 분실 = 복구 불가
---------------------
`ENCRYPTION_KEY` 를 잃으면 저장된 내용을 되살릴 방법이 없다. 그래서
  - 키는 Render 환경변수에만 두고 저장소·로그에 남기지 않는다
  - 키를 바꾸면 기존 레코드를 읽을 수 없다 (`decrypt` 가 None 을 돌려준다)
  - 앱은 그 경우 **덮어쓰지 않고** 복구 실패를 그대로 알린다 (remote_store 참고)
운영 절차는 HANDOFF.md 에 적어 둔다.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets as pysecrets
from typing import Optional

from . import secrets as appsecrets

VERSION = b"v1"
NONCE_BYTES = 16
_SCRYPT_SALT = b"home-radar/kv-encryption/v1"
# scrypt 매개변수 — 2026년 기준 상호작용 로그인에 쓰는 보통 세기.
# 저장·조회가 드물어(하루 몇 번) 넉넉히 잡아도 체감되지 않는다.
_N, _R, _P = 2 ** 14, 8, 1


class CryptoError(Exception):
    """복호화 실패 — 키가 다르거나 내용이 손상됐다."""


def key_configured() -> bool:
    return bool(appsecrets.get("ENCRYPTION_KEY"))


def _master_key() -> bytes:
    raw = appsecrets.get("ENCRYPTION_KEY")
    if not raw:
        raise CryptoError(
            "ENCRYPTION_KEY 가 설정되지 않았습니다. "
            "개인정보를 외부 DB에 평문으로 저장하지 않기 위해 반드시 필요합니다.")
    return hashlib.scrypt(raw.encode("utf-8"), salt=_SCRYPT_SALT,
                          n=_N, r=_R, p=_P, dklen=32, maxmem=64 * 1024 * 1024)


# 마스터 키 유도는 느리다(의도적으로). 프로세스 안에서 한 번만 한다.
_cached_master: Optional[tuple[str, bytes]] = None


def _master() -> bytes:
    global _cached_master
    raw = appsecrets.get("ENCRYPTION_KEY")
    if _cached_master is not None and _cached_master[0] == raw:
        return _cached_master[1]
    mk = _master_key()
    _cached_master = (raw, mk)
    return mk


def reset_cache() -> None:
    """테스트용 — 유도된 키 캐시를 비운다."""
    global _cached_master
    _cached_master = None


def _subkeys(master: bytes, nonce: bytes) -> tuple[bytes, bytes]:
    enc = hmac.new(master, b"enc" + nonce, hashlib.sha256).digest()
    mac = hmac.new(master, b"mac" + nonce, hashlib.sha256).digest()
    return enc, mac


def _keystream(enc_key: bytes, nonce: bytes, length: int) -> bytes:
    """HMAC-SHA256 을 PRF 로 쓴 카운터 모드 키스트림."""
    out = bytearray()
    counter = 0
    while len(out) < length:
        out += hmac.new(enc_key, nonce + counter.to_bytes(4, "big"),
                        hashlib.sha256).digest()
        counter += 1
    return bytes(out[:length])


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def encrypt(plaintext: str) -> str:
    """문자열을 암호화해 한 줄 토큰으로. 같은 입력도 매번 다른 값이 나온다."""
    master = _master()
    nonce = pysecrets.token_bytes(NONCE_BYTES)
    enc_key, mac_key = _subkeys(master, nonce)
    data = plaintext.encode("utf-8")
    ct = bytes(a ^ b for a, b in zip(data, _keystream(enc_key, nonce, len(data))))
    tag = hmac.new(mac_key, VERSION + nonce + ct, hashlib.sha256).digest()
    return f"v1.{_b64e(nonce)}.{_b64e(ct)}.{_b64e(tag)}"


def decrypt(token: str) -> str:
    """암호문을 되돌린다. 태그 검증에 실패하면 CryptoError."""
    if not token or not token.startswith("v1."):
        raise CryptoError("알 수 없는 형식입니다 (v1 토큰이 아님)")
    try:
        _, n_b64, ct_b64, tag_b64 = token.split(".", 3)
        nonce, ct, tag = _b64d(n_b64), _b64d(ct_b64), _b64d(tag_b64)
    except (ValueError, base64.binascii.Error) as e:
        raise CryptoError(f"토큰을 해석할 수 없습니다: {e}") from e
    if len(nonce) != NONCE_BYTES:
        raise CryptoError("nonce 길이가 맞지 않습니다")

    master = _master()
    enc_key, mac_key = _subkeys(master, nonce)
    expect = hmac.new(mac_key, VERSION + nonce + ct, hashlib.sha256).digest()
    # **복호화 전에** 검증한다. 위조된 내용을 풀어서 쓰는 일이 없게.
    if not hmac.compare_digest(tag, expect):
        raise CryptoError(
            "인증 태그가 맞지 않습니다 — 키가 다르거나 내용이 손상됐습니다")
    pt = bytes(a ^ b for a, b in zip(ct, _keystream(enc_key, nonce, len(ct))))
    return pt.decode("utf-8")


# ---------------------------------------------------------------- JSON 편의
def encrypt_json(obj) -> str:
    return encrypt(json.dumps(obj, ensure_ascii=False, separators=(",", ":")))


def decrypt_json(token: str):
    return json.loads(decrypt(token))


def fingerprint() -> str:
    """지금 키의 짧은 지문 — 키가 바뀌었는지 **값을 노출하지 않고** 확인한다.

    저장된 레코드에 같이 적어 두면, 복호화가 실패했을 때
    '키가 바뀌었다'와 '데이터가 깨졌다'를 구분할 수 있다.
    """
    if not key_configured():
        return ""
    return hashlib.sha256(b"fp" + _master()).hexdigest()[:12]
