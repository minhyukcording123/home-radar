"""중복 공고 제거와 정정 공고 감지.

수집원을 늘리면 같은 공고가 두 번 들어온다. 예를 들어 LH 신혼희망타운은
청약홈에도 올라오고, SH 공고가 서울주거포털과 다른 경로로 동시에 보일 수 있다.

중복 판단은 **보수적으로** 한다. 잘못 합치면 실제로 다른 공고가 사라져
사용자가 기회를 놓친다. 그래서 다음 두 가지만 중복으로 본다.

  1. 같은 수집원 안에서 제목·지역·접수시작·접수마감이 **모두** 같을 때
  2. 다른 수집원끼리 제목·지역이 같고 접수시작이 3일 이내로 붙어 있을 때

정정 공고는 제목의 '정정·변경·연장·취소·재공고' 표시로 찾고,
표시를 떼어낸 제목이 같은 원본 공고를 찾아 연결한다. 원본을 지우지는 않는다.
"""
from __future__ import annotations

import re
from datetime import date
from typing import Optional

from .models import Notice

# 수집원 신뢰 순위 — 숫자가 작을수록 우선 (중복일 때 이쪽을 남긴다)
SOURCE_RANK = {
    "applyhome_api": 0,   # 공식 오픈API
    "lh": 1,              # 공식 오픈API
    "applyhome": 2,       # 공개 화면이지만 일정이 가장 정확하다
    "sh": 3,
    "youth": 4,
    "ish": 5,
}

# 정정 공고 표시.
# '취소'를 그냥 찾으면 '(조합원 취소분)' 같은 **공급 유형 설명**이 전부 걸린다.
# 조합원 취소분·계약취소분은 정상적인 무순위/잔여 공급이지 정정 공고가 아니다.
# 그래서 공고 자체를 손본다는 맥락이 있을 때만 정정으로 본다.
CORRECTION_PATTERNS = [
    ("정정", re.compile(r"정정\s*공고|공고\s*정정|정정\s*안내")),
    ("변경", re.compile(r"변경\s*공고|공고\s*변경|일정\s*변경|기간\s*변경")),
    ("연장", re.compile(r"기간\s*연장|연장\s*공고|접수\s*연장")),
    ("취소", re.compile(r"공고\s*취소|모집\s*취소|취소\s*공고")),
    ("재공고", re.compile(r"재공고|재모집")),
    ("추가모집", re.compile(r"추가\s*모집")),
]
# 위 표시를 제목에서 떼어낼 때 쓴다
_STRIP_CORRECTION = re.compile(
    r"정정\s*공고|공고\s*정정|정정\s*안내|변경\s*공고|공고\s*변경|일정\s*변경"
    r"|기간\s*변경|기간\s*연장|연장\s*공고|접수\s*연장|공고\s*취소|모집\s*취소"
    r"|취소\s*공고|재공고|재모집|추가\s*모집")

# 제목 정규화에서 떼어낼 것들
_PAREN = re.compile(r"[\(\[（][^\)\]）]*[\)\]）]")
_ORDINAL = re.compile(r"\d+\s*차")
_NOISE_WORDS = re.compile(
    r"(모집공고|입주자\s*모집|공고문|공고|모집|안내|\d+년도?|제?\d+회차?)")
_NON_WORD = re.compile(r"[^0-9A-Za-z가-힣]")


def normalize_title(title: str) -> str:
    """비교용 제목 — 괄호·차수·군더더기·공백·기호를 떼어낸다."""
    t = _PAREN.sub(" ", title or "")
    t = _ORDINAL.sub(" ", t)
    t = _NOISE_WORDS.sub(" ", t)
    return _NON_WORD.sub("", t)


def correction_marker(title: str) -> str:
    """제목에 붙은 정정 표시. 없으면 빈 문자열.

    '(조합원 취소분)' 처럼 공급 유형을 설명하는 말은 정정으로 보지 않는다.
    """
    t = title or ""
    for label, pat in CORRECTION_PATTERNS:
        if pat.search(t):
            return label
    return ""


