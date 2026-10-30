"""공고 × 내 조건 → 조건별 판정 → 추천 / 확인 필요 / 제외.

jisangfolio/cheongyak-auto(MIT) 의 판정 설계에서 출발했고, 조건을 하나씩 따로
판정하는 구조(checks.py)로 바꿨다. 원본의 원칙은 그대로 둔다.

    구조적으로 확실히 안 되는 것만 제외하고, 애매하면 '확인'으로 살린다.
    (잘못 걸러서 놓치는 손해 > 알림 하나 더 받는 손해)

여기에 '적격을 함부로 확정하지 않는다'를 더했다. 그래서 '충족'으로 인정하는 건
**어떤 공고 기준이든 확실히 통과하는 임계값**일 때만이다. 예를 들어 거주기간은
공고마다 6개월~2년으로 다르지만, 24개월을 넘겼으면 어느 기준이든 통과한다.
그보다 짧거나 입력이 없으면 '미확인'이다.

그 결과:
  - 민영분양·무순위·오피스텔·민간임대 → 조건이 다 차면 '추천'까지 간다
  - 국민임대·행복주택·통합공공임대·신혼희망타운 등 → standards.py 의
    2026년 공식 기준으로 소득·자산을 실제로 충족/불충족 판정한다.
    단 공고문이 항상 우선하므로 todo 에 그 뜻을 남긴다.
  - 공공분양(뉴:홈)·민영 특공 → 공식 기준을 확정하지 못해(official=False)
    항상 '확인 필요'에 머문다 — 의도된 동작이다.
  - 사업소득이 있으면 인정 방식(종합소득세 신고 기준 등)이 달라
    합산값으로 적격을 확정하지 않는다 → 소득은 '미확인'.
"""
from __future__ import annotations

import re
from datetime import date
from typing import Optional

from . import profile as prof_mod
from . import standards
from .checks import (ELIGIBILITY, FILTER, INFO, LEVEL_IGNORE, MET, NOTE,
                     SIGNAL, UNKNOWN, UNMET, Check, Verdict, filter_block,
                     missing)
from .models import Notice
from .rules import (ACCOUNT_FIRST_RANK, AGE_RULES, ASSET_GUIDE,
                    ASSET_GUIDE_SOURCE, RULE_SOURCES, SPECIAL_SUPPLY,
                    business_income_total, fmt_won, income_base, income_ratio,
                    income_signal, net_assets, recognized_monthly_income)

# 모집이 아닌 후속 안내(결과·서류·계약)는 목록에서 뺀다
NOISE = re.compile(r"당첨자|서류심사|서류제출|계약\s*(체결|안내|일정)|결과\s*(발표|안내|공고)"
                   r"|순번|합격자|명단|동호수|정산|사전점검|입주\s*안내")
RECRUIT = re.compile(r"모집|청약\s*접수|접수\s*안내|입주자\s*선정|공급")

# ---------------------------------------------------------------- 유형 / 태그
# 순서가 중요하다. '토지임대부 공공분양'처럼 이름에 '임대'가 들어가도 분양인 공고가 있고,
# 거꾸로 '국민임대'는 '국민'이 들어가도 임대다.
RENTAL_TYPES = ("국민임대", "통합공공임대", "영구임대", "행복주택", "장기전세",
                "매입임대", "전세임대", "든든전세", "청년안심", "재개발임대",
                "사회주택", "희망하우징", "공공기숙사", "기숙사", "공공임대")
PUBLIC_SALE_TYPES = ("공공분양", "신혼희망타운", "신혼희망", "뉴홈", "뉴:홈",
                     "토지임대부", "지분적립", "국민주택")
UNRANKED_TYPES = ("무순위", "잔여", "임의공급", "선착순")

# 화면 필터용 태그 — 공고가 스스로 밝힌 대상 계층·특성
TAG_WORDS = {
    "청년": ("청년",),
    "신혼부부": ("신혼", "예비신혼", "미리내집"),
    "신생아": ("신생아",),
    "다자녀": ("다자녀", "다둥이"),
    "고령자": ("고령자", "어르신", "노인", "실버", "은빛"),
    "장애인": ("장애인",),
    "생애최초": ("생애최초",),
    "행복주택": ("행복주택",),
    "국민임대": ("국민임대",),
    "통합공공임대": ("통합공공임대",),
    "장기전세": ("장기전세",),
    "매입임대": ("매입임대",),
    "전세임대": ("전세임대", "든든전세"),
    "신혼희망타운": ("신혼희망타운", "신혼희망"),
    "사회주택": ("사회주택",),
    # 아래 태그가 없으면 standards.py 의 해당 공식 기준이 실공고에서 안 걸린다
    "영구임대": ("영구임대",),
    "청년안심": ("청년안심", "역세권청년"),
    # 뉴:홈 유형 — 자산 체계가 서로 달라(순자산 vs 부동산) 구분이 필요하다.
    # '일반형'은 '일반공급'과 혼동될 위험이 커서 태그로 두지 않는다 (오탐 방지).
    "나눔형": ("나눔형", "이익공유형"),
    "선택형": ("선택형", "6년 분양전환"),
    "무순위": UNRANKED_TYPES,
    "노부모": ("노부모",),
    "특별공급": ("특별공급",),
    "1순위": ("1순위",),
    "2순위": ("2순위",),
}

# 청약통장이 지원에 쓰이는가
ACCOUNT_FREE = UNRANKED_TYPES + ("오피스텔", "도시형", "민간임대", "매입임대",
                                 "전세임대", "든든전세", "청년안심", "사회주택")
ACCOUNT_USE = ("특별공급", "1순위", "2순위", "일반공급", "공공분양",
               "신혼희망타운", "뉴홈", "뉴:홈", "분양전환", "토지임대부", "지분적립")
ACCOUNT_KEEP = ("행복주택", "국민임대", "통합공공임대", "영구임대", "장기전세")
ACCOUNT_LABEL = {
    "불필요": "청약통장 필요 없음",
    "가입만": "가입 사실만 확인 — 당첨돼도 통장 유지",
    "사용": "당첨되면 통장 효력 소멸",
    "확인": "공고문에서 확인",
}
ACCOUNT_MODES = {"불필요만": ("사용", "가입만"), "가입만까지": ("사용",), "상관없음": ()}

