"""청약홈 분양정보 공식 오픈API — 공공데이터포털 15098547 (한국부동산원).

캘린더(applyhome_cal)가 '언제'를, 상세 HTML(applyhome_detail)이 '무엇을'을 주는데,
이 API 는 같은 공고의 **권위 있는 값**(주소·총 세대수·당첨발표일·접수기간·공고 URL)을
한 번에 준다. 키 없는 경로를 대체하지 않고 보강한다 — dedup.SOURCE_RANK 에서
applyhome_api(0) 가 applyhome(2) 보다 우선이라 같은 공고가 양쪽에서 들어오면
공식 API 쪽 값이 남는다. 그래서 제목(HOUSE_NM)·지역(to_sido)·접수기간을
applyhome_cal 과 같은 모양으로 만들어야 중복이 잡힌다 (아래 to_notice 참고).

엔드포인트 (BASE 아래, 같은 키로 모두 호출):
  getAPTLttotPblancDetail          민영·국민 APT 분양           [포털 문서에서 확인]
  getRemndrLttotPblancDetail       무순위·잔여세대              [포털 문서에서 확인]
  getUrbtyOfctlLttotPblancDetail   오피스텔·도시형·민간임대      [포털 문서에서 확인]
  getPblPvtRentLttotPblancDetail   공공지원 민간임대
      ← 2026-10-30 현재 포털 문서(15098547 openapi.do)에는 **안 보이지만**
        게이트웨이에 라우트가 살아 있다 — 더미 키로 호출하면 401 {code:-4} 가 오고,
        없는 서비스는 404 {"code":-3,"msg":"등록되지 않은 서비스 입니다."} 가 온다.
        cheongyak-auto(MIT) 도 이 엔드포인트를 쓴다. 승인 키로 404 가 오면
        경고만 남기고 나머지 유형은 계속 수집한다.

**오류 응답 형식** — LH(apis.data.go.kr)와 호스트도 형식도 다르다. 2026-10-30 실측:
  키 없음:     HTTP 401  {"code":-401,"msg":"인증키는 필수 항목 입니다."}
  미등록 키:   HTTP 401  {"code":-4,"msg":"등록되지 않은 인증키 입니다."}
  없는 서비스: HTTP 404  {"code":-3,"msg":"등록되지 않은 서비스 입니다."}
LH 의 OpenAPI_ServiceResponse/cmmMsgHeader XML 헤더 꼴이 아니라 평평한
{code, msg} JSON 이므로 lh._request 를 재사용하지 않고 odcloud 전용 분류를
따로 뒀다. safe_client(close 중 오류가 진짜 원인을 가리지 않게) 만 가져다 쓴다.

**인증키 취급** — lh.py 와 같은 원칙:
  키는 registry.api_key("applyhome_api") 로만 읽는다 (환경변수/.env,
  Encoding 키 이중 인코딩 방지 포함). 예외·경고 문자열은 전부 _scrub() 을
  거쳐 키 원문이 로그·화면에 남지 않게 한다.
"""
from __future__ import annotations

import time
from datetime import date, timedelta
from typing import Optional

import httpx

from ..models import Notice
from ..util import parse_date, to_sido
from . import registry
from .lh import safe_client

BASE = "https://api.odcloud.kr/api/ApplyhomeInfoDetailSvc/v1/"

# kind → (오퍼레이션, 유형 라벨, HOUSE_SECD 기본값)
# HOUSE_SECD 기본값은 models.HOUSE_SECD 와 상세 화면 분기(applyhome_detail.detail_path)
# 기준이다. 응답에 HOUSE_SECD 가 있으면 그 값을 쓴다 (포털 문서에
# cond[HOUSE_SECD::EQ] 조건이 있어 필드 존재는 확인했지만 전 오퍼레이션 공통인지는
# 키가 없어 못 봤다 — 추정).
KINDS: dict[str, tuple[str, str, str]] = {
    "apt": ("getAPTLttotPblancDetail", "APT", "01"),
    "remndr": ("getRemndrLttotPblancDetail", "무순위/잔여", "04"),
    "urbty": ("getUrbtyOfctlLttotPblancDetail", "오피스텔/도시형/민간임대", "02"),
    "pvtrent": ("getPblPvtRentLttotPblancDetail", "공공지원민간임대", "05"),
}
DEFAULT_KINDS = list(KINDS)

# 호출 예절 / 안정성 — lh.py 와 같은 수준
POLITE_DELAY = 0.35        # 연속 호출 사이 간격(초)
MAX_RETRY = 3
BACKOFF = (0.8, 2.0, 4.5)  # 재시도 대기(초)

AUTH_STATUS = (401, 403)                      # 2026-10-30 실측: 키 문제는 401
RETRY_STATUS = (408, 429, 500, 502, 503, 504)


