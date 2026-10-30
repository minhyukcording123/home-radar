"""2026년 공식 소득·자산 기준(app/standards.py)과 그 적용 엔진의 회귀 테스트.

수치는 전부 공식 포털에서 확인한 2026년도 적용기준이다:
  - 도시근로자 월평균소득: 마이홈 국민임대 페이지 (기저값·완화 분리 저장)
  - 기준 중위소득: 보건복지부 고시 제2025-135호 ('26.1.1. 시행)
검산 기준점은 마이홈 표의 완성값이다 (1인 90% = 3,432,027 등).

    python -m unittest discover -s tests

tests/test_core.py 는 다른 작업 흐름이 보고 있으므로 건드리지 않는다 —
공식 기준 수치가 바뀌면 여기에 추가한다.
"""
import unittest
from datetime import date

from app import profile, standards
from app.checks import MET, UNKNOWN, UNMET, Check
from app.eligibility import judge
from app.models import Area, Notice
from app.rules import RULE_SOURCES, is_official
from app.standards import (HOUSEHOLD_RELAX_PP, MEDIAN_INCOME_2026,
                           URBAN_WORKER_INCOME_2026, asset_limits,
                           compare_income, income_limit, standards_for)

TODAY = date(2026, 10, 30)


def mk(title, category="", region="서울", **kw):
    return Notice(source="청약홈", uid="t", title=title, category=category,
                  region=region, **kw)


def base_profile(**kw):
    """1994년생(만 32세) 1인 가구 — 테스트 기본형."""
    return profile.merge({"birth": "1994-07-15", "regions": ["서울", "경기"],
                          "household_size": 1, **kw})


def newlywed_profile(**kw):
    """2인 신혼(혼인 2년) 가구."""
    return base_profile(marital="신혼", marriage_date="2024-05",
                        household_size=2, **kw)


def check_of(v, name):
    found = [c for c in v.checks if c.name == name]
    return found[-1] if found else None


# ============================================================ 기저표 · 완화 검산
class TestBaseTables(unittest.TestCase):
    def test_urban_income_is_base_without_relax(self):
        """기저표에는 1·2인 완화가 섞여 있으면 안 된다 (마이홈 표의 함정)."""
        self.assertEqual(URBAN_WORKER_INCOME_2026[1], 3_813_363)
        self.assertEqual(URBAN_WORKER_INCOME_2026[2], 5_866_270)
        self.assertEqual(URBAN_WORKER_INCOME_2026[3], 8_168_429)
        self.assertEqual(URBAN_WORKER_INCOME_2026[7], 10_485_541)
        self.assertEqual(HOUSEHOLD_RELAX_PP, {1: 20, 2: 10})

    def test_median_income_2026(self):
        """보건복지부 고시 제2025-135호 — 복지부 페이지에서 확인한 값."""
        self.assertEqual(MEDIAN_INCOME_2026[1], 2_564_238)
        self.assertEqual(MEDIAN_INCOME_2026[4], 6_494_738)

    def test_relax_reproduces_myhome_table(self):
        """기저 × 완화 = 마이홈 표의 '100%' 열 완성값."""
        # 1인 100% 열 = 3,813,363 × 1.2
        self.assertEqual(income_limit("청년행복주택", 1)[0], 4_576_036)
        # 2인 100% 열 = 5,866,270 × 1.1
        self.assertEqual(income_limit("신혼행복주택", 2)[0], 6_452_897)

    def test_relax_reproduces_national_rental_70_column(self):
        """국민임대 70% 열 검산 — 1인 90%, 2인 80%, 3인 70%."""
        self.assertEqual(income_limit("국민임대", 1)[0], 3_432_027)   # ×0.9
        self.assertEqual(income_limit("국민임대", 2)[0], 4_693_016)   # ×0.8
        self.assertEqual(income_limit("국민임대", 3)[0], 5_717_900)   # ×0.7

    def test_integrated_rental_uses_median_income(self):
        """통합공공임대는 기준 중위소득 기반 — 일반 150%(1인 170%)."""
        amount, desc, src = income_limit("통합공공임대", 1)
        self.assertEqual(amount, 4_359_205)          # 2,564,238 × 1.7
        self.assertIn("기준 중위소득", desc)
        self.assertTrue(src["official"])
        # 우선공급 tier — 100%(1인 120%)
        self.assertEqual(income_limit("통합공공임대", 1, tier="우선공급")[0],
                         3_077_086)                  # 2,564,238 × 1.2

    def test_dual_income_limits(self):
        """맞벌이 한도 — 신혼행복주택 120%, 신혼희망타운 200%."""
        self.assertEqual(income_limit("신혼행복주택", 2, dual_income=True)[0],
                         7_039_524)                  # 5,866,270 × 1.2
        self.assertEqual(income_limit("신혼희망타운", 2, dual_income=True)[0],
                         11_732_540)                 # 5,866,270 × 2.0


