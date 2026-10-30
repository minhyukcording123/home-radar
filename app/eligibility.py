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
  - 공공분양·공공임대 → 소득·자산 공식 기준이 없어 항상 '확인 필요'에 머문다
    (샘플 기준으로 적격을 확정하지 않기 때문이다 — 의도된 동작이다)
"""
from __future__ import annotations

import re
from datetime import date
from typing import Optional

from . import profile as prof_mod
from .checks import (ELIGIBILITY, FILTER, LEVEL_IGNORE, MET, SIGNAL, UNKNOWN,
                     UNMET, Check, Verdict, filter_block, missing)
from .models import Notice
from .rules import (ACCOUNT_FIRST_RANK, AGE_RULES, ASSET_GUIDE,
                    ASSET_GUIDE_SOURCE, INCOME_BASE_SOURCE, SPECIAL_SUPPLY,
                    fmt_won, income_base, income_ratio, income_signal,
                    net_assets)

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
    "무순위": UNRANKED_TYPES,
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

# 소득·자산 요건이 실제로 붙는 유형 (공식 기준이 없어 '미확인'으로 남는다)
INCOME_SENSITIVE = ("공공분양", "공공임대")

# '어떤 공고 기준이든 통과'로 볼 수 있는 임계값
RESIDENCE_SAFE_MONTHS = 24     # 해당지역 거주기간 요건은 보통 6~24개월
ACCOUNT_SAFE_MONTHS = 24       # 수도권 1순위 가입기간
ACCOUNT_SAFE_PAYMENTS = 24     # 수도권 공공분양 1순위 납입횟수


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

    if _has(t, "영구임대", "수급자", "주거취약", "쪽방"):
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

    # 공공 유형은 세대원 전원 무주택을 본다
    if v.kind in ("공공분양", "공공임대"):
        if prof.get("household") == "단독세대주":
            v.add(Check("세대 무주택", MET, "단독세대주 — 세대원 주택 없음",
                        source_key="profile"))
        elif prof.get("household_homeless", True):
            v.add(Check("세대 무주택", MET, "세대원 전원 무주택",
                        source_key="profile"))
        else:
            v.add(Check("세대 무주택", UNMET,
                        "세대원 중 주택 보유자가 있다 — 공공 유형은 대부분 불가",
                        source_key="supply-rule"))


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

    # 무순위/잔여세대는 거주지역·무주택 요건이 개정을 거치며 여러 번 바뀌었다.
    # 현재 기준을 코드로 단정할 수 없으므로 '순위가 밀린다' 같은 말을 하지 않고
    # 공고문 확인으로 넘긴다 (무순위에는 애초에 순위 개념이 없다).
    if v.kind == "무순위":
        v.add(Check("거주 요건", UNKNOWN,
                    f"공고 지역 {n.region or '미상'} · 내 거주지 {prof.get('residence')}"
                    " — 무순위는 거주지역 요건이 공고별로 다르다",
                    source_key="notice",
                    todo="공고문에서 거주지역·무주택 요건 확인"))
        return

    if not same:
        v.add(Check("거주 요건", UNKNOWN,
                    f"공고 지역 {n.region or '미상'} · 내 거주지 {prof.get('residence')}"
                    " — 기타지역 지원은 순위가 밀릴 수 있다",
                    source_key="supply-rule",
                    todo="공고문의 해당지역/기타지역 배정 물량 확인"))
        return

    if months is None:
        v.add(missing("거주 요건", "현 거주지 전입일"))
    elif months >= RESIDENCE_SAFE_MONTHS:
        v.add(Check("거주 요건", MET,
                    f"{n.region} 거주 {months}개월 — 거주기간 요건(최대 24개월) 충족",
                    source_key="supply-rule"))
    else:
        v.add(Check("거주 요건", UNKNOWN,
                    f"{n.region} 거주 {months}개월 — 공고별 요건이 6~24개월로 다르다",
                    source_key="supply-rule",
                    todo="공고문의 거주기간 요건 확인"))


def _check_income(n: Notice, prof: dict, v: Verdict) -> None:
    """소득 — 공식 기준이 없어 '참고 신호'로만 둔다 (항상 미확인).

    샘플 기준은 checks.Check 가 구조적으로 '미확인'으로 내려앉힌다.
    """
    ratio = income_ratio(prof)
    base_manwon = income_base(prof.get("household_size", 1)) // 10_000

    if v.kind not in INCOME_SENSITIVE and "특별공급" not in v.tags:
        v.income = {"applies": False, "ratio": ratio,
                    "note": f"{v.kind} 일반공급은 소득 요건이 없는 경우가 많다"}
        return

    limit, label = _special_limit(v)
    sig = income_signal(ratio, limit)
    v.income = {
        "applies": True, "ratio": ratio, "limit_pct": limit, "label": label,
        "signal": sig, "base_manwon": base_manwon,
        "note": (f"가구원 {prof.get('household_size', 1)}인 도시근로자 월평균소득"
                 f" {base_manwon:,}만원 기준"),
    }

    if ratio is None:
        v.add(missing("소득", "근로소득·사업소득"))
        return

    detail = f"입력 소득은 도시근로자 월평균소득의 {ratio}%"
    if limit:
        detail += f" ({label} 상한 {limit}% · 신호 {sig})"
    v.add(Check("소득", MET if sig in ("여유", "경계") else UNMET, detail,
                source_key=INCOME_BASE_SOURCE,
                todo="공고문의 연도별 소득 기준으로 확인 필요"))


def _special_limit(v: Verdict) -> tuple[Optional[int], str]:
    for tag in ("신혼부부", "생애최초", "청년", "다자녀"):
        if tag in v.tags and tag in SPECIAL_SUPPLY:
            return SPECIAL_SUPPLY[tag]["pct"], f"{tag} 특별공급"
    if "특별공급" in v.tags:
        return SPECIAL_SUPPLY["생애최초"]["pct"], "생애최초 특별공급"
    return None, ""


def _check_assets(n: Notice, prof: dict, v: Verdict) -> None:
    """총자산·자동차가액 — 역시 샘플 기준이라 '미확인' 신호로만 둔다."""
    guide = ASSET_GUIDE.get(v.kind)
    if guide is None:
        v.asset = {"applies": False,
                   "note": f"{v.kind}은 총자산 요건이 없는 경우가 많다"}
        return

    na = net_assets(prof)
    car = int(prof.get("car") or 0)
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
    _check_income(n, prof, v)
    _check_assets(n, prof, v)
    _check_price(n, prof, v)

    v.settle()
    return v
