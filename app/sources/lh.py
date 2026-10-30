"""LH 분양임대공고문 — 공공데이터포털 공식 오픈API.

cheongyak-auto(MIT) 의 `cheong/sources/lh.py` 를 httpx 기반으로 각색했다.
  목록: 15058530  lhLeaseNoticeInfo1      공고명·지역·유형·게시일·마감일·URL
  상세: 15057999  getLeaseNoticeDtlInfo1  접수시작·당첨발표일·주소·세대수

2026-10-30 확인: 키 없이 호출하면 401 SERVICE_KEY_IS_NULL.
그래서 키가 없으면 수집을 건너뛰고 '키 미설정'으로 상태를 남긴다.
같은 data.go.kr 키라도 서비스별로 활용신청을 각각 해야 한다.

UPP_AIS_TP_CD: 05 분양주택 · 06 임대주택 · 13 주거복지(매입·전세임대·든든전세) · 39 신혼희망타운
기본은 06·13 (분양·신혼희망타운은 청약홈이 이미 커버한다).
"""
from __future__ import annotations

from datetime import date
from typing import Optional

import httpx

from ..models import Notice
from ..util import new_client, parse_date, to_sido
from . import registry

LIST_URL = "https://apis.data.go.kr/B552555/lhLeaseNoticeInfo1/lhLeaseNoticeInfo1"
DTL_URL = "https://apis.data.go.kr/B552555/lhLeaseNoticeDtlInfo1/getLeaseNoticeDtlInfo1"

DEFAULT_UPP = ["06", "13"]


def _seg(body, name: str) -> list:
    """LH 응답은 [{head:...}, {dsList:[...]}] 꼴이라 세그먼트를 이름으로 찾는다."""
    if isinstance(body, list):
        for s in body:
            if isinstance(s, dict) and isinstance(s.get(name), list):
                return s[name]
    if isinstance(body, dict) and isinstance(body.get(name), list):
        return body[name]
    return []


def _auth_error(body) -> str:
    """키 문제면 메시지를 돌려준다."""
    if isinstance(body, dict):
        hdr = (body.get("OpenAPI_ServiceResponse") or {}).get("cmmMsgHeader") or {}
        if hdr:
            return f"{hdr.get('errMsg', '')} / {hdr.get('returnAuthMsg', '')}".strip(" /")
    return ""


def to_notice(it: dict) -> Notice:
    return Notice(
        source="LH",
        source_id="lh",
        uid=str(it.get("PAN_ID", "")),
        title=str(it.get("PAN_NM", "")).strip(),
        category=str(it.get("AIS_TP_CD_NM") or it.get("UPP_AIS_TP_NM") or "").strip(),
        region=to_sido(str(it.get("CNP_CD_NM", ""))),
        posted=parse_date(it.get("PAN_NT_ST_DT") or it.get("PAN_DT")),
        end=parse_date(it.get("CLSG_DT")),
        url=str(it.get("DTL_URL", "")),
        status=str(it.get("PAN_SS", "")),
        extra={
            "_ccr": str(it.get("CCR_CNNT_SYS_DS_CD") or ""),
            "_spl": str(it.get("SPL_INF_TP_CD") or ""),
            "_upp": str(it.get("UPP_AIS_TP_CD") or ""),
            "_ais": str(it.get("AIS_TP_CD") or ""),
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
    r = client.get(DTL_URL, params=params)
    r.raise_for_status()
    apply_detail(n, r.json())


def collect(today: date, upp_types: Optional[list[str]] = None,
            max_pages: int = 5, detail_limit: int = 30
            ) -> tuple[list[Notice], list[str]]:
    """LH 공고 목록 + (마감 전 공고만) 상세 보강."""
    key = registry.api_key("lh")
    if not key:
        raise PermissionError(
            "LH_API_KEY 환경변수가 없다. "
            "https://www.data.go.kr/data/15058530/openapi.do 활용신청 후 "
            "일반 인증키(Decoding)를 넣어야 한다")

    upp_types = upp_types or DEFAULT_UPP
    warnings: list[str] = []
    out: list[Notice] = []
    seen: set[str] = set()

    with new_client() as client:
        for upp in upp_types:
            page = 1
            while page <= max_pages:
                try:
                    r = client.get(LIST_URL, params={
                        "serviceKey": key, "PG_SZ": 100, "PAGE": page,
                        "UPP_AIS_TP_CD": upp})
                    body = r.json()
                except Exception as e:  # noqa: BLE001
                    warnings.append(f"LH 목록 실패(유형 {upp} p{page}): {e}")
                    break

                auth = _auth_error(body)
                if auth:
                    raise PermissionError(f"LH API 인증 실패: {auth}")

                rows = _seg(body, "dsList")
                for row in rows:
                    if not row.get("PAN_ID"):
                        continue
                    n = to_notice(row)
                    if n.uid in seen:
                        continue
                    seen.add(n.uid)
                    out.append(n)

                total = int((rows[0].get("ALL_CNT") if rows else 0) or 0)
                if not rows or page * 100 >= total:
                    break
                page += 1

        # 마감 전 공고만 상세 보강 (호출 수 제한)
        budget = detail_limit
        for n in out:
            if budget <= 0:
                break
            if n.end is not None and n.end < today:
                continue
            try:
                _fetch_detail(client, key, n)
            except Exception as e:  # noqa: BLE001
                warnings.append(f"LH 상세 실패 {n.uid}: {e}")
            budget -= 1

    return out, warnings
