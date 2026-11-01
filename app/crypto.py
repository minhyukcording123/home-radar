"""개인정보를 외부에 저장하기 전에 **앱에서 암호화한다.**

왜 필요한가
-----------
생년·소득·자산·주거정보가 바깥(GitHub 비공개 저장소)으로 나간다. 전송구간은
HTTPS 로 보호되지만 그것만으로는 **저장된 쪽에 평문으로 쌓인다.** 그래서
저장하기 전에 앱이 암호화하고, 바깥에는 의미를 알 수 없는 덩어리만 올린다.
열쇠는 Render 환경변수에만 있다.

무엇을 쓰나 — Fernet (검증된 표준 구현)
---------------------------------------
`cryptography` 라이브러리의 **Fernet** 을 쓴다. 직접 만든 조합이 아니라
널리 검토된 표준 구현이라는 점이 핵심이다.

    AES-128-CBC (암호화) + HMAC-SHA256 (인증) + encrypt-then-MAC
    버전 바이트·타임스탬프·IV 를 포함한 고정 형식, 복호화 전 태그 검증

예전에는 표준 라이브러리만으로 같은 구조를 직접 조합했다(scrypt + HMAC 카운터
모드 + encrypt-then-MAC). 구성 자체는 교과서적이었지만 **이름 있는 AEAD 가
아니었다.** 소득·자산을 다루는 이상 "검증된 표준 구현"이라고 말할 수 있는
쪽이 낫다고 보고 Fernet 으로 옮겼다. 의존성 하나(`cryptography`)가 늘지만
리눅스 휠이 있어 배포 빌드에는 영향이 없다.

키 유도
-------
사람이 고른 문자열을 그대로 키로 쓰지 않는다. `scrypt` 로 32바이트를 뽑아
Fernet 이 요구하는 형식(urlsafe base64)으로 바꾼다. 같은 `ENCRYPTION_KEY`
문자열이면 언제나 같은 키가 나오므로 재시작해도 읽힌다.

열쇠 분실 = 복구 불가
---------------------
`ENCRYPTION_KEY` 를 잃으면 저장된 내용을 되살릴 방법이 없다. 그래서
  - 키는 Render 환경변수에만 두고 저장소·로그에 남기지 않는다
  - 키를 바꾸면 기존 레코드를 읽을 수 없다 (`CryptoError`)
  - 앱은 그 경우 **덮어쓰지 않고** 복구 실패를 그대로 알린다 (remote_store 참고)
운영 절차는 HANDOFF.md 에 적어 둔다.
"""
from __future__ import annotations

import base64
import hashlib
import json
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

from . import secrets as appsecrets

_SCRYPT_SALT = b"home-radar/kv-encryption/v1"
# scrypt 매개변수 — 저장·조회가 드물어(하루 몇 번) 넉넉히 잡아도 체감되지 않는다.
_N, _R, _P = 2 ** 14, 8, 1


class CryptoError(Exception):
    """복호화 실패 — 키가 다르거나 내용이 손상됐다."""


def key_configured() -> bool:
    return bool(appsecrets.get("ENCRYPTION_KEY"))


def _derive(raw: str) -> bytes:
    """사람이 고른 문자열 → Fernet 키(urlsafe base64 32바이트)."""
    digest = hashlib.scrypt(raw.encode("utf-8"), salt=_SCRYPT_SALT,
                            n=_N, r=_R, p=_P, dklen=32,
                            maxmem=64 * 1024 * 1024)
    return base64.urlsafe_b64encode(digest)


# 키 유도는 느리다(의도적으로). 프로세스 안에서 한 번만 한다.
_cached: Optional[tuple[str, Fernet]] = None


def _fernet() -> Fernet:
    global _cached
    raw = appsecrets.get("ENCRYPTION_KEY")
    if not raw:
        raise CryptoError(
            "ENCRYPTION_KEY 가 설정되지 않았습니다. "
            "개인정보를 외부에 평문으로 저장하지 않기 위해 반드시 필요합니다.")
    if _cached is not None and _cached[0] == raw:
        return _cached[1]
    f = Fernet(_derive(raw))
    _cached = (raw, f)
    return f


def reset_cache() -> None:
    """테스트용 — 유도된 키 캐시를 비운다."""
    global _cached
    _cached = None


# ---------------------------------------------------------------- 암·복호화
def encrypt(plaintext: str) -> str:
    """문자열을 암호화해 한 줄 토큰으로. 같은 입력도 매번 다른 값이 나온다."""
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt(token: str) -> str:
    """암호문을 되돌린다. 위조·손상·키 불일치면 CryptoError.

    Fernet 은 **복호화 전에** HMAC 태그를 검증하므로, 한 바이트만 바뀌어도
    내용을 풀지 않고 거부한다.
    """
    if not token:
        raise CryptoError("빈 토큰입니다")
    try:
        return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except InvalidToken as e:
        raise CryptoError(
            "복호화에 실패했습니다 — 키가 다르거나 내용이 손상됐습니다") from e
    except (UnicodeDecodeError, UnicodeEncodeError, ValueError) as e:
        raise CryptoError(f"토큰을 해석할 수 없습니다: {e}") from e


def encrypt_json(obj) -> str:
    return encrypt(json.dumps(obj, ensure_ascii=False, separators=(",", ":")))


def decrypt_json(token: str):
    return json.loads(decrypt(token))


def fingerprint() -> str:
    """지금 키의 짧은 지문 — 키가 바뀌었는지 **값을 노출하지 않고** 확인한다.

    저장된 레코드에 같이 적어 두면, 복호화가 실패했을 때
    '키가 바뀌었다'와 '데이터가 깨졌다'를 구분할 수 있다.
    """
    raw = appsecrets.get("ENCRYPTION_KEY")
    if not raw:
        return ""
    # 유도된 키를 한 번 더 해시한다 — 지문에서 키를 되돌릴 수 없게
    return hashlib.sha256(b"fp" + _derive(raw)).hexdigest()[:12]
