"""네트워크 없이 도는 회귀 테스트.

    python -m unittest discover -s tests

규칙(app/rules.py)을 바꾸면 RULES_VERSION 을 올리고 여기에 테스트를 추가한다.
"""
import json
import os
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from app import dedup, profile, store
from app.checks import (FILTER, MET, SIGNAL, UNKNOWN, UNMET, Check, Verdict,
                        missing)
from app.eligibility import account_need, judge, kind_of, tags_of
from app.models import Area, Event, Notice
from app.rules import (RULES_VERSION, fmt_won, income_base, income_ratio,
                       income_signal, net_assets, recognized_monthly_income)
from app.sources.applyhome_cal import _finalize, _is_noise, _row_date
from app.sources.applyhome_detail import (_areas, _field, _price_range,
                                          _receptions, detail_path)
from app.sources.sh import parse_list as sh_parse_list
from app.sources.youth import apply_period
from app.util import KST, parse_date, parse_period, to_sido, today_kst

TODAY = date(2026, 10, 30)


def mk(title, category="", region="서울", **kw):
    return Notice(source="청약홈", uid="t", title=title, category=category,
                  region=region, **kw)


def base_profile(**kw):
    return profile.merge({"birth": "1994-07-15", "regions": ["서울", "경기"],
                          "household_size": 1, **kw})


# ============================================================ 유틸 · 타임존
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

    def test_kst_offset(self):
        self.assertEqual(KST.utcoffset(None), timedelta(hours=9))

    def test_kst_conversion_crosses_date_line(self):
        """UTC 15:00 을 넘기면 한국은 이미 다음 날이다."""
        utc_evening = datetime(2026, 10, 30, 16, 30, tzinfo=timezone.utc)
        self.assertEqual(utc_evening.astimezone(KST).date(), date(2026, 10, 31))

    def test_today_kst_uses_kst_not_utc(self):
        """today_kst 가 UTC 날짜를 쓰면 D-day 가 하루 밀린다."""
        utc_evening = datetime(2026, 10, 30, 16, 30, tzinfo=timezone.utc)
        with mock.patch("app.util.datetime") as dt:
            dt.now.side_effect = lambda tz=None: utc_evening.astimezone(tz or timezone.utc)
            self.assertEqual(today_kst(), date(2026, 10, 31))
            dt.now.assert_called_with(KST)

    def test_fmt_won(self):
        self.assertEqual(fmt_won(99_400), "9억 9,400만원")
        self.assertEqual(fmt_won(60_000), "6억원")
        self.assertEqual(fmt_won(8_500), "8,500만원")
        self.assertEqual(fmt_won(0), "0원")
        self.assertEqual(fmt_won(None), "")


# ============================================================ 유형 · 태그
class TestKindOf(unittest.TestCase):
    """순서에 민감하다 — 이름에 '임대'가 들어간 분양 공고가 실제로 있다."""

    def test_land_lease_public_sale_is_not_rental(self):
        self.assertEqual(kind_of(mk("삼도이동 1지구 토지임대부 공공분양주택")), "공공분양")

    def test_equity_accumulation_is_public_sale(self):
        self.assertEqual(kind_of(mk("광교 자연앤 센트레빌 지분적립형 분양주택")), "공공분양")

    def test_national_rental_is_rental_despite_gukmin(self):
        self.assertEqual(kind_of(mk("국민임대주택 예비입주자 모집")), "공공임대")

    def test_unranked_is_its_own_kind(self):
        self.assertEqual(kind_of(mk("과천 디에트르", "무순위/잔여")), "무순위")

    def test_others(self):
        self.assertEqual(kind_of(mk("성남 신혼희망타운")), "공공분양")
        self.assertEqual(kind_of(mk("행복주택 입주자 모집")), "공공임대")
        self.assertEqual(kind_of(mk("OO 공공지원민간임대주택")), "민간임대")
        self.assertEqual(kind_of(mk("왕십리역 어반홈스", "오피스텔/도시형")), "오피스텔")
        self.assertEqual(kind_of(mk("청라 아크원 푸르지오", "1순위 2순위")), "민영분양")

    def test_tags(self):
        t = tags_of(mk("청년 신혼부부 행복주택 모집", "특별공급"))
        for want in ("청년", "신혼부부", "행복주택", "특별공급"):
            self.assertIn(want, t)


class TestAccount(unittest.TestCase):
    def test_unranked_needs_no_account(self):
        self.assertEqual(account_need(mk("과천", "무순위/잔여"), "무순위"), "불필요")

    def test_general_sale_uses_account(self):
        self.assertEqual(account_need(mk("청라 아크원", "1순위 2순위"), "민영분양"), "사용")

    def test_happy_house_keeps_account(self):
        self.assertEqual(account_need(mk("행복주택 모집"), "공공임대"), "가입만")