# ============================================================ 경계값 (±1원)
class TestCompareIncomeBoundaries(unittest.TestCase):
    def test_national_rental_1p_exact_boundary(self):
        """국민임대 1인 90% — 완화가 확정이라 ±1원에서 충족/불충족이 갈린다."""
        self.assertEqual(compare_income("국민임대", 1, 3_432_027)[0], "충족")
        self.assertEqual(compare_income("국민임대", 1, 3_432_028)[0], "불충족")

    def test_youth_happy_house_1p_exact_boundary(self):
        self.assertEqual(compare_income("청년행복주택", 1, 4_576_036)[0], "충족")
        self.assertEqual(compare_income("청년행복주택", 1, 4_576_037)[0], "불충족")

    def test_newlywed_happy_house_dual_band(self):
        """맞벌이 120% 확정이어도 완화 중복 적용이 미확정 → 위쪽은 밴드."""
        self.assertEqual(
            compare_income("신혼행복주택", 2, 7_039_524, dual=True)[0], "충족")
        self.assertEqual(
            compare_income("신혼행복주택", 2, 7_039_525, dual=True)[0], "미확인")
        # 완화를 다 얹어도(130%) 초과 — 어느 해석으로도 불충족
        self.assertEqual(
            compare_income("신혼행복주택", 2, 7_626_152, dual=True)[0], "불충족")

    def test_newlywed_hope_town_dual_200(self):
        self.assertEqual(
            compare_income("신혼희망타운", 2, 11_732_540, dual=True)[0], "충족")
        self.assertEqual(
            compare_income("신혼희망타운", 2, 11_732_541, dual=True)[0], "미확인")
        self.assertEqual(
            compare_income("신혼희망타운", 2, 12_319_168, dual=True)[0], "불충족")

    def test_integrated_rental_1p_exact_boundary(self):
        self.assertEqual(compare_income("통합공공임대", 1, 4_359_205)[0], "충족")
        self.assertEqual(compare_income("통합공공임대", 1, 4_359_206)[0], "불충족")

    def test_purchase_rental_never_rejects(self):
        """매입임대는 수급자 1순위가 공존 — 초과해도 불충족을 만들지 않는다."""
        status, _, todo = compare_income("청년매입임대", 1, 99_999_999)
        self.assertEqual(status, "미확인")
        self.assertIn("순위", todo)
        # 3순위(본인, 1인 가구 100%) 이하면 충족은 확정할 수 있다
        self.assertEqual(compare_income("청년매입임대", 1, 3_813_363)[0], "충족")

    def test_college_student_cannot_confirm_met_alone(self):
        """대학생은 본인+부모 합산 — 본인 소득이 이하라는 것만으론 충족 불가."""
        status, _, todo = compare_income("대학생행복주택", 1, 1_000_000)
        self.assertEqual(status, "미확인")
        self.assertIn("부모", todo)


# ============================================================ 자산 기준표
class TestAssetLimits(unittest.TestCase):
    def test_per_type_asset_tables(self):
        self.assertEqual(asset_limits("국민임대")["total"], 34_500)
        self.assertEqual(asset_limits("국민임대")["car"], 4_542)
        self.assertEqual(asset_limits("대학생행복주택")["total"], 10_800)
        self.assertTrue(asset_limits("대학생행복주택")["car_forbidden"])
        self.assertEqual(asset_limits("청년행복주택")["total"], 25_100)
        self.assertEqual(asset_limits("신혼희망타운")["total"], 36_200)
        # 5·10년 공공임대 특공은 총자산이 아니라 부동산을 본다
        self.assertIsNone(asset_limits("생애최초특공")["total"])
        self.assertEqual(asset_limits("생애최초특공")["estate"], 21_550)
        # 민영 일반공급은 자산 요건 자체가 없다
        self.assertFalse(asset_limits("민영일반공급")["applies"])

    def test_sources_are_registered_and_official(self):
        """standards 출처가 rules 레지스트리에 합쳐져야 판정이 선다."""
        for key in ("myhome-national-rental", "myhome-happy-house",
                    "myhome-integrated-rental", "mohw-median-income",
                    "myhome-newlywed-town", "myhome-public-rental-5-10",
                    "lh-youth-purchase", "lh-newlywed-purchase"):
            self.assertIn(key, RULE_SOURCES, key)
            self.assertTrue(is_official(key), key)
            self.assertTrue(RULE_SOURCES[key]["url"], key)
        # 뉴:홈은 공식 확정 실패 — official=False 로 남아 있어야 한다
        self.assertIn("newhome-public-sale", RULE_SOURCES)
        self.assertFalse(is_official("newhome-public-sale"))