# 소득·자산 요건이 실제로 붙는 유형. 공식 기준(standards.py)이 있으면
# 실제 판정, 없으면 '미확인'에 머문다.
INCOME_SENSITIVE = ("공공분양", "공공임대")

# ---------------------------------------------------------------- 안전 임계값
#
# 여기 있는 값은 **그 유형의 공식 기준이 아니라**, "어떤 공고 기준이든 통과한다"고
# 말할 수 있는 선이다. 그래서 쓸 때 두 가지를 지킨다.
#
#   1) 근거가 규정 수준으로 **상한이 정해진 항목**에만 쓴다.
#      다른 유형들의 값을 평균내거나 일반화해서 만들면 안 된다 —
#      실제로 자동차가액에서 그렇게 했다가 '무차량' 반례에 걸렸다.
#   2) 상한을 확인하지 못했으면 출처를 공식으로 표기하지 않는다
#      (checks.Check 가 official=False 를 자동으로 '미확인'으로 내린다).
ACCOUNT_SAFE_MONTHS = 24       # 수도권 1순위 가입기간 (ACCOUNT_FIRST_RANK 근거)
ACCOUNT_SAFE_PAYMENTS = 24     # 수도권 공공분양 1순위 납입횟수 (동일)
RESIDENCE_SAFE_MONTHS = 24     # 해당지역 거주기간 — 상한을 공식 확인하지 못했다

# 자동차가액 — **통과 기준이 아니라 참고용 통상값**이다.
# 확인된 15개 유형이 4,542만원이지만 대학생 행복주택은 '무차량'이다.
# 기준을 모르는 유형에 이 값을 적용해 충족을 주면 안 된다.
CAR_REFERENCE_MANWON = 4_542


def _has(text: str, *words: str) -> bool:
    return any(w in text for w in words)


def kind_of(n: Notice) -> str:
    """공급 유형 — 화면 필터와 소득 요건 판단의 기준."""
    t = n.text
    if _has(t, "오피스텔", "도시형"):
        return "오피스텔"
    if _has(t, "공공지원민간임대", "민간임대"):
        return "민간임대"
    if _has(t, *UNRANKED_TYPES):
        return "무순위"
    if _has(t, *RENTAL_TYPES):
        return "공공임대"
    if _has(t, *PUBLIC_SALE_TYPES):
        return "공공분양"
    if _has(t, "임대"):
        return "공공임대"
    return "민영분양"


def tags_of(n: Notice) -> list[str]:
    t = n.text
    return [tag for tag, words in TAG_WORDS.items() if _has(t, *words)]


def account_need(n: Notice, kind: str) -> str:
    t = n.text
    if _has(t, *ACCOUNT_FREE) or kind in ("오피스텔", "민간임대", "무순위"):
        return "불필요"
    if _has(t, *ACCOUNT_KEEP):
        return "가입만"
    if _has(t, *ACCOUNT_USE) or kind in ("공공분양", "민영분양"):
        return "사용"
    return "확인"


# ---------------------------------------------------------------- 개별 조건
def _check_region(n: Notice, prof: dict, v: Verdict) -> None:
    regions = prof.get("regions") or []
    if regions and n.region and n.region not in regions:
        v.add(filter_block("관심 지역", f"{n.region} — 내가 고른 지역이 아니다"))


def _check_kind(n: Notice, prof: dict, v: Verdict) -> None:
    if not (prof.get("want") or {}).get(v.kind, True):
        v.add(filter_block("관심 유형", f"{v.kind} — 내가 끈 유형이다"))


def _check_price(n: Notice, prof: dict, v: Verdict) -> None:
    """희망 최대 분양가. 설정하지 않았으면 검사하지 않는다."""
    cap = prof.get("max_price")
    if cap in (None, ""):
        return
    cap = int(cap)
    lo = n.price_min
    if lo is None:
        v.add(Check("희망 분양가", UNKNOWN,
                    f"공고의 공급금액을 확인하지 못했다 (내 상한 {fmt_won(cap)})",
                    source_key="notice", category=FILTER,
                    todo="공고문에서 공급금액 확인"))
    elif lo > cap:
        v.add(Check("희망 분양가", UNMET,
                    f"최저 공급금액 {fmt_won(lo)} > 내 상한 {fmt_won(cap)}",
                    source_key="profile", category=FILTER))
    else:
        v.add(Check("희망 분양가", MET,
                    f"최저 공급금액 {fmt_won(lo)} ≤ 내 상한 {fmt_won(cap)}",
                    source_key="notice", category=FILTER))


def _check_age(n: Notice, prof: dict, v: Verdict, today: date) -> None:
    """공고가 연령을 제한하는 경우만 검사한다."""
    limited = [tag for tag in v.tags if tag in AGE_RULES]
    if not limited:
        return
    a = prof_mod.age(prof, today)
    if a is None:
        v.add(missing("연령", "생년월일"))
        return
    for tag in limited:
        lo, hi = AGE_RULES[tag]
        ok = a >= lo and (hi is None or a <= hi)
        rng = f"만 {lo}세 이상" if hi is None else f"만 {lo}~{hi}세"
        v.add(Check("연령", MET if ok else UNMET,
                    f"{tag} 대상({rng}) · 현재 만 {a}세",
                    source_key="notice-title"))


