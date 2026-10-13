from services import output_fact_check as ofc


def test_grounded_numbers_pass():
    ctx = "KOSPI 7,412.55 (+1.23%) USD/KRW 1,381 삼성전자 78,500원"
    report = "KOSPI는 +1.23% 올랐고 환율 1,381원, 삼성전자 78,500원."
    r = ofc.check_report_numbers(report, ctx)
    assert r["count"] == 0 and r["total"] == 3


def test_invented_number_detected():
    ctx = "KOSPI 7,412 (+1.23%)"
    report = "외국인이 4,820억원 순매수, 반도체 상승률 9.7%."
    r = ofc.check_report_numbers(report, ctx)
    assert set(r["ungrounded"]) == {"4,820억", "9.7%"}


def test_rounding_tolerance():
    r = ofc.check_report_numbers("PER 12.3배", "PER 12.28")
    assert r["count"] == 0


def test_sign_ignored_and_prompt_numbers_allowed():
    prompt = "손절 -15% 기준, 확신도 상 5~10%"
    r = ofc.check_report_numbers("손절선 15% 하회, 비중 10%", "", prompt)
    assert r["count"] == 0


def test_dates_and_times_not_claims():
    r = ofc.check_report_numbers("2026-10-13 08:20 기준", "")
    assert r["total"] == 0


def test_trivial_values_skipped():
    r = ofc.check_report_numbers("0% 변화, 1배 수준", "")
    assert r["count"] == 0


def test_warn_threshold():
    report = " ".join(f"{n}%" for n in (11.1, 22.2, 33.3, 44.4, 55.5))
    r = ofc.check_report_numbers(report, "아무 숫자 없음")
    assert r["warn"] is True
    assert "수치 검증" in ofc.format_warning(r)
    r2 = ofc.check_report_numbers("11.1% 22.2%", "아무 숫자 없음")
    assert r2["warn"] is False