# ============================================================ 유형 매핑
class TestStandardsFor(unittest.TestCase):
    def test_mapping(self):
        self.assertEqual(standards_for("공공임대", ["행복주택", "청년"]),
                         ["청년행복주택"])
        self.assertEqual(standards_for("공공임대", ["국민임대"]), ["국민임대"])
        self.assertEqual(standards_for("공공임대", ["통합공공임대"]),
                         ["통합공공임대"])
        self.assertEqual(standards_for("공공임대", ["매입임대", "청년"]),
                         ["청년매입임대"])
        self.assertEqual(
            standards_for("공공분양", ["신혼희망타운", "신혼부부"]),
            ["신혼희망타운"])
        self.assertEqual(standards_for("공공분양", []), ["공공분양"])
        self.assertEqual(standards_for("무순위", ["무순위"]), ["무순위"])
        self.assertEqual(standards_for("민영분양", ["1순위"]), ["민영일반공급"])
        # 민영 특공은 공식 기준 미보유 → 빈 리스트 (샘플 신호로만)
        self.assertEqual(standards_for("민영분양", ["특별공급", "신혼부부"]), [])

    def test_happy_house_without_tier_returns_all(self):
        self.assertEqual(standards_for("공공임대", ["행복주택"]),
                         list(standards.HAPPY_HOUSE_KEYS))