def _check_target_group(n: Notice, prof: dict, v: Verdict, today: date) -> None:
    """대상 계층이 정해진 공고 — 해당하지 않으면 구조적으로 지원이 안 된다."""
    t = n.text

    if "신혼부부" in v.tags or "신생아" in v.tags:
        if prof_mod.is_newlywed(prof, today):
            y = prof_mod.marriage_years(prof, today)
            detail = "신혼·신생아 대상에 해당"
            if y is not None:
                detail += f" (혼인 {y}년)"
            v.add(Check("대상 계층", MET, detail, source_key="notice-title"))
        else:
            v.add(Check("대상 계층", UNMET,
                        f"신혼·예비신혼·신생아·한부모 대상 공고 — 현재 {prof.get('marital')}",
                        source_key="notice-title"))

    if "다자녀" in v.tags:
        kids = int(prof.get("children") or 0)
        v.add(Check("대상 계층", MET if kids >= 2 else UNMET,
                    f"다자녀 대상(미성년 2명 이상) · 현재 {kids}명",
                    source_key="notice-title"))

    if "장애인" in v.tags:
        v.add(Check("대상 계층", MET if prof.get("disabled") else UNMET,
                    "장애인 대상 공고", source_key="notice-title"))

    # 영구임대는 수급자 전용이 아니다. 1순위가 생계·의료급여 수급자·한부모 등이지만
    # **2순위는 도시근로자 소득 50%(1인 70%·2인 60%)** 로 수급자가 아니어도 된다.
    # 그래서 수급자가 아니라고 제외하면 과잉 제외가 된다 — 미확인으로 둔다.
    if "영구임대" in v.tags:
        if prof.get("low_income"):
            v.add(Check("대상 계층", MET,
                        "영구임대 1순위(수급자·차상위) 대상에 해당",
                        source_key="notice-title"))
        else:
            v.add(Check("대상 계층", UNKNOWN,
                        "영구임대는 1순위가 수급자·한부모지만 2순위는 "
                        "도시근로자 소득 50% 이하면 지원할 수 있다",
                        source_key="supply-rule",
                        todo="공고문에서 순위별 자격과 잔여 물량 확인"))
    elif _has(t, "수급자", "주거취약", "쪽방"):
        v.add(Check("대상 계층", MET if prof.get("low_income") else UNMET,
                    "수급자·저소득 취약계층 대상 공고", source_key="notice-title"))

    if _has(t, "대학생", "희망하우징", "공공기숙사"):
        v.add(Check("대상 계층", MET if prof.get("student") else UNMET,
                    "대학(원)생 대상 공고", source_key="notice-title"))

    if _has(t, "예술인", "창업") and not _has(t, "창업지원주택 일반"):
        if not prof.get("artist_or_startup"):
            v.add(Check("대상 계층", UNMET, "예술인·창업가 대상 공고",
                        source_key="notice-title"))


def _check_homeless(n: Notice, prof: dict, v: Verdict) -> None:
    """무주택 요건. 오피스텔·민간임대는 요건이 없어 검사하지 않는다."""
    if v.kind in ("오피스텔", "민간임대"):
        return
    if not prof.get("homeless", True):
        v.add(Check("무주택", UNMET, "본인 주택 보유 — 분양·공공임대는 대부분 불가",
                    source_key="supply-rule"))
        return
    v.add(Check("무주택", MET, "본인 무주택", source_key="profile"))

    # 세대원 전원 무주택을 보는 유형.
    #
    # 무순위를 뒤늦게 넣었다. 청약홈 공식 페이지(무순위/잔여세대)가 신청자격을
    # **"무주택세대구성원인 성년자"** 로 규정한다 — 본인만 무주택이면 되는 게
    # 아니다. 본인 무주택만 보고 있어서 세대원이 집을 가진 경우까지 추천했다.
    if v.kind in ("공공분양", "공공임대", "무순위"):
        if prof.get("household") == "단독세대주":
            v.add(Check("세대 무주택", MET, "단독세대주 — 세대원 주택 없음",
                        source_key="profile"))
        elif prof.get("household_homeless", True):
            v.add(Check("세대 무주택", MET, "세대원 전원 무주택",
                        source_key="profile"))
        else:
            why = ("무순위는 신청자격이 '무주택세대구성원'이다"
                   if v.kind == "무순위"
                   else "공공 유형은 세대원 전원 무주택을 본다")
            v.add(Check("세대 무주택", UNMET,
                        f"세대원 중 주택 보유자가 있다 — {why}",
                        source_key="applyhome-unranked"
                        if v.kind == "무순위" else "supply-rule"))


def _check_account(n: Notice, prof: dict, v: Verdict, today: date) -> None:
    """청약통장 — 보유 여부와 1순위 요건."""
    v.account = account_need(n, v.kind)
    v.account_label = ACCOUNT_LABEL[v.account]

    if v.account == "불필요":
        v.add(Check("청약통장", MET, "통장 없이 지원 가능한 공고",
                    source_key="notice-title"))
        return

    if not prof.get("has_account", True):
        v.add(Check("청약통장", UNMET, "통장이 있어야 지원할 수 있는 공고",
                    source_key="supply-rule"))
        return

    if v.account == "확인":
        v.add(Check("청약통장", UNKNOWN, "통장 소요 여부를 분류하지 못했다",
                    source_key="notice", todo="공고문에서 청약통장 요건 확인"))
        return

    v.add(Check("청약통장 보유", MET, "청약통장 보유", source_key="profile"))

    rank = ACCOUNT_FIRST_RANK.get(v.kind)
    if rank is None:
        return

    months = prof_mod.account_months(prof, today)
    payments = int(prof.get("account_payments") or 0)
    need_m, need_p = rank["years"] * 12, rank["payments"]

    if months is None:
        v.add(missing("청약통장 가입기간", "통장 가입일"))
    elif months >= max(need_m, ACCOUNT_SAFE_MONTHS):
        v.add(Check("청약통장 가입기간", MET,
                    f"가입 {months}개월 — 1순위 요건({need_m}개월) 충족",
                    source_key="supply-rule"))
    else:
        v.add(Check("청약통장 가입기간", UNKNOWN,
                    f"가입 {months}개월 — 1순위 요건은 보통 {need_m}개월",
                    source_key="supply-rule",
                    todo="순위·지역별 요건이 달라 공고문 확인 필요"))

    if need_p:
        if payments <= 0:
            v.add(missing("청약통장 납입횟수", "납입 횟수"))
        elif payments >= max(need_p, ACCOUNT_SAFE_PAYMENTS):
            v.add(Check("청약통장 납입횟수", MET,
                        f"{payments}회 — 1순위 요건({need_p}회) 충족",
                        source_key="supply-rule"))
        else:
            v.add(Check("청약통장 납입횟수", UNKNOWN,
                        f"{payments}회 — 1순위 요건은 보통 {need_p}회",
                        source_key="supply-rule",
                        todo="순위·지역별 요건이 달라 공고문 확인 필요"))


