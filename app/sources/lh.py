"""LH 분양임대공고문 — 공공데이터포털 공식 오픈API.

  목록: 15058530  lhLeaseNoticeInfo1      공고명·지역·유형·게시일·마감일·원문링크
  상세: 15057999  getLeaseNoticeDtlInfo1  접수시작·마감·당첨발표일·주소·세대수

cheongyak-auto(MIT) 의 `cheong/sources/lh.py` 에서 엔드포인트와 응답 구조 해석을
가져왔고, 인증키 처리·재시도·호출 제한·유형 세분화를 새로 붙였다.

UPP_AIS_TP_CD (상위 공고 유형)
  05 분양주택 · 06 임대주택 · 13 주거복지(매입·전세임대) · 39 신혼희망타운

**인증키 취급**
  - 키는 환경변수 또는 .env 에서만 읽는다 (app/secrets.py). 코드에 쓰지 않는다.
  - 포털이 주는 Encoding 키를 넣어도 한 번 풀어서 넘긴다 → 이중 인코딩 방지.
  - 예외 메시지·로그·화면에 키 원문이 섞이지 않도록 응답 본문에서 걸러낸다.
"""
from __future__ import annotations

import time
from contextlib import contextmanager
from datetime import date
from typing import Optional
from urllib.parse import urlparse

import httpx

from ..models import Notice
from ..util import new_client, parse_date, to_sido
from . import registry

LIST_URL = "https://apis.data.go.kr/B552555/lhLeaseNoticeInfo1/lhLeaseNoticeInfo1"
DTL_URL = "https://apis.data.go.kr/B552555/lhLeaseNoticeDtlInfo1/getLeaseNoticeDtlInfo1"

APPLY_HOST = "https://apply.lh.or.kr"

# 상위 공고 유형 — 기본으로 네 가지를 모두 모은다
UPP_TYPES = {
    "05": "분양주택",
    "06": "임대주택",
    "13": "주거복지",
    "39": "신혼희망타운",
}
DEFAULT_UPP = ["05", "06", "13", "39"]

# 호출 예절 / 안정성
PAGE_SIZE = 100
POLITE_DELAY = 0.4        # 연속 호출 사이 간격(초)
MAX_RETRY = 3
BACKOFF = (0.8, 2.0, 4.5)  # 재시도 대기

# data.go.kr 공통 오류코드 → 사람이 읽고 바로 조치할 수 있는 문구
AUTH_ERRORS = {
    "SERVICE_KEY_IS_NULL":
        "인증키가 전달되지 않았다 (LH_API_KEY 미설정)",
    "SERVICE_KEY_IS_NOT_REGISTERED_ERROR":
        "등록되지 않은 인증키다. 활용신청이 승인된 키인지, "
        "일반 인증키(Decoding)를 넣었는지 확인",
    "SERVICE_ACCESS_DENIED_ERROR":
        "이 서비스에 대한 활용신청이 승인되지 않았다 "
        "(목록 15058530 · 상세 15057999 · 공급정보 15056765 를 각각 신청해야 한다)",
    "LIMITED_NUMBER_OF_SERVICE_REQUESTS_EXCEEDS_ERROR":
        "일일 호출 한도를 초과했다. 내일 다시 시도하거나 한도 증설을 신청",
    "DEADLINE_HAS_EXPIRED_ERROR":
        "활용신청 기간이 만료됐다. 포털에서 연장 신청",
    "UNREGISTERED_IP_ERROR":
        "등록되지 않은 IP 다. 포털에서 허용 IP 를 확인",
    "NODATA_ERROR": "조회 결과가 없다",
}
RETRYABLE = {"HTTP_ERROR", "APPLICATION_ERROR", "DB_ERROR",
             "SERVICETIMEOUT_ERROR", "UNKNOWN_ERROR"}


class LhAuthError(PermissionError):
    """키·권한 문제 — 재시도해도 소용없다."""


class LhQuotaError(RuntimeError):
    """호출 한도 초과."""


def _scrub(text: str, key: str) -> str:
    """혹시 응답이나 URL 에 키가 섞여 있으면 지운다 (로그 유출 방지)."""
    s = str(text)
    if key and len(key) >= 8:
        s = s.replace(key, "<KEY>")
    return s