# ============================================================ 엔진 판정
class TestJudgeOfficialIncome(unittest.TestCase):
    def test_youth_happy_house_met_and_unmet(self):
        """1인 청년 행복주택 — 100%(1인 +20%p) = 4,576,036원 경계."""
        n = mk("청년 행복주택 입주자 모집")
        # 457만원 = 4,570,000 ≤ 4,576,036 → 충족
        v = judge(n, base_profile(income_work=457), TODAY)
        c = check_of(v, "소득")
        self.assertEqual(c.status, MET)
        self.assertIn("4,576,036", c.detail)
        self.assertTrue(c.official)
        # 458만원 = 4,580,000 → 불충족 → 공식 기준이라 제외
        v = judge(n, base_profile(income_work=458), TODAY)
        self.assertEqual(check_of(v, "소득").status, UNMET)
        self.assertEqual(v.level, "제외")

    def test_national_rental_boundary_when_area_known(self):
        """국민임대 1인 — 전 세대 60㎡ 이하가 확인되면 90% 선에서 갈린다."""
        n = mk("국민임대주택 입주자 모집")
        n.areas = [Area(name="059A", exclusive="59.91", units="100")]
        v = judge(n, base_profile(income_work=343), TODAY)   # 3,430,000
        self.assertEqual(check_of(v, "소득").status, MET)
        v = judge(n, base_profile(income_work=344), TODAY)   # 3,440,000
        self.assertEqual(check_of(v, "소득").status, UNMET)
        self.assertEqual(v.level, "제외")

    def test_national_rental_unknown_area_keeps_band_open(self):
        """전용면적을 모르면 60㎡ 초과 세대(100%) 가능성 — 제외하면 안 된다."""
        n = mk("국민임대주택 입주자 모집")      # areas 없음
        v = judge(n, base_profile(income_work=344), TODAY)
        c = check_of(v, "소득")
        self.assertEqual(c.status, UNKNOWN)
        self.assertNotEqual(v.level, "제외")
        # 어느 해석(100% + 1인 완화 = 120%)으로도 초과면 불충족
        v = judge(n, base_profile(income_work=458), TODAY)
        self.assertEqual(check_of(v, "소득").status, UNMET)

    def test_newlywed_happy_house_band(self):
        """2인 신혼 — 배우자 소득을 모르면 '충족'을 확정하지 않는다.

        행복주택 신혼부부 기준은 **본인+배우자** 소득을 본다. 본인 소득만
        한도 안이어도 배우자 소득이 더해지면 넘을 수 있으므로 확정하지 않는다.
        단 **불충족은 확정할 수 있다** — 본인 소득만으로 이미 상한을 넘었다면
        배우자 소득을 더해도 결론이 안 바뀐다.
        """
        n = mk("신혼부부 행복주택 입주자 모집")
        # 645만원 = 6,450,000 ≤ 6,452,897(110%) 이지만 배우자 소득 미입력 → 미확인
        v = judge(n, newlywed_profile(income_work=645), TODAY)
        c = check_of(v, "소득")
        self.assertEqual(c.status, UNKNOWN)
        self.assertIn("배우자", c.detail + c.todo)
        # 646만원 — 외벌이면 초과, 맞벌이면 이내 → 역시 미확인
        v = judge(n, newlywed_profile(income_work=646), TODAY)
        self.assertEqual(check_of(v, "소득").status, UNKNOWN)
        # 763만원 = 7,630,000 > 7,626,151(130%) → 배우자와 무관하게 불충족
        v = judge(n, newlywed_profile(income_work=763), TODAY)
        self.assertEqual(check_of(v, "소득").status, UNMET)
        self.assertEqual(v.level, "제외")

    def test_spouse_income_resolves_the_band(self):
        """배우자 소득을 입력하면 밴드가 확정 판정으로 좁혀진다."""
        n = mk("신혼부부 행복주택 입주자 모집")
        # 배우자 소득 0 = 외벌이 확정 → 110% 한도(6,452,897)로 판정
        v = judge(n, newlywed_profile(income_work=645, spouse_income_work=0),
                  TODAY)
        self.assertEqual(check_of(v, "소득").status, MET)
        v = judge(n, newlywed_profile(income_work=646, spouse_income_work=0),
                  TODAY)
        self.assertEqual(check_of(v, "소득").status, UNMET)

        # 맞벌이 — 본인 400 + 배우자 300 = 700만원.
        # 맞벌이 한도(120% + 2인 완화)는 그보다 높아 충족이어야 한다
        v = judge(n, newlywed_profile(income_work=400, spouse_income_work=300),
                  TODAY)
        c = check_of(v, "소득")
        self.assertEqual(c.status, MET)
        # 합산값으로 비교했는지 확인
        self.assertIn("7,000,000", c.detail)

        # 맞벌이인데 합산이 상한을 넘으면 불충족
        v = judge(n, newlywed_profile(income_work=500, spouse_income_work=400),
                  TODAY)
        self.assertEqual(check_of(v, "소득").status, UNMET)

    def test_dual_income_flag_without_amount_stays_unknown(self):
        """맞벌이 체크만 하고 금액을 안 적으면 확정하지 않는다."""
        n = mk("신혼부부 행복주택 입주자 모집")
        v = judge(n, newlywed_profile(income_work=400, dual_income=True), TODAY)
        self.assertEqual(check_of(v, "소득").status, UNKNOWN)

    def test_spouse_business_income_blocks_confirmation(self):
        """배우자 사업소득이 있으면 합산으로 적격을 확정하지 않는다."""
        n = mk("신혼부부 행복주택 입주자 모집")
        v = judge(n, newlywed_profile(income_work=300,
                                      spouse_income_business=200), TODAY)
        c = check_of(v, "소득")
        self.assertEqual(c.status, UNKNOWN)
        self.assertIn("사업소득", c.detail + c.todo)

    def test_single_person_standard_ignores_spouse(self):
        """본인 소득만 보는 기준(청년행복주택)은 배우자 소득을 섞지 않는다."""
        n = mk("청년 행복주택 입주자 모집")
        v = judge(n, base_profile(income_work=300, spouse_income_work=900),
                  TODAY)
        c = check_of(v, "소득")
        # 배우자 900만원을 더했다면 불충족이 됐을 것이다
        self.assertEqual(c.status, MET)

    def test_integrated_rental_median_based(self):
        """통합공공임대 1인 — 중위소득 150%(1인 170%) = 4,359,205원."""
        n = mk("통합공공임대주택 입주자 모집")
        v = judge(n, base_profile(income_work=435), TODAY)   # 4,350,000
        c = check_of(v, "소득")
        self.assertEqual(c.status, MET)
        self.assertIn("4,359,205", c.detail)
        v = judge(n, base_profile(income_work=436), TODAY)   # 4,360,000
        self.assertEqual(check_of(v, "소득").status, UNMET)
        self.assertEqual(v.level, "제외")

    def test_business_income_never_confirms(self):
        """근로+사업 혼재 — 합산으로 적격을 확정하지 않는다 (반드시 미확인)."""
        n = mk("국민임대주택 입주자 모집")
        n.areas = [Area(name="059A", exclusive="59.91", units="100")]
        # 합산 250만원은 한도(343만원) 이내지만, 사업소득 인정 방식이 달라
        # 충족으로 확정하지 않는다
        v = judge(n, base_profile(income_work=200, income_business=50), TODAY)
        c = check_of(v, "소득")
        self.assertEqual(c.status, UNKNOWN)
        self.assertIn("종합소득세", c.todo)
        self.assertIn("합산", c.detail)
        self.assertNotEqual(v.level, "제외")
        # 합산이 한도를 크게 넘어도 마찬가지 — 미확인이지 불충족이 아니다
        v = judge(n, base_profile(income_work=300, income_business=300), TODAY)
        self.assertEqual(check_of(v, "소득").status, UNKNOWN)

    def test_work_income_only_still_judges(self):
        """같은 금액이라도 근로소득만이면 판정이 선다 — 대조군."""
        n = mk("국민임대주택 입주자 모집")
        n.areas = [Area(name="059A", exclusive="59.91", units="100")]
        v = judge(n, base_profile(income_work=250), TODAY)
        self.assertEqual(check_of(v, "소득").status, MET)


