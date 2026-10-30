"""LH 분양임대공고별 공급정보 — 공공데이터포털 공식 오픈API.

  공급정보: 15056765  lhLeaseNoticeSplInfo1/getLeaseNoticeSplInfo1
            특정 공고의 주택형/필지/상가호 목록 (면적·세대수·금액)

**요청 파라미터** (포털 문서 2026-10-30 확인, openapi.do?format=md 로 원문 조회)
  ServiceKey(필) · SPL_INF_TP_CD(필) · CCR_CNNT_SYS_DS_CD(필) ·
  PAN_ID(필) · UPP_AIS_TP_CD(필) · AIS_TP_CD(옵)
  → 전부 목록 API(lhLeaseNoticeInfo1) 응답의 같은 이름 필드를 그대로 되돌려
    보내는 구조다. lh.to_notice() 가 extra["_ccr"/"_spl"/"_upp"/"_ais"] 에
    담아 둔 값을 여기서 꺼내 쓴다 (_fetch_detail 과 같은 연결 방식).

**응답 필드** — 포털 문서의 출력결과 표는 토지 변형만 싣고 있다:
  SS_CODE · RS_DTTM · LND_US_DS_CD_NM(공급용도) · LGDN_DTL_ADR(소재지) ·
  LNO(지번) · AR(면적) · SPL_XPC_AMT(예정가격)
주택 변형은 문서에 없어서 **2026-10-30 실제 호출로 확인**했다
(유형별 필드명은 아래 _TEXT_CANDIDATES / _NUM_CANDIDATES 주석에 적어 뒀다).
확인한 사실:
  - 세그먼트: dsList01~dsList04 (dsList01Nm 같은 *Nm 은 컬럼 라벨 행이라 제외)
  - **금액 단위는 원**이다 (분양 SIL_AMT 실측 407,471,000 = 4.07억).
    Notice.price_min/max 는 만원 단위이므로 여기 값을 그쪽에 바로 넣으면 안 된다.
  - 임대·신혼희망타운은 보증금(LS_GMY)·월임대료(RFE)가 **"공고문 참조" 문자열**로
    오는 경우가 많다 → 0 으로 채우지 않고 *_raw 로 원문을 보존한다.
    금액이 0 으로 보이면 '공짜'로 오독된다.
  - 매입임대(13)는 주택형·세대수 대신 **호별 주소(ADR/DNG_HS_ADR)와
    층(VLD_VL_NM)** 으로 온다. 유형마다 모양이 전혀 달라 후보 목록으로 흡수한다.
  - 공급정보가 아예 없는 공고도 정상적으로 있다 → 오류가 아니라 empty=True.

재시도·백오프·오류분류(LhAuthError/LhQuotaError)·키 스크러빙은 전부
lh._request() 를 재사용한다 — 같은 게이트웨이(B552555)라 오류 응답 형식이 같다.
"""
from __future__ import annotations

import time
from datetime import date
from typing import Optional

import httpx

from .. import store
from ..models import Area, Notice
from ..util import new_client, today_kst
from . import lh, registry

SPL_URL = ("https://apis.data.go.kr/B552555/lhLeaseNoticeSplInfo1/"
           "getLeaseNoticeSplInfo1")

# 캐시 스키마 — 정규화 형식을 바꾸면 올려서 구버전 캐시를 자연히 무효화한다
SCHEMA = 2
CACHE_PREFIX = "LH공급:"

# 정규화 결과의 항목 순서 (available/missing 계산에 쓴다)
FIELDS = ("complex", "house_type", "exclusive_area", "supply_area",
          "units", "total_units", "deposit", "rent", "price",
          "address", "floor")

