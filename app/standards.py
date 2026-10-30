"""2026년 공식 소득·자산 기준 데이터와 적용 엔진.

rules.py 의 샘플 기준을 대체하는 **공식 기준표**다. 원칙:

  * 수치는 전부 공식 포털(마이홈·보건복지부·LH·서울시)에서 확인한 것만 적는다.
    확인하지 못한 것은 official=False 로 남겨 checks.py 의 안전장치가
    자동으로 '미확인'을 만들게 한다. 수치를 창작하지 않는다.

  * **기저 소득표와 가구원수 완화율을 분리해서 들고 다닌다.**
    마이홈 표의 "100%" 열은 1·2인 가구 완화(+20%p/+10%p)가 이미 반영된
    값이라(1인 4,576,036 = 3,813,363 × 1.2), 표의 완성값을 그대로 쓰면
    70%·130% 같은 다른 퍼센트에 완화를 중복/누락 적용하게 된다.

  * 완화·맞벌이 적용 여부를 공식 페이지에서 확정하지 못한 유형은
    relax=None 으로 두고, 판정은 [확실한 하한 lo, 가능한 상한 hi] 밴드로
    한다: lo 이하면 어느 해석으로도 '충족', hi 초과면 어느 해석으로도
    '불충족', 그 사이는 '미확인'. (잘못 걸러서 놓치는 손해 > 알림 하나)

이 모듈은 app 내부 다른 모듈을 import 하지 않는다 — rules.py 가 여기의
출처 레지스트리를 끌어가 RULE_SOURCES 에 합치므로 순환을 피해야 한다.
"""
from __future__ import annotations

from typing import Optional

# ================================================================ 기저 소득표
# 도시근로자 가구원수별 월평균소득 100% — **완화 미반영 기저값** (원/월)
# 전년도 소득이며 2026년도 공고에 적용된다.
# 검산: 국민임대 70% 열 3인 5,717,900 = 8,168,429 × 0.7 ✓
#       국민임대 1인 90% 3,432,027 = 3,813,363 × 0.9 ✓
URBAN_WORKER_INCOME_2026: dict[int, int] = {
    1: 3_813_363,
    2: 5_866_270,
    3: 8_168_429,
    4: 8_802_202,
    5: 9_326_985,
    6: 9_906_263,
    7: 10_485_541,   # 7인 이상
}

# 2026년 기준 중위소득 (원/월) — 통합공공임대 판정용.
# 보건복지부 고시 제2025-135호(2026-01-01 시행). 복지부 정책 페이지에서
# 1~7인 전 구간을 직접 확인했다 (출처 레지스트리 "mohw-median-income").
MEDIAN_INCOME_2026: dict[int, int] = {
    1: 2_564_238,
    2: 4_199_292,
    3: 5_359_036,
    4: 6_494_738,
    5: 7_556_719,
    6: 8_555_952,
    7: 9_515_150,
}

# 가구원수 완화율 (%p) — 1인 +20%p, 2인 +10%p.
# 마이홈 국민임대·통합공공임대 페이지가 명시한 완화 구조다.
HOUSEHOLD_RELAX_PP: dict[int, int] = {1: 20, 2: 10}

_BASE_TABLES = {
    "urban": URBAN_WORKER_INCOME_2026,    # 가구원수별 도시근로자 소득
    "urban1": URBAN_WORKER_INCOME_2026,   # 1인 가구 표 고정 (청년매입 3순위)
    "median": MEDIAN_INCOME_2026,         # 기준 중위소득
}
_BASE_LABEL = {
    "urban": "도시근로자 월평균소득",
    "urban1": "도시근로자 1인 가구 월평균소득",
    "median": "기준 중위소득",
}