# ============================================================ 일정 · D-day
class TestNoticeDates(unittest.TestCase):
    def test_closed_by_end_date(self):
        self.assertTrue(mk("x", end=date(2026, 10, 29)).is_closed(TODAY))
        self.assertFalse(mk("x", end=date(2026, 10, 30)).is_closed(TODAY))

    def test_begin_and_end_dday_are_separate(self):
        n = mk("x", begin=date(2026, 11, 4), end=date(2026, 11, 8))
        self.assertEqual(n.days_to_begin(TODAY), 5)
        self.assertEqual(n.days_left(TODAY), 9)

    def test_dday_none_when_date_missing(self):
        n = mk("x", begin=date(2026, 11, 4))
        self.assertEqual(n.days_to_begin(TODAY), 5)
        self.assertIsNone(n.days_left(TODAY))
        self.assertIsNone(n.days_to_announce(TODAY))

    def test_phase(self):
        self.assertEqual(mk("x").phase(TODAY), "미정")
        self.assertEqual(mk("x", begin=date(2026, 11, 4),
                            end=date(2026, 11, 8)).phase(TODAY), "예정")
        self.assertEqual(mk("x", begin=date(2026, 10, 29),
                            end=date(2026, 11, 2)).phase(TODAY), "접수중")
        self.assertEqual(mk("x", begin=date(2026, 10, 20),
                            end=date(2026, 10, 25)).phase(TODAY), "마감")
        self.assertEqual(mk("x", begin=date(2026, 10, 20), end=date(2026, 10, 25),
                            announce=date(2026, 11, 5)).phase(TODAY), "발표대기")

    def test_phase_without_begin_does_not_claim_open(self):
        """회귀: 시작일을 모르면 '접수중'이라고 단정하지 않는다.

        LH 상세 API 가 미승인이면 마감일만 온다. 그때 '접수중'으로 찍으면
        아직 시작하지 않은 공고까지 접수중으로 보여 준다.
        """
        self.assertEqual(mk("x", end=date(2026, 11, 8)).phase(TODAY),
                         "마감전(시작일 미상)")
        # 마감일이 지났으면 그대로 마감이다
        self.assertEqual(mk("x", end=date(2026, 10, 20)).phase(TODAY), "마감")
        # 시작일을 알면 정상 판정
        self.assertEqual(mk("x", begin=date(2026, 10, 29),
                            end=date(2026, 11, 8)).phase(TODAY), "접수중")

    def test_closed_falls_back_to_posted(self):
        self.assertTrue(mk("x", posted=date(2026, 10, 1)).is_closed(TODAY))
        self.assertFalse(mk("x", posted=date(2026, 10, 20)).is_closed(TODAY))

    def test_to_dict_serializes(self):
        n = mk("x", begin=date(2026, 11, 1))
        n.events = [Event(date=date(2026, 11, 1), code="01", label="특별공급")]
        n.areas = [Area(name="084A", exclusive="84.97", units="120")]
        d = n.to_dict()
        self.assertEqual(d["begin"], "2026-11-01")
        self.assertEqual(d["events"][0]["date"], "2026-11-01")
        self.assertEqual(d["areas"][0]["name"], "084A")
        self.assertEqual(d["key"], "청약홈:t")