# 응답 필드 후보 — 유형그룹마다 필드명이 달라서 후보 목록으로 흡수한다. 앞쪽일수록 우선.
#
# 2026-10-30 실제 응답으로 확인한 변형 (키 발급 후 라이브 검증):
#   임대·신혼희망타운 : HTY_NNA · DDO_AR · SPL_AR · NOW_HSH_CNT · HSH_CNT
#                      · LS_GMY · RFE · SBD_LGO_NM(단지명)
#   분양             : HTY_NM · RSDN_DDO_AR · SPL_AR · SIL_HSH_CNT
#                      · TOT_HSH_CNT · SIL_AMT(분양가) · BZDT_NM(사업지구명)
#   매입임대(일반)    : ADR(주소) · DDO_AR · HO_NO · SBD_LGO_NM · VLD_VL_NM(층)
#   매입임대(다자녀)  : DNG_HS_ADR(주소) · LTR_SPL_RMNO · QUP_CNT · SGG_NM(시군구)
#   토지             : LND_US_DS_CD_NM · LGDN_DTL_ADR · AR · SPL_XPC_AMT
#
# QUP_CNT·LTR_SPL_RMNO·HO_NO 는 의미를 확정하지 못해 매핑하지 않았다 —
# 추측해서 세대수로 쓰면 틀린 숫자를 사실처럼 보여주게 된다.
_TEXT_CANDIDATES = {
    # 단지명 — 임대는 SBD_LGO_NM, 분양은 BZDT_NM(사업지구명).
    # 매입임대(다자녀)는 단지명이 없어 시군구로 대체한다.
    "complex": ("SBD_LGO_NM", "BZDT_NM", "SGG_NM", "LGDN_DTL_ADR"),
    "house_type": ("HTY_NM", "HTY_NNA", "LND_US_DS_CD_NM"),
    # 매입임대는 주택형 대신 호별 주소·층으로 온다
    "address": ("ADR", "DNG_HS_ADR"),
    "floor": ("VLD_VL_NM",),
}
_NUM_CANDIDATES = {
    "exclusive_area": ("RSDN_DDO_AR", "DDO_AR", "AR"),   # ㎡
    "supply_area": ("SPL_AR",),                          # ㎡
    "units": ("SIL_HSH_CNT", "NOW_HSH_CNT"),             # 이번 공급 세대수
    "total_units": ("TOT_HSH_CNT", "HSH_CNT"),           # 단지 총 세대수
    "deposit": ("LS_GMY",),                              # 임대보증금(원)
    "rent": ("MM_RFE", "RFE"),                           # 월임대료(원)
    "price": ("SIL_AMT", "SPL_XPC_AMT"),                 # 분양가/예정가격(원)
}


def _blank_result(error: str = "") -> dict:
    return {"rows": [], "available": [], "missing": list(FIELDS),
            "empty": not error, "error": error}


def _pick(row: dict, names: tuple[str, ...]) -> str:
    for nm in names:
        v = row.get(nm)
        if v not in (None, ""):
            s = str(v).strip()
            if s:
                return s
    return ""


def _num(text: str) -> Optional[float]:
    """'353,694,000' → 353694000. 숫자가 아니면(예: '공고문 참조') None."""
    s = text.replace(",", "").replace(" ", "")
    try:
        f = float(s)
    except ValueError:
        return None
    return f


def _rows(body) -> list[dict]:
    """dsList / dsList01~04 세그먼트를 전부 합친다.

    응답 최상위가 [{dsSch:...}, {dsList01:[...], dsList01Nm:[...], ...}] 꼴이라
    모든 객체를 훑으며 'dslist' 로 시작하는 배열을 모은다. *Nm 은 컬럼 라벨
    행(값이 '주택형' 같은 표제)이라 데이터로 섞이면 안 돼서 제외한다.
    """
    objs = body if isinstance(body, list) else [body]
    out: list[dict] = []
    for o in objs:
        if not isinstance(o, dict):
            continue
        for k, v in o.items():
            kl = str(k).lower()
            if kl.startswith("dslist") and not kl.endswith("nm") \
                    and isinstance(v, list):
                out.extend(r for r in v if isinstance(r, dict))
    return out