# ================================================================ 출처 레지스트리
# rules.RULE_SOURCES 에 병합된다. checks.Check 가 official 여부를 여기서 읽어
# official=False 근거로는 충족·불충족을 확정하지 못하게 막는다.
_MYHOME = "https://www.myhome.go.kr/hws/portal/cont"
STANDARD_SOURCES: dict[str, dict] = {
    "myhome-urban-income": {
        "label": "도시근로자 가구원수별 월평균소득 (마이홈, 2026년도 적용)",
        "url": f"{_MYHOME}/selectNationalRentalHouseView.do",
        "asof": "2026년도 적용기준",
        "official": True,
        "note": "전년도 소득 기준. 표의 100% 열은 1·2인 완화가 반영된 값이라 "
                "기저값과 완화율을 분리해 저장했다",
    },
    "myhome-national-rental": {
        "label": "국민임대 입주자격 (마이홈)",
        "url": f"{_MYHOME}/selectNationalRentalHouseView.do",
        "asof": "2026년도 적용기준",
        "official": True,
        "note": "소득 70% (1인 90%·2인 80%, 전용 60㎡ 초과 100%) · "
                "총자산 3억 4,500만원 · 자동차 4,542만원",
    },
    "myhome-happy-house": {
        "label": "행복주택 입주자격 (마이홈)",
        "url": f"{_MYHOME}/selectHappyHouseView.do",
        "asof": "2026년도 적용기준",
        "official": True,
        "note": "계층별 소득 100% (신혼 맞벌이 120%) · 계층별 총자산·자동차 상한",
    },
    "myhome-integrated-rental": {
        "label": "통합공공임대 입주자격 (마이홈)",
        "url": f"{_MYHOME}/selectIntegratedPubRentalHouseView.do",
        "asof": "소득 '26.1.1. 공고부터 · 자산 '26.2.27. 공고부터",
        "official": True,
        "note": "기준 중위소득 기반 — 우선공급 100%(1인 120%·2인 110%), "
                "일반공급 150%(1인 170%·2인 160%) · 총자산 3억 4,500만원 · "
                "자동차 4,542만원. 적용 시점 전 공고는 공고문 기준",
    },
    "mohw-median-income": {
        "label": "2026년 기준 중위소득 (보건복지부)",
        "url": "https://www.mohw.go.kr/menu.es?mid=a10708010900",
        "asof": "2026-01-01",
        "official": True,
        "note": "보건복지부 고시 제2025-135호. 복지부 기초생활보장 정책 "
                "페이지에서 1~7인 월액을 확인했다",
    },
    "myhome-newlywed-town": {
        "label": "신혼희망타운 입주자격 (마이홈)",
        "url": f"{_MYHOME}/selectNewlywedHopeTownView.do",
        "asof": "2026년도 적용기준",
        "official": True,
        "note": "소득 130% (맞벌이 200%) · 총자산 3억 6,200만원 · "
                "혼인 7년 이내 또는 6세 이하 자녀. 주택가격이 총자산기준 "
                "초과 시 전용 모기지 30% 이상 가입",
    },
    "myhome-public-rental-5-10": {
        "label": "5년·10년·50년 공공임대 입주자격 (마이홈)",
        "url": f"{_MYHOME}/selectPubRentalHouseView.do",
        "asof": "2026년도 적용기준",
        "official": True,
        "note": "일반(60㎡ 이하) 100% / 신혼 130%(배우자 소득 시 140%) / "
                "생애최초 130% / 다자녀·노부모 120% · 부동산 2억 1,550만원 · "
                "자동차 4,542만원",
    },
    "lh-youth-purchase": {
        "label": "청년매입임대 입주자격 (LH)",
        "url": "https://www.lh.or.kr/menu.es?mid=a10401020200",
        "asof": None,
        "official": True,
        "note": "1순위 수급자·차상위·한부모 / 2순위 본인+부모 100% + 국민임대 "
                "자산기준 / 3순위 본인 100%(1인 가구 기준) + 행복주택 청년 "
                "자산기준. 페이지에 적용연도 미표기 — 금액은 참조 기준의 "
                "2026년 값을 쓰되 공고문이 우선한다",
    },
    "lh-newlywed-purchase": {
        "label": "신혼·신생아 매입임대 입주자격 (LH)",
        "url": "https://www.lh.or.kr/menu.es?mid=a10401020600",
        "asof": None,
        "official": True,
        "note": "Ⅰ형 70%(배우자 소득 시 90%) + 국민임대 자산기준 / "
                "Ⅱ형 100%(120%), 5순위 120%(140%) + 행복주택 신혼·신혼희망타운 "
                "자산기준. 페이지에 적용연도 미표기 — 공고문이 우선한다",
    },
    "newhome-public-sale": {
        "label": "공공분양(뉴:홈) 자격 (마이홈 — 미확정)",
        "url": f"{_MYHOME}/selectNHTQualificationView.do",
        "asof": None,
        "official": False,
        "note": "페이지에서 특공 %(신혼·생애최초 130, 다자녀·노부모 120, "
                "신생아 140)와 부동산 2억 1,550만원·자동차 기준을 봤지만 "
                "나눔형·선택형·일반형별 기준과 적용연도를 확정하지 못했다. "
                "유형별 기준이 달라(나눔형 청년특공 등) 공고문 확인 필요",
    },
    # ---- 뉴:홈 유형별 — 마이홈 유형별 페이지에서 직접 확인 ----
    # 공고가 유형(나눔형·선택형·일반형)을 밝힌 경우에만 쓴다. 유형 미상이면
    # 위의 newhome-public-sale(official=False)이 그대로 남아 '미확인'을 만든다.
    "myhome-newhome-share": {
        "label": "뉴:홈 나눔형(이익공유형 분양주택) 입주자격 (마이홈)",
        "url": f"{_MYHOME}/selectNewHomeShareTypeView.do",
        "asof": None,
        "official": True,
        "note": "일반공급 100% / 신혼 130(맞벌이 140) / 생애최초 130(우선 100) / "
                "신생아 140(우선 100) / 청년 140(본인·1인 가구 기준) · "
                "순자산 3억 6,200만원, 청년특공은 본인 2억 7,600만원+부모 "
                "10억 3,500만원. 적용연도 미표기 — 청년 한도 예시값이 2026년 "
                "소득표와 일치함을 확인했다. 공고문이 우선한다",
    },
    "myhome-newhome-choice": {
        "label": "뉴:홈 선택형(6년 분양전환 공공임대) 입주자격 (마이홈)",
        "url": f"{_MYHOME}/selectNewHomeChoiceTypeView.do",
        "asof": None,
        "official": True,
        "note": "일반공급 100% / 신혼 130(맞벌이 140, 우선 100) / "
                "생애최초 130(우선 100) / 다자녀·노부모 120 / 청년 140(본인) · "
                "순자산 3억 6,200만원, 청년특공 본인 2억 7,600만원+부모 "
                "10억 3,500만원. 적용연도 미표기 — 공고문이 우선한다",
    },
    "myhome-newhome-general": {
        "label": "뉴:홈 일반형(공공분양주택) 입주자격 (마이홈)",
        "url": f"{_MYHOME}/selectNHTQualificationView.do",
        "asof": None,
        "official": True,
        "note": "일반공급 100%(전용 60㎡ 이하만 소득·자산 검사) / 신혼 130 / "
                "생애최초 130 / 다자녀·노부모 120 / 신생아 140 · "
                "부동산 2억 1,550만원·자동차 4,542만원. 맞벌이 완화(최대 "
                "200%)의 적용 범위와 적용연도는 페이지에서 확정하지 못했다 "
                "— 자동차 한도가 2026년 값(4,542만원)과 일치. 공고문이 우선",
    },
    "myhome-longterm-lease": {
        "label": "장기전세주택 입주자격 (마이홈)",
        "url": f"{_MYHOME}/selectlongTermLeaseHouseView.do",
        "asof": "2026년도 적용기준",
        "official": True,
        "note": "전용 60㎡ 이하 100%(1인 120%·2인 110%), 60~85㎡ 120%, "
                "우선공급 50㎡ 미만 50%·50~60㎡ 70% · 총자산 서울 6억 "
                "6,200만원·경기 4억 1,700만원 · 자동차 4,542만원. "
                "85㎡ 초과분의 소득 기준은 페이지에 없어 미확정",
    },
    "myhome-permanent-rental": {
        "label": "영구임대주택 입주자격 (마이홈)",
        "url": f"{_MYHOME}/selectPermanentRHView.do",
        "asof": None,
        "official": True,
        "note": "1순위 생계·의료급여 수급자·한부모 등 / 국가유공자 등 70% "
                "(1인 90%·2인 80%) / 2순위 50%(1인 70%·2인 60%) / 일부 "
                "장애인 100%. 자산 요건의 존재는 명시돼 있으나 금액이 "
                "페이지에 없어 자산은 미확정 — 공고문 확인",
    },
    "lh-jeonse-rental": {
        "label": "전세임대 입주자격 (LH 전세임대포털)",
        "url": "https://jeonse.lh.or.kr/jw/li/lfstsRentBsnsGuidance.do",
        "asof": None,
        "official": True,
        "note": "기존주택 1순위 수급자·한부모 등, 2순위 소득 50% / 청년 "
                "2순위 본인+부모 100%·3순위 본인 100%(1인 가구 기준) / "
                "신혼·신생아 Ⅰ형 70%(배우자 소득 시 90%)·Ⅱ형 100%(120%). "
                "자산은 영구·국민임대·행복주택 기준 준용 — 페이지의 금액은 "
                "과년도 값이라 참조 기준의 2026년 값을 쓴다. 공고문이 우선",
    },
    "seoul-youth-safe-housing": {
        "label": "청년안심주택 입주자격 (서울시)",
        "url": "https://soco.seoul.go.kr/youth/pgm/home/yohome/"
               "supportYouth1.do?menuNo=400039",
        "asof": "2026년 기준",
        "official": True,
        "note": "민간임대 특별공급은 세대 소득 120%(완화 없음 — 2026년 표가 "
                "전 가구원수 ×1.2로 확인됨), 자산 청년 본인 2억 5,100만원· "
                "신혼 3억 4,500만원. 공공임대분(SH)은 청년 3순위 본인 100% "
                "(1인 가구 기준) 등 더 낮은 기준 — 본인 100% 이하만 충족으로 "
                "확정한다. 소득 초과 시 민간 일반공급으로는 신청 가능",
    },
}