class TestJudgeOfficialAssets(unittest.TestCase):
    def test_national_rental_asset_boundary(self):
        n = mk("국민임대주택 입주자 모집")
        v = judge(n, base_profile(income_work=300, assets=34_500), TODAY)
        self.assertEqual(check_of(v, "총자산").status, MET)
        v = judge(n, base_profile(income_work=300, assets=34_501), TODAY)
        self.assertEqual(check_of(v, "총자산").status, UNMET)
        self.assertEqual(v.level, "제외")

    def test_debt_reduces_assets(self):
        """총자산 기준은 순자산(자산-부채)으로 비교한다."""
        n = mk("국민임대주택 입주자 모집")
        v = judge(n, base_profile(income_work=300, assets=40_000,
                                  debts=10_000), TODAY)
        self.assertEqual(check_of(v, "총자산").status, MET)

    def test_car_boundary(self):
        n = mk("국민임대주택 입주자 모집")
        v = judge(n, base_profile(income_work=300, assets=10_000,
                                  car=4_542), TODAY)
        self.assertEqual(check_of(v, "자동차가액").status, MET)
        v = judge(n, base_profile(income_work=300, assets=10_000,
                                  car=4_543), TODAY)
        self.assertEqual(check_of(v, "자동차가액").status, UNMET)
        self.assertEqual(v.level, "제외")

    def test_student_happy_house_forbids_car(self):
        n = mk("대학생 행복주택 입주자 모집")
        p = base_profile(student=True, income_work=100, assets=5_000, car=500)
        v = judge(n, p, TODAY)
        c = check_of(v, "자동차")
        self.assertEqual(c.status, UNMET)
        self.assertIn("미보유", c.detail)
        self.assertEqual(v.level, "제외")

    def test_public_sale_assets_stay_unknown(self):
        """공공분양(뉴:홈)은 official=False — 자산 초과도 미확인까지만."""
        n = mk("서울 공공분양주택")
        v = judge(n, base_profile(income_work=300, assets=50_000,
                                  car=5_000), TODAY)
        self.assertEqual(v.unmet, [])
        self.assertEqual(v.level, "확인")
        self.assertTrue(any(not c.official for c in v.unknown))

    def test_purchase_rental_asset_over_is_unknown(self):
        """매입임대 자산 초과 — 순위별 기준이 달라 미확인."""
        n = mk("청년 매입임대주택 모집")
        v = judge(n, base_profile(income_work=300, assets=30_000), TODAY)
        c = check_of(v, "총자산")
        self.assertEqual(c.status, UNKNOWN)
        self.assertIn("순위", c.todo)


class TestJudgeHouseholdAndExtras(unittest.TestCase):
    def test_married_blocked_from_youth_purchase_rental(self):
        """청년매입임대는 LH 공식 기준으로 '미혼 청년' — 기혼이면 제외."""
        n = mk("청년 매입임대주택 모집")
        v = judge(n, base_profile(marital="기혼"), TODAY)
        c = check_of(v, "세대구성")
        self.assertEqual(c.status, UNMET)
        self.assertEqual(v.level, "제외")

    def test_hope_town_mortgage_extra_when_price_over_asset_cap(self):
        """주택가격 > 총자산기준(3억 6,200만원)이면 모기지 요건이 미확인으로 뜬다."""
        n = mk("성남 신혼희망타운", price_min=40_000)
        v = judge(n, newlywed_profile(income_work=500), TODAY)
        self.assertIsNotNone(check_of(v, "기타 요건"))
        # 가격이 기준 이하로 확인되면 띄우지 않는다
        n2 = mk("성남 신혼희망타운", price_min=30_000)
        v2 = judge(n2, newlywed_profile(income_work=500), TODAY)
        self.assertIsNone(check_of(v2, "기타 요건"))

    def test_happy_house_tier_unresolved_is_unknown(self):
        """행복주택인데 계층을 공고·프로필 어느 쪽으로도 못 좁히면 미확인."""
        n = mk("행복주택 입주자 모집")
        p = base_profile(birth="1970-01-01", income_work=300)   # 만 56세
        v = judge(n, p, TODAY)
        c = check_of(v, "세대구성")
        self.assertEqual(c.status, UNKNOWN)
        self.assertNotEqual(v.level, "추천")


class TestOfficialGuards(unittest.TestCase):
    def test_unofficial_standard_cannot_confirm_or_reject(self):
        """official=False 기준(뉴:홈)은 충족·불충족을 만들 수 없다."""
        for status in (MET, UNMET):
            c = Check("소득", status, "x", source_key="newhome-public-sale")
            self.assertEqual(c.status, UNKNOWN)

    def test_source_dict_contract_kept(self):
        """UI 계약 — source 에 {key,label,url,asof,official,note} 유지."""
        n = mk("국민임대주택 입주자 모집")
        v = judge(n, base_profile(income_work=300, assets=10_000), TODAY)
        d = check_of(v, "소득").to_dict()
        for key in ("key", "label", "url", "asof", "official", "note"):
            self.assertIn(key, d["source"])
        self.assertTrue(d["source"]["official"])
        self.assertIn("myhome.go.kr", d["source"]["url"])