def _check_residence(n: Notice, prof: dict, v: Verdict, today: date) -> None:
    """해당지역 거주 요건. 공고마다 6개월~2년으로 달라 24개월 넘으면 안전하게 본다."""
    if v.kind in ("오피스텔", "민간임대"):
        return
    same = bool(n.region) and n.region == prof.get("residence")
    months = prof_mod.residence_months(prof, today)
    mine = prof.get("residence")

    # 거주 요건은 **자격이 아니라 순위·가점 요소**다.
    #
    # 마이홈 국민임대 페이지: 해당지역 거주는 '선정순위 기준'이고 동일순위
    # 경쟁 시 '연속 거주 기간' 가점(5년 3점·3년 2점·1년 1점)이다.
    # 수도권 투기과열지구 분양의 2년도 '우선공급' 요건이지 자격요건이 아니다.
    #
    # 전에는 이걸 자격 조건으로 다뤄서, 확정할 수 없다는 이유로 공고 38건을
    # '확인 필요'에 가둬 놨다. 이제 INFO 로 보여만 주고 등급은 건드리지 않는다.
    # (당첨 가능성에는 영향이 크니 숨기지는 않는다)
    if months is None and same:
        v.add(Check("거주 지역", NOTE,
                    f"{n.region} 거주 — 전입일을 입력하면 거주기간 가점을 "
                    "가늠할 수 있다",
                    source_key="supply-rule", category=INFO,
                    todo="내 조건에서 현 거주지 전입일 입력"))
        return

    if not same:
        v.add(Check("거주 지역", NOTE,
                    f"공고 지역 {n.region or '미상'} · 내 거주지 {mine} — "
                    "해당지역 거주자가 아니라 순위·가점에서 밀릴 수 있다 "
                    "(자격 자체를 막는 요건은 아니다)",
                    source_key="supply-rule", category=INFO,
                    todo="공고문의 해당지역/기타지역 배정 물량 확인"))
        return

    # 해당지역 거주자 — 거주기간을 가점 구간으로 알려 준다
    if months >= 60:
        band = "5년 이상 (동일순위 가점 최상위 구간)"
    elif months >= 36:
        band = "3년 이상 5년 미만"
    elif months >= 12:
        band = "1년 이상 3년 미만"
    else:
        band = "1년 미만 (가점 없음)"
    extra = ""
    if v.kind in ("민영분양", "공공분양") and months < 24:
        extra = " · 수도권 투기과열지구는 2년 이상에 우선공급"
    v.add(Check("거주 지역", NOTE,
                f"{n.region} 거주 {months}개월 — {band}{extra}",
                source_key="supply-rule", category=INFO,
                todo="공고문의 거주기간·우선공급 기준 확인"))


def _applicable_standard(v: Verdict, prof: dict,
                         today: date) -> tuple[Optional[str], str]:
    """공고 유형·태그 + 프로필 → 적용할 공식 기준 키.

    후보가 여럿이면(행복주택 계층 미상 등) 프로필로 좁힌다.
    좁히지 못하면 (None, 이유) — 판정은 미확인에 머문다.
    """
    cands = standards.standards_for(v.kind, v.tags)
    if not cands:
        return None, ""
    if len(cands) == 1:
        return cands[0], ""

    a = prof_mod.age(prof, today)
    newlywed = prof_mod.is_newlywed(prof, today)
    cs = set(cands)

    if cs <= set(standards.HAPPY_HOUSE_KEYS):
        # 행복주택 — 공고가 계층을 안 밝혔으면 내 프로필의 계층으로 좁힌다
        if prof.get("student") and "대학생행복주택" in cs:
            return "대학생행복주택", ""
        if newlywed and "신혼행복주택" in cs:
            return "신혼행복주택", ""
        if a is not None and a >= 65 and "고령자행복주택" in cs:
            return "고령자행복주택", ""
        if a is not None and 19 <= a <= 39 and "청년행복주택" in cs:
            return "청년행복주택", ""
        return None, "행복주택 계층(청년·신혼·대학생·고령자)을 특정하지 못했다"

    if cs <= set(standards.PURCHASE_RENTAL_KEYS):
        if newlywed and "신혼부부매입임대" in cs:
            return "신혼부부매입임대", ""
        if a is not None and 19 <= a <= 39 and "청년매입임대" in cs:
            return "청년매입임대", ""
        return None, "매입임대 대상 계층을 특정하지 못했다"

    # 전세임대도 매입임대와 같은 구조다 — 계층을 못 좁히면 미확인으로 둔다.
    # (좁히지 않고 cands[0] 를 쓰면 청년·신혼 태그가 둘 다 있을 때
    #  엉뚱한 계층의 한도로 판정한다. met_only 라 오제외는 없지만
    #  화면에 틀린 기준 금액을 보여 주게 된다)
    if cs <= set(getattr(standards, "JEONSE_RENTAL_KEYS", ())):
        if newlywed and "신혼부부전세임대" in cs:
            return "신혼부부전세임대", ""
        if a is not None and 19 <= a <= 39 and "청년전세임대" in cs:
            return "청년전세임대", ""
        if "기존주택전세임대" in cs:
            return "기존주택전세임대", ""
        return None, "전세임대 대상 계층을 특정하지 못했다"

    # 특별공급이 여러 개 걸린 공고 — 내가 해당하는 특공 기준을 고른다
    if int(prof.get("children") or 0) >= 2 and "다자녀특공" in cs:
        return "다자녀특공", ""
    if newlywed and "신혼부부특공" in cs:
        return "신혼부부특공", ""
    if "생애최초특공" in cs:
        return "생애최초특공", ""
    return cands[0], ""


def _income_tier(n: Notice, std_key: Optional[str]) -> tuple[Optional[str], bool]:
    """전용면적 tier (국민임대 60㎡ 초과 100% 등) — (tier, 미확정 여부).

    공고의 주택형 전용면적을 알면 확정하고, 모르면 상한을 열어 둔다
    (60㎡ 초과 세대가 섞여 있을 수 있는데 70% 초과라고 제외하면 안 된다).
    """
    if std_key is None or not standards.STANDARDS[std_key].get("income"):
        return None, False
    tiers = standards.STANDARDS[std_key]["income"]["tiers"]
    if not tiers:
        return None, False

    sizes = []
    for a in (n.areas or []):
        try:
            sizes.append(float(a.exclusive))
        except (TypeError, ValueError):
            pass
    if sizes and all(s <= 60 for s in sizes):
        return None, False          # 전부 60㎡ 이하 — 기본 % 확정
    if sizes and all(s > 60 for s in sizes):
        return "전용60초과", False  # 전부 초과 — tier % 확정
    return None, True               # 모름/혼재 — 상한만 열어 둔다


