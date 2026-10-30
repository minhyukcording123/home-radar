"""공고 공통 레코드.

cheongyak-auto(MIT) 의 `cheong/models.py` Notice 설계를 각색했다.
원본과 다른 점:
  - 청약홈 캘린더는 '일정 행' 단위로 오므로 events 를 추가해 공고 단위로 묶는다
  - 상세 조회로 채워지는 필드(detail)를 분리해 지연 로딩할 수 있게 했다
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Optional

from .util import iso

# 청약홈 캘린더 RCEPT_SE (접수 구분) 코드.
# 프런트 레전드와 실제 수집 데이터(2026-10-30 기준 census)로 의미를 확정했다.
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
class Notice:
    source: str                     # 청약홈
    uid: str                        # 수집처 내 유일 id
    title: str
    category: str = ""              # 접수 유형 모음 (특별공급, 1순위 …)
    region: str = ""                # 시도
    address: str = ""
    house_secd: str = "01"
    house_manage_no: str = ""
    pblanc_no: str = ""

    posted: Optional[date] = None   # 모집공고일
    begin: Optional[date] = None    # 접수 시작
    end: Optional[date] = None      # 접수 마감
    announce: Optional[date] = None # 당첨자 발표

    households: str = ""
    url: str = ""
    doc_url: str = ""               # 모집공고문 PDF
    phone: str = ""
    price: str = ""
    move_in: str = ""
    status: str = ""

    events: list[Event] = field(default_factory=list)
    detail_loaded: bool = False
    extra: dict = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.source}:{self.uid}"

    @property
    def text(self) -> str:
        """판정용으로 제목·유형을 합친 문자열."""
        return f"{self.title} {self.category}"

    def is_closed(self, today: date) -> bool:
        """원본 cheongyak-auto 의 보수적 마감 추정을 그대로 가져왔다."""
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

    def days_left(self, today: date) -> Optional[int]:
        """접수 마감까지 남은 일수. 마감일을 모르면 None."""
        if self.end is None:
            return None
        return (self.end - today).days

    def to_dict(self):
        d = asdict(self)
        for k in ("posted", "begin", "end", "announce"):
            d[k] = iso(d[k])
        d["events"] = [e.to_dict() for e in self.events]
        d["key"] = self.key
        return d
