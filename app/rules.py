"""소득·자산 기준 데이터.

출처: verisworks-ai/naejipgak-mcp (MIT)
  - src/data/rules/income-limits.json   → MONTHLY_INCOME_BASE
  - src/data/rules/special-supply.json  → SPECIAL_SUPPLY

⚠️ 원 저장소가 스스로 밝히듯 이 수치는 "OSS 샘플 룰 엔진 검증용 기본값"이며
공고별 실제 기준이 항상 우선한다. 그래서 HOME RADAR 는 이 값을
**제외 근거로 쓰지 않고** '확인 필요' 판정과 안내 문구에만 사용한다.
"""
from __future__ import annotations

from typing import Optional

# 도시근로자 가구원수별 월평균소득 100% (원)
MONTHLY_INCOME_BASE = {
    1: 4_180_000,
    2: 6_840_000,
    3: 8_980_000,
    4: 10_600_000,
    5: 11_200_000,
    6: 12_000_000,
    7: 12_800_000,   # 7인 이상
}

# 특별공급 유형별 소득 상한(도시근로자 월평균소득 대비 %)
SPECIAL_SUPPLY = {
    "신혼부부": {"pct": 140, "note": "혼인기간·자녀 요건은 공고별로 다르다"},
    "생애최초": {"pct": 160, "note": "소득세 납부 이력 요건이 붙는 경우가 많다"},
    "다자녀": {"pct": None, "note": "미성년 자녀 수와 거주지역 배점은 공고문 기준"},
    "노부모부양": {"pct": None, "note": "부양기간·세대구성 요건은 공고문 기준"},
}

# 공공분양·공공임대에서 흔히 쓰이는 총자산/자동차 상한 (만원)
# 연도별로 고시되며 공고마다 다르다 — 어디까지나 어림값이다.
ASSET_GUIDE = {
    "공공분양": {"assets": 36_200, "car": 3_900},
    "임대": {"assets": 36_200, "car": 3_900},
}


def income_base(household_size: int) -> int:
    """가구원수에 해당하는 도시근로자 월평균소득 100% (원)."""
    n = max(1, int(household_size or 1))
    return MONTHLY_INCOME_BASE.get(min(n, 7), MONTHLY_INCOME_BASE[7])


def income_ratio(monthly_income_manwon: Optional[int], household_size: int) -> Optional[float]:
    """내 월소득이 도시근로자 월평균소득의 몇 %인지. 소득 미입력이면 None."""
    if monthly_income_manwon in (None, ""):
        return None
    base = income_base(household_size)
    return round(int(monthly_income_manwon) * 10_000 / base * 100, 1)


def income_verdict(ratio: Optional[float], pct_limit: Optional[int]) -> str:
    """'여유' / '경계' / '초과' / '확인' — 제외가 아니라 신호등이다."""
    if ratio is None or pct_limit is None:
        return "확인"
    if ratio <= pct_limit * 0.9:
        return "여유"
    if ratio <= pct_limit:
        return "경계"
    return "초과"