def _check_income(n: Notice, prof: dict, v: Verdict, today: date,
                  std_key: Optional[str], std_note: str) -> None:
    """소득 — 공식 기준(standards.py)이 있으면 실제 판정한다.

    원칙:
      * 근로소득만 있으면 공식 기준과 비교해 충족/불충족을 낸다.
      * 사업소득이 있으면 인정 방식(종합소득세 신고 기준 등)이 달라
        **합산값으로 적격을 확정하지 않는다** — 참고로 보여주고 미확인.
      * official=False 기준(뉴:홈 등)은 checks.py 가 자동으로 미확인 처리.
      * 공고문에 별도 기준이 있으면 공고문이 우선 — todo 에 남긴다.
    """
    hh = prof.get("household_size", 1)
    ratio = income_ratio(prof)
    base_manwon = income_base(hh) // 10_000

    spec = standards.STANDARDS[std_key].get("income") if std_key else None
    if std_key and spec is None:
        # 민영 일반공급·무순위 — 소득 요건 자체가 없다.
        # **왜 없다고 말할 수 있는지**를 출처와 함께 남긴다.
        # (요건이 없는 것과 요건을 확인하지 못한 것은 다른 얘기다)
        std = standards.STANDARDS[std_key]
        src = RULE_SOURCES.get(std["source"], {})
        v.income = {
            "applies": False, "ratio": ratio,
            "note": f"{std['label']} — 자격 요건에 소득이 열거되지 않는다",
            "source": {"label": src.get("label", std["source"]),
                        "url": src.get("url", ""), "asof": src.get("asof"),
                        "official": bool(src.get("official"))},
        }
        if not src.get("official"):
            # 요건 유무를 공식 출처로 확인하지 못했으면 통과로 간주하지 않는다
            v.add(Check("소득", UNKNOWN,
                        f"{std['label']}의 소득 요건 유무를 공식 출처로 "
                        "확인하지 못했다",
                        source_key=std["source"],
                        todo="공고문에서 소득 요건을 확인하세요"))
        return

    if std_key is None:
        if v.kind not in INCOME_SENSITIVE and "특별공급" not in v.tags:
            v.income = {"applies": False, "ratio": ratio,
                        "note": f"{v.kind} 일반공급은 소득 요건이 없는 경우가 많다"}
            return
        _income_without_standard(prof, v, ratio, base_manwon, std_note)
        return

    # ---- 공식 기준 적용 ----
    std = standards.STANDARDS[std_key]
    src_key = std["source"]
    tier, tier_uncertain = _income_tier(n, std_key)

    # 배우자 소득을 합산할지는 기준의 **scope_mode** 로 가른다.
    #   self        본인만 (청년 매입임대 3순위 등)
    #   parents_add 본인+부모 (대학생 행복주택) — 부모 소득을 받지 않으므로 미확인
    #   household   세대 전원 → 배우자가 있으면 합산
    # 사람이 읽는 scope 문자열을 파싱하면 '세대(단독세대주면 본인)' 처럼
    # 예외 표기에 걸린다. 기계 판독 필드를 쓴다.
    scope = str(spec.get("scope") or "")
    mode = str(spec.get("scope_mode") or "household")
    has_spouse = prof.get("marital") in ("예비신혼", "신혼", "기혼")
    with_spouse = mode == "household" and has_spouse

    # 맞벌이 여부 — 프로필에서 확정할 수 있으면 확정한다.
    # None 이면 외벌이/맞벌이 한도 사이가 '확인 필요'로 남는다.
    dual = prof_mod.is_dual_income(prof)
    if not spec["dual_pct"]:
        dual = False        # 맞벌이 구분이 없는 기준이면 의미가 없다

    limit_lo, _, _ = standards.income_limit(std_key, hh, tier=tier)

    sp = prof_mod.spouse_income(prof)
    v.income = {
        "applies": True, "ratio": ratio, "label": std["label"],
        "limit_pct": spec["pct"], "signal": income_signal(ratio, spec["pct"]),
        "base_manwon": base_manwon, "scope": scope,
        "dual_income": dual, "spouse_income": sp,
        "note": (f"{std['label']} 공식 기준 적용 — 가구원 {hh}인 한도 "
                 f"월 {limit_lo:,}원 ({scope} 소득 기준)"
                 + ("" if dual is not None or not spec["dual_pct"]
                    else " · 맞벌이 여부를 몰라 외벌이~맞벌이 한도 사이는 확인 필요")),
    }

    biz = business_income_total(prof, include_spouse=with_spouse)
    inc = recognized_monthly_income(prof, include_spouse=with_spouse)   # 만원
    if inc is None:
        v.add(missing("소득", "근로소득·사업소득"))
        return
    inc_won = int(inc) * 10_000

    # 배우자 소득을 합산해야 하는 기준인데 입력이 없으면 '충족'을 확정할 수 없다.
    # 내가 아는 건 본인 소득뿐이고, 배우자 소득이 더해지면 한도를 넘을 수 있다.
    #
    # 다만 **불충족은 확정할 수 있다** — 본인 소득만으로 이미 상한을 넘었다면
    # 배우자 소득을 더해도 결론이 바뀌지 않는다. 그래서 compare_income 의
    # 밴드 판정을 그대로 쓰고, '충족'만 '미확인'으로 내린다.
    spouse_unknown = with_spouse and dual is not False and sp is None

    if biz > 0:
        # 사업소득 인정액은 단순 합산과 다르다 — 합산으로 확정하지 않는다
        whose = "본인·배우자" if (with_spouse and sp) else "본인"
        v.add(Check(
            "소득", UNKNOWN,
            f"{whose} 근로+사업 단순 합산 월 {inc_won:,}원 (참고) / "
            f"{std['label']} 한도 월 {limit_lo:,}원 — 사업소득은 "
            "인정 방식이 달라 합산값으로 판정하지 않는다",
            source_key=src_key,
            todo="사업소득 인정액은 종합소득세 신고 기준으로 산정된다 — "
                 "공고문·LH 상담으로 확인"))
        return

    status_txt, detail, todo = standards.compare_income(
        std_key, hh, inc_won, dual=dual, tier=tier,
        tier_uncertain=tier_uncertain)
    status = {"충족": MET, "불충족": UNMET}.get(status_txt, UNKNOWN)

    if spouse_unknown and status == MET:
        # 본인 소득은 한도 안이지만 배우자 소득을 모른다 → 확정하지 않는다
        status = UNKNOWN
        detail = (f"본인 소득 월 {inc_won:,}원은 {std['label']} 한도 "
                  f"월 {limit_lo:,}원 이내지만, 이 기준은 {scope} 소득을 본다")
        todo = "내 조건에서 맞벌이 여부와 배우자 소득을 입력하면 판정이 확정된다"
    # official=False 기준은 어차피 미확인으로 내려앉는다 — 그때 detail 에
    # '충족'이라고 적혀 있으면 어긋나 보이므로 참고 표현으로 바꾼다
    official = bool(RULE_SOURCES.get(src_key, {}).get("official"))
    suffix = {MET: " → 충족" if official else " → 기준 이하 (참고)",
              UNMET: " → 불충족" if official else " → 기준 초과 (참고)",
              UNKNOWN: ""}[status]
    # 배우자 소득을 합산했으면 '내 근로소득'이라고 쓰면 안 된다 —
    # 400만(본인)+300만(배우자)=700만을 "내 근로소득 700만"으로 보여주면 오해다
    who = "본인·배우자 근로소득" if (with_spouse and sp) else "내 근로소득"
    detail = detail.replace("내 소득", who) + suffix
    if status != UNKNOWN and not todo:
        todo = "공고문에 별도 소득 기준이 있으면 공고문이 우선한다"
    v.add(Check("소득", status, detail, source_key=src_key, todo=todo))