def normalize(body) -> dict:
    """공급정보 응답 → 화면/판정이 쓰기 좋은 정규화 dict.

    - 금액·면적·세대수는 숫자로. 파싱이 안 되는 값("공고문 참조" 등)은
      버리지 않고 `<키>_raw` 로 원문을 보존한다 — 사용자에게 그대로 보여줄 수
      있고, 0 으로 채워서 '공짜'로 오독되는 사고를 막는다.
    - available/missing: 응답에 실제 값이 있는 항목만 available 에 둔다.
      LH 는 유형마다 제공 항목이 달라서(분양가는 분양 유형만 등) 호출한 쪽이
      '없는 값'과 '빈 값'을 구분할 수 있어야 한다.
    """
    rows_out: list[dict] = []
    present: set[str] = set()

    for r in _rows(body):
        row: dict = {}
        for key, names in _TEXT_CANDIDATES.items():
            raw = _pick(r, names)
            if raw:
                row[key] = raw
                present.add(key)
        for key, names in _NUM_CANDIDATES.items():
            raw = _pick(r, names)
            if not raw:
                continue
            n = _num(raw)
            if n is None:
                row[f"{key}_raw"] = raw      # 원문 보존 — 0 으로 채우지 않는다
            else:
                # 세대수·금액은 정수가 자연스럽고, 면적은 소수점을 살린다
                row[key] = int(n) if float(n).is_integer() and \
                    key not in ("exclusive_area", "supply_area") else n
            present.add(key)
        if row:                              # 전부 null 인 행([{}])은 버린다
            rows_out.append(row)

    return {
        "rows": rows_out,
        "available": [f for f in FIELDS if f in present],
        "missing": [f for f in FIELDS if f not in present],
        "empty": not rows_out,
        "error": "",
    }


def fetch_supply(pan_id, *, ccr: str = "", spl: str = "", upp: str = "",
                 ais: str = "", client: Optional[httpx.Client] = None,
                 use_cache: bool = True) -> dict:
    """공고 하나의 공급정보(주택형/필지 목록)를 가져와 정규화한다.

    ccr/spl/upp/ais 는 목록 API 응답의 재조회 코드 — Notice.extra 의
    _ccr/_spl/_upp/_ais 를 그대로 넘긴다. 폴백(ccr=03, upp=06)은
    lh._fetch_detail() 과 같은 값을 쓴다.

    키가 없으면 LhAuthError. 한도 초과면 LhQuotaError (호출한 쪽이 배치를
    세울 수 있게 삼키지 않는다). 그 외 실패는 error 메시지를 담은 결과로
    돌려준다 — 공급정보는 보강 데이터라 공고 수집 전체를 세우면 안 된다.
    """
    pan_id = str(pan_id or "").strip()
    if not pan_id:
        return _blank_result(error="PAN_ID 가 비어 있다")

    cache_key = CACHE_PREFIX + pan_id
    if use_cache:
        cached = store.load_detail(cache_key)
        if cached and cached.get("schema") == SCHEMA \
                and isinstance(cached.get("result"), dict):
            return cached["result"]

    key = registry.api_key("lh")
    if not key:
        raise lh.LhAuthError(
            "LH_API_KEY 가 없다. 프로젝트 루트의 .env 에 "
            "LH_API_KEY=<일반 인증키(Decoding)> 를 넣거나 환경변수로 설정한다")

    if not spl:
        # SPL_INF_TP_CD 는 문서상 필수 — 없이 부르면 호출량만 날린다
        return _blank_result(
            error="SPL_INF_TP_CD 가 없다 — 목록 응답의 공급정보구분코드가 필요")

    params = {
        "serviceKey": key, "PAN_ID": pan_id,
        "SPL_INF_TP_CD": spl,
        "CCR_CNNT_SYS_DS_CD": ccr or "03",
        "UPP_AIS_TP_CD": upp or "06",
        "AIS_TP_CD": ais or "",
    }

    own = client is None
    cl = client or new_client()
    try:
        body = lh._request(cl, SPL_URL, params, key)
    except (lh.LhAuthError, lh.LhQuotaError):
        raise
    except Exception as e:  # noqa: BLE001 — 보강 실패는 공고 자체를 막지 않는다
        return _blank_result(error=lh._scrub(f"{type(e).__name__}: {e}", key))
    finally:
        if own:
            cl.close()

    result = normalize(body)
    # 빈 결과도 캐시한다 — 공급정보가 없는 공고는 다시 불러도 없다.
    # 오류 결과는 캐시하지 않는다 — 일시 장애가 눌러앉으면 안 된다.
    store.save_detail(cache_key, {"schema": SCHEMA, "result": result})
    return result