class ApplyhomeAuthError(PermissionError):
    """키·권한 문제 — 재시도해도 소용없다. 호출한 쪽이 '키 없음'으로 기록한다."""


class ApplyhomeQuotaError(RuntimeError):
    """호출 한도 초과 — 더 긁어도 소용없다."""


class ApplyhomeApiError(RuntimeError):
    """odcloud 가 본문 {code, msg} 로 설명한 결정적 오류 — 재시도해도 같은 결과다.

    -2 잘못된 요청 파라미터(cond 거부가 여기로 온다) · -3 등록되지 않은 서비스 등.
    재시도 대상(일시 오류)과 구분해 호출을 아낀다."""


def _scrub(text, key: str) -> str:
    """예외·경고에 키 원문이 섞이지 않게 지운다 (로그 유출 방지)."""
    s = str(text)
    if key and len(key) >= 8:
        s = s.replace(key, "<KEY>")
    return s


def _classify(status: int, body) -> Optional[Exception]:
    """odcloud 응답을 예외로 분류한다. 정상이면 None.

    상태코드보다 본문의 {code, msg} 를 먼저 본다 — 메시지가 한국어로 원인을
    설명해 주므로 그대로 쓰는 쪽이 사용자에게 더 유용하다.
    """
    code, msg = None, ""
    if isinstance(body, dict) and "code" in body and "data" not in body:
        try:
            code = int(body.get("code"))
        except (TypeError, ValueError):
            code = None
        msg = str(body.get("msg") or "").strip()

    if code is not None and code < 0:
        # -401 인증키 누락 · -4 미등록 인증키 (둘 다 2026-10-30 실측)
        if code in (-401, -4) or status in AUTH_STATUS:
            return ApplyhomeAuthError(
                f"odcloud {code} — {msg or '인증키가 거부됐다'}. "
                "일반 인증키(Decoding)인지, 15098547 활용신청이 승인됐는지 확인")
        if "한도" in msg or status == 429:
            return ApplyhomeQuotaError(f"odcloud {code} — {msg or '호출 한도 초과'}")
        # -2 잘못된 파라미터 · -3 등록되지 않은 서비스(404) 등 — 재시도해도 같은 결과
        return ApplyhomeApiError(f"odcloud {code} — {msg}")

    if status in AUTH_STATUS:
        return ApplyhomeAuthError(
            f"HTTP {status} — 인증키가 거부됐다. 일반 인증키(Decoding)인지, "
            "15098547 활용신청이 승인됐는지 확인")
    if status == 429:
        return ApplyhomeQuotaError("HTTP 429 — 호출 한도 초과")
    if status in RETRY_STATUS:
        return httpx.HTTPError(f"HTTP {status} (일시 오류)")
    if status >= 400:
        return ApplyhomeApiError(f"HTTP {status}")
    return None


def _request(client: httpx.Client, url: str, params: dict, key: str) -> dict:
    """재시도·백오프를 입힌 GET. 키·권한·한도 문제면 바로 포기한다."""
    last: Optional[Exception] = None
    for attempt in range(MAX_RETRY):
        try:
            r = client.get(url, params=params)
            try:
                body = r.json()
            except Exception:  # noqa: BLE001 — JSON 이 아니면 상태코드로만 분류
                body = None
            err = _classify(r.status_code, body)
            if err is not None:
                raise err
            if not isinstance(body, dict):
                raise RuntimeError("응답이 JSON 객체가 아니다")
            return body
        except (ApplyhomeAuthError, ApplyhomeQuotaError, ApplyhomeApiError):
            raise                      # 재시도해도 소용없다
        except Exception as e:  # noqa: BLE001 — 네트워크·일시 오류는 재시도
            last = e
            if attempt < MAX_RETRY - 1:
                time.sleep(BACKOFF[attempt])
    raise RuntimeError(_scrub(f"{type(last).__name__}: {last}", key))


def _first(it: dict, *keys: str) -> str:
    """후보 필드명 중 처음으로 값이 있는 것 — 날짜 필드명이 오퍼레이션마다 다르다."""
    for k in keys:
        v = it.get(k)
        if v not in (None, ""):
            return str(v).strip()
    return ""