def _income_without_standard(prof: dict, v: Verdict, ratio, base_manwon: int,
                             std_note: str) -> None:
    """공식 기준을 특정하지 못한 소득 요건 유형 — 미확인에 머문다."""
    v.income = {
        "applies": True, "ratio": ratio, "limit_pct": None, "label": "",
        "signal": "확인", "base_manwon": base_manwon,
        "note": (f"가구원 {prof.get('household_size', 1)}인 도시근로자 "
                 f"월평균소득 {base_manwon:,}만원 기준"),
    }
    if ratio is None:
        v.add(missing("소득", "근로소득·사업소득"))
        return

    if v.kind == "민영분양":
        # 민영 특공 — 공식 확정값이 없어 샘플 %로 신호만 만든다 (자동 미확인)
        limit, label = _special_limit(v)
        sig = income_signal(ratio, limit)
        v.income.update({"limit_pct": limit, "label": label, "signal": sig})
        detail = f"입력 소득은 도시근로자 월평균소득의 {ratio}%"
        if limit:
            detail += f" ({label} 참고 상한 {limit}% · 신호 {sig})"
        v.add(Check("소득", MET if sig in ("여유", "경계") else UNMET, detail,
                    source_key="sample-special",
                    todo="공고문의 특별공급 소득 기준으로 확인 필요"))
        return

    v.add(Check("소득", UNKNOWN,
                "소득 요건이 있는 유형이지만 적용할 공식 기준을 특정하지 못했다"
                + (f" — {std_note}" if std_note else ""),
                source_key="notice",
                todo="공고문의 소득 기준(% ·산정 대상)으로 확인"))


def _special_limit(v: Verdict) -> tuple[Optional[int], str]:
    for tag in ("신혼부부", "생애최초", "청년", "다자녀"):
        if tag in v.tags and tag in SPECIAL_SUPPLY:
            return SPECIAL_SUPPLY[tag]["pct"], f"{tag} 특별공급"
    if "특별공급" in v.tags:
        return SPECIAL_SUPPLY["생애최초"]["pct"], "생애최초 특별공급"
    return None, ""


