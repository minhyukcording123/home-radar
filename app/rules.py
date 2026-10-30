"""판정에 쓰는 규칙과 기준 데이터 — 출처·기준일·공식 여부를 함께 들고 다닌다.

가장 중요한 규칙:

    **official=False(샘플·미확정) 기준은 '불충족'(제외)을 만들 수 없다.**

공급 유형별 공식 소득·자산 기준(2026년도 적용)은 standards.py 에 있다 —
마이홈·보건복지부·LH 공식 페이지에서 확인한 값만 official=True 다.
공식 확인에 실패한 것(뉴:홈 유형별 기준 등)은 official=False 로 남아
'미확인'까지만 만들고 참고 신호로만 보인다. 공고별 실제 기준이 항상 우선한다.

규칙을 바꾸면 RULES_VERSION 을 올리고 tests 에 회귀 테스트를 추가한다.
(공식 기준 수치는 tests/test_standards.py, 나머지는 tests/test_core.py)
"""
from __future__ import annotations

from typing import Optional

from .standards import STANDARD_SOURCES, URBAN_WORKER_INCOME_2026

RULES_VERSION = "2026.10.30-3"

# ---------------------------------------------------------------- 출처 레지스트리
# label  : 화면에 보여줄 근거 이름
# url    : 확인할 수 있는 링크
# asof   : 기준일 (모르면 None)
# official: 공식 기준인가? False 면 판정 확정에 쓰지 않는다
RULE_SOURCES: dict[str, dict] = {
    "notice": {
        "label": "공고문",
        "url": "",
        "asof": None,
        "official": True,
        "note": "최종 판단은 항상 해당 공고문이 우선한다",
    },
    "supply-rule": {
        "label": "주택공급에 관한 규칙(일반 규정)",
        "url": "https://www.law.go.kr/법령/주택공급에관한규칙",
        "asof": None,
        "official": True,
        "note": "일반 규정이며 공고별 예외가 있을 수 있다",
    },
    "notice-title": {
        "label": "공고 제목·유형",
        "url": "",
        "asof": None,
        "official": True,
        "note": "공고가 스스로 밝힌 대상 계층·공급 유형",
    },
    "profile": {
        "label": "내가 입력한 조건",
        "url": "",
        "asof": None,
        "official": True,
        "note": "입력값이 틀리면 판정도 틀린다",
    },
    "sample-income": {
        "label": "도시근로자 월평균소득 (샘플 기준)",
        "url": "https://github.com/verisworks-ai/naejipgak-mcp",
        "asof": "2026-07-07",
        "official": False,
        "note": "naejipgak-mcp(MIT) 의 샘플 룰 값이다. "
                "원 저장소도 '검증용 기본값'이라고 밝히고 있어 적격 확정에 쓰지 않는다",
    },
    "sample-asset": {
        "label": "공공 유형 총자산·자동차 상한 (샘플 기준)",
        "url": "",
        "asof": None,
        "official": False,
        "note": "연도별 고시값으로 교체 필요. 지금은 참고 신호로만 쓴다",
    },
    "sample-special": {
        "label": "민영 특별공급 소득 상한 (샘플 기준)",
        "url": "",
        "asof": None,
        "official": False,
        "note": "민영주택 특별공급 소득 기준은 공고·물량 배정별로 달라 "
                "공식 확정값이 없다. 참고 신호로만 쓴다",
    },
}

# 공식 기준(standards.py)의 출처를 같은 레지스트리에 합친다 —
# checks.Check 가 source_of() 하나로 공식 여부를 판단할 수 있게.
RULE_SOURCES.update(STANDARD_SOURCES)


def source_of(key: str) -> dict:
    return RULE_SOURCES.get(key, {"label": key, "url": "", "asof": None,
                                  "official": False, "note": ""})


def is_official(key: str) -> bool:
    return bool(source_of(key).get("official"))


# ---------------------------------------------------------------- 소득 기저표 (공식)
# 도시근로자 가구원수별 월평균소득 100% — 완화 미반영 기저값 (원/월).
# standards.URBAN_WORKER_INCOME_2026 (마이홈, 2026년도 적용기준) 을 그대로 쓴다.
# 샘플값(naejipgak-mcp)을 쓰던 자리를 공식값으로 교체했다 — income_ratio 의
# 참고 신호도 이제 공식 기저로 계산된다.
MONTHLY_INCOME_BASE = dict(URBAN_WORKER_INCOME_2026)
INCOME_BASE_SOURCE = "myhome-urban-income"

# 민영주택 특별공급 소득 상한 참고치 (%) — 공식 확정값이 아니다.
# 공공 유형의 공식 특공 기준은 standards.py 에 있다. 이 표는 공식 기준이
# 없는 민영 특공의 '참고 신호'에만 쓴다 (source: sample-special → 항상 미확인).
SPECIAL_SUPPLY = {
    "신혼부부": {"pct": 140, "note": "혼인기간·자녀 여부에 따라 달라진다"},
    "생애최초": {"pct": 160, "note": "소득세 납부 이력 요건이 붙는 경우가 많다"},
    "다자녀": {"pct": None, "note": "미성년 자녀 수와 거주지역 배점은 공고문 기준"},
    "노부모부양": {"pct": None, "note": "부양기간·세대구성 요건은 공고문 기준"},
    "청년": {"pct": 140, "note": "본인 소득 기준이 따로 적용되는 경우가 많다"},
}