def _source(key: str) -> dict:
    src = dict(STANDARD_SOURCES.get(
        key, {"label": key, "url": "", "asof": None,
              "official": False, "note": ""}))
    src["key"] = key
    return src


# ================================================================ 유형별 기준표
# income 스펙:
#   base      : urban | urban1 | median — 어느 기저 소득표를 쓰는가
#   pct       : 기본 퍼센트
#   tiers     : {이름: 퍼센트} — 전용면적·공급 구분별 다른 퍼센트
#   dual_pct  : 맞벌이(배우자 소득 있음) 퍼센트. None 이면 구분 없음
#   relax     : 1·2인 완화(+20/+10%p) 적용 여부 — True/False/None(미확정)
#   dual_relax: 맞벌이 퍼센트에 완화가 또 적용되는지 — 공식 페이지가
#               중복 적용을 명시하지 않아 대부분 None(미확정)
#   scope     : 누구의 소득을 보는가 (설명용)
#   scope_mode: household(입력=세대 합산으로 보고 양방향 판정)
#               | self(본인 소득) | parents_add(본인+부모 합산 — 입력이 본인
#               소득뿐이면 '이하'여도 충족 확정 불가, '초과'면 불충족 확정 가능)
#   met_only  : True 면 불충족을 만들지 않는다 — 더 느슨한 순위·수급자 특례가
#               공존해 초과만으로 탈락을 확정할 수 없는 유형(매입임대)
# asset 스펙 (만원):
#   total / estate / car / car_forbidden / met_only
STANDARDS: dict[str, dict] = {
    # ---- 행복주택 (계층별) ----
    "청년행복주택": {
        "label": "행복주택(청년)",
        "source": "myhome-happy-house",
        "income": {"base": "urban", "pct": 100, "tiers": {}, "dual_pct": None,
                   "relax": True, "dual_relax": None,
                   "scope": "세대(단독세대주면 본인)", "scope_mode": "household",
                   "met_only": False},
        "asset": {"total": 25_100, "estate": None, "car": 4_542,
                  "car_forbidden": False, "met_only": False},
        "household": "만 19~39세 청년 계층",
    },
    "신혼행복주택": {
        "label": "행복주택(신혼부부·한부모)",
        "source": "myhome-happy-house",
        "income": {"base": "urban", "pct": 100, "tiers": {}, "dual_pct": 120,
                   "relax": True, "dual_relax": None,
                   "scope": "세대(배우자 포함)", "scope_mode": "household",
                   "met_only": False},
        "asset": {"total": 34_500, "estate": None, "car": 4_542,
                  "car_forbidden": False, "met_only": False},
        "household": "혼인 7년 이내 신혼·예비신혼·한부모",
    },
    "대학생행복주택": {
        "label": "행복주택(대학생)",
        "source": "myhome-happy-house",
        # 대학생은 본인+부모 소득을 합산한다 — 입력이 본인 소득뿐이면
        # 한도 이하라도 충족을 확정할 수 없다 (부모 소득이 더해진다)
        "income": {"base": "urban", "pct": 100, "tiers": {}, "dual_pct": None,
                   "relax": True, "dual_relax": None,
                   "scope": "본인+부모", "scope_mode": "parents_add",
                   "met_only": False},
        "asset": {"total": 10_800, "estate": None, "car": None,
                  "car_forbidden": True, "met_only": False},
        "household": "대학(원)생 계층",
    },
    "고령자행복주택": {
        "label": "행복주택(고령자)",
        "source": "myhome-happy-house",
        "income": {"base": "urban", "pct": 100, "tiers": {}, "dual_pct": None,
                   "relax": True, "dual_relax": None,
                   "scope": "세대", "scope_mode": "household",
                   "met_only": False},
        # 마이홈 페이지가 고령자 총자산만 명시 — 자동차 상한은 미기재라
        # None 으로 두고 보유 시 '미확인'으로 처리한다
        "asset": {"total": 34_500, "estate": None, "car": None,
                  "car_forbidden": False, "met_only": False},
        "household": "만 65세 이상",
    },

    # ---- 건설 임대 ----
    "국민임대": {
        "label": "국민임대",
        "source": "myhome-national-rental",
        "income": {"base": "urban", "pct": 70, "tiers": {"전용60초과": 100},
                   "dual_pct": None, "relax": True, "dual_relax": None,
                   "scope": "무주택세대구성원 전원", "scope_mode": "household",
                   "met_only": False},
        "asset": {"total": 34_500, "estate": None, "car": 4_542,
                  "car_forbidden": False, "met_only": False},
        "household": "무주택세대구성원",
    },
    "통합공공임대": {
        "label": "통합공공임대",
        "source": "myhome-integrated-rental",
        # 기준 중위소득 기반. 일반공급 150% 가 지원 가능 상한이므로 기본값.
        # 우선공급(100%)은 tier 로 따로 계산한다.
        "income": {"base": "median", "pct": 150, "tiers": {"우선공급": 100},
                   "dual_pct": None, "relax": True, "dual_relax": None,
                   "scope": "세대", "scope_mode": "household",
                   "met_only": False},
        "asset": {"total": 34_500, "estate": None, "car": 4_542,
                  "car_forbidden": False, "met_only": False},
        "household": "무주택세대구성원",
    },
    "장기전세": {
        "label": "장기전세주택",
        "source": "myhome-longterm-lease",
        # 85㎡ 초과분의 소득 기준이 페이지에 없어(물량이 실제로 존재한다)
        # 초과로 탈락을 확정할 수 없다 → met_only. 60㎡ 이하 100%가 확실한
        # 하한이고, 전용면적이 60㎡ 초과로 확인되면 120% tier 로 올라간다.
        "income": {"base": "urban", "pct": 100, "tiers": {"전용60초과": 120},
                   "dual_pct": None, "relax": True, "dual_relax": None,
                   "scope": "무주택세대구성원 전원", "scope_mode": "household",
                   "met_only": True},
        # 총자산이 지역별로 다르다(서울 66,200 / 경기 41,700) — 기준 키에는
        # 지역 축이 없어 낮은 쪽(경기)을 하한으로 쓴다: 이하면 어느 지역이든
        # 충족, 초과는 서울 한도 가능성이 있어 met_only 로 미확인에 머문다.
        "asset": {"total": 41_700, "estate": None, "car": 4_542,
                  "car_forbidden": False, "met_only": True},
        "household": "무주택세대구성원",
    },
    "영구임대": {
        "label": "영구임대주택",
        "source": "myhome-permanent-rental",
        # 2순위(일반) 소득 50%가 비수급자도 보장되는 하한이다. 초과해도
        # 수급자 1순위·장애인 100% 등이 공존해 불충족을 확정하지 않는다.
        # 1·2인 완화는 페이지가 명시(1인 70%·2인 60%)해 확정.
        "income": {"base": "urban", "pct": 50, "tiers": {}, "dual_pct": None,
                   "relax": True, "dual_relax": None,
                   "scope": "세대", "scope_mode": "household",
                   "met_only": True},
        # 자산 요건의 존재만 명시되고 금액이 페이지에 없다 — 전부 None 으로
        # 두면 금액 비교를 만들지 않아 자동으로 '미확인'에 머문다
        "asset": {"total": None, "estate": None, "car": None,
                  "car_forbidden": False, "met_only": True},
        "household": "수급자·국가유공자·한부모 등 취약계층 "
                     "(2순위는 소득 50% 이하 일반가구)",
    },
    "청년안심주택": {
        "label": "청년안심주택(서울)",
        "source": "seoul-youth-safe-housing",
        # 공공임대분(본인 100%, 1인 가구 기준)과 민간임대 특공(세대 120%)이
        # 한 공고에 섞인다. 본인 100% 이하면 어느 트랙이든 소득 충족이
        # 보장되는 하한. 초과는 민간 일반공급(소득 무관)이 남아 있어
        # 탈락을 확정하지 않는다 → met_only.
        "income": {"base": "urban1", "pct": 100, "tiers": {}, "dual_pct": None,
                   "relax": None, "dual_relax": None,
                   "scope": "본인(민간 특공은 세대)", "scope_mode": "self",
                   "met_only": True},
        # 청년 본인 2억 5,100만원이 가장 낮은 한도(신혼은 3억 4,500만원) —
        # 이하면 어느 계층이든 충족. 자동차 기준은 페이지 미기재.
        "asset": {"total": 25_100, "estate": None, "car": None,
                  "car_forbidden": False, "met_only": True},
        "household": "만 19~39세 청년·신혼부부 (서울)",
    },

    # ---- 매입임대 (LH 공식 페이지 — 순위 구조만 확인, 적용연도 미표기) ----
    "청년매입임대": {
        "label": "청년매입임대",
        "source": "lh-youth-purchase",
        # 3순위(본인 소득, 1인 가구 기준 100%)가 소득으로 확정할 수 있는
        # 선이다. 이하면 최소 3순위로 지원 가능 → 충족.
        # 초과해도 1순위(수급자 등) 가능성이 있어 불충족을 만들지 않는다.
        "income": {"base": "urban1", "pct": 100, "tiers": {}, "dual_pct": None,
                   "relax": None, "dual_relax": None,
                   "scope": "3순위 본인 / 2순위 본인+부모", "scope_mode": "self",
                   "met_only": True},
        # 3순위 기준(행복주택 청년) 이하면 어느 순위든 자산 충족.
        # 초과는 2순위(국민임대 3억 4,500만원) 가능성이 있어 미확인.
        "asset": {"total": 25_100, "estate": None, "car": 4_542,
                  "car_forbidden": False, "met_only": True},
        "household": "만 19~39세 미혼 청년·대학생·취업준비생",
    },
    "신혼부부매입임대": {
        "label": "신혼·신생아 매입임대",
        "source": "lh-newlywed-purchase",
        # Ⅰ형 70% 이하면 어느 유형·순위로든 소득 충족이 보장된다.
        # 초과는 Ⅱ형(100~140%) 가능성이 있어 불충족을 만들지 않는다.
        "income": {"base": "urban", "pct": 70, "tiers": {}, "dual_pct": 90,
                   "relax": None, "dual_relax": None,
                   "scope": "세대(배우자 포함)", "scope_mode": "household",
                   "met_only": True},
        # 국민임대(Ⅰ형)·행복주택 신혼(Ⅱ형) 모두 3억 4,500만원 —
        # 이하면 보장 충족. 초과는 5순위(신혼희망타운 3억 6,200만원) 미확인.
        "asset": {"total": 34_500, "estate": None, "car": 4_542,
                  "car_forbidden": False, "met_only": True},
        "household": "혼인 7년 이내·예비신혼·한부모·유자녀 혼인가구",
    },

    # ---- 전세임대 (LH 전세임대포털 — 순위 구조 확인, 자산은 준용 기준) ----
    "기존주택전세임대": {
        "label": "기존주택 전세임대",
        "source": "lh-jeonse-rental",
        # 2순위(소득 50%)가 비수급자도 보장되는 하한. 1·2인 완화 적용 여부는
        # 페이지에서 확정하지 못해 relax=None 밴드로 둔다.
        # 1순위(수급자·한부모)가 공존해 초과로 탈락을 확정하지 않는다.
        "income": {"base": "urban", "pct": 50, "tiers": {}, "dual_pct": None,
                   "relax": None, "dual_relax": None,
                   "scope": "세대", "scope_mode": "household",
                   "met_only": True},
        # 2순위는 영구임대 자산기준 준용인데 그 금액을 공식 확인하지 못했다
        # — 전부 None 으로 두어 금액 비교를 만들지 않는다 (미확인 유지)
        "asset": {"total": None, "estate": None, "car": None,
                  "car_forbidden": False, "met_only": True},
        "household": "1순위 수급자·보호대상 한부모 등 / 2순위 소득 50% 이하",
    },
    "청년전세임대": {
        "label": "청년 전세임대",
        "source": "lh-jeonse-rental",
        # 청년매입임대와 같은 순위 구조 — 3순위(본인 소득, 1인 가구 100%)가
        # 소득으로 확정할 수 있는 선이다. 초과는 1순위(수급가구) 가능성.
        "income": {"base": "urban1", "pct": 100, "tiers": {}, "dual_pct": None,
                   "relax": None, "dual_relax": None,
                   "scope": "3순위 본인 / 2순위 본인+부모", "scope_mode": "self",
                   "met_only": True},
        # 3순위는 행복주택 청년 자산기준 준용 — 참조 기준의 2026년 값.
        # 이하면 어느 순위든 충족, 초과는 2순위(국민임대 기준) 가능성.
        "asset": {"total": 25_100, "estate": None, "car": 4_542,
                  "car_forbidden": False, "met_only": True},
        "household": "만 19~39세 청년 (대학생·취업준비생 포함)",
    },
    "신혼부부전세임대": {
        "label": "신혼·신생아 전세임대",
        "source": "lh-jeonse-rental",
        # Ⅰ형 70%(배우자 소득 시 90%) 이하면 어느 유형이든 소득 충족 보장.
        # 초과는 Ⅱ형(100~120%) 가능성이 있어 불충족을 만들지 않는다.
        "income": {"base": "urban", "pct": 70, "tiers": {}, "dual_pct": 90,
                   "relax": None, "dual_relax": None,
                   "scope": "세대(배우자 포함)", "scope_mode": "household",
                   "met_only": True},
        # Ⅰ·Ⅱ형 모두 국민임대 수준(3억 4,500만원) 준용 — 참조 기준의 2026년 값
        "asset": {"total": 34_500, "estate": None, "car": 4_542,
                  "car_forbidden": False, "met_only": True},
        "household": "혼인 7년 이내·예비신혼·신생아·한부모 가구",
    },

    # ---- 분양 ----
    "신혼희망타운": {
        "label": "신혼희망타운",
        "source": "myhome-newlywed-town",
        # 1·2인 완화 적용 여부를 공식 페이지에서 확정하지 못했다(relax=None)
        # → 130% 이하 충족 / 완화 상한 초과 불충족 / 사이 미확인
        "income": {"base": "urban", "pct": 130, "tiers": {}, "dual_pct": 200,
                   "relax": None, "dual_relax": None,
                   "scope": "세대(배우자 포함)", "scope_mode": "household",
                   "met_only": False},
        "asset": {"total": 36_200, "estate": None, "car": None,
                  "car_forbidden": False, "met_only": False},
        "household": "혼인 7년 이내 또는 6세 이하 자녀",
        "extra": ["주택가격이 총자산기준(3억 6,200만원)을 초과하면 "
                  "전용 모기지 30% 이상 가입 요건이 붙는다"],
    },
    "공공분양": {
        "label": "공공분양(뉴:홈)",
        "source": "newhome-public-sale",   # official=False — 항상 미확인
        "income": {"base": "urban", "pct": 100, "tiers": {}, "dual_pct": None,
                   "relax": None, "dual_relax": None,
                   "scope": "세대", "scope_mode": "household",
                   "met_only": False},
        "asset": {"total": None, "estate": 21_550, "car": 4_542,
                  "car_forbidden": False, "met_only": False},
        "household": "무주택세대구성원",
    },

    # ---- 뉴:홈 유형별 (공고가 나눔형·선택형·일반형을 밝힌 경우) ----
    # 한 공고 안에 일반공급(100%)과 특공(최대 140%)이 공존하고, 기준 키에는
    # 특공 축이 없다. 그래서 일반공급 100% 를 '보장되는 하한'으로 쓰고
    # met_only 로 둔다: 100% 이하면 어느 공급으로든 지원 가능(충족),
    # 초과는 특공(130~140%) 가능성이 있어 탈락을 확정하지 않는다.
    "공공분양나눔형": {
        "label": "뉴:홈 나눔형(이익공유형)",
        "source": "myhome-newhome-share",
        "income": {"base": "urban", "pct": 100, "tiers": {}, "dual_pct": None,
                   "relax": None, "dual_relax": None,
                   "scope": "세대", "scope_mode": "household",
                   "met_only": True},
        # 나눔형·선택형은 부동산가액이 아니라 **순자산 3억 6,200만원**을 본다.
        # 청년특공만 본인 2억 7,600만원(+부모 10억 3,500만원)으로 더 낮지만,
        # 3억 6,200만원 이하면 일반공급으로는 지원 가능하므로 충족이 성립한다.
        "asset": {"total": 36_200, "estate": None, "car": None,
                  "car_forbidden": False, "met_only": True},
        "household": "무주택세대구성원 (청년특공은 19~39세 미혼, "
                     "부모 자산 기준 별도)",
    },
    "공공분양선택형": {
        "label": "뉴:홈 선택형(6년 분양전환)",
        "source": "myhome-newhome-choice",
        "income": {"base": "urban", "pct": 100, "tiers": {}, "dual_pct": None,
                   "relax": None, "dual_relax": None,
                   "scope": "세대", "scope_mode": "household",
                   "met_only": True},
        "asset": {"total": 36_200, "estate": None, "car": None,
                  "car_forbidden": False, "met_only": True},
        "household": "무주택세대구성원 (청년특공은 19~39세 미혼, "
                     "부모 자산 기준 별도)",
    },
    "공공분양일반형": {
        "label": "뉴:홈 일반형(공공분양)",
        "source": "myhome-newhome-general",
        # 일반형 일반공급의 소득·자산 검사는 전용 60㎡ 이하에만 붙는다 —
        # 60㎡ 초과 일반공급은 소득 무관이라 초과로 탈락을 확정할 수 없다.
        "income": {"base": "urban", "pct": 100, "tiers": {}, "dual_pct": None,
                   "relax": None, "dual_relax": None,
                   "scope": "세대", "scope_mode": "household",
                   "met_only": True},
        # 일반형은 순자산이 아니라 부동산 2억 1,550만원 + 자동차를 본다
        "asset": {"total": None, "estate": 21_550, "car": 4_542,
                  "car_forbidden": False, "met_only": True},
        "household": "무주택세대구성원",
    },

    # ---- 특별공급 (5년·10년·50년 공공임대 페이지 기준 — 공공 유형에 적용) ----
    "생애최초특공": {
        "label": "생애최초 특별공급(공공)",
        "source": "myhome-public-rental-5-10",
        "income": {"base": "urban", "pct": 130, "tiers": {}, "dual_pct": None,
                   "relax": None, "dual_relax": None,
                   "scope": "세대", "scope_mode": "household",
                   "met_only": False},
        "asset": {"total": None, "estate": 21_550, "car": 4_542,
                  "car_forbidden": False, "met_only": False},
        "household": "세대원 전원 주택 소유 이력 없음 등 — 공고문 확인",
    },
    "신혼부부특공": {
        "label": "신혼부부 특별공급(공공)",
        "source": "myhome-public-rental-5-10",
        "income": {"base": "urban", "pct": 130, "tiers": {}, "dual_pct": 140,
                   "relax": None, "dual_relax": None,
                   "scope": "세대(배우자 포함)", "scope_mode": "household",
                   "met_only": False},
        "asset": {"total": None, "estate": 21_550, "car": 4_542,
                  "car_forbidden": False, "met_only": False},
        "household": "혼인 7년 이내 등 — 공고문 확인",
    },
    "다자녀특공": {
        "label": "다자녀 특별공급(공공)",
        "source": "myhome-public-rental-5-10",
        "income": {"base": "urban", "pct": 120, "tiers": {}, "dual_pct": None,
                   "relax": None, "dual_relax": None,
                   "scope": "세대", "scope_mode": "household",
                   "met_only": False},
        "asset": {"total": None, "estate": 21_550, "car": 4_542,
                  "car_forbidden": False, "met_only": False},
        "household": "미성년 자녀 2명 이상(공고별 상이)",
    },
    "노부모특공": {
        "label": "노부모부양 특별공급(공공)",
        "source": "myhome-public-rental-5-10",
        "income": {"base": "urban", "pct": 120, "tiers": {}, "dual_pct": None,
                   "relax": None, "dual_relax": None,
                   "scope": "세대", "scope_mode": "household",
                   "met_only": False},
        "asset": {"total": None, "estate": 21_550, "car": 4_542,
                  "car_forbidden": False, "met_only": False},
        "household": "만 65세 이상 직계존속 3년 이상 부양",
    },

    # ---- 소득·자산 요건이 없는 유형 ----
    "민영일반공급": {
        "label": "민영주택 일반공급",
        "source": "supply-rule",
        "income": None,    # 소득 요건 없음 — 검사하지 않는다
        "asset": None,
        "household": "",
    },
    # 무순위는 2026-10-30 청약홈 공식 안내 페이지로 자격을 확인했다.
    #   신청자격 "무주택세대구성원인 성년자" · 청약통장 사용여부 X ·
    #   거주지역은 시·군·구가 정하는 기준(단지별)
    # 소득·자산은 자격 요건으로 열거되지 않는다 → 검사하지 않는다.
    # (세대 무주택 요건은 eligibility._check_homeless 가 무순위에도 적용한다)
    "무순위": {
        "label": "무순위·잔여세대",
        "source": "applyhome-unranked",
        "income": None,
        "asset": None,
        "household": "무주택세대구성원",
    },
}