@contextmanager
def safe_client(existing: Optional[httpx.Client] = None):
    """httpx 클라이언트를 쓰고 **닫는 중 오류가 진짜 원인을 가리지 않게** 한다.

    `with new_client() as c:` 를 쓰면 블록에서 예외가 난 뒤 close() 가 또 실패할 때
    (data.go.kr 로 가는 TLS 연결이 간헐적으로 끊긴다) 바깥에서 보이는 예외가
    close 쪽 SSLError 로 바뀐다. 그러면 "등록되지 않은 인증키다" 같은 조치 안내를
    잃고 사용자는 뭘 고쳐야 할지 모른다. 그래서 close 는 삼킨다.

    `existing` 을 넘기면 그 클라이언트를 쓰고 닫지 않는다 (소유권은 호출한 쪽).
    """
    own = existing is None
    client = existing or new_client()
    try:
        yield client
    finally:
        if own:
            try:
                client.close()
            except Exception:  # noqa: BLE001 — 닫는 중 오류는 보고할 가치가 없다
                pass


def _seg(body, name: str) -> list:
    """LH 응답은 [{head:...}, {dsList:[...]}] 꼴이라 세그먼트를 이름으로 찾는다."""
    if isinstance(body, list):
        for s in body:
            if isinstance(s, dict) and isinstance(s.get(name), list):
                return s[name]
    if isinstance(body, dict) and isinstance(body.get(name), list):
        return body[name]
    return []


def _error_code(body) -> str:
    """공통 오류 응답에서 errMsg 코드를 꺼낸다. 정상이면 빈 문자열."""
    if isinstance(body, dict):
        hdr = ((body.get("OpenAPI_ServiceResponse") or {})
               .get("cmmMsgHeader") or {})
        if hdr:
            return str(hdr.get("errMsg") or "").strip()
        # LH 고유 헤더 (dsSch/head 안의 결과코드)
        for seg in ("RESULT_CODE", "resultCode"):
            if seg in body:
                return ""
    if isinstance(body, list):
        for s in body:
            if isinstance(s, dict):
                hdr = ((s.get("OpenAPI_ServiceResponse") or {})
                       .get("cmmMsgHeader") or {})
                if hdr:
                    return str(hdr.get("errMsg") or "").strip()
    return ""


def _raise_for_api_error(body, key: str) -> None:
    code = _error_code(body)
    if not code:
        return
    if code == "NODATA_ERROR":
        return      # 데이터 없음은 오류가 아니다
    msg = AUTH_ERRORS.get(code)
    if code == "LIMITED_NUMBER_OF_SERVICE_REQUESTS_EXCEEDS_ERROR":
        raise LhQuotaError(f"{code} — {msg}")
    if msg:
        raise LhAuthError(f"{code} — {msg}")
    if code in RETRYABLE:
        raise httpx.HTTPError(f"{code} (재시도 대상)")
    raise RuntimeError(_scrub(code, key))


# 인증·권한 문제로 떨어지는 상태코드.
# 2026-10-30 실측: 더미 키를 보내면 **403 SERVICE_KEY_IS_NOT_REGISTERED_ERROR**,
# 키를 아예 안 보내면 401 SERVICE_KEY_IS_NULL 이 온다.
# 403 을 일반 오류로 두면 재시도만 세 번 하고 "왜 안 되는지" 안내를 잃는다.
AUTH_STATUS = (401, 403)
RETRY_STATUS = (408, 429, 500, 502, 503, 504)


def _request(client: httpx.Client, url: str, params: dict, key: str):
    """재시도·백오프를 입힌 GET. 키·권한 문제면 재시도하지 않고 바로 포기한다.

    상태코드로 먼저 가르지 않고 **본문의 errMsg 를 먼저 본다.** 같은 게이트웨이가
    상황에 따라 401/403/200 에 오류 헤더를 실어 보내기 때문에, 본문이 분류해 줄 수
    있으면 그게 가장 정확하다.
    """
    last: Optional[Exception] = None
    for attempt in range(MAX_RETRY):
        try:
            r = client.get(url, params=params)

            # 1) 본문에 오류 헤더가 있으면 상태코드와 무관하게 그걸로 분류한다
            body = None
            try:
                body = r.json()
            except Exception:  # noqa: BLE001 — JSON 이 아니면 아래에서 상태코드로 처리
                body = None
            if body is not None:
                _raise_for_api_error(body, key)   # LhAuthError/LhQuotaError 등

            # 2) 본문이 설명해 주지 않는 실패는 상태코드로 가른다
            if r.status_code in AUTH_STATUS:
                raise LhAuthError(
                    f"HTTP {r.status_code} — 인증키가 거부됐다. "
                    "일반 인증키(Decoding)인지, 이 서비스의 활용신청이 "
                    "승인됐는지 확인 "
                    "(목록 15058530 · 상세 15057999 · 공급정보 15056765)")
            if r.status_code in RETRY_STATUS:
                raise httpx.HTTPError(f"HTTP {r.status_code} (일시 오류)")
            r.raise_for_status()

            if body is None:
                raise RuntimeError("응답이 JSON 이 아니다")
            return body

        except (LhAuthError, LhQuotaError):
            raise                      # 재시도해도 소용없다
        except Exception as e:  # noqa: BLE001 — 네트워크·일시 오류는 재시도
            last = e
            if attempt < MAX_RETRY - 1:
                time.sleep(BACKOFF[attempt])
    raise RuntimeError(_scrub(f"{type(last).__name__}: {last}", key))


