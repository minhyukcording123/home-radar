"""공고 공통 레코드.

cheongyak-auto(MIT) 의 `cheong/models.py` Notice 설계를 각색했다.
원본과 다른 점:
  - 캘린더가 '일정 행' 단위로 오므로 events 를 두고 공고 단위로 묶는다
  - 접수 시작 / 접수 마감 / 당첨 발표를 따로 들고 D-day 도 따로 계산한다
  - 공급금액을 숫자(price_min/price_max)로 들고 있어 분양가 필터와 억 단위 표시에 쓴다
  - 공급면적(areas)을 담는다
  - source_id 로 수집원(청약홈/LH/SH)을 구분한다
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Optional

from .util import iso

# 청약홈 캘린더 RCEPT_SE (접수 구분) 코드.
# 프런트 레전드와 실제 수집 데이터 census 로 의미를 확정했다.
RCEPT_SE = {
    "01": "특별공급",
    "02": "1순위",
    "03": "2순위",
    "04": "공공지원민간임대",
    "05": "오피스텔/도시형",
    "06": "무순위/잔여",
    "07": "무순위/잔여",
    "08": "특별공급",
    "09": "1순위",
    "10": "2순위",
    "11": "무순위/잔여",
    # "12" 는 공고가 붙지 않은 빈 행(HOUSE_NM = null)이라 수집 단계에서 버린다
}

# HOUSE_SECD (주택 구분) → 상세 조회 화면이 달라진다
HOUSE_SECD = {
    "01": "APT",
    "02": "오피스텔/도시형",
    "03": "민간임대",
    "04": "무순위/잔여",
    "05": "민간임대",
    "06": "무순위/잔여",
    "09": "APT",
    "11": "무순위/잔여",
}


@dataclass
class Event:
    """공고 하나에 붙는 개별 일정 (특별공급 접수일, 1순위 접수일 …)."""
    date: date
    code: str
    label: str

    def to_dict(self):
        return {"date": iso(self.date), "code": self.code, "label": self.label}


@dataclass
class Area:
    """공급 면적 한 줄 (주택형 / 전용면적 / 세대수)."""
    name: str = ""          # 주택형 (084.9876A)
    exclusive: str = ""     # 전용면적
    units: str = ""         # 공급 세대수

    def to_dict(self):
        return {"name": self.name, "exclusive": self.exclusive, "units": self.units}


@dataclass
class Notice:
    source: str                     # 청약홈 / LH / SH / 청년안심
    uid: str                        # 수집처 내 유일 id
    title: str
    source_id: str = "applyhome"    # 수집원 식별자 (registry 키)
    category: str = ""              # 접수 유형 모음
    region: str = ""                # 시도
    address: str = ""
    house_secd: str = "01"
    house_manage_no: str = ""
    pblanc_no: str = ""

    posted: Optional[date] = None    # 모집공고일
    begin: Optional[date] = None     # 접수 시작
    end: Optional[date] = None       # 접수 마감
    announce: Optional[date] = None  # 당첨자 발표

    households: str = ""
    url: str = ""
    doc_url: str = ""                # 모집공고문 PDF
    phone: str = ""
    price: str = ""                  # 사람이 읽는 공급금액 문자열
    price_min: Optional[int] = None  # 만원
    price_max: Optional[int] = None  # 만원
    move_in: str = ""
    status: str = ""

    events: list[Event] = field(default_factory=list)
    areas: list[Area] = field(default_factory=list)
    detail_loaded: bool = False
    dup_of: str = ""                 # 중복 제거로 흡수된 원본 key
    correction_of: str = ""          # 정정 공고라면 원본 key
    extra: dict = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.source}:{self.uid}"

    @property
    def text(self) -> str:
        """판정용으로 제목·유형을 합친 문자열."""
        return f"{self.title} {self.category}"

    def is_closed(self, today: date) -> bool:
        """원본 cheongyak-auto 의 보수적 마감 추정을 가져왔다."""
        if self.end is not None:
            return self.end < today
        if self.status and any(k in self.status for k in ("마감", "완료", "종료")):
            return True
        if self.announce is not None and self.announce < today:
            return True
        if self.begin is not None and (today - self.begin).days > 7:
            return True
        if self.begin is None and self.posted is not None and (today - self.posted).days > 21:
            return True
        return False

    # ---- D-day: 시작과 마감을 따로 센다 ----
    def days_to_begin(self, today: date) -> Optional[int]:
        return None if self.begin is None else (self.begin - today).days

    def days_left(self, today: date) -> Optional[int]:
        """접수 마감까지 남은 일수."""
        return None if self.end is None else (self.end - today).days

    def days_to_announce(self, today: date) -> Optional[int]:
        return None if self.announce is None else (self.announce - today).days

    def phase(self, today: date) -> str:
        """예정 / 접수중 / 마감 / 발표대기 / 미정."""
        if self.begin is None and self.end is None:
            return "미정"
        if self.begin is not None and today < self.begin:
            return "예정"
        if self.end is not None and today > self.end:
            if self.announce is not None and today <= self.announce:
                return "발표대기"
            return "마감"
        return "접수중"

    def to_dict(self):
        d = asdict(self)
        for k in ("posted", "begin", "end", "announce"):
            d[k] = iso(d[k])
        d["events"] = [e.to_dict() for e in self.events]
        d["areas"] = [a.to_dict() for a in self.areas]
        d["key"] = self.key
        return d