HAPPY_HOUSE_KEYS = ("청년행복주택", "신혼행복주택", "대학생행복주택",
                    "고령자행복주택")
PURCHASE_RENTAL_KEYS = ("청년매입임대", "신혼부부매입임대")
# 전세임대 계층별 키 — 후보가 여럿 남으면 호출부가 프로필로 좁혀야 한다
JEONSE_RENTAL_KEYS = ("청년전세임대", "신혼부부전세임대", "기존주택전세임대")
# 뉴:홈 유형별 키 — 공고가 유형을 밝혔을 때만 선택된다
NEWHOME_SALE_KEYS = ("공공분양나눔형", "공공분양선택형", "공공분양일반형")


# ================================================================ 계산
def _money(base: int, pct: int) -> int:
    """base × pct% 를 원 단위 반올림 — 마이홈 표 값과 일치하는 방식.

    검산: 3,813,363 × 90% = 3,432,027 / 8,168,429 × 70% = 5,717,900
          3,813,363 × 120% = 4,576,036  (전부 마이홈 표와 일치)
    """
    return (base * pct + 50) // 100


def _base_amount(base_kind: str, household_size: int) -> int:
    table = _BASE_TABLES[base_kind]
    n = 1 if base_kind == "urban1" else max(1, int(household_size or 1))
    return table[min(n, 7)]


