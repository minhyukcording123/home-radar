"""공고 × 내 조건 → 추천 / 확인 / 제외.

jisangfolio/cheongyak-auto(MIT) 의 `cheong/eligibility.py` 판정 설계를 각색했다.
원본의 핵심 원칙을 그대로 따른다:

    공고 목록에는 제목·유형 정도만 있어서 세부 소득·자산 기준은 알 수 없다.
    그래서 '구조적으로 확실히 안 되는 것'만 제외하고, 애매하면 '확인'으로 살린다.
    (잘못 걸러서 놓치는 손해 > 알림 하나 더 받는 손해)

HOME RADAR 가 더한 것:
  - 소득·자산을 '신호등'으로 계산해 노출한다 (rules.py)
    공고별 실제 기준이 우선하므로 소득·자산만으로는 제외하지 않고,
    초과가 뚜렷하면 추천 → 확인으로 내리고 사유를 남긴다.
  - 청약통장 미보유를 반영한다.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from .models import Notice
from .profile import age as calc_age
from .rules import ASSET_GUIDE, SPECIAL_SUPPLY, income_ratio, income_verdict

LEVELS = {"추천": 3, "확인": 2, "제외": 1, "무시": 0}

# 모집이 아닌 후속 안내(결과·서류·계약)는 알림 대상이 아니다
NOISE = re.compile(r"당첨자|서류심사|서류제출|계약\s*(체결|안내|일정)|결과\s*(발표|안내|공고)"
                   r"|순번|합격자|명단|동호수|정산")
RECRUIT = re.compile(r"모집|청약\s*접수|접수\s*안내|입주자\s*선정|공급")

# 청약통장이 지원에 쓰이는가
ACCOUNT_FREE = ("무순위", "잔여", "임의공급", "오피스텔", "도시형", "민간임대",
                "매입임대", "전세임대", "든든전세", "청년안심", "역세권")
ACCOUNT_USE = ("특별공급", "1순위", "2순위", "일반공급", "공공분양", "신혼희망타운",
               "뉴홈", "뉴:홈", "분양전환")
ACCOUNT_KEEP = ("행복주택", "국민임대", "통합공공임대")
ACCOUNT_LABEL = {
    "불필요": "청약통장 필요 없음",
    "가입만": "가입 사실만 확인 — 당첨돼도 통장 유지",
    "사용": "당첨되면 통장 효력 소멸",
    "확인": "공고문에서 확인",
}
# 어떤 공고를 아예 뺄지 (기본은 빼지 않고 표시만 한다)
ACCOUNT_MODES = {"불필요만": ("사용", "가입만"), "가입만까지": ("사용",), "상관없음": ()}


@dataclass
class Verdict:
    level: str = "확인"
    kind: str = "민영분양"
    reasons: list[str] = field(default_factory=list)      # 추천/확인 근거
    blockers: list[str] = field(default_factory=list)      # 제외 사유
    tips: list[str] = field(default_factory=list)          # 챙겨야 할 점
    account: str = "확인"
    income: dict = field(default_factory=dict)
    asset: dict = field(default_factory=dict)

    @property
    def score(self) -> int:
        return LEVELS[self.level]

    def to_dict(self):
        return {
            "level": self.level, "kind": self.kind, "reasons": self.reasons,
            "blockers": self.blockers, "tips": self.tips,
            "account": self.account, "account_label": ACCOUNT_LABEL[self.account],
            "income": self.income, "asset": self.asset,
        }


def _has(text: str, *words: str) -> bool:
    return any(w in text for w in words)


# 유형 판별 — 순서가 중요하다.
# '토지임대부 공공분양주택'처럼 이름에 '임대'가 들어가도 실제로는 분양인 공고가 있고,
# 거꾸로 '국민임대'는 '국민'이 들어가도 분양이 아니라 임대다.
# 그래서 구체적인 유형명을 먼저 보고, 뭉뚱그린 '임대' 검사는 마지막에 둔다.
RENTAL_TYPES = ("국민임대", "통합공공임대", "영구임대", "행복주택", "장기전세",
                "매입임대", "전세임대", "든든전세", "청년안심", "재개발임대",
                "사회주택", "희망하우징", "공공기숙사", "기숙사")
PUBLIC_SALE_TYPES = ("공공분양", "신혼희망타운", "신혼희망", "뉴홈", "뉴:홈",
                     "토지임대부", "지분적립", "국민주택")


def kind_of(n: Notice) -> str:
    """청약홈 공고를 5가지 성격으로 나눈다."""
    t = n.text
    if _has(t, "오피스텔", "도시형"):
        return "오피스텔"
    if _has(t, "공공지원민간임대", "민간임대"):
        return "민간임대"
    if _has(t, *RENTAL_TYPES):
        return "임대"
    if _has(t, *PUBLIC_SALE_TYPES):
        return "공공분양"
    if _has(t, "임대"):
        return "임대"
    return "민영분양"


def account_need(n: Notice, kind: str) -> str:
    t = n.text
    if _has(t, *ACCOUNT_FREE) or kind in ("오피스텔", "민간임대"):
        return "불필요"
    if _has(t, *ACCOUNT_KEEP):
        return "가입만"
    if _has(t, *ACCOUNT_USE) or kind in ("공공분양", "민영분양"):
        return "사용"
    return "확인"


def _special_supply_limit(n: Notice, prof: dict) -> tuple[Optional[int], str]:
    """이 공고에서 내가 노릴 만한 특별공급 유형의 소득 상한(%)과 그 이름."""
    t = n.text
    if _has(t, "신혼", "신생아", "예비신혼") or prof.get("marital") in ("예비신혼", "신혼"):
        return SPECIAL_SUPPLY["신혼부부"]["pct"], "신혼부부 특별공급"
    if not prof.get("ever_owned"):
        return SPECIAL_SUPPLY["생애최초"]["pct"], "생애최초 특별공급"
    return None, ""


def _income_signal(n: Notice, prof: dict, kind: str) -> dict:
    """소득 신호등. 제외 근거가 아니라 참고 신호다."""
    ratio = income_ratio(prof.get("monthly_income"), prof.get("household_size", 1))
    if kind in ("오피스텔", "민영분양", "민간임대") and not _has(n.text, "특별공급"):
        return {"ratio": ratio, "limit_pct": None, "verdict": "무관",
                "note": f"{kind}은 일반공급 추첨제에 소득 요건이 없는 경우가 많다"}
    limit, label = _special_supply_limit(n, prof)
    return {
        "ratio": ratio,
        "limit_pct": limit,
        "label": label,
        "verdict": income_verdict(ratio, limit),
        "note": ("월소득을 입력하면 도시근로자 월평균소득 대비 비율을 계산한다"
                 if ratio is None else
                 f"가구원 {prof.get('household_size', 1)}인 기준 도시근로자 월평균소득의 {ratio}%"),
    }


def _asset_signal(prof: dict, kind: str) -> dict:
    """총자산·자동차가액 신호등."""
    guide = ASSET_GUIDE.get("공공분양" if kind == "공공분양" else
                            "임대" if kind == "임대" else "")
    if not guide:
        return {"verdict": "무관",
                "note": f"{kind}은 총자산 요건이 없는 경우가 많다"}
    assets = int(prof.get("assets") or 0)
    car = int(prof.get("car") or 0)
    over = []
    if assets > guide["assets"]:
        over.append(f"총자산 {assets:,}만원 > 기준 약 {guide['assets']:,}만원")
    if car > guide["car"]:
        over.append(f"자동차 {car:,}만원 > 기준 약 {guide['car']:,}만원")
    return {
        "verdict": "초과" if over else ("여유" if assets else "확인"),
        "over": over,
        "guide": guide,
        "note": ("공공 유형은 총자산·자동차가액 상한이 있다 — 연도·공고별로 다르니 공고문 확인"),
    }


def judge(n: Notice, prof: dict, today: date) -> Verdict:
    t = n.text
    kind = kind_of(n)
    a = calc_age(prof, today)
    marital = prof.get("marital", "미혼")
    single = marital == "미혼"
    newly = (marital in ("예비신혼", "신혼")
             or prof.get("newborn") or prof.get("single_parent"))
    v = Verdict(level="확인", kind=kind)

    def block(reason: str) -> Verdict:
        v.level = "제외"
        v.blockers.append(reason)
        return v

    # 0) 모집이 아닌 후속 안내는 목록에서 뺀다
    if NOISE.search(n.title) and not RECRUIT.search(n.title.split("결과")[-1]):
        v.level = "무시"
        v.blockers.append("모집이 아닌 후속 안내")
        return v

    # 1) 관심 지역 / 관심 유형
    regions = prof.get("regions") or []
    if regions and n.region and n.region not in regions:
        return block(f"관심 지역 아님 ({n.region})")
    if not (prof.get("want") or {}).get(kind, True):
        return block(f"관심 유형 아님 ({kind})")

    # 2) 청약통장
    v.account = account_need(n, kind)
    blocked = ACCOUNT_MODES.get(prof.get("account_mode", "상관없음"), ())
    if v.account in blocked:
        return block("청약통장을 쓰는 공고" if v.account == "사용"
                     else "청약통장 가입이 필요한 공고")
    if v.account in ("사용", "가입만") and not prof.get("has_account", True):
        return block("청약통장이 있어야 지원할 수 있는 공고")

    # 3) 무주택
    if not prof.get("homeless", True) and kind not in ("오피스텔", "민간임대"):
        return block("유주택자는 대부분 지원 불가")

    # 4) 대상 계층이 정해진 공고 — 구조적으로 안 되는 것만 뺀다
    if _has(t, "신혼", "신생아", "미리내집", "예비신혼"):
        if not newly:
            return block("신혼·신생아·예비신혼·한부모 대상 공고")
        v.level = "추천"
        v.reasons.append("신혼·신생아 대상에 해당")
    if _has(t, "고령자", "어르신", "은빛", "노인", "실버"):
        if a is None or a < 65:
            return block("만 65세 이상 고령자 대상 공고")
    if _has(t, "장애인") and not prof.get("disabled"):
        return block("장애인 대상 공고")
    if _has(t, "영구임대", "수급자", "주거취약", "쪽방") and not prof.get("low_income"):
        return block("수급자·저소득 취약계층 대상 공고")
    if _has(t, "다자녀", "다둥이") and int(prof.get("children") or 0) < 2:
        return block("다자녀(미성년 2명 이상) 대상 공고")
    if _has(t, "대학생", "희망하우징", "공공기숙사") and not prof.get("student"):
        return block("대학생 대상 공고")

    youth_ok = a is not None and 19 <= a <= 39
    if _has(t, "청년"):
        if a is not None and not youth_ok:
            return block(f"만 19~39세 청년 대상 공고 (현재 만 {a}세)")
        v.level = "추천"
        v.reasons.append("청년 대상" + (f" (만 {a}세)" if a is not None else ""))

    # 5) 유형별 판단
    if kind == "민영분양":
        if _has(t, "무순위", "잔여", "임의공급"):
            v.level = "추천" if prof.get("homeless", True) else v.level
            v.reasons.append("무순위/잔여세대 — 청약통장 없이 지원, 소득·자산 요건 없음")
            v.tips.append("해당 지역 거주 요건이 붙는 경우가 있다 — 공고문 확인")
        else:
            v.reasons.append("민영 APT — 추첨제 물량을 노려볼 수 있다")
            if a is not None and a < 30 and single:
                v.tips.append("만 30세 전 미혼은 무주택 가점이 0점 — 가점제보다 추첨제")
            if int(prof.get("account_years") or 0) < 2:
                v.tips.append("수도권 1순위는 통장 2년 이상이 필요한 경우가 많다")
    elif kind == "공공분양":
        if youth_ok and single and not prof.get("ever_owned"):
            v.level = "추천"
            v.reasons.append("청년·생애최초 특별공급 대상 구조 (만 19~39·미혼·무주택)")
        elif not prof.get("ever_owned"):
            v.reasons.append("생애최초·일반공급 가능성")
        if int(prof.get("account_years") or 0) < 2:
            v.tips.append("수도권 공공분양 1순위는 통장 2년·24회 이상 (특별공급은 6개월)")
        if prof.get("household") != "단독세대주" and not prof.get("household_homeless", True):
            v.tips.append("세대원 중 유주택자가 있으면 일반공급에 불리 — 세대분리 검토")
    elif kind in ("오피스텔", "민간임대"):
        v.reasons.append(f"{kind} — 청약통장·무주택 요건이 느슨하다")
        v.tips.append("분양가·관리비 구조가 APT 와 다르다 — 수익/실거주 목적 확인")
    else:  # 임대
        v.reasons.append("임대 공고 — 소득·자산 기준을 공고문에서 확인")

    # 6) 소득·자산 신호 (제외 근거로 쓰지 않는다)
    v.income = _income_signal(n, prof, kind)
    v.asset = _asset_signal(prof, kind)

    if v.income.get("verdict") == "초과":
        if v.level == "추천":
            v.level = "확인"
        v.tips.append(
            f"소득이 {v.income['label']} 상한({v.income['limit_pct']}%)을 넘는다"
            f" — 현재 {v.income['ratio']}%. 일반공급/추첨제는 가능할 수 있다")
    elif v.income.get("verdict") == "경계":
        v.tips.append(f"소득이 상한({v.income['limit_pct']}%)에 가깝다 — 현재 {v.income['ratio']}%")

    if v.asset.get("verdict") == "초과":
        if v.level == "추천":
            v.level = "확인"
        v.tips.extend(f"{o} — 공공 유형은 제한될 수 있다" for o in v.asset["over"])

    if not v.reasons:
        v.reasons.append("자격 요건을 공고문에서 확인")
    return v