# ---------------------------------------------------------------- 유형 세분화
# AIS_TP_CD_NM / 공고명에서 실제 공급 유형을 집어낸다.
# 화면 필터(태그)와 자격 판정이 이 값을 본다.
DETAIL_TYPES = (
    "통합공공임대", "국민임대", "영구임대", "행복주택", "장기전세",
    "신혼희망타운", "매입임대", "전세임대", "든든전세", "분양주택",
    "공공분양", "기존주택", "청년", "신혼", "신생아", "고령자", "다자녀",
)


def classify(it: dict) -> str:
    """공고 유형 라벨 — 상위 유형 + 세부 유형을 합친다."""
    upp = UPP_TYPES.get(str(it.get("UPP_AIS_TP_CD") or ""), "")
    ais = str(it.get("AIS_TP_CD_NM") or it.get("UPP_AIS_TP_NM") or "").strip()
    name = str(it.get("PAN_NM") or "")
    hay = f"{ais} {name}"
    found = [t for t in DETAIL_TYPES if t in hay]
    parts = [p for p in ([upp] + ([ais] if ais and ais != upp else []) + found) if p]
    # 중복 제거(순서 유지)
    seen, out = set(), []
    for p in parts:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return " ".join(out)


def _detail_url(it: dict) -> str:
    """공식 공고문 링크. 상대경로면 절대경로로 만들고, 스킴을 검증한다."""
    url = str(it.get("DTL_URL") or "").strip()
    if not url:
        return ""
    if url.startswith("//"):
        url = "https:" + url
    elif url.startswith("/"):
        url = APPLY_HOST + url
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.netloc:
        return ""
    return url


def to_notice(it: dict) -> Notice:
    return Notice(
        source="LH",
        source_id="lh",
        uid=str(it.get("PAN_ID", "")),
        title=str(it.get("PAN_NM", "")).strip(),
        category=classify(it),
        region=to_sido(str(it.get("CNP_CD_NM", ""))),
        posted=parse_date(it.get("PAN_NT_ST_DT") or it.get("PAN_DT")),
        end=parse_date(it.get("CLSG_DT")),
        url=_detail_url(it),
        status=str(it.get("PAN_SS", "")).strip(),
        extra={
            "_ccr": str(it.get("CCR_CNNT_SYS_DS_CD") or ""),
            "_spl": str(it.get("SPL_INF_TP_CD") or ""),
            "_upp": str(it.get("UPP_AIS_TP_CD") or ""),
            "_ais": str(it.get("AIS_TP_CD") or ""),
            "upp_label": UPP_TYPES.get(str(it.get("UPP_AIS_TP_CD") or ""), ""),
            "ais_label": str(it.get("AIS_TP_CD_NM") or "").strip(),
        },
    )


def apply_detail(n: Notice, body) -> None:
    """상세 응답으로 접수시작·마감·발표·주소·세대수를 채운다."""
    scdl = _seg(body, "dsSplScdl")
    sbd = _seg(body, "dsSbd")
    begins = sorted(d for d in (parse_date(r.get("SBSC_ACP_ST_DT")) for r in scdl) if d)
    ends = sorted(d for d in (parse_date(r.get("SBSC_ACP_CLSG_DT")) for r in scdl) if d)
    awards = sorted(d for d in (parse_date(r.get("PZWR_ANC_DT")) for r in scdl) if d)
    if begins:
        n.begin = begins[0]
    if ends:
        n.end = ends[-1]
    if awards:
        n.announce = awards[0]
    if sbd:
        n.address = str(sbd[0].get("LGDN_ADR", "")).strip()
        hh = [str(r.get("HSH_CNT", "")).strip() for r in sbd
              if str(r.get("HSH_CNT", "")).strip()]
        if hh:
            try:
                n.households = f"{sum(int(h) for h in hh)}세대"
            except ValueError:
                n.households = hh[0]
    n.detail_loaded = True


