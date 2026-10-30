"""조건별 판정 단위(Check)와 종합 규칙.

공고 하나에 대해 "무주택은 충족, 청약통장 가입기간은 미확인, 연령은 불충족" 처럼
**조건마다 따로** 판정하고, 그 모음으로 추천/확인/제외를 정한다.
그래서 화면에서 왜 추천인지, 왜 제외인지 조건 단위로 보여줄 수 있다.

종합 규칙 (순서가 곧 우선순위다):

  1. 관심 지역·유형 필터에 걸리면        → 제외 (자격 문제가 아니라 내 설정)
  2. 공식 기준으로 '불충족'이 하나라도    → 제외
  3. '미확인'이 하나라도 있으면           → 확인 필요
  4. 전부 '충족'                          → 추천

핵심 안전장치 두 개:

  * **샘플 기준(official=False)은 충족도 불충족도 만들 수 없다.**
    항상 '미확인'으로 내려앉는다. 그래서 샘플 소득·자산 규칙만 보고
    적격을 확정하거나 제외해 버리는 일이 구조적으로 불가능하다.

  * **정보가 없으면 '미확인'이다.** 비어 있는 입력을 충족으로 보지 않는다.
    따라서 자료가 부족하면 절대 '추천'까지 올라가지 않는다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .rules import RULES_VERSION, source_of

MET = "충족"
UNKNOWN = "미확인"
UNMET = "불충족"

# 조건의 성격
ELIGIBILITY = "자격"    # 공고가 요구하는 자격 요건
FILTER = "내설정"        # 내가 걸어둔 관심 지역·유형·분양가 필터
SIGNAL = "참고"          # 참고 신호 (샘플 기준 등) — 판정을 확정하지 않는다

LEVEL_RECOMMEND = "추천"
LEVEL_CHECK = "확인"
LEVEL_EXCLUDE = "제외"
LEVEL_IGNORE = "무시"


@dataclass
class Check:
    """조건 하나에 대한 판정."""
    name: str                       # "무주택", "연령", "청약통장 가입기간"
    status: str                     # 충족 | 미확인 | 불충족
    detail: str = ""                # 사람이 읽는 설명
    source_key: str = "notice"      # RULE_SOURCES 키
    category: str = ELIGIBILITY     # 자격 | 내설정 | 참고
    todo: str = ""                  # 확인하려면 무엇을 해야 하는지

    def __post_init__(self):
        src = source_of(self.source_key)
        # 샘플 기준은 판정을 확정할 수 없다 — 항상 '미확인'으로 내린다
        if not src.get("official") and self.status in (MET, UNMET):
            if not self.todo:
                self.todo = "공고문의 실제 기준으로 확인 필요"
            self.status = UNKNOWN
            self.category = SIGNAL

    @property
    def official(self) -> bool:
        return bool(source_of(self.source_key).get("official"))

    def to_dict(self) -> dict:
        src = source_of(self.source_key)
        return {
            "name": self.name,
            "status": self.status,
            "detail": self.detail,
            "category": self.category,
            "todo": self.todo,
            "source": {
                "key": self.source_key,
                "label": src.get("label", self.source_key),
                "url": src.get("url", ""),
                "asof": src.get("asof"),
                "official": bool(src.get("official")),
                "note": src.get("note", ""),
            },
        }


@dataclass
class Verdict:
    """공고 하나에 대한 종합 판정."""
    level: str = LEVEL_CHECK
    kind: str = "민영분양"
    tags: list[str] = field(default_factory=list)   # 청년, 신혼부부, 무순위 …
    checks: list[Check] = field(default_factory=list)
    account: str = "확인"
    account_label: str = ""
    rules_version: str = RULES_VERSION

    # 참고 신호 (판정에 쓰지 않는다)
    income: dict = field(default_factory=dict)
    asset: dict = field(default_factory=dict)

    def add(self, check: Check) -> "Verdict":
        self.checks.append(check)
        return self

    # ---- 조건 모음 조회 ----
    def by_status(self, status: str) -> list[Check]:
        return [c for c in self.checks if c.status == status]

    @property
    def unmet(self) -> list[Check]:
        return self.by_status(UNMET)

    @property
    def unknown(self) -> list[Check]:
        return self.by_status(UNKNOWN)

    @property
    def met(self) -> list[Check]:
        return self.by_status(MET)

    def settle(self) -> str:
        """조건 모음에서 최종 등급을 정한다."""
        if self.level == LEVEL_IGNORE:
            return self.level
        # 1) 내 설정 필터에 걸린 것 먼저 — 자격 문제와 구분해서 보여주기 위해
        if any(c.status == UNMET and c.category == FILTER for c in self.checks):
            self.level = LEVEL_EXCLUDE
        # 2) 공식 기준 불충족
        elif any(c.status == UNMET and c.official for c in self.checks):
            self.level = LEVEL_EXCLUDE
        # 3) 미확인이 하나라도 있으면 확정하지 않는다
        elif self.unknown:
            self.level = LEVEL_CHECK
        else:
            self.level = LEVEL_RECOMMEND
        return self.level

    @property
    def summary(self) -> str:
        if self.level == LEVEL_EXCLUDE:
            u = self.unmet
            return u[0].detail or u[0].name if u else "지원 요건에 맞지 않음"
        if self.level == LEVEL_RECOMMEND:
            return f"확인된 조건 {len(self.met)}개 모두 충족"
        return (f"충족 {len(self.met)} · 확인 필요 {len(self.unknown)}"
                + (f" · 불충족 {len(self.unmet)}" if self.unmet else ""))

    def to_dict(self) -> dict:
        return {
            "level": self.level,
            "kind": self.kind,
            "tags": self.tags,
            "summary": self.summary,
            "rules_version": self.rules_version,
            "account": self.account,
            "account_label": self.account_label,
            "counts": {
                MET: len(self.met),
                UNKNOWN: len(self.unknown),
                UNMET: len(self.unmet),
            },
            "checks": [c.to_dict() for c in self.checks],
            "income": self.income,
            "asset": self.asset,
            # 화면에서 바로 쓰도록 분리해 둔다
            "unmet": [c.to_dict() for c in self.unmet],
            "unknown": [c.to_dict() for c in self.unknown],
            "met": [c.to_dict() for c in self.met],
        }


# ---- 자주 쓰는 Check 생성 헬퍼 ----
def met(name: str, detail: str, source_key: str = "profile", **kw) -> Check:
    return Check(name=name, status=MET, detail=detail, source_key=source_key, **kw)


def unmet(name: str, detail: str, source_key: str = "notice-title", **kw) -> Check:
    return Check(name=name, status=UNMET, detail=detail, source_key=source_key, **kw)


def unknown(name: str, detail: str, source_key: str = "notice",
            todo: str = "", **kw) -> Check:
    return Check(name=name, status=UNKNOWN, detail=detail,
                 source_key=source_key, todo=todo, **kw)


def filter_block(name: str, detail: str) -> Check:
    return Check(name=name, status=UNMET, detail=detail,
                 source_key="profile", category=FILTER)


def missing(name: str, what: str) -> Check:
    """입력이 비어 있어 판단할 수 없을 때."""
    return Check(name=name, status=UNKNOWN,
                 detail=f"{what}을 입력하지 않아 판단할 수 없다",
                 source_key="profile",
                 todo=f"내 조건에서 {what}을 입력하면 판정이 정확해진다")


def optional_check(name: str, status: str, detail: str,
                   source_key: str, **kw) -> Check:
    return Check(name=name, status=status, detail=detail,
                 source_key=source_key, **kw)