# ============================================================ 수집 파싱
class TestCalendarParsing(unittest.TestCase):
    def test_noise_rows_dropped(self):
        self.assertTrue(_is_noise({"RCEPT_SE": "12", "HOUSE_NM": None,
                                   "HOUSE_MANAGE_NO": None}))
        self.assertTrue(_is_noise({"RCEPT_SE": "01", "HOUSE_NM": "X",
                                   "HOUSE_MANAGE_NO": 1, "RESTDE_AT": "Y"}))
        self.assertFalse(_is_noise({"RCEPT_SE": "01", "HOUSE_NM": "X",
                                    "HOUSE_MANAGE_NO": 1}))

    def test_row_date(self):
        self.assertEqual(_row_date("202611", 2), date(2026, 11, 2))
        self.assertIsNone(_row_date("202611", None))
        self.assertIsNone(_row_date("202611", 31))

    def test_finalize_derives_period_and_labels(self):
        n = mk("GTX운정중앙역", region="경기")
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
    <h3>공급대상</h3>
    <table>
      <tr><th>주택형</th><th>주택공급면적</th><th>공급세대수</th></tr>
      <tr><td>084.9876A</td><td>112.3456</td><td>120</td></tr>
      <tr><td>059.9100B</td><td>84.1200</td><td>60</td></tr>
    </table>
    <h3>공급금액</h3>
    <table><tr><th>타입</th><th>금액</th></tr>
      <tr><td>84A</td><td>59,400</td></tr><tr><td>84B</td><td>99,400</td></tr></table>
    <h3>기타사항</h3>
    """

    def test_field(self):
        self.assertEqual(_field(self.HTML, "공급위치"),
                         "서울특별시 영등포구 신길동 449-11")
        self.assertEqual(_field(self.HTML, "없는항목"), "")

    def test_receptions(self):
        rc = _receptions(self.HTML)
        self.assertEqual([r["type"] for r in rc], ["특별공급", "1순위"])
        self.assertEqual(rc[1]["periods"][0],
                         {"text": "2026.11.03 ~ 2026.11.04",
                          "begin": "2026-11-03", "end": "2026-11-04"})

    def test_price_range_in_eok(self):
        """회귀: 연도(2,026)·전용면적이 분양가 하한으로 잡히던 문제."""
        text, lo, hi = _price_range(self.HTML)
        self.assertEqual((lo, hi), (59_400, 99_400))
        self.assertEqual(text, "5억 9,400만원 ~ 9억 9,400만원")

    def test_areas(self):
        a = _areas(self.HTML)
        self.assertEqual(len(a), 2)
        self.assertEqual(a[0]["name"], "084.9876A")
        self.assertEqual(a[0]["exclusive"], "112.3456")
        self.assertEqual(a[0]["units"], "120")

    def test_detail_path_by_house_secd(self):
        self.assertIn("selectAPTLttotPblancDetail", detail_path("01"))
        self.assertIn("Remndr", detail_path("04"))
        self.assertIn("PRMO", detail_path("02"))


class TestAreaTableWithSpans(unittest.TestCase):
    """회귀: rowspan 때문에 열이 밀려 '128 / 230 / 358' 같은 값이 들어오던 문제."""

    HTML = """
    <h3>공급대상</h3>
    <table>
     <tr>
       <th rowspan="2">주택형</th>
       <th colspan="2">공급면적(㎡)</th>
       <th colspan="3">공급세대수</th>
     </tr>
     <tr><th>주거전용</th><th>주거공용</th>
         <th>특별공급</th><th>일반공급</th><th>계</th></tr>
     <tr>
       <td rowspan="2">084.9876A</td>
       <td>84.9876</td><td>27.4523</td><td>80</td><td>40</td><td>120</td>
     </tr>
     <tr><td>84.9900</td><td>27.5000</td><td>10</td><td>5</td><td>15</td></tr>
     <tr><td>059.9100B</td>
         <td>59.9100</td><td>24.2100</td><td>40</td><td>20</td><td>60</td></tr>
     <tr><td>계</td><td></td><td></td><td>130</td><td>65</td><td>195</td></tr>
    </table>
    <h3>공급금액</h3>
    """

    def test_grid_expands_spans(self):
        from bs4 import BeautifulSoup

        from app.sources.applyhome_detail import _table_grid
        grid = _table_grid(BeautifulSoup(self.HTML, "html.parser").find("table"))
        # rowspan 으로 묶인 주택형이 다음 행에도 채워져야 한다
        self.assertEqual(grid[2][0][0], "084.9876A")
        self.assertEqual(grid[3][0][0], "084.9876A")
        # colspan 으로 펼쳐진 머리글
        self.assertEqual(grid[0][1][0], "공급면적(㎡)")
        self.assertEqual(grid[0][2][0], "공급면적(㎡)")

    def test_areas_align_with_two_row_header(self):
        a = _areas(self.HTML)
        self.assertEqual(a[0], {"name": "084.9876A", "exclusive": "84.9876",
                                "units": "120"})
        self.assertEqual(a[1]["name"], "084.9876A")       # rowspan 이어받음
        self.assertEqual(a[1]["exclusive"], "84.9900")
        self.assertEqual(a[2], {"name": "059.9100B", "exclusive": "59.9100",
                                "units": "60"})

    def test_total_row_dropped(self):
        self.assertTrue(all(x["name"] != "계" for x in _areas(self.HTML)))

    def test_units_prefers_total_column(self):
        """세대수는 특별공급(80)이 아니라 합계(120)를 보여줘야 쓸모 있다."""
        self.assertEqual(_areas(self.HTML)[0]["units"], "120")


class TestShParsing(unittest.TestCase):
    HTML = """<div class="board-list"><table>
      <thead><tr><th>번호</th><th>청약유형</th><th>공고명</th>
        <th>공고게시일</th><th>발표일</th><th>모집상태</th></tr></thead>
      <tbody><tr>
        <td>12</td><td>장기전세</td>
        <td><a href="/site/main/sh/publicLease/view?seq=98765">2026년 장기전세주택 모집</a></td>
        <td>2026-10-20</td><td>2026-12-01</td><td>모집중</td>
      </tr></tbody></table></div>"""

    def test_parse_list(self):
        rows = sh_parse_list(self.HTML, "publicLease")
        self.assertEqual(len(rows), 1)
        n = rows[0]
        self.assertEqual(n.uid, "98765")
        self.assertEqual(n.source_id, "sh")
        self.assertEqual(n.region, "서울")
        self.assertIn("공공임대", n.category)
        self.assertIn("장기전세", n.category)
        self.assertEqual(n.posted, date(2026, 10, 20))
        self.assertEqual(n.announce, date(2026, 12, 1))
        # SH 목록에는 접수기간이 없다 — 없는 걸 있다고 하지 않는다
        self.assertIsNone(n.begin)
        self.assertIsNone(n.end)


class TestYouthParsing(unittest.TestCase):
    def test_apply_period_from_content(self):
        content = "<p>청약신청 : ‘26. 10. 11. (일) 09:00 ~ 10. 15. (목) 23:00</p>"
        b, e = apply_period(content, date(2026, 10, 11))
        self.assertEqual((b, e), (date(2026, 10, 11), date(2026, 10, 15)))

    def test_apply_period_rejects_absurd_range(self):
        content = "<p>청약신청 : 2026. 1. 1. ~ 2026. 12. 31.</p>"
        b, e = apply_period(content, date(2026, 1, 1))
        self.assertIsNone(e)   # 60일 초과는 버린다


# ============================================================ 중복 · 정정
class TestDedup(unittest.TestCase):
    def test_exact_duplicate_same_source(self):
        a = mk("더샵 청주그리니티", begin=date(2026, 11, 2), end=date(2026, 11, 4))
        a.uid = "1"
        b = mk("더샵 청주그리니티", begin=date(2026, 11, 2), end=date(2026, 11, 4))
        b.uid = "2"
        kept, merged = dedup.dedupe([a, b])
        self.assertEqual(len(kept), 1)
        self.assertEqual(len(merged), 1)

    def test_different_period_is_not_duplicate(self):
        a = mk("같은단지", begin=date(2026, 11, 2), end=date(2026, 11, 4))
        a.uid = "1"
        b = mk("같은단지", begin=date(2026, 12, 2), end=date(2026, 12, 4))
        b.uid = "2"
        kept, _ = dedup.dedupe([a, b])
        self.assertEqual(len(kept), 2)

    def test_cross_source_duplicate_prefers_official(self):
        ah = Notice(source="청약홈", source_id="applyhome", uid="1",
                    title="성남 신혼희망타운", region="경기", begin=date(2026, 11, 2))
        lh = Notice(source="LH", source_id="lh", uid="9",
                    title="성남 신혼희망타운", region="경기", begin=date(2026, 11, 3))
        kept, merged = dedup.dedupe([ah, lh])
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0].source_id, "lh")   # 공식 API 우선
        self.assertEqual(merged[0]["dropped"], ah.key)

    def test_union_cancelled_units_is_not_a_correction(self):
        """회귀: '(조합원 취소분)' 을 '취소 정정공고'로 오탐하던 문제."""
        for title in ("야목역 서희스타힐스(조합원 취소분)",
                      "더샵 시에르네(조합원취소분)(2회차)",
                      "OO 계약취소주택 공급"):
            self.assertEqual(dedup.correction_marker(title), "", title)

    def test_real_corrections_detected(self):
        self.assertEqual(dedup.correction_marker("OO단지 정정공고"), "정정")
        self.assertEqual(dedup.correction_marker("OO단지 모집기간 연장"), "연장")
        self.assertEqual(dedup.correction_marker("OO단지 추가모집공고"), "추가모집")
        self.assertEqual(dedup.correction_marker("OO단지 공고 취소"), "취소")

    def test_link_corrections_to_base(self):
        base = mk("한강뷰 아파트 입주자 모집", region="서울")
        base.uid = "1"
        base.posted = date(2026, 10, 1)
        fix = mk("한강뷰 아파트 입주자 모집 정정공고", region="서울")
        fix.uid = "2"
        found = dedup.link_corrections([base, fix])
        self.assertEqual(fix.correction_of, base.key)
        self.assertEqual(found[0]["marker"], "정정")
        self.assertIn(fix.key, base.extra["corrected_by"])


# ============================================================ 저장소
class TestStoreBaseline(unittest.TestCase):
    """'첫 수집을 당일 신규로 표시하지 않기' 를 보장한다."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.patches = [
            mock.patch.object(store, "DATA", d),
            mock.patch.object(store, "DETAILS", d / "details"),
            mock.patch.object(store, "SEEN_FILE", d / "seen.json"),
            mock.patch.object(store, "REVISIONS_FILE", d / "revisions.json"),
            mock.patch.object(store, "SOURCE_STATUS_FILE", d / "src.json"),
            mock.patch.object(store, "BOOKMARKS_FILE", d / "bm.json"),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def test_first_run_is_baseline_not_new(self):
        r = store.mark_seen([("청약홈:a", "applyhome"), ("청약홈:b", "applyhome")],
                            TODAY)
        self.assertEqual(r["new"], [])
        self.assertEqual(r["baselined"], {"applyhome": 2})
        self.assertTrue(store.is_baseline("청약홈:a"))

    def test_second_run_new_notices_are_new(self):
        store.mark_seen([("청약홈:a", "applyhome")], TODAY)
        r = store.mark_seen([("청약홈:a", "applyhome"), ("청약홈:c", "applyhome")],
                            TODAY + timedelta(days=1))
        self.assertEqual(r["new"], ["청약홈:c"])
        self.assertFalse(store.is_baseline("청약홈:c"))

    def test_newly_added_source_gets_its_own_baseline(self):
        """수집원을 나중에 붙여도 그 첫 배치가 '오늘의 신규'로 쏟아지지 않는다."""
        store.mark_seen([("청약홈:a", "applyhome")], TODAY)
        r = store.mark_seen(
            [("청약홈:a", "applyhome"), ("SH:1", "sh"), ("SH:2", "sh")],
            TODAY + timedelta(days=1))
        self.assertEqual(r["new"], [])
        self.assertEqual(r["baselined"], {"sh": 2})
        self.assertEqual(r["baseline_sources"], ["sh"])

    def test_legacy_seen_format_migrates_as_baseline(self):
        (Path(self.tmp.name) / "seen.json").write_text(
            json.dumps({"청약홈:old": "2026-10-29"}), encoding="utf-8")
        self.assertTrue(store.is_baseline("청약홈:old"))
        self.assertEqual(store.first_seen("청약홈:old"), "2026-10-29")

    def test_revision_tracking(self):
        n = {"key": "청약홈:a", "end": "2026-11-04", "begin": "2026-11-02"}
        self.assertEqual(store.track_revisions([n]), {})       # 최초 등록
        n2 = dict(n, end="2026-11-07")
        changed = store.track_revisions([n2])
        self.assertIn("청약홈:a", changed)
        diff = changed["청약홈:a"][0]
        self.assertEqual((diff["field"], diff["from"], diff["to"]),
                         ("end", "2026-11-04", "2026-11-07"))

    def test_revision_no_change_is_quiet(self):
        n = {"key": "청약홈:a", "end": "2026-11-04"}
        store.track_revisions([n])
        self.assertEqual(store.track_revisions([n]), {})

    def test_source_status_records_success_and_failure(self):
        store.record_source("sh", ok=True, count=33)
        st = store.load_source_status()["sh"]
        self.assertEqual(st["state"], "정상")
        self.assertEqual(st["last_success_count"], 33)
        store.record_source("sh", ok=False, error="timeout")
        st = store.load_source_status()["sh"]
        self.assertEqual(st["state"], "실패")
        self.assertEqual(st["consecutive_failures"], 1)
        self.assertTrue(st["last_success_at"])   # 마지막 성공 시각은 남아 있다

    def test_bookmark_toggle(self):
        self.assertTrue(store.toggle_bookmark("청약홈:a")["bookmarked"])
        self.assertFalse(store.toggle_bookmark("청약홈:a")["bookmarked"])