def to_notice(it: dict, kind: str) -> Notice:
    """API 응답 한 건 → Notice.

    필드명 출처 구분 (2026-10-30, 포털 문서 15098547 openapi.do 에서 대조):
      [확인] HOUSE_NM / HSSPLY_ADRES / RCRIT_PBLANC_DE / PRZWNER_PRESNATN_DE /
             TOT_SUPLY_HSHLDCO / PBLANC_NO / HOUSE_MANAGE_NO /
             SUBSCRPT_AREA_CODE_NM / HOUSE_SECD_NM / HOUSE_DTL_SECD_NM /
             HMPG_ADRES / MDHS_TELNO / MVN_PREARNGE_YM
      [확인] 접수기간: APT 는 RCEPT_BGNDE/RCEPT_ENDDE,
             무순위·오피스텔은 SUBSCRPT_RCEPT_BGNDE/SUBSCRPT_RCEPT_ENDDE
      [추정] PBLANC_URL — 포털 문서에는 없지만 cheongyak-auto 가 실응답에서 써 온
             필드다. 없으면 HMPG_ADRES(사업주체 홈페이지)로 대신한다.
      [추정] GNRL_RCEPT_*/GNRL_RNK* (공공지원민간임대 쪽 날짜),
             SUPLY_HSHLDCO, HOUSE_DETAIL_SECD_NM — cheongyak-auto 의 실응답 해석.

    **중복 제거와의 약속**: dedup 2단계는 (normalize_title(제목), 지역) 이 같고
    접수시작이 3일 이내면 합친다. applyhome_cal 과 맞물리려면
      제목 = HOUSE_NM 그대로(군더더기 금지),
      지역 = to_sido(SUBSCRPT_AREA_CODE_NM)  ← 캘린더와 같은 필드·같은 변환
    이어야 한다. 캘린더의 begin 은 특별공급 포함 최소 일정이라 API 의
    접수시작과 1~2일 어긋날 수 있는데, 3일 허용 범위 안이라 잡힌다.
    """
    _, label, default_secd = KINDS[kind]
    addr = _first(it, "HSSPLY_ADRES")
    region = _first(it, "SUBSCRPT_AREA_CODE_NM")
    hmn = _first(it, "HOUSE_MANAGE_NO")
    pbn = _first(it, "PBLANC_NO")
    dtl = _first(it, "HOUSE_DTL_SECD_NM", "HOUSE_DETAIL_SECD_NM")
    secd_nm = _first(it, "HOUSE_SECD_NM")

    households = _first(it, "TOT_SUPLY_HSHLDCO", "SUPLY_HSHLDCO")
    if households.isdigit():
        households = f"{int(households)}세대"   # LH·상세 보강과 같은 표기

    return Notice(
        source="청약홈",
        source_id="applyhome_api",
        # uid 에 kind 를 넣는 이유: 캘린더의 uid(hmn-pbn)와 겹치면 Notice.key
        # ("청약홈:" + uid)가 충돌해 북마크·관측 기록이 섞인다. 중복 제거는
        # key 가 아니라 제목·지역·접수기간으로 하므로 uid 는 달라도 된다.
        uid=f"{kind}:{pbn or hmn}",
        title=_first(it, "HOUSE_NM"),
        category=" ".join(x for x in (label, dtl or secd_nm) if x),
        region=to_sido(region) or to_sido(addr),
        address=addr,
        house_secd=_first(it, "HOUSE_SECD") or default_secd,
        house_manage_no=hmn,
        pblanc_no=pbn,
        posted=parse_date(_first(it, "RCRIT_PBLANC_DE")),
        begin=parse_date(_first(
            it, "RCEPT_BGNDE", "SUBSCRPT_RCEPT_BGNDE",
            "GNRL_RCEPT_BGNDE", "GNRL_RNK1_CRSPAREA_RCPTDE")),
        end=parse_date(_first(
            it, "RCEPT_ENDDE", "SUBSCRPT_RCEPT_ENDDE",
            "GNRL_RCEPT_ENDDE", "GNRL_RNK2_ETC_AREA_ENDDE")),
        announce=parse_date(_first(it, "PRZWNER_PRESNATN_DE")),
        households=households,
        url=_first(it, "PBLANC_URL", "HMPG_ADRES"),
        phone=_first(it, "MDHS_TELNO"),
        move_in=_first(it, "MVN_PREARNGE_YM"),
        detail_loaded=True,     # 공식 값이 이미 들어 있다 — 상세 보강 불필요 표시
    )


