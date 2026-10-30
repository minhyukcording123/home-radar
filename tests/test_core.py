"""네트워크 없이 도는 회귀 테스트.

    python -m unittest discover -s tests
"""
import unittest
from datetime import date

from app import profile
from app.eligibility import account_need, judge, kind_of
from app.models import Event, Notice
from app.rules import income_ratio, income_verdict
from app.sources.applyhome_cal import _finalize, _is_noise, _row_date
from app.sources.applyhome_detail import _field, _price, _receptions, detail_path
from app.util import parse_date, parse_period, to_sido

TODAY = date(2026, 10, 30)


def mk(title, category="", region="서울", **kw):
    return Notice(source="청약홈", uid="t", title=title, category=category,
                  region=region, **kw)


class TestUtil(unittest.TestCase):
    def test_parse_date_formats(self):
        for raw in ("2026-11-02", "20261102", "2026.11.02", "2026년 11월 2일"):
            self.assertEqual(parse_date(raw), date(2026, 11, 2), raw)

    def test_parse_date_rejects_garbage(self):
        for raw in ("", None, "미정", "2026-13-45"):
            self.assertIsNone(parse_date(raw), raw)

    def test_parse_period(self):
        b, e = parse_period("2026.11.10 ~ 2026.11.12")
        self.assertEqual((b, e), (date(2026, 11, 10), date(2026, 11, 12)))

    def test_to_sido(self):
        self.assertEqual(to_sido("서울특별시 영등포구 신길동"), "서울")
        self.assertEqual(to_sido("충청남도 천안시 서북구"), "충남")
        self.assertEqual(to_sido("경기"), "경기")


class TestKindOf(unittest.TestCase):
    """유형 판별은 순서에 민감하다 — 이름에 '임대'가 들어간 분양 공고가 실제로 있다."""

    def test_land_lease_public_sale_is_not_rental(self):
        # 회귀: '토지임대부 공공분양주택'이 '임대'로 분류되던 버그
        self.assertEqual(kind_of(mk("삼도이동 1지구 토지임대부 공공분양주택")), "공공분양")

    def test_equity_accumulation_is_public_sale(self):
        self.assertEqual(kind_of(mk("광교 자연앤 센트레빌 지분적립형 분양주택")), "공공분양")

    def test_national_rental_is_rental_despite_gukmin(self):
        # '국민'이 들어가도 국민임대는 분양이 아니다
        self.assertEqual(kind_of(mk("국민임대주택 예비입주자 모집")), "임대")

    def test_others(self):
        self.assertEqual(kind_of(mk("성남 신혼희망타운")), "공공분양")
        self.assertEqual(kind_of(mk("행복주택 입주자 모집")), "임대")
        self.assertEqual(kind_of(mk("OO 공공지원민간임대주택")), "민간임대")
        self.assertEqual(kind_of(mk("왕십리역 어반홈스", "오피스텔/도시형")), "오피스텔")
        self.assertEqual(kind_of(mk("청라 아크원 푸르지오", "1순위 2순위")), "민영분양")


class TestAccount(unittest.TestCase):
    def test_unranked_needs_no_account(self):
        n = mk("과천 디에트르", "무순위/잔여")
        self.assertEqual(account_need(n, "민영분양"), "불필요")

    def test_general_sale_uses_account(self):
        n = mk("청라 아크원", "1순위 2순위")
        self.assertEqual(account_need(n, "민영분양"), "사용")

    def test_happy_house_keeps_account(self):
        self.assertEqual(account_need(mk("행복주택 모집"), "임대"), "가입만")


class TestNotice(unittest.TestCase):
    def test_closed_by_end_date(self):
        self.assertTrue(mk("x", end=date(2026, 10, 29)).is_closed(TODAY))
        self.assertFalse(mk("x", end=date(2026, 10, 30)).is_closed(TODAY))

    def test_days_left(self):
        self.assertEqual(mk("x", end=date(2026, 11, 4)).days_left(TODAY), 5)
        self.assertIsNone(mk("x").days_left(TODAY))

    def test_closed_falls_back_to_posted(self):
        # 접수일 정보가 전혀 없으면 게시 후 21일이 지나면 끝난 것으로 본다
        self.assertTrue(mk("x", posted=date(2026, 10, 1)).is_closed(TODAY))
        self.assertFalse(mk("x", posted=date(2026, 10, 20)).is_closed(TODAY))

    def test_to_dict_serializes_dates(self):
        n = mk("x", begin=date(2026, 11, 1), end=date(2026, 11, 3))
        n.events = [Event(date=date(2026, 11, 1), code="01", label="특별공급")]
        d = n.to_dict()
        self.assertEqual(d["begin"], "2026-11-01")
        self.assertEqual(d["events"][0], {"date": "2026-11-01", "code": "01",
                                          "label": "특별공급"})
        self.assertEqual(d["key"], "청약홈:t")


