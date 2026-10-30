"""수집원 레지스트리 — 무엇을 어디서 긁는지, 왜 켜고 끄는지 한곳에 모았다.

`robots` 항목은 2026-10-30 에 각 사이트의 robots.txt 를 직접 받아 확인한 결과다.
공개 화면을 긁는 수집원은 여기에 확인 결과와 판단 근거를 남긴다.
"""
from __future__ import annotations

from .. import secrets

OFFICIAL_API = "공식 오픈API"
PUBLIC_PAGE = "공개 화면"

SOURCES: list[dict] = [
    {
        "id": "applyhome",
        "name": "청약홈",
        "owner": "한국부동산원",
        "access": PUBLIC_PAGE,
        "enabled": True,
        "key_env": None,
        "covers": "민영·공공 APT 분양, 무순위/잔여, 오피스텔·도시형, 공공지원민간임대",
        "robots": "robots.txt 없음(404) — 제한 고지 없음",
        "note": "공개 달력 화면이 쓰는 엔드포인트. 공식 오픈API(odcloud)는 키가 있어야 한다",
    },
    {
        "id": "applyhome_api",
        "name": "청약홈 공식 오픈API",
        "owner": "공공데이터포털 15098547",
        "access": OFFICIAL_API,
        "enabled": True,          # 키가 있으면 자동으로 켜진다
        "key_env": "APPLYHOME_API_KEY",
        "covers": "청약홈과 같은 범위를 권위 있는 값으로 보강 (주소·세대수·발표일)",
        "robots": "해당 없음",
        "note": "키 없이 호출하면 401. 활용신청 후 일반 인증키(Decoding)를 환경변수에 넣는다",
        "signup": "https://www.data.go.kr/data/15098547/openapi.do",
    },
    {
        "id": "lh",
        "name": "LH 분양임대공고문",
        "owner": "공공데이터포털 15058530 / 15057999 / 15056765",
        "access": OFFICIAL_API,
        "enabled": True,          # 키가 있으면 자동으로 켜진다
        "key_env": "LH_API_KEY",
        "covers": "LH 임대주택(국민·행복·통합공공임대), 주거복지(매입·전세임대, 든든전세), "
                  "공고별 공급정보(주택형·면적·세대수·금액)",
        "robots": "해당 없음",
        "note": "2026-10-30 확인: 키 없이 호출하면 401 SERVICE_KEY_IS_NULL. "
                "서비스별로 활용신청을 각각 해야 한다 "
                "(목록 15058530 · 상세 15057999 · 공급정보 15056765)",
        "signup": "https://www.data.go.kr/data/15058530/openapi.do",
    },
    {
        "id": "sh",
        "name": "SH 서울주거포털",
        "owner": "서울주택도시공사 / 서울시",
        "access": PUBLIC_PAGE,
        "enabled": True,
        "key_env": None,
        "covers": "SH 공공임대·공공분양 공고 (장기전세, 행복주택, 매입임대 등)",
        "robots": "2026-10-30 확인: User-agent: * / Allow: /  — 전체 허용",
        "note": "SH 는 공식 공고 API 가 없다. 목록에 접수기간이 없어 게시일·발표일·상태만 쓴다",
    },
    {
        "id": "youth",
        "name": "서울 청년안심주택",
        "owner": "서울시",
        "access": PUBLIC_PAGE,
        "enabled": True,
        "key_env": None,
        "covers": "청년안심주택(구 역세권청년주택) 공공·민간 모집공고",
        "robots": "2026-10-30 확인: /robots.txt 가 403 으로 조회 불가. "
                  "RFC 9309 는 robots.txt 가 4xx 면 제한 없음으로 보도록 정한다",
        "note": "목록 화면은 비어 있고 JS 가 JSON 을 불러 채운다 → JSON 엔드포인트를 읽는다",
    },
    {
        "id": "ish",
        "name": "SH 홈페이지 게시판",
        "owner": "서울주택도시공사 (i-sh.co.kr)",
        "access": PUBLIC_PAGE,
        "enabled": False,
        "key_env": None,
        "covers": "서울주거포털에 안 올라오는 SH 공고 (사회주택 등)",
        "robots": "2026-10-30 확인: robots.txt 가 ClaudeBot·Claude-SearchBot·GPTBot 등 "
                  "AI 에이전트를 User-agent 별로 Disallow: / 로 명시 차단",
        "note": "일반 User-agent(*) 규칙은 이 경로를 막지 않지만, 사이트가 AI 에이전트를 "
                "명시적으로 거부했으므로 기본값을 끔으로 둔다. "
                "켜려면 config.yaml 에서 sources.ish.enabled: true 로 바꾼다",
        "disabled_reason": "사이트 robots.txt 가 AI 에이전트를 명시 차단",
    },
]

BY_ID = {s["id"]: s for s in SOURCES}


def api_key(source_id: str) -> str:
    """수집원에 필요한 API 키. 환경변수 → .env 순서. 없으면 빈 문자열.

    돌려주는 값은 **디코딩된 원문**이다. 공공데이터포털의 Encoding 키를 넣어도
    secrets.normalize_api_key 가 한 번 풀어 주므로 이중 인코딩이 생기지 않는다.
    """
    env = (BY_ID.get(source_id) or {}).get("key_env")
    if not env:
        return ""
    return secrets.normalize_api_key(secrets.get(env))


def key_status(source_id: str) -> dict:
    """키 설정 여부·출처·마스킹된 값. **원문 키는 담지 않는다.**"""
    env = (BY_ID.get(source_id) or {}).get("key_env")
    if not env:
        return {"name": None, "set": False, "source": None, "masked": "",
                "looks_url_encoded": False}
    return secrets.status(env)


def needs_key(source_id: str) -> bool:
    return bool((BY_ID.get(source_id) or {}).get("key_env"))


def has_key(source_id: str) -> bool:
    return bool(api_key(source_id))


def describe(source_id: str, cfg_enabled: bool | None = None) -> dict:
    """화면에 보여줄 수집원 설명 + 현재 사용 가능 여부.

    키 원문은 담지 않는다 — 설정 여부와 마스킹된 값만 넣는다.
    """
    s = dict(BY_ID.get(source_id) or {"id": source_id, "name": source_id})
    enabled = s.get("enabled", False) if cfg_enabled is None else cfg_enabled
    s["enabled"] = enabled
    s["needs_key"] = needs_key(source_id)
    s["has_key"] = has_key(source_id)
    s["key"] = key_status(source_id)        # set/source/masked 만
    if not enabled:
        s["ready"] = False
        s["blocked_by"] = s.get("disabled_reason") or "설정에서 꺼져 있음"
    elif s["needs_key"] and not s["has_key"]:
        s["ready"] = False
        s["blocked_by"] = (f"API 키 없음 — {s.get('key_env')} 를 환경변수나 "
                           f".env 에 넣어야 한다")
    else:
        s["ready"] = True
        s["blocked_by"] = ""
    return s