# ============================================================ 프로필 파생값
class TestProfileDerived(unittest.TestCase):
    def test_age(self):
        self.assertEqual(profile.age({"birth": "1994-07-15"}, TODAY), 32)
        self.assertIsNone(profile.age({"birth": ""}, TODAY))

    def test_residence_and_account_months(self):
        p = base_profile(residence_since="2019-03", account_opened="2019-04-01")
        self.assertEqual(profile.residence_months(p, TODAY), 91)
        self.assertEqual(profile.account_months(p, TODAY), 90)

    def test_missing_dates_give_none(self):
        p = base_profile()
        self.assertIsNone(profile.residence_months(p, TODAY))
        self.assertIsNone(profile.account_months(p, TODAY))

    def test_newlywed_by_marriage_years(self):
        self.assertTrue(profile.is_newlywed(
            base_profile(marital="기혼", marriage_date="2021-05"), TODAY))
        self.assertFalse(profile.is_newlywed(
            base_profile(marital="기혼", marriage_date="2010-05"), TODAY))
        self.assertTrue(profile.is_newlywed(base_profile(marital="예비신혼"), TODAY))

    def test_legacy_monthly_income_migrates(self):
        p = profile.merge({"monthly_income": 330})
        self.assertEqual(p["income_work"], 330)

    def test_income_split_sums_for_reference_only(self):
        p = base_profile(income_work=300, income_business=50)
        self.assertEqual(recognized_monthly_income(p), 350)
        self.assertEqual(income_ratio(p), round(350 * 10000 / income_base(1) * 100, 1))

    def test_net_assets(self):
        self.assertEqual(net_assets(base_profile(assets=30_000, debts=12_000)), 18_000)
        self.assertIsNone(net_assets(base_profile()))