def _band(spec: dict, household_size: int, *,
          dual: Optional[bool] = None, tier: Optional[str] = None,
          tier_uncertain: bool = False) -> dict:
    """적용 가능한 한도의 [lo, hi] 밴드와 설명 재료.

    dual: True=맞벌이 확정, False=외벌이 확정, None=모름(프로필로 알 수 없음).
    tier_uncertain: 전용면적 등 tier 조건 해당 여부를 몰라 상한이 열려 있음.
    """
    hh = 1 if spec["base"] == "urban1" else max(1, int(household_size or 1))
    pp = HOUSEHOLD_RELAX_PP.get(min(hh, 7), 0)

    lo_pct = spec["tiers"].get(tier, spec["pct"]) if tier else spec["pct"]
    if dual is True and spec["dual_pct"]:
        lo_pct = spec["dual_pct"]
    hi_pct = lo_pct
    if dual is None and spec["dual_pct"]:
        hi_pct = max(hi_pct, spec["dual_pct"])
    if tier_uncertain and spec["tiers"]:
        hi_pct = max(hi_pct, *spec["tiers"].values())

    relax = spec["dual_relax"] if dual is True else spec["relax"]
    relax_note = ""
    if pp:
        if relax is True:
            lo_pct += pp
            hi_pct += pp
            relax_note = f"{hh}인 +{pp}%p 완화"
        elif relax is None:
            # 완화 적용 여부 미확정 — 상한만 열어 둔다
            hi_pct += pp
            relax_note = f"{hh}인 완화(+{pp}%p) 적용 여부 미확정"

    base = _base_amount(spec["base"], household_size)
    return {
        "lo": _money(base, lo_pct), "hi": _money(base, hi_pct),
        "lo_pct": lo_pct, "hi_pct": hi_pct,
        "base": base, "base_label": _BASE_LABEL[spec["base"]],
        "relax_note": relax_note,
    }