class TestCalendarParsing(unittest.TestCase):
    def test_noise_rows_dropped(self):
        # RCEPT_SE 12 는 공고가 붙지 않은 빈 행 (실데이터에 185건 있었다)
        self.assertTrue(_is_noise({"RCEPT_SE": "12", "HOUSE_NM": None,
                                   "HOUSE_MANAGE_NO": None}))
        self.assertTrue(_is_noise({"RCEPT_SE": "01", "HOUSE_NM": "X",
                                   "HOUSE_MANAGE_NO": 1, "RESTDE_AT": "Y"}))
        self.assertFalse(_is_noise({"RCEPT_SE": "01", "HOUSE_NM": "X",
                                    "HOUSE_MANAGE_NO": 1}))

    def test_row_date(self):
        self.assertEqual(_row_date("202611", 2), date(2026, 11, 2))
        self.assertIsNone(_row_date("202611", None))
        self.assertIsNone(_row_date("202611", 31))   # 11월 31일은 없다

    def test_finalize_derives_period_and_labels(self):
        n = mk("GTX운정중앙역 시티프라디움", region="경기")
        n.events = [
            Event(date=date(2026, 11, 4), code="03", label="2순위"),
            Event(date=date(2026, 11, 2), code="01", label="특별공급"),
            Event(date=date(2026, 11, 3), code="02", label="1순위"),
        ]
        _finalize(n)
        self.assertEqual(n.begin, date(2026, 11, 2))
        self.assertEqual(n.end, date(2026, 11, 4))
        self.assertEqual(n.category, "특별공급 1순위 2순위")


class TestDetailParsing(unittest.TestCase):
    HTML = """
    <table>
      <tr><th>공급위치</th><td>서울특별시 영등포구 신길동 449-11</td></tr>
      <tr><th>공급규모</th><td>4세대</td></tr>
      <tr><th>모집공고일</th><td>2026-10-23</td></tr>
      <tr><th>당첨자 발표일</th><td>2026-11-06</td></tr>
    </table>
    <h3>청약접수</h3>
    <table>
      <tr><th>구분</th><th>해당지역</th><th>기타지역</th></tr>
      <tr><td>특별공급</td><td>2026.11.02</td><td>2026.11.02</td></tr>
      <tr><td>1순위</td><td>2026.11.03 ~ 2026.11.04</td><td>2026.11.04</td></tr>
    </table>
    <h3>당첨자 발표</h3>
    <h3>공급금액</h3>
    <table><tr><th>타입</th><th>금액</th></tr>
      <tr><td>84A</td><td>59,400</td></tr><tr><td>84B</td><td>99,400</td></tr></table>
    <h3>기타사항</h3>
    """

    def test_field(self):
        self.assertEqual(_field(self.HTML, "공급위치"),
                         "서울특별시 영등포구 신길동 449-11")
        self.assertEqual(_field(self.HTML, "공급규모"), "4세대")
        self.assertEqual(_field(self.HTML, "없는항목"), "")

    def test_receptions(self):
        rc = _receptions(self.HTML)
        types = [r["type"] for r in rc]
        self.assertEqual(types, ["특별공급", "1순위"])
        self.assertEqual(rc[1]["periods"][0],
                         {"text": "2026.11.03 ~ 2026.11.04",
                          "begin": "2026-11-03", "end": "2026-11-04"})

    def test_price_ignores_small_numbers(self):
        # 회귀: 연도(2,026)·전용면적이 분양가 하한으로 잡히던 문제
        self.assertEqual(_price(self.HTML), "59,400~99,400만원")

    def test_detail_path_by_house_secd(self):
        self.assertIn("selectAPTLttotPblancDetail", detail_path("01"))
        self.assertIn("Remndr", detail_path("04"))
        self.assertIn("PRMO", detail_path("02"))