def apply_supply(n: Notice, result: dict) -> None:
    """정규화된 공급정보를 Notice 에 붙인다.

    - extra["supply"] 에 전체를 둔다 (available/missing 포함 — 화면이
      '제공 안 됨'과 '0' 을 구분해 보여줄 수 있게).
    - areas 는 상세 API 등 다른 경로가 먼저 채웠을 수 있어 비어 있을 때만 쓴다.
    """
    n.extra["supply"] = result
    if n.areas or not result.get("rows"):
        return
    for r in result["rows"]:
        excl = r.get("exclusive_area", r.get("exclusive_area_raw", ""))
        units = r.get("units", r.get("units_raw", ""))
        n.areas.append(Area(
            name=str(r.get("house_type", "")),
            exclusive="" if excl == "" else str(excl),
            units="" if units == "" else str(units),
        ))


def enrich_supply(notices: list[Notice], *, limit: int = 20,
                  client: Optional[httpx.Client] = None) -> list[str]:
    """LH 공고들에 공급정보를 배치로 보강한다. 돌려주는 값은 경고 목록.

    - 접수가 끝나지 않은 공고만, 마감이 가까운 순으로 (마감 미상은 뒤로)
    - 캐시 히트는 호출량(limit)을 쓰지 않는다 — limit 은 실제 API 호출 수 제한
    - 한도 초과(LhQuotaError)·키 문제(LhAuthError)면 즉시 중단하고 경고만 남긴다
      (공급정보는 보강이라 수집 전체를 실패로 만들지 않는다)
    """
    warnings: list[str] = []
    today = today_kst()

    cands = [n for n in notices
             if n.source_id == "lh" and not (n.end and n.end < today)]
    # 마감 가까운 순 — 사용자가 지금 판단해야 하는 공고에 호출량을 먼저 쓴다
    cands.sort(key=lambda n: (n.end is None, n.end or date.max))

    own = client is None
    cl = client or new_client()
    budget = max(0, int(limit))
    fetched = hits = empties = no_spl = 0
    errors = 0

    try:
        for n in cands:
            cached = store.load_detail(CACHE_PREFIX + n.uid)
            hit = bool(cached and cached.get("schema") == SCHEMA
                       and isinstance(cached.get("result"), dict))
            if not hit and not n.extra.get("_spl"):
                no_spl += 1     # 필수 코드가 없으면 호출해도 실패 — 호출량 아낌
                continue
            if not hit and budget <= 0:
                continue        # 이후에도 캐시된 공고가 있을 수 있어 break 안 함
            try:
                result = fetch_supply(
                    n.uid,
                    ccr=n.extra.get("_ccr", ""), spl=n.extra.get("_spl", ""),
                    upp=n.extra.get("_upp", ""), ais=n.extra.get("_ais", ""),
                    client=cl)
            except lh.LhQuotaError as e:
                warnings.append(f"LH 공급정보 호출 한도: {e}")
                break
            except lh.LhAuthError as e:
                warnings.append(f"LH 공급정보 건너뜀(키 문제): {e}")
                break
            if hit:
                hits += 1
            else:
                fetched += 1
                budget -= 1
                time.sleep(lh.POLITE_DELAY)
            if result.get("error"):
                errors += 1
                if errors <= 3:     # 같은 장애가 공고 수만큼 반복되는 걸 막는다
                    warnings.append(f"LH 공급정보 실패 {n.uid}: {result['error']}")
                continue
            if result.get("empty"):
                empties += 1
            apply_supply(n, result)
    finally:
        if own:
            cl.close()

    if errors > 3:
        warnings.append(f"LH 공급정보 실패 {errors - 3}건 추가 (경고 생략)")
    if fetched or hits:
        warnings.append(
            f"[정보] LH 공급정보 보강: 신규 {fetched}건 · 캐시 {hits}건"
            + (f" · 공급정보 없음 {empties}건" if empties else "")
            + (f" · 재조회코드 없어 건너뜀 {no_spl}건" if no_spl else ""))
    return warnings
