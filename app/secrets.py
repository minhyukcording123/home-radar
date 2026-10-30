"""API 키 같은 비밀값 읽기 — 로그·화면에 절대 원문을 내보내지 않는다.

읽는 순서:
  1. 프로세스 환경변수 (`$env:LH_API_KEY = "..."`)
  2. 프로젝트 루트의 `.env` 파일  ← .gitignore 에 들어 있다

`.env` 형식 (따옴표는 있어도 되고 없어도 된다):

    LH_API_KEY=여기에_일반인증키_Decoding
    APPLYHOME_API_KEY=여기에_일반인증키_Decoding

**원문 키는 어디에도 출력하지 않는다.** 상태를 보여줄 때는 mask() 로
앞 4자·뒤 2자·길이만 남긴다. 화면·CLI·로그 전부 이 함수를 통과한다.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"

_cache: Optional[dict[str, str]] = None
_cache_mtime: Optional[float] = None


def _parse_env(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
            v = v[1:-1]
        if k:
            out[k] = v
    return out


def load_dotenv(force: bool = False) -> dict[str, str]:
    """.env 를 읽어 캐시한다. 프로세스 환경변수를 덮어쓰지는 않는다.

    **파일 수정시각이 바뀌면 자동으로 다시 읽는다.**
    그래야 서버를 띄운 뒤 .env 에 키를 넣어도 재시작 없이 바로 먹는다
    (키를 받아 넣는 흐름에서 재시작을 요구하면 불편하다).
    """
    global _cache, _cache_mtime
    try:
        mtime = ENV_FILE.stat().st_mtime if ENV_FILE.exists() else None
    except OSError:
        mtime = None

    if _cache is not None and not force and mtime == _cache_mtime:
        return _cache

    if mtime is not None:
        try:
            _cache = _parse_env(ENV_FILE.read_text(encoding="utf-8"))
        except OSError:
            _cache = {}
    else:
        _cache = {}
    _cache_mtime = mtime
    return _cache


def reset_cache() -> None:
    """캐시를 비운다 (테스트용)."""
    global _cache, _cache_mtime
    _cache = None
    _cache_mtime = None


def get(name: str) -> str:
    """환경변수 → .env 순서로 찾는다. 없으면 빈 문자열."""
    v = (os.environ.get(name) or "").strip()
    if v:
        return v
    return (load_dotenv().get(name) or "").strip()


def source_of(name: str) -> Optional[str]:
    """키를 어디서 읽었는지. 설정돼 있지 않으면 None."""
    if (os.environ.get(name) or "").strip():
        return "환경변수"
    if (load_dotenv().get(name) or "").strip():
        return ".env"
    return None


def mask(value: str) -> str:
    """키를 화면에 보여줄 때 쓰는 형태. 원문은 절대 나가지 않는다."""
    v = (value or "").strip()
    if not v:
        return ""
    if len(v) <= 8:
        return "*" * len(v) + f" ({len(v)}자)"
    return f"{v[:4]}{'*' * 6}{v[-2:]} ({len(v)}자)"


def status(name: str) -> dict:
    """설정 여부·출처·마스킹된 값. 원문 키는 담지 않는다."""
    v = get(name)
    return {
        "name": name,
        "set": bool(v),
        "source": source_of(name),
        "masked": mask(v),
        "looks_url_encoded": looks_url_encoded(v),
    }


# ---------------------------------------------------------------- 인코딩 처리
def looks_url_encoded(key: str) -> bool:
    """공공데이터포털 '일반 인증키(Encoding)' 처럼 이미 퍼센트 인코딩된 키인가.

    포털은 같은 키를 Encoding/Decoding 두 가지로 보여준다.
    Decoding 키는 base64 문자(A-Za-z0-9+/=)로만 돼 있어 '%' 가 없다.
    그래서 '%' + 16진수 2자리가 보이면 Encoding 키로 본다.
    """
    k = (key or "").strip()
    if "%" not in k:
        return False
    import re
    return bool(re.search(r"%[0-9A-Fa-f]{2}", k))


def normalize_api_key(key: str) -> str:
    """HTTP 클라이언트에 넘길 형태(디코딩된 원문)로 맞춘다.

    httpx 는 쿼리 파라미터를 알아서 퍼센트 인코딩한다. 그래서 이미 인코딩된
    Encoding 키를 그대로 넘기면 '+' → '%2B' → '%252B' 로 **이중 인코딩**되어
    인증이 깨진다. 여기서 한 번 풀어 주면 어느 쪽 키를 넣어도 동작한다.
    """
    k = (key or "").strip()
    if not k:
        return ""
    if looks_url_encoded(k):
        once = unquote(k)
        # 풀었는데도 여전히 인코딩처럼 보이면(이중 인코딩된 입력) 한 번 더 푼다
        if looks_url_encoded(once):
            once = unquote(once)
        return once.strip()
    return k