class TestRules(unittest.TestCase):
    def test_income_ratio(self):
        # 1인 가구 기준 418만원 → 월 330만원은 약 78.9%
        self.assertEqual(income_ratio(330, 1), 78.9)
        self.assertIsNone(income_ratio(None, 1))

    def test_income_verdict(self):
        self.assertEqual(income_verdict(78.9, 160), "여유")
        self.assertEqual(income_verdict(150.0, 160), "경계")
        self.assertEqual(income_verdict(167.5, 160), "초과")
        self.assertEqual(income_verdict(None, 160), "확인")
        self.assertEqual(income_verdict(500.0, None), "확인")


class TestJudge(unittest.TestCase):
    def base(self, **kw):
        return profile.merge({"birth": "1994-07-15", "regions": ["서울", "경기"],
                              "household_size": 1, **kw})

    def test_region_blocks(self):
        v = judge(mk("아무 단지", region="제주"), self.base(), TODAY)
        self.assertEqual(v.level, "제외")
        self.assertIn("관심 지역 아님 (제주)", v.blockers)

    def test_homeowner_blocked(self):
        v = judge(mk("청라 아크원", "1순위"), self.base(homeless=False), TODAY)
        self.assertEqual(v.level, "제외")
        self.assertIn("유주택자는 대부분 지원 불가", v.blockers)

    def test_no_account_blocked_for_account_using_notice(self):
        v = judge(mk("청라 아크원", "1순위"), self.base(has_account=False), TODAY)
        self.assertEqual(v.level, "제외")
        self.assertIn("청약통장이 있어야 지원할 수 있는 공고", v.blockers)

    def test_no_account_still_ok_for_unranked(self):
        v = judge(mk("과천 디에트르", "무순위/잔여"), self.base(has_account=False), TODAY)
        self.assertEqual(v.level, "추천")
        self.assertEqual(v.account, "불필요")

    def test_elderly_only_notice_blocked_for_young(self):
        v = judge(mk("고령자 복지주택 입주자 모집"), self.base(), TODAY)
        self.assertEqual(v.level, "제외")

    def test_youth_notice_recommended(self):
        v = judge(mk("청년 매입임대주택 모집"), self.base(), TODAY)
        self.assertEqual(v.level, "추천")

    def test_youth_notice_blocked_when_too_old(self):
        v = judge(mk("청년 매입임대주택 모집"), self.base(birth="1975-01-01"), TODAY)
        self.assertEqual(v.level, "제외")

    def test_multichild_blocked_without_children(self):
        v = judge(mk("다자녀 특별공급 모집"), self.base(), TODAY)
        self.assertEqual(v.level, "제외")

    def test_newlywed_blocked_for_single(self):
        v = judge(mk("신혼희망타운 입주자 모집"), self.base(), TODAY)
        self.assertEqual(v.level, "제외")

    def test_followup_notice_ignored(self):
        v = judge(mk("OO단지 당첨자 서류제출 안내"), self.base(), TODAY)
        self.assertEqual(v.level, "무시")

    def test_unwanted_kind_blocked(self):
        prof = self.base(want={"오피스텔": False})
        v = judge(mk("왕십리역 어반홈스", "오피스텔/도시형"), prof, TODAY)
        self.assertEqual(v.level, "제외")

    def test_income_over_downgrades_but_never_excludes(self):
        """소득·자산 초과는 제외 근거가 아니다 — 추천을 '확인'으로만 내린다."""
        prof = self.base(monthly_income=700, assets=50_000, car=5_000)
        v = judge(mk("광교 공공분양주택"), prof, TODAY)
        self.assertEqual(v.level, "확인")
        self.assertEqual(v.blockers, [])
        self.assertEqual(v.income["verdict"], "초과")
        self.assertEqual(v.asset["verdict"], "초과")
        self.assertTrue(any("160%" in t for t in v.tips))

    def test_low_income_keeps_recommendation(self):
        prof = self.base(monthly_income=260, assets=8_000)
        v = judge(mk("광교 공공분양주택"), prof, TODAY)
        self.assertEqual(v.level, "추천")
        self.assertEqual(v.income["verdict"], "여유")

    def test_verdict_always_has_a_reason(self):
        v = judge(mk("청라 아크원", "1순위"), self.base(), TODAY)
        self.assertTrue(v.reasons)
        self.assertIn(v.level, ("추천", "확인", "제외", "무시"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