def _check_assets(n: Notice, prof: dict, v: Verdict,
                  std_key: Optional[str]) -> None:
    """총자산·부동산·자동차 — 공식 기준이 있으면 항목별로 실제 판정한다."""
    na = net_assets(prof)                      # 순자산(자산-부채, 만원)
    gross = prof.get("assets")                 # 총자산 입력값 (만원)
    car = int(prof.get("car") or 0)

    lims = standards.asset_limits(std_key) if std_key else None
    if lims and not lims["applies"]:
        v.asset = {"applies": False,
                   "note": f"{lims['label']} — 총자산 요건 없음"}
        return

    if lims is None:
        _assets_without_standard(prof, v, na, car)
        return

    src_key = lims["source"]["key"]
    label = lims["label"]
    met_only = lims["met_only"]
    # official=False 기준은 미확인으로 내려앉는다 — detail 의 단정 표현을 피한다
    ok_word = "충족" if lims["source"]["official"] else "기준 이하 (참고)"
    over_word = "초과" if lims["source"]["official"] else "기준 초과 (참고)"
    v.asset = {
        "applies": True, "net_assets": na, "car": car,
        "guide": {"assets": lims["total"] or lims["estate"], "car": lims["car"]},
        "note": f"{label} 공식 자산 기준 적용 ({lims['source']['asof'] or '기준일 공고문 확인'})",
    }

    # ---- 총자산 ----
    if lims["total"] is not None:
        if na is None:
            v.add(missing("자산", "총자산"))
        elif na <= lims["total"]:
            v.add(Check("총자산", MET,
                        f"{label} 총자산 기준 {fmt_won(lims['total'])} / "
                        f"내 순자산(자산-부채) {fmt_won(na)} → {ok_word}",
                        source_key=src_key,
                        todo="공고문에 별도 자산 기준이 있으면 공고문이 우선한다"))
        elif met_only:
            # 더 느슨한 순위·특례가 공존 — 초과로 탈락을 확정하지 않는다
            v.add(Check("총자산", UNKNOWN,
                        f"{label} 기본 자산 기준 {fmt_won(lims['total'])} 초과 "
                        f"(내 순자산 {fmt_won(na)}) — 순위별 기준이 달라 확정 불가",
                        source_key=src_key,
                        todo="순위별 자산 기준이 달라 공고문·LH 상담으로 확인"))
        else:
            v.add(Check("총자산", UNMET,
                        f"{label} 총자산 기준 {fmt_won(lims['total'])} / "
                        f"내 순자산 {fmt_won(na)} → {over_word}",
                        source_key=src_key,
                        todo="공고문에 별도 자산 기준이 있으면 공고문이 우선한다"))
    elif lims["estate"] is not None:
        # 부동산가액만 보는 유형 (특별공급·뉴:홈 일반형).
        # 부동산가액을 분리 입력했으면 그 값으로 바로 판정하고,
        # 없으면 '총자산 전체 ≤ 부동산 한도' 일 때만 충족을 확정한다.
        estate = prof.get("real_estate")
        if estate not in (None, ""):
            estate = int(estate)
            if estate <= lims["estate"]:
                v.add(Check("부동산", MET,
                            f"{label} 부동산 기준 {fmt_won(lims['estate'])} / "
                            f"내 부동산가액 {fmt_won(estate)} → {ok_word}",
                            source_key=src_key,
                            todo="공고문에 별도 자산 기준이 있으면 공고문이 우선한다"))
            elif met_only:
                v.add(Check("부동산", UNKNOWN,
                            f"{label} 부동산 기준 {fmt_won(lims['estate'])} 초과 "
                            f"(내 부동산가액 {fmt_won(estate)}) — "
                            "순위별 기준이 달라 확정 불가",
                            source_key=src_key,
                            todo="순위별 자산 기준이 달라 공고문으로 확인"))
            else:
                v.add(Check("부동산", UNMET,
                            f"{label} 부동산 기준 {fmt_won(lims['estate'])} / "
                            f"내 부동산가액 {fmt_won(estate)} → {over_word}",
                            source_key=src_key,
                            todo="공고문에 별도 자산 기준이 있으면 공고문이 우선한다"))
        elif gross in (None, ""):
            v.add(missing("자산", "총자산 또는 부동산가액"))
        elif int(gross) <= lims["estate"]:
            v.add(Check("부동산", MET,
                        f"{label} 부동산 기준 {fmt_won(lims['estate'])} / "
                        f"총자산 전체({fmt_won(int(gross))})가 기준 이하라 "
                        f"부동산가액도 이하 → {ok_word}",
                        source_key=src_key))
        else:
            v.add(Check("부동산", UNKNOWN,
                        f"{label}은 부동산가액({fmt_won(lims['estate'])})만 본다 — "
                        f"총자산 {fmt_won(int(gross))} 중 부동산 비중을 알 수 없다",
                        source_key=src_key,
                        todo="내 조건에서 '부동산가액'을 입력하면 판정이 확정된다"))

    # ---- 자동차 ----
    if lims["car_forbidden"]:
        v.add(Check("자동차", UNMET if car > 0 else MET,
                    f"{label}은 자동차 미보유 요건"
                    + (f" — 보유 차량 {fmt_won(car)}" if car > 0 else " — 미보유"),
                    source_key=src_key))
    elif lims["car"] is not None:
        if car > lims["car"]:
            status = UNKNOWN if met_only else UNMET
            v.add(Check("자동차가액", status,
                        f"{label} 자동차 기준 {fmt_won(lims['car'])} / "
                        f"내 차량 {fmt_won(car)} → {over_word}"
                        + (" — 순위별 기준 확인 필요" if met_only else ""),
                        source_key=src_key,
                        todo="공고문에 별도 자동차 기준이 있으면 공고문이 우선한다"))
        elif car:
            v.add(Check("자동차가액", MET,
                        f"{label} 자동차 기준 {fmt_won(lims['car'])} / "
                        f"내 차량 {fmt_won(car)} → {ok_word}",
                        source_key=src_key))
    elif car:
        # 기준이 공식 페이지에 **적혀 있지 않은** 유형
        # (고령자 행복주택·청년안심주택·영구임대·신혼희망타운·뉴:홈 나눔/선택형 등).
        #
        # 한때 "확인된 유형이 모두 4,542만원이니 그 아래면 통과"로 충족을 줬는데,
        # **그건 유형 간 기준 전용이고 근거가 안 된다.** 우리 데이터 안에
        # 대학생 행복주택의 '무차량'(= 0원) 이라는 더 엄격한 반례가 있다.
        # 기준을 모르는 유형이 4,542만원 이상이라고 단정할 수 없다.
        #
        # 그래서 '확인 필요'로 둔다. 통상값은 참고로만 알려 준다.
        v.add(Check("자동차가액", UNKNOWN,
                    f"{label}의 자동차가액 기준을 공식 출처에서 확인하지 못했다 "
                    f"(내 차량 {fmt_won(car)}). 다른 공공 유형은 보통 "
                    f"{fmt_won(CAR_REFERENCE_MANWON)}이지만 "
                    f"'자동차 미보유'를 요구하는 유형도 있다",
                    source_key=src_key,
                    todo="공고문에서 자동차가액 기준을 확인하세요"))


def _assets_without_standard(prof: dict, v: Verdict, na, car: int) -> None:
    """공식 기준을 특정하지 못한 공공 유형 — 샘플 참고 신호만 (자동 미확인)."""
    guide = ASSET_GUIDE.get(v.kind)
    if guide is None:
        v.asset = {"applies": False,
                   "note": f"{v.kind}은 총자산 요건이 없는 경우가 많다"}
        return

    v.asset = {
        "applies": True, "net_assets": na, "car": car, "guide": guide,
        "note": "공공 유형은 총자산·자동차가액 상한이 있다 (연도·공고별로 다름)",
    }
    if na is None:
        v.add(missing("자산", "총자산"))
    else:
        over = na > guide["assets"]
        v.add(Check("총자산", UNMET if over else MET,
                    f"순자산 {fmt_won(na)} vs 참고 상한 {fmt_won(guide['assets'])}",
                    source_key=ASSET_GUIDE_SOURCE,
                    todo="공고문의 연도별 자산 기준으로 확인 필요"))
    if car:
        over = car > guide["car"]
        v.add(Check("자동차가액", UNMET if over else MET,
                    f"{fmt_won(car)} vs 참고 상한 {fmt_won(guide['car'])}",
                    source_key=ASSET_GUIDE_SOURCE,
                    todo="공고문의 연도별 자동차 기준으로 확인 필요"))