def income_limit(std_key: str, household_size: int, *,
                 dual_income: bool = False,
                 tier: Optional[str] = None) -> tuple[Optional[int], str, dict]:
    """유형·가구원수의 소득 한도 (금액 원/월, 설명, 출처dict).

    dual_income/tier 가 확정값으로 주어졌다는 전제의 한도(밴드 하한)다.
    출처dict 의 official 이 False 면 이 금액으로 적격을 확정하면 안 된다.
    """
    std = STANDARDS[std_key]
    src = _source(std["source"])
    spec = std.get("income")
    if spec is None:
        return None, f"{std['label']} — 소득 요건 없음", src

    b = _band(spec, household_size, dual=bool(dual_income), tier=tier)
    desc = (f"{std['label']} 소득기준 {b['lo_pct']}%"
            + (f" ({b['relax_note']})" if b["relax_note"] else "")
            + f" = 월 {b['lo']:,}원 ({b['base_label']} 기준)")
    return b["lo"], desc, src


def compare_income(std_key: str, household_size: int, income_won: int, *,
                   dual: Optional[bool] = None, tier: Optional[str] = None,
                   tier_uncertain: bool = False) -> tuple[str, str, str]:
    """소득을 공식 한도 밴드와 비교 — ("충족"|"미확인"|"불충족", 설명, todo).

    출처가 official=False 인 유형이면 호출부가 그대로 Check 를 만들어도
    checks.py 가 자동으로 '미확인'으로 내려앉힌다.
    """
    std = STANDARDS[std_key]
    spec = std["income"]
    b = _band(spec, household_size, dual=dual, tier=tier,
              tier_uncertain=tier_uncertain)

    head = (f"{std['label']} 소득기준 {b['lo_pct']}%"
            + (f" ({b['relax_note']})" if b["relax_note"] else "")
            + f" = 월 {b['lo']:,}원")
    todo = ""

    if income_won <= b["lo"]:
        status = "충족"
        detail = f"{head} / 내 소득 {income_won:,}원"
        if spec["scope_mode"] == "parents_add":
            # 본인+부모 합산 기준 — 입력(본인 소득)이 이하라는 것만으로는
            # 합산 충족을 보장할 수 없다
            status = "미확인"
            detail += f" — {spec['scope']} 합산액으로 봐야 한다"
            todo = (f"{spec['scope']} 합산 소득 기준 — 합산액을 "
                    "공고문 기준으로 확인")
    elif income_won > b["hi"]:
        status = "불충족"
        detail = (f"{head} / 내 소득 {income_won:,}원 — "
                  f"가능 상한(월 {b['hi']:,}원)도 초과")
        if spec["met_only"]:
            # 수급자 특례·다른 순위가 공존하는 유형 — 초과로 탈락을 확정 못 한다
            status = "미확인"
            todo = "순위별 기준·수급자 특례가 달라 공고문·LH 상담으로 확인"
    else:
        status = "미확인"
        detail = (f"{head} / 내 소득 {income_won:,}원 — 기본 한도 초과, "
                  f"가능 상한(월 {b['hi']:,}원) 이내")
        reasons = []
        if b["hi_pct"] > b["lo_pct"]:
            if spec["dual_pct"] and dual is None:
                reasons.append(f"맞벌이면 {spec['dual_pct']}%")
            if b["relax_note"].endswith("미확정"):
                reasons.append(b["relax_note"])
            if tier_uncertain and spec["tiers"]:
                reasons.append("전용면적별 기준 상이")
        todo = ("적용 % 가 변수에 따라 다르다"
                + (f" ({' · '.join(reasons)})" if reasons else "")
                + " — 공고문 기준으로 확인")
    return status, detail, todo