class TestIncomeSignal(unittest.TestCase):
    def test_signals(self):
        self.assertEqual(income_signal(78.9, 160), "여유")
        self.assertEqual(income_signal(150.0, 160), "경계")
        self.assertEqual(income_signal(167.5, 160), "초과")
        self.assertEqual(income_signal(None, 160), "확인")
        self.assertEqual(income_signal(500.0, None), "확인")


# ============================================================ Check 안전장치
class TestCheckSafety(unittest.TestCase):
    def test_sample_source_cannot_confirm_or_reject(self):
        """샘플 기준은 충족도 불충족도 만들 수 없다 — 항상 미확인으로 내려앉는다."""
        for status in (MET, UNMET):
            c = Check("소득", status, "x", source_key="sample-income")
            self.assertEqual(c.status, UNKNOWN)
            self.assertEqual(c.category, SIGNAL)
            self.assertTrue(c.todo)

    def test_official_source_keeps_status(self):
        c = Check("무주택", MET, "본인 무주택", source_key="supply-rule")
        self.assertEqual(c.status, MET)
        self.assertTrue(c.official)

    def test_unknown_blocks_recommendation(self):
        v = Verdict()
        v.add(Check("무주택", MET, "ok", source_key="profile"))
        v.add(missing("소득", "근로소득"))
        self.assertEqual(v.settle(), "확인")

    def test_all_met_recommends(self):
        v = Verdict()
        v.add(Check("무주택", MET, "ok", source_key="profile"))
        v.add(Check("연령", MET, "ok", source_key="notice-title"))
        self.assertEqual(v.settle(), "추천")

    def test_official_unmet_excludes(self):
        v = Verdict()
        v.add(Check("무주택", UNMET, "유주택", source_key="supply-rule"))
        self.assertEqual(v.settle(), "제외")

    def test_filter_block_excludes_and_is_labelled(self):
        v = Verdict()
        v.add(Check("관심 지역", UNMET, "제주", source_key="profile", category=FILTER))
        self.assertEqual(v.settle(), "제외")
        self.assertEqual(v.unmet[0].category, FILTER)

    def test_verdict_carries_rules_version(self):
        self.assertEqual(Verdict().rules_version, RULES_VERSION)