def _check_household_comp(prof: dict, v: Verdict, today: date,
                          std_key: Optional[str], std_note: str) -> None:
    """세대구성 — 적용한 계층 기준을 명시하고, 계층 미상이면 미확인.

    연령·신혼·다자녀처럼 공고 태그로 이미 검사된 것(_check_age,
    _check_target_group)은 중복해서 만들지 않는다. 여기서는 공고가 계층을
    안 밝혀 프로필로 기준을 고른 경우의 근거와, 태그 검사가 못 다루는
    요건(미혼·부양기간 등)만 다룬다.
    """
    if std_note:
        # 계층 후보가 여럿인데 프로필로도 못 좁혔다 — 세대구성부터 미확인
        v.add(Check("세대구성", UNKNOWN, std_note, source_key="notice",
                    todo="공고문에서 모집 계층과 내 해당 여부 확인"))
        return
    if std_key is None:
        return
    desc = standards.STANDARDS[std_key].get("household", "")

    if std_key == "청년행복주택":
        # 혼인 요건은 공식 페이지 전사에 없어 미혼이 아닐 때만 확인을 요구한다
        if prof.get("marital") == "미혼":
            v.add(Check("세대구성", MET, f"{desc} — 미혼 단독가구로 판정",
                        source_key="profile"))
        else:
            v.add(Check("세대구성", UNKNOWN,
                        f"{desc} — 혼인 상태({prof.get('marital')})의 "
                        "청년 계층 해당 여부는 공고문 기준",
                        source_key="notice", todo="공고문의 청년 계층 요건 확인"))
    elif std_key == "청년매입임대":
        # LH 공식 페이지가 '미혼 청년'을 명시한다 — 기혼이면 구조적으로 제외
        v.add(Check("세대구성",
                    MET if prof.get("marital") == "미혼" else UNMET,
                    f"{desc} · 현재 {prof.get('marital')}",
                    source_key="lh-youth-purchase"))
    elif std_key == "대학생행복주택":
        v.add(Check("세대구성", MET, f"{desc} — 대학(원)생으로 판정",
                    source_key="profile"))
    elif std_key in ("신혼행복주택", "신혼부부매입임대") \
            and "신혼부부" not in v.tags:
        # 공고가 계층을 안 밝혀 프로필(신혼)로 골랐다 — 근거를 남긴다
        v.add(Check("세대구성", MET, f"적용 기준: {desc}", source_key="profile"))
    elif std_key == "고령자행복주택" and "고령자" not in v.tags:
        v.add(Check("세대구성", MET, f"적용 기준: {desc}", source_key="profile"))
    elif std_key == "생애최초특공":
        v.add(Check("세대구성", UNKNOWN,
                    "생애최초 요건(세대원 전원 주택 소유 이력 없음·소득세 "
                    "납부 이력 등)은 입력만으로 확정할 수 없다",
                    source_key="notice", todo="공고문의 생애최초 요건 확인"))
    elif std_key == "노부모특공":
        v.add(Check("세대구성", UNKNOWN,
                    f"{desc} — 부양 기간·세대 구성은 입력만으로 확정할 수 없다",
                    source_key="notice", todo="공고문의 노부모부양 요건 확인"))


def _check_extra(n: Notice, prof: dict, v: Verdict,
                 std_key: Optional[str]) -> None:
    """기타 공고별 요건 — 조건부 요건(모기지 등)이 걸릴 때만 미확인으로 띄운다."""
    if std_key is None:
        return
    std = standards.STANDARDS[std_key]
    for extra in std.get("extra", ()):
        if std_key == "신혼희망타운":
            lims = standards.asset_limits(std_key)
            lo = n.price_min
            # 주택가격이 총자산기준 이하로 확인되면 모기지 요건이 안 붙는다
            if lo is not None and lims["total"] and lo <= lims["total"]:
                continue
        v.add(Check("기타 요건", UNKNOWN, extra,
                    source_key=std["source"],
                    todo="공고문에서 해당 여부 확인"))


# ---------------------------------------------------------------- 종합
def judge(n: Notice, prof: dict, today: date) -> Verdict:
    v = Verdict(kind=kind_of(n), tags=tags_of(n))
    v.account_label = ACCOUNT_LABEL[v.account]

    # 0) 모집이 아닌 후속 안내는 목록에서 뺀다
    if NOISE.search(n.title) and not RECRUIT.search(n.title.split("결과")[-1]):
        v.level = LEVEL_IGNORE
        v.add(Check("공고 성격", UNMET, "모집이 아닌 후속 안내",
                    source_key="notice-title"))
        return v

    # 1) 내가 걸어둔 필터 — 여기 걸리면 자격은 보지 않는다
    _check_region(n, prof, v)
    _check_kind(n, prof, v)
    if v.checks and any(c.category == FILTER and c.status == UNMET
                        for c in v.checks):
        v.settle()
        return v

    # 2) 통장 모드로 아예 빼기로 한 유형
    v.account = account_need(n, v.kind)
    v.account_label = ACCOUNT_LABEL[v.account]
    blocked = ACCOUNT_MODES.get(prof.get("account_mode", "상관없음"), ())
    if v.account in blocked:
        v.add(filter_block(
            "청약통장 사용",
            "당첨 시 통장이 소멸되는 공고를 빼도록 설정했다" if v.account == "사용"
            else "통장 가입이 필요한 공고를 빼도록 설정했다"))
        v.settle()
        return v

    # 3) 자격 요건
    _check_age(n, prof, v, today)
    _check_target_group(n, prof, v, today)
    _check_homeless(n, prof, v)
    _check_account(n, prof, v, today)
    _check_residence(n, prof, v, today)

    # 소득·자산·세대구성·기타는 같은 공식 기준(standards.py)을 공유한다
    std_key, std_note = _applicable_standard(v, prof, today)
    _check_household_comp(prof, v, today, std_key, std_note)
    _check_income(n, prof, v, today, std_key, std_note)
    _check_assets(n, prof, v, std_key)
    _check_extra(n, prof, v, std_key)
    _check_price(n, prof, v)

    v.settle()
    return v