def asset_limits(std_key: str) -> dict:
    """유형의 자산 기준 — 총자산·부동산·자동차를 구분해 돌려준다 (만원)."""
    std = STANDARDS[std_key]
    src = _source(std["source"])
    spec = std.get("asset")
    if spec is None:
        return {"applies": False, "total": None, "estate": None, "car": None,
                "car_forbidden": False, "met_only": False,
                "label": std["label"], "source": src}
    return {"applies": True, "total": spec["total"], "estate": spec["estate"],
            "car": spec["car"], "car_forbidden": spec["car_forbidden"],
            "met_only": spec["met_only"], "label": std["label"], "source": src}


# ================================================================ 유형 매핑
def standards_for(kind: str, tags: list[str]) -> list[str]:
    """공고 유형/태그 → 적용 가능한 기준 키 후보.

    후보가 여럿이면(행복주택 계층 미상 등) 호출부가 프로필로 좁힌다.
    빈 리스트는 '공식 기준 미보유' — 판정은 미확인에 머문다.
    """
    t = set(tags)

    if "신혼희망타운" in t:
        return ["신혼희망타운"]

    if "행복주택" in t:
        picks = []
        if "청년" in t:
            picks.append("청년행복주택")
        if "신혼부부" in t or "신생아" in t:
            picks.append("신혼행복주택")
        if "고령자" in t:
            picks.append("고령자행복주택")
        return picks or list(HAPPY_HOUSE_KEYS)

    if "국민임대" in t:
        return ["국민임대"]
    if "통합공공임대" in t:
        return ["통합공공임대"]
    if "장기전세" in t:
        return ["장기전세"]
    if "영구임대" in t:
        return ["영구임대"]
    if "청년안심" in t:
        # 서울 청년안심주택 — 공공임대분·민간임대분이 한 공고에 섞여
        # 하나의 보수적 기준(본인 100% 하한)으로 판정한다
        return ["청년안심주택"]

    if "매입임대" in t:
        picks = []
        if "청년" in t:
            picks.append("청년매입임대")
        if "신혼부부" in t or "신생아" in t:
            picks.append("신혼부부매입임대")
        return picks or list(PURCHASE_RENTAL_KEYS)

    if "전세임대" in t:
        picks = []
        if "청년" in t:
            picks.append("청년전세임대")
        if "신혼부부" in t or "신생아" in t:
            picks.append("신혼부부전세임대")
        # 계층 태그가 없으면 기존주택 전세임대(일반) 기준으로 본다 —
        # 든든전세는 소득·자산 기준이 달라(사실상 없음) 공고문 확인이 필요
        # 하지만, 여기 기준은 met_only 라 잘못 제외하는 일은 없다
        return picks or ["기존주택전세임대"]

    if kind in ("공공분양", "공공임대"):
        # 뉴:홈 유형이 공고에 명시되면 특공 기준보다 유형 기준이 먼저다 —
        # 나눔형·선택형은 자산 체계(순자산)가 특공(부동산가액)과 다르다.
        # 선택형은 '6년 분양전환 공공임대'라 kind 가 공공임대로 잡힐 수 있다.
        if "나눔형" in t:
            return ["공공분양나눔형"]
        if "선택형" in t:
            return ["공공분양선택형"]
        picks = []
        if "생애최초" in t:
            picks.append("생애최초특공")
        if "다자녀" in t:
            picks.append("다자녀특공")
        if "노부모" in t:
            picks.append("노부모특공")
        if "신혼부부" in t and "특별공급" in t:
            picks.append("신혼부부특공")
        if picks:
            return picks
        if kind == "공공분양":
            if "일반형" in t:
                return ["공공분양일반형"]
            return ["공공분양"]        # 유형 미상 — official=False, 항상 미확인
        return []                       # 재개발임대 등 — 기준 미보유

    if kind == "무순위":
        return ["무순위"]
    if kind == "민영분양" and not ({"특별공급", "신혼부부", "다자녀",
                                    "생애최초", "노부모"} & t):
        return ["민영일반공급"]
    return []