def _completeness(n: Notice) -> int:
    """정보가 더 많이 찬 쪽을 남기기 위한 점수."""
    score = 0
    for f in ("begin", "end", "announce", "address", "households",
              "price", "doc_url", "phone"):
        if getattr(n, f, None):
            score += 1
    if n.areas:
        score += 2
    if n.detail_loaded:
        score += 2
    return score


def _prefer(a: Notice, b: Notice) -> tuple[Notice, Notice]:
    """(남길 것, 흡수될 것) — 수집원 신뢰도 우선, 같으면 정보가 많은 쪽."""
    ra = SOURCE_RANK.get(a.source_id, 99)
    rb = SOURCE_RANK.get(b.source_id, 99)
    if ra != rb:
        return (a, b) if ra < rb else (b, a)
    return (a, b) if _completeness(a) >= _completeness(b) else (b, a)


def _days_apart(x: Optional[date], y: Optional[date]) -> Optional[int]:
    if x is None or y is None:
        return None
    return abs((x - y).days)


def dedupe(notices: list[Notice]) -> tuple[list[Notice], list[dict]]:
    """중복을 흡수한 목록과 흡수 기록을 돌려준다."""
    kept: list[Notice] = []
    merged: list[dict] = []

    # 1) 같은 수집원 · 제목·지역·시작·마감이 모두 같은 것
    exact: dict[tuple, Notice] = {}
    for n in notices:
        k = (n.source_id, normalize_title(n.title), n.region, n.begin, n.end)
        prev = exact.get(k)
        if prev is None:
            exact[k] = n
            continue
        keep, drop = _prefer(prev, n)
        exact[k] = keep
        drop.dup_of = keep.key
        merged.append({"dropped": drop.key, "kept": keep.key,
                       "reason": "같은 수집원 · 제목·지역·접수기간 동일",
                       "title": drop.title})
    stage1 = list(exact.values())

    # 2) 다른 수집원끼리 제목·지역이 같고 접수시작이 3일 이내
    buckets: dict[tuple, list[Notice]] = {}
    for n in stage1:
        buckets.setdefault((normalize_title(n.title), n.region), []).append(n)

    dropped_keys: set[str] = set()
    for group in buckets.values():
        if len(group) < 2:
            continue
        group.sort(key=lambda x: SOURCE_RANK.get(x.source_id, 99))
        anchor = group[0]
        for other in group[1:]:
            if other.source_id == anchor.source_id:
                continue
            gap = _days_apart(anchor.begin, other.begin)
            # 양쪽 다 시작일이 없으면 게시일로 비교한다
            if gap is None:
                gap = _days_apart(anchor.posted, other.posted)
            if gap is not None and gap > 3:
                continue
            keep, drop = _prefer(anchor, other)
            drop.dup_of = keep.key
            dropped_keys.add(drop.key)
            merged.append({"dropped": drop.key, "kept": keep.key,
                           "reason": f"다른 수집원 중복 ({drop.source} → {keep.source})",
                           "title": drop.title})

    kept = [n for n in stage1 if n.key not in dropped_keys]
    return kept, merged


def link_corrections(notices: list[Notice]) -> list[dict]:
    """정정 공고를 원본과 연결한다. 목록에서 지우지는 않는다."""
    by_base: dict[tuple, list[Notice]] = {}
    for n in notices:
        if correction_marker(n.title):
            continue
        by_base.setdefault((normalize_title(n.title), n.region), []).append(n)

    found: list[dict] = []
    for n in notices:
        marker = correction_marker(n.title)
        if not marker:
            continue
        # 정정 표시를 뺀 제목으로 원본을 찾는다
        stripped = _STRIP_CORRECTION.sub(" ", n.title)
        cands = by_base.get((normalize_title(stripped), n.region)) or []
        cands = [c for c in cands if c.key != n.key]
        if not cands:
            n.extra["correction"] = {"marker": marker, "base": None}
            found.append({"key": n.key, "marker": marker, "base": None,
                          "title": n.title})
            continue
        base = sorted(cands, key=lambda c: (c.posted or date.min))[-1]
        n.correction_of = base.key
        n.extra["correction"] = {"marker": marker, "base": base.key}
        base.extra.setdefault("corrected_by", []).append(n.key)
        found.append({"key": n.key, "marker": marker, "base": base.key,
                      "title": n.title})
    return found