# 공공분양·공공임대에서 흔히 쓰이는 총자산/자동차 상한 (만원) — 샘플 기준
ASSET_GUIDE = {
    "공공분양": {"assets": 36_200, "car": 3_900},
    "공공임대": {"assets": 36_200, "car": 3_900},
    "임대": {"assets": 36_200, "car": 3_900},
}
ASSET_GUIDE_SOURCE = "sample-asset"

# ---------------------------------------------------------------- 구조적 규칙 (공식)
# 대상 계층이 정해진 공고 — 해당하지 않으면 구조적으로 지원이 안 된다
AGE_RULES = {
    "청년": (19, 39),
    "고령자": (65, None),
}

# 청약통장 가입기간·납입횟수 1순위 요건 (수도권 기준, 일반 규정)
ACCOUNT_FIRST_RANK = {
    "공공분양": {"years": 2, "payments": 24,
                 "note": "수도권 공공분양 1순위. 특별공급은 가입 6개월·6회"},
    "민영분양": {"years": 2, "payments": None,
                 "note": "수도권 민영 1순위는 가입 2년. 지역·면적별 예치금 기준도 있다"},
}


# ---------------------------------------------------------------- 계산 헬퍼
def income_base(household_size: int) -> int:
    """가구원수에 해당하는 도시근로자 월평균소득 100% (원)."""
    n = max(1, int(household_size or 1))
    return MONTHLY_INCOME_BASE.get(min(n, 7), MONTHLY_INCOME_BASE[7])


def recognized_monthly_income(prof: dict,
                              include_spouse: bool = False) -> Optional[int]:
    """참고용 월소득 합계(만원). `include_spouse` 면 배우자 소득도 더한다.

    배우자 소득을 더하는 건 기준의 소득 범위(scope)가 '본인+배우자'나 '세대'일
    때만이다 — 호출하는 쪽(eligibility)이 기준을 보고 결정한다.
    배우자 소득을 입력하지 않았으면 더하지 않는다 (없는 값을 0으로 보지 않는다).

    청약 소득 심사는 근로소득과 사업소득의 인정 방식이 다르고(사업소득은
    종합소득세 신고 기준, 근로소득은 세전 월평균 등) 가구원 합산 범위도
    공고별로 다르다. 그 공식 기준을 아직 구현하지 않았으므로,
    **합산해서 '소득 인정액'이라고 부르지 않는다.**

    대신 입력값을 그대로 돌려줘 참고 신호(ratio)만 만들고,
    판정에서는 '미확인'으로 둔다.
    """
    work = prof.get("income_work")
    biz = prof.get("income_business")
    own: Optional[int]
    if work in (None, "") and biz in (None, ""):
        # 구버전 프로필 호환
        legacy = prof.get("monthly_income")
        own = int(legacy) if legacy not in (None, "") else None
    else:
        own = int(work or 0) + int(biz or 0)

    if not include_spouse:
        return own

    sw, sb = prof.get("spouse_income_work"), prof.get("spouse_income_business")
    if sw in (None, "") and sb in (None, ""):
        return own          # 배우자 소득 미입력 — 합산할 수 없다
    spouse = int(sw or 0) + int(sb or 0)
    return spouse if own is None else own + spouse


def business_income_total(prof: dict, include_spouse: bool = False) -> int:
    """사업·기타소득 합계(만원). 0보다 크면 소득 판정을 확정하지 않는다."""
    total = int(prof.get("income_business") or 0)
    if include_spouse:
        total += int(prof.get("spouse_income_business") or 0)
    return total


def income_ratio(prof: dict) -> Optional[float]:
    """입력 월소득이 도시근로자 월평균소득의 몇 % 인지 (참고용)."""
    inc = recognized_monthly_income(prof)
    if inc is None:
        return None
    base = income_base(prof.get("household_size", 1))
    return round(inc * 10_000 / base * 100, 1)


def income_signal(ratio: Optional[float], pct_limit: Optional[int]) -> str:
    """'여유' / '경계' / '초과' / '확인' — 신호등이지 판정이 아니다."""
    if ratio is None or pct_limit is None:
        return "확인"
    if ratio <= pct_limit * 0.9:
        return "여유"
    if ratio <= pct_limit:
        return "경계"
    return "초과"


def net_assets(prof: dict) -> Optional[int]:
    """순자산(자산 - 부채, 만원). 자산 미입력이면 None."""
    assets = prof.get("assets")
    if assets in (None, ""):
        return None
    return int(assets) - int(prof.get("debts") or 0)


def fmt_won(manwon: Optional[int]) -> str:
    """만원 단위 숫자를 '3억 2,000만원' 형태로."""
    if manwon in (None, ""):
        return ""
    v = int(manwon)
    if v == 0:
        return "0원"
    eok, rest = divmod(abs(v), 10_000)
    sign = "-" if v < 0 else ""
    if eok and rest:
        return f"{sign}{eok}억 {rest:,}만원"
    if eok:
        return f"{sign}{eok}억원"
    return f"{sign}{rest:,}만원"