def _fetch_detail(client: httpx.Client, key: str, n: Notice) -> None:
    params = {
        "serviceKey": key, "PAN_ID": n.uid,
        "CCR_CNNT_SYS_DS_CD": n.extra.get("_ccr") or "03",
        "SPL_INF_TP_CD": n.extra.get("_spl", ""),
        "UPP_AIS_TP_CD": n.extra.get("_upp") or "06",
        "AIS_TP_CD": n.extra.get("_ais", ""),
    }
    apply_detail(n, _request(client, DTL_URL, params, key))


def collect(today: date, upp_types: Optional[list[str]] = None,
            max_pages: int = 5, detail_limit: int = 30
            ) -> tuple[list[Notice], list[str]]:
    """LH 공고 목록 + (마감 전 공고만) 상세 보강.

    키가 없으면 PermissionError 를 던진다 — 호출한 쪽이 '키 없음'으로 기록한다.
    """
    key = registry.api_key("lh")
    if not key:
        raise LhAuthError(
            "LH_API_KEY 가 없다. 프로젝트 루트의 .env 에 "
            "LH_API_KEY=<일반 인증키(Decoding)> 를 넣거나 환경변수로 설정한다")

    upp_types = [u for u in (upp_types or DEFAULT_UPP) if u in UPP_TYPES]
    if not upp_types:
        upp_types = DEFAULT_UPP

    warnings: list[str] = []
    out: list[Notice] = []
    seen: set[str] = set()
    per_type: dict[str, int] = {}

    with safe_client() as client:
        for upp in upp_types:
            got = 0
            page = 1
            while page <= max_pages:
                params = {"serviceKey": key, "PG_SZ": PAGE_SIZE, "PAGE": page,
                          "UPP_AIS_TP_CD": upp}
                try:
                    body = _request(client, LIST_URL, params, key)
                except LhAuthError:
                    raise                      # 키 문제는 전체를 세우는 게 맞다
                except LhQuotaError as e:
                    warnings.append(f"LH 호출 한도: {e}")
                    return out, warnings       # 더 긁어도 소용없다
                except Exception as e:  # noqa: BLE001
                    warnings.append(
                        f"LH 목록 실패({UPP_TYPES[upp]} p{page}): "
                        f"{_scrub(str(e), key)}")
                    break

                rows = _seg(body, "dsList")
                for row in rows:
                    if not row.get("PAN_ID"):
                        continue
                    n = to_notice(row)
                    if n.uid in seen:
                        continue
                    seen.add(n.uid)
                    out.append(n)
                    got += 1

                total = int((rows[0].get("ALL_CNT") if rows else 0) or 0)
                if not rows or page * PAGE_SIZE >= total:
                    break
                page += 1
                time.sleep(POLITE_DELAY)
            per_type[UPP_TYPES[upp]] = got
            time.sleep(POLITE_DELAY)

        # 마감 전 공고만 상세 보강 (호출 수 제한)
        budget = detail_limit
        enriched = 0
        for n in out:
            if budget <= 0:
                break
            if n.end is not None and n.end < today:
                continue
            try:
                _fetch_detail(client, key, n)
                enriched += 1
            except LhAuthError:
                raise
            except LhQuotaError as e:
                warnings.append(f"LH 상세 호출 한도: {e}")
                break
            except Exception as e:  # noqa: BLE001
                warnings.append(f"LH 상세 실패 {n.uid}: {_scrub(str(e), key)}")
            budget -= 1
            time.sleep(POLITE_DELAY)

    # 수집 내역을 경고가 아닌 정보로 남긴다 (화면의 note 로 보인다)
    detail = " · ".join(f"{k} {v}건" for k, v in per_type.items() if v)
    if detail:
        warnings.append(f"[정보] LH 유형별 수집: {detail} / 상세보강 {enriched}건")
    missing_link = sum(1 for n in out if not n.url)
    if missing_link:
        warnings.append(f"[정보] LH 공고 중 원문 링크가 없는 건 {missing_link}건")
    return out, warnings