# ============================================================ 2026-10 추가 기준
# 장기전세·영구임대·전세임대·청년안심주택·뉴:홈 유형별 기준의 회귀 테스트.
# 전부 공식 페이지에서 확인한 값이다 (마이홈 장기전세 2026년도 적용기준,
# 마이홈 영구임대·뉴홈 유형별 페이지, LH 전세임대포털, 서울시 청년안심주택).
class TestLongTermLeaseBoundaries(unittest.TestCase):
    """장기전세 — 마이홈 2026년도 적용기준 표와 ±1원 검산."""

    def test_1p_relaxed_boundary(self):
        """1인 100%+20%p 완화 = 4,576,036원 — 마이홈 표의 완성값."""
        self.assertEqual(compare_income("장기전세", 1, 4_576_036)[0], "충족")
        # 초과해도 85㎡ 초과분(기준 미확정)이 있을 수 있어 불충족을 안 만든다
        self.assertEqual(compare_income("장기전세", 1, 4_576_037)[0], "미확인")
        self.assertEqual(compare_income("장기전세", 1, 99_999_999)[0], "미확인")

    def test_3p_base_and_over60_tier(self):
        self.assertEqual(income_limit("장기전세", 3)[0], 8_168_429)   # 100%
        self.assertEqual(compare_income("장기전세", 3, 8_168_429)[0], "충족")
        # 전용 60㎡ 초과가 확정되면 120% (마이홈 표 3인 9,802,115)
        self.assertEqual(
            compare_income("장기전세", 3, 9_802_115, tier="전용60초과")[0],
            "충족")

    def test_asset_uses_lower_region_cap(self):
        """총자산이 지역별(서울 66,200/경기 41,700)이라 낮은 쪽이 하한이다."""
        lims = asset_limits("장기전세")
        self.assertEqual(lims["total"], 41_700)
        self.assertEqual(lims["car"], 4_542)
        self.assertTrue(lims["met_only"])     # 초과 = 서울 한도 가능성 → 미확인
        self.assertTrue(lims["source"]["official"])

    def test_judge_long_term_lease(self):
        """공고 제목 '장기전세' → 기준 매칭, met_only 라 제외는 안 만든다."""
        n = mk("제34차 장기전세주택 입주자 모집")
        v = judge(n, base_profile(income_work=457, assets=41_700), TODAY)
        self.assertEqual(check_of(v, "소득").status, MET)
        self.assertTrue(check_of(v, "소득").official)
        self.assertEqual(check_of(v, "총자산").status, MET)
        # 자산 초과 — 서울 한도(66,200) 가능성이 있어 미확인에 머문다
        v = judge(n, base_profile(income_work=457, assets=41_701), TODAY)
        self.assertEqual(check_of(v, "총자산").status, UNKNOWN)
        self.assertNotEqual(v.level, "제외")
        # 소득 초과 — 85㎡ 초과분 기준 미확정이라 역시 미확인
        v = judge(n, base_profile(income_work=600), TODAY)
        self.assertEqual(check_of(v, "소득").status, UNKNOWN)
        self.assertNotEqual(v.level, "제외")


class TestPermanentRentalBoundaries(unittest.TestCase):
    """영구임대 — 2순위 50%(1인 70%·2인 60%)가 보장 하한. 완화는 페이지 명시."""

    def test_relaxed_boundaries(self):
        # 1인 70% = 2,669,354 (마이홈 장기전세 페이지 50% 열과도 일치)
        self.assertEqual(compare_income("영구임대", 1, 2_669_354)[0], "충족")
        self.assertEqual(compare_income("영구임대", 1, 2_669_355)[0], "미확인")
        # 2인 60% / 3인 50%
        self.assertEqual(income_limit("영구임대", 2)[0], 3_519_762)
        self.assertEqual(compare_income("영구임대", 3, 4_084_215)[0], "충족")

    def test_over_income_never_rejects(self):
        """수급자 1순위·장애인 100% 가 공존 — 초과로 탈락을 확정하지 않는다."""
        self.assertEqual(compare_income("영구임대", 1, 99_999_999)[0], "미확인")

    def test_asset_amounts_not_confirmed(self):
        """자산 금액이 공식 페이지에 없다 — 금액 비교를 만들면 안 된다."""
        lims = asset_limits("영구임대")
        self.assertTrue(lims["applies"])
        self.assertIsNone(lims["total"])
        self.assertIsNone(lims["estate"])
        self.assertIsNone(lims["car"])
        self.assertTrue(lims["met_only"])