def _fetch_kind(client: httpx.Client, op: str, key: str, since: date,
                per_page: int, max_pages: int) -> tuple[list[dict], bool]:
    """한 오퍼레이션을 페이지 끝까지(상한 내) 긁는다. (items, cond 거부 여부).

    cond[RCRIT_PBLANC_DE::GTE] 는 포털 문서에서 확인한 조건 파라미터지만,
    odcloud 게이트웨이가 조건을 거부하는 사례가 있어(cheongyak-auto 의 경험)
    1페이지에서 실패하면 조건 없이 한 번 더 시도한다. 키·한도 문제는 예외 —
    조건을 빼도 같은 결과라 바로 올린다.
    """
    url = BASE + op
    items: list[dict] = []
    page, use_cond, dropped_cond = 1, True, False
    while page <= max_pages:
        params: dict = {"page": page, "perPage": per_page,
                        "serviceKey": key, "returnType": "JSON"}
        if use_cond:
            params["cond[RCRIT_PBLANC_DE::GTE]"] = since.isoformat()
        try:
            body = _request(client, url, params, key)
        except (ApplyhomeAuthError, ApplyhomeQuotaError):
            raise
        except Exception:
            if use_cond and page == 1:
                use_cond, dropped_cond = False, True
                continue
            raise
        data = body.get("data") or []
        items.extend(d for d in data if isinstance(d, dict))
        # totalCount(전체)와 matchCount(조건 일치)의 의미가 조건 유무에 따라
        # 달라서 개수 계산에 기대지 않고 '가득 차지 않은 페이지'에서 멈춘다.
        if len(data) < per_page:
            break
        page += 1
        time.sleep(POLITE_DELAY)
    return items, dropped_cond


def collect(today: date, *, kinds: Optional[list[str]] = None,
            lookback_days: int = 60, per_page: int = 300,
            max_pages: int = 10) -> tuple[list[Notice], list[str]]:
    """청약홈 공식 API 에서 공고를 모은다. (Notice 목록, 경고 목록).

    키가 없으면 ApplyhomeAuthError(PermissionError) 를 던진다 —
    호출한 쪽이 '키 없음'으로 기록한다. 한 유형(엔드포인트)이 실패해도
    나머지 유형은 계속 수집하고, 키·한도 문제만 전체를 세운다.
    """
    key = registry.api_key("applyhome_api")
    if not key:
        raise ApplyhomeAuthError(
            "APPLYHOME_API_KEY 가 없다. https://www.data.go.kr/data/15098547/openapi.do "
            "에서 활용신청 후 .env 에 APPLYHOME_API_KEY=<일반 인증키(Decoding)> 를 "
            "넣거나 환경변수로 설정한다")

    kinds = [k for k in (kinds or DEFAULT_KINDS) if k in KINDS] or DEFAULT_KINDS
    # 호출량 상한 — 유형당 max_pages 페이지가 전부라 최대 len(kinds)*max_pages 회
    per_page = max(1, min(int(per_page), 1000))
    max_pages = max(1, min(int(max_pages), 30))
    since = today - timedelta(days=max(1, int(lookback_days)))

    out: list[Notice] = []
    warnings: list[str] = []
    seen: set[str] = set()
    per_kind: dict[str, int] = {}
    auth_failed: Optional[ApplyhomeAuthError] = None

    with safe_client() as client:
        for i, kind in enumerate(kinds):
            op, label, _ = KINDS[kind]
            if i:
                time.sleep(POLITE_DELAY)
            try:
                items, dropped_cond = _fetch_kind(
                    client, op, key, since, per_page, max_pages)
            except ApplyhomeAuthError as e:
                auth_failed = e
                break                  # 다른 유형도 같은 키 — 호출 아낌
            except ApplyhomeQuotaError as e:
                warnings.append(f"청약홈API 호출 한도: {_scrub(e, key)}")
                break
            except Exception as e:  # noqa: BLE001 — 한 유형 실패가 전체를 죽이지 않게
                warnings.append(f"청약홈API {label} 조회 실패: {_scrub(e, key)}")
                continue

            if dropped_cond:
                warnings.append(
                    f"[정보] 청약홈API {label}: 날짜 조건(cond)이 거부되어 "
                    "조건 없이 수집한 뒤 모집공고일로 걸렀다")

            got = 0
            for it in items:
                n = to_notice(it, kind)
                if not n.uid.split(":", 1)[1] or not n.title:
                    continue           # 식별자·제목 없는 행은 쓸 수 없다
                # cond 가 거부됐을 때를 대비한 클라이언트 측 기간 필터.
                # 모집공고일이 없는 행은 버리지 않는다 (접수가 남아 있을 수 있다).
                if n.posted is not None and n.posted < since:
                    continue
                if n.key in seen:
                    continue
                seen.add(n.key)
                out.append(n)
                got += 1
            per_kind[label] = got

    if auth_failed:
        if out:
            # 일부 유형을 이미 모은 뒤 키가 거부된 건 비정상 — 모은 것은 살리되 남긴다
            warnings.append(f"청약홈API 인증 거부(일부 유형만 수집됨): "
                            f"{_scrub(auth_failed, key)}")
        else:
            raise auth_failed

    detail = " · ".join(f"{k} {v}건" for k, v in per_kind.items() if v)
    if detail:
        warnings.append(f"[정보] 청약홈API 유형별 수집: {detail}")
    return out, warnings