# ============================================================ 판정
class TestJudge(unittest.TestCase):
    def test_region_filter_blocks(self):
        v = judge(mk("아무 단지", region="제주"), base_profile(), TODAY)
        self.assertEqual(v.level, "제외")
        self.assertEqual(v.unmet[0].name, "관심 지역")

    def test_homeowner_blocked(self):
        v = judge(mk("청라 아크원", "1순위"), base_profile(homeless=False), TODAY)
        self.assertEqual(v.level, "제외")
        self.assertTrue(any(c.name == "무주택" for c in v.unmet))

    def test_no_account_blocked_for_account_using_notice(self):
        v = judge(mk("청라 아크원", "1순위"), base_profile(has_account=False), TODAY)
        self.assertEqual(v.level, "제외")

    def test_no_account_fine_for_unranked(self):
        v = judge(mk("과천 디에트르", "무순위/잔여"),
                  base_profile(has_account=False), TODAY)
        self.assertNotEqual(v.level, "제외")

    def test_elderly_only_blocked_for_young(self):
        v = judge(mk("고령자 복지주택 입주자 모집"), base_profile(), TODAY)
        self.assertEqual(v.level, "제외")

    def test_youth_notice_age_ok(self):
        v = judge(mk("청년 매입임대주택 모집"), base_profile(), TODAY)
        self.assertTrue(any(c.name == "연령" and c.status == MET for c in v.checks))

    def test_youth_notice_blocked_when_too_old(self):
        v = judge(mk("청년 매입임대주택 모집"),
                  base_profile(birth="1975-01-01"), TODAY)
        self.assertEqual(v.level, "제외")

    def test_missing_birth_gives_unknown_not_met(self):
        v = judge(mk("청년 매입임대주택 모집"), base_profile(birth=""), TODAY)
        self.assertNotEqual(v.level, "추천")
        self.assertTrue(any(c.name == "연령" and c.status == UNKNOWN
                            for c in v.checks))

    def test_multichild_blocked_without_children(self):
        v = judge(mk("다자녀 특별공급 모집"), base_profile(), TODAY)
        self.assertEqual(v.level, "제외")

    def test_newlywed_blocked_for_single(self):
        v = judge(mk("신혼희망타운 입주자 모집"), base_profile(), TODAY)
        self.assertEqual(v.level, "제외")

    def test_followup_notice_ignored(self):
        v = judge(mk("OO단지 당첨자 서류제출 안내"), base_profile(), TODAY)
        self.assertEqual(v.level, "무시")

    def test_unwanted_kind_blocked(self):
        v = judge(mk("왕십리역 어반홈스", "오피스텔/도시형"),
                  base_profile(want={"오피스텔": False}), TODAY)
        self.assertEqual(v.level, "제외")

    def test_public_sale_never_recommended_on_sample_rules(self):
        """소득·자산 공식 기준이 없으면 공공분양은 '추천'까지 가지 않는다."""
        p = base_profile(residence="서울", residence_since="2015-01",
                         account_opened="2015-01-01", account_payments=100,
                         income_work=200, assets=5_000)
        v = judge(mk("서울 공공분양주택", region="서울"), p, TODAY)
        self.assertEqual(v.level, "확인")
        self.assertEqual(v.unmet, [])
        self.assertTrue(any(not c.official for c in v.unknown))

    def test_income_over_never_excludes(self):
        p = base_profile(income_work=700, assets=50_000, car=5_000)
        v = judge(mk("광교 공공분양주택", region="경기"), p, TODAY)
        self.assertEqual(v.level, "확인")
        self.assertEqual(v.unmet, [])

    def test_private_sale_can_be_recommended_when_complete(self):
        p = base_profile(residence="서울", residence_since="2015-01",
                         account_opened="2015-01-01", account_payments=100)
        v = judge(mk("서울 민영 아파트", "1순위 2순위", region="서울"), p, TODAY)
        self.assertEqual(v.level, "추천")

    def test_max_price_filter(self):
        n = mk("비싼 단지", "1순위", region="서울", price_min=120_000)
        v = judge(n, base_profile(max_price=60_000), TODAY)
        self.assertEqual(v.level, "제외")
        self.assertTrue(any(c.name == "희망 분양가" for c in v.unmet))

    def test_max_price_unknown_is_unknown_not_pass(self):
        n = mk("가격미상 단지", "1순위", region="서울")
        v = judge(n, base_profile(max_price=60_000), TODAY)
        self.assertNotEqual(v.level, "추천")

    def test_real_estate_input_settles_estate_check(self):
        """특별공급은 총자산이 아니라 부동산가액만 본다.

        분리 입력하지 않으면 '총자산 전체 ≤ 한도' 일 때만 충족을 확정할 수
        있어서, 총자산이 한도를 넘으면 미확인에 머문다. 부동산가액을
        입력하면 그 값으로 바로 판정한다.
        """
        n = mk("생애최초 특별공급 공공분양주택 입주자 모집", region="서울")
        p = dict(household_size=1, income_work=200, debts=0)

        # 총자산 3억 > 부동산 한도 2억 1,550만 → 비중을 몰라 미확인
        v = judge(n, base_profile(assets=30_000, **p), TODAY)
        estate = next(c for c in v.checks if c.name == "부동산")
        self.assertEqual(estate.status, UNKNOWN)
        self.assertIn("부동산가액", estate.todo)

        # 총자산 자체가 한도 이하면 부동산도 이하 → 충족
        v = judge(n, base_profile(assets=20_000, **p), TODAY)
        self.assertEqual(
            next(c for c in v.checks if c.name == "부동산").status, MET)

        # 부동산가액을 입력하면 총자산과 무관하게 확정된다
        v = judge(n, base_profile(assets=30_000, real_estate=10_000, **p), TODAY)
        self.assertEqual(
            next(c for c in v.checks if c.name == "부동산").status, MET)

    def test_real_estate_boundary(self):
        """부동산가액 경계 ±1만원."""
        n = mk("생애최초 특별공급 공공분양주택 입주자 모집", region="서울")
        p = dict(household_size=1, income_work=200, assets=30_000, debts=0)

        v = judge(n, base_profile(real_estate=21_550, **p), TODAY)
        self.assertEqual(
            next(c for c in v.checks if c.name == "부동산").status, MET)
        v = judge(n, base_profile(real_estate=21_551, **p), TODAY)
        self.assertEqual(
            next(c for c in v.checks if c.name == "부동산").status, UNMET)

    def test_real_estate_zero_is_met_not_missing(self):
        """부동산 0(무주택)은 '미입력'이 아니라 '충족'이다."""
        n = mk("생애최초 특별공급 공공분양주택 입주자 모집", region="서울")
        v = judge(n, base_profile(household_size=1, income_work=200,
                                  assets=30_000, debts=0, real_estate=0), TODAY)
        self.assertEqual(
            next(c for c in v.checks if c.name == "부동산").status, MET)

    def test_permanent_rental_does_not_over_exclude(self):
        """회귀: 영구임대는 수급자 전용이 아니다.

        1순위가 생계·의료급여 수급자·한부모지만 **2순위는 도시근로자 소득
        50%(1인 70%·2인 60%)** 로 수급자가 아니어도 지원할 수 있다.
        수급자가 아니라고 제외하면 과잉 제외다.
        """
        n = mk("광주우산3 영구임대주택 예비입주자 모집", region="서울")
        v = judge(n, base_profile(low_income=False, household_size=1,
                                  income_work=150), TODAY)
        self.assertNotEqual(v.level, "제외")
        tier = next(c for c in v.checks if c.name == "대상 계층")
        self.assertEqual(tier.status, UNKNOWN)
        self.assertIn("2순위", tier.detail)

        # 수급자면 1순위로 충족
        v = judge(n, base_profile(low_income=True, household_size=1,
                                  income_work=150), TODAY)
        tier = next(c for c in v.checks if c.name == "대상 계층")
        self.assertEqual(tier.status, MET)

    def test_other_low_income_notices_still_exclude(self):
        """수급자 전용이 명시된 공고(쪽방·주거취약)는 그대로 제외한다."""
        v = judge(mk("쪽방 주거취약계층 주거상향 지원", region="서울"),
                  base_profile(low_income=False), TODAY)
        self.assertEqual(v.level, "제외")

    def test_new_tags_fire(self):
        """standards 의 새 공식 기준이 실공고에서 걸리도록 태그가 붙는가."""
        cases = {
            "영구임대주택 예비입주자 모집": "영구임대",
            "청년안심주택 입주자 모집": "청년안심",
            "뉴:홈 나눔형 공공분양주택": "나눔형",
            "뉴:홈 선택형 공공분양주택": "선택형",
        }
        for title, tag in cases.items():
            self.assertIn(tag, tags_of(mk(title)), title)

    def test_general_type_is_not_tagged(self):
        """'일반형'은 '일반공급'과 혼동되기 쉬워 태그로 두지 않았다."""
        self.assertNotIn("일반형", tags_of(mk("민영 APT 일반공급 1순위")))

    def test_unstated_car_limit_does_not_block_recommendation(self):
        """자동차 기준이 공식 페이지에 없는 유형에서, 통상 기준 이하면 충족.

        있는지도 확인되지 않은 요건으로 추천을 막으면 과하다. 확인된 공공 유형의
        자동차 기준이 전부 4,542만원이므로 그 선 아래면 어느 기준이든 통과한다.
        (거주기간·통장에 쓰는 '확실히 통과하는 임계값'과 같은 방식)
        """
        n = mk("청년안심주택 입주자 모집", region="서울")
        p = dict(household_size=1, income_work=200, assets=5_000, debts=0,
                 residence="서울", residence_since="2015-01")

        v = judge(n, base_profile(car=1_200, **p), TODAY)
        c = next(c for c in v.checks if c.name == "자동차가액")
        self.assertEqual(c.status, MET)
        self.assertIn("4,542만원", c.detail)

        # 통상 기준을 넘으면 확정하지 않는다
        v = judge(n, base_profile(car=6_000, **p), TODAY)
        c = next(c for c in v.checks if c.name == "자동차가액")
        self.assertEqual(c.status, UNKNOWN)

        # 차량이 없으면 검사 자체를 만들지 않는다
        v = judge(n, base_profile(car=0, **p), TODAY)
        self.assertFalse([c for c in v.checks if c.name == "자동차가액"])

    def test_explicit_no_car_requirement_still_excludes(self):
        """'자동차 미보유'가 명시된 유형(대학생 행복주택)은 그대로 불충족."""
        n = mk("대학생 행복주택 입주자 모집", region="서울")
        v = judge(n, base_profile(student=True, household_size=1,
                                  income_work=100, assets=3_000, car=1_200),
                  TODAY)
        c = next(c for c in v.checks if c.name == "자동차")
        self.assertEqual(c.status, UNMET)

    def test_every_check_has_a_source(self):
        v = judge(mk("서울 공공분양주택", region="서울"), base_profile(), TODAY)
        for c in v.checks:
            d = c.to_dict()
            self.assertTrue(d["source"]["label"], c.name)
            self.assertIn("official", d["source"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