class TestJeonseRentalBoundaries(unittest.TestCase):
    """전세임대 — LH 전세임대포털의 순위 구조. 전부 met_only(수급자 특례 공존)."""

    def test_youth_mirrors_single_table(self):
        """청년 3순위 — 본인 소득, 1인 가구 100% = 3,813,363원."""
        self.assertEqual(compare_income("청년전세임대", 1, 3_813_363)[0], "충족")
        self.assertEqual(compare_income("청년전세임대", 1, 99_999_999)[0],
                         "미확인")
        lims = asset_limits("청년전세임대")
        self.assertEqual(lims["total"], 25_100)   # 행복주택 청년 준용(2026)
        self.assertEqual(lims["car"], 4_542)
        self.assertTrue(lims["met_only"])

    def test_newlywed_type1_boundary(self):
        """신혼 Ⅰ형 70%(배우자 소득 시 90%) — 2인 가구 ±1원."""
        self.assertEqual(
            compare_income("신혼부부전세임대", 2, 4_106_389, dual=False)[0],
            "충족")
        self.assertEqual(
            compare_income("신혼부부전세임대", 2, 5_279_643, dual=True)[0],
            "충족")
        # Ⅱ형(100~120%) 가능성 — 초과는 미확인까지만
        self.assertEqual(
            compare_income("신혼부부전세임대", 2, 5_279_644, dual=True)[0],
            "미확인")
        self.assertEqual(asset_limits("신혼부부전세임대")["total"], 34_500)

    def test_existing_house_floor_is_rank2(self):
        """기존주택 — 2순위 소득 50%가 비수급자도 보장되는 하한."""
        # 1인 50% = 1,906,682. 완화 적용 여부 미확정 → 70% 까지는 밴드
        self.assertEqual(compare_income("기존주택전세임대", 1, 1_906_682)[0],
                         "충족")
        self.assertEqual(compare_income("기존주택전세임대", 1, 1_906_683)[0],
                         "미확인")
        # 자산은 영구임대 준용인데 금액 미확정 — 비교를 만들지 않는다
        self.assertIsNone(asset_limits("기존주택전세임대")["total"])

    def test_judge_jeonse_rental(self):
        """공고 제목 '전세임대' → 기준 매칭과 공식 판정."""
        n = mk("2026년 기존주택 전세임대 입주자 모집")
        v = judge(n, base_profile(income_work=190), TODAY)   # 1,900,000
        self.assertEqual(check_of(v, "소득").status, MET)
        self.assertTrue(check_of(v, "소득").official)
        # 청년 전세임대는 본인 소득(1인 가구 기준)으로 판정한다
        n = mk("2026년 청년 전세임대 1순위 입주자 수시모집")
        v = judge(n, base_profile(income_work=381), TODAY)   # 3,810,000
        self.assertEqual(check_of(v, "소득").status, MET)


class TestYouthSafeHousing(unittest.TestCase):
    """청년안심주택 — 본인 100%(1인 가구 기준)가 전 트랙 보장 하한."""

    def test_income_floor_and_band(self):
        self.assertEqual(compare_income("청년안심주택", 1, 3_813_363)[0], "충족")
        # 민간 특공 120%·민간 일반공급(소득 무관)이 남아 있어 불충족은 없다
        self.assertEqual(compare_income("청년안심주택", 1, 4_576_037)[0],
                         "미확인")
        self.assertEqual(compare_income("청년안심주택", 1, 99_999_999)[0],
                         "미확인")

    def test_asset_floor_is_youth_cap(self):
        """청년 본인 2억 5,100만원이 가장 낮은 한도 (신혼은 3억 4,500만원)."""
        lims = asset_limits("청년안심주택")
        self.assertEqual(lims["total"], 25_100)
        self.assertIsNone(lims["car"])            # 자동차 기준 페이지 미기재
        self.assertTrue(lims["met_only"])
        self.assertTrue(lims["source"]["official"])
        self.assertIn("2026", lims["source"]["asof"])


class TestNewHomeByType(unittest.TestCase):
    """뉴:홈 유형별 — 일반공급 100% 하한 + met_only (특공 130~140% 공존)."""

    def test_income_floor_met_only(self):
        for key in standards.NEWHOME_SALE_KEYS:
            # 3인 100% = 8,168,429 — 이하면 어느 공급으로든 지원 가능
            self.assertEqual(compare_income(key, 3, 8_168_429)[0], "충족", key)
            # 초과는 특공(최대 140%) 가능성 — 불충족을 만들지 않는다
            self.assertEqual(compare_income(key, 3, 8_168_430)[0], "미확인", key)
            self.assertEqual(compare_income(key, 3, 99_999_999)[0], "미확인",
                             key)

    def test_share_and_choice_use_net_asset(self):
        """나눔형·선택형은 부동산가액이 아니라 순자산 3억 6,200만원을 본다."""
        for key in ("공공분양나눔형", "공공분양선택형"):
            lims = asset_limits(key)
            self.assertEqual(lims["total"], 36_200, key)
            self.assertIsNone(lims["estate"], key)
            self.assertTrue(lims["met_only"], key)
            self.assertTrue(lims["source"]["official"], key)

    def test_general_type_uses_estate(self):
        """일반형은 부동산 2억 1,550만원 — estate 로 담겨야 eligibility 가
        프로필의 real_estate(부동산가액)와 직접 비교할 수 있다."""
        lims = asset_limits("공공분양일반형")
        self.assertIsNone(lims["total"])
        self.assertEqual(lims["estate"], 21_550)
        self.assertEqual(lims["car"], 4_542)
        self.assertTrue(lims["source"]["official"])

    def test_untyped_public_sale_stays_unofficial(self):
        """유형 미상 공공분양은 official=False 유지 — 충족·불충족 불가."""
        self.assertEqual(STANDARDS_PUBLIC_SALE_SOURCE, "newhome-public-sale")
        self.assertFalse(is_official("newhome-public-sale"))
        for status in (MET, UNMET):
            c = Check("소득", status, "x", source_key="newhome-public-sale")
            self.assertEqual(c.status, UNKNOWN)


class TestNewSourcesRegistered(unittest.TestCase):
    NEW_KEYS = ("myhome-newhome-share", "myhome-newhome-choice",
                "myhome-newhome-general", "myhome-longterm-lease",
                "myhome-permanent-rental", "lh-jeonse-rental",
                "seoul-youth-safe-housing")

    def test_registered_official_with_url(self):
        """새 출처 전부 — rules 레지스트리 병합 + official + URL 보유."""
        for key in self.NEW_KEYS:
            self.assertIn(key, RULE_SOURCES, key)
            self.assertTrue(is_official(key), key)
            self.assertTrue(RULE_SOURCES[key]["url"].startswith("https://"),
                            key)

    def test_asof_or_notice_priority_documented(self):
        """기준연도가 페이지에 있으면 asof 에, 없으면 note 에 공고문 우선을
        남긴다 — 연도 불명인 값을 연도 확정처럼 보이게 하지 않는다."""
        self.assertIn("2026", RULE_SOURCES["myhome-longterm-lease"]["asof"])
        self.assertIn("2026", RULE_SOURCES["seoul-youth-safe-housing"]["asof"])
        for key in ("myhome-newhome-share", "myhome-newhome-choice",
                    "myhome-newhome-general", "lh-jeonse-rental",
                    "myhome-permanent-rental"):
            self.assertIsNone(RULE_SOURCES[key]["asof"], key)
            self.assertIn("공고문", RULE_SOURCES[key]["note"], key)


class TestNewStandardsFor(unittest.TestCase):
    def test_rental_type_mapping(self):
        self.assertEqual(standards_for("공공임대", ["장기전세"]), ["장기전세"])
        self.assertEqual(standards_for("공공임대", ["영구임대"]), ["영구임대"])
        self.assertEqual(standards_for("공공임대", ["청년안심", "청년"]),
                         ["청년안심주택"])
        self.assertEqual(standards_for("공공임대", ["전세임대"]),
                         ["기존주택전세임대"])
        self.assertEqual(standards_for("공공임대", ["전세임대", "청년"]),
                         ["청년전세임대"])
        self.assertEqual(standards_for("공공임대", ["전세임대", "신혼부부"]),
                         ["신혼부부전세임대"])

    def test_newhome_type_mapping(self):
        self.assertEqual(standards_for("공공분양", ["나눔형"]),
                         ["공공분양나눔형"])
        # 유형이 명시되면 특공 태그보다 유형 기준(자산 체계가 다르다)이 먼저다
        self.assertEqual(
            standards_for("공공분양", ["나눔형", "신혼부부", "특별공급"]),
            ["공공분양나눔형"])
        self.assertEqual(standards_for("공공분양", ["선택형"]),
                         ["공공분양선택형"])
        # 선택형은 '6년 분양전환 공공임대' 제목이라 kind 가 공공임대로 잡힌다
        self.assertEqual(standards_for("공공임대", ["선택형"]),
                         ["공공분양선택형"])
        self.assertEqual(standards_for("공공분양", ["일반형"]),
                         ["공공분양일반형"])
        # 일반형 + 특공 태그 — 특공 기준이 더 구체적이라 그쪽이 우선
        self.assertEqual(standards_for("공공분양", ["일반형", "생애최초"]),
                         ["생애최초특공"])
        # 유형 미상 공공분양은 기존대로 보수적 기준(official=False) 유지
        self.assertEqual(standards_for("공공분양", []), ["공공분양"])


# 유형 미상 공공분양 키가 가리키는 출처 — 테스트에서 상수로 고정
STANDARDS_PUBLIC_SALE_SOURCE = standards.STANDARDS["공공분양"]["source"]


if __name__ == "__main__":
    unittest.main(verbosity=2)
