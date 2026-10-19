"""2026-10-14 회귀: 수치 줄 오기·환율 방향 반대 서술·전일 급락 누락."""
from services import briefing_facts as bf

RAW = {
    "sp500_futures": {"change_pct": -0.09, "realtime_pct": -0.03},
    "nasdaq_futures": {"change_pct": -0.45, "realtime_pct": -0.39},
    "usd_krw": {"close": 1340.3, "change_pct": 0.46},
    "vix": {"close": 15.27}, "ewy": {"change_pct": -2.09},
}
KR = {"kospi": {"current": 6035.35, "change_pct": -5.46}, "kosdaq": {"current": 800.0, "change_pct": -4.0}}
REPORT = ("시장 분위기: 나쁨 — 선물 소폭 상승에도 원달러 강세(+0.46%)로 이탈\n\n"
          "수치: KOSPI선물 부재 · S&P500선물 +0.12% · 달러/원 1,340원 · VIX 15.27\n")


def test_numbers_line_uses_code_values():
    line = bf.build_numbers_line(RAW, KR)
    assert "S&P500선물 -0.03%" in line and "원화 약세" in line and "KOSPI" not in line  # 지수는 상단 블록에만


def test_apply_overwrites_numbers_and_flags_shock():
    out, contra = bf.apply(REPORT, RAW, KR, "pre_market")
    assert "+0.12%" not in out
    assert "🚨 전일 마감 KOSPI -5.46%" in out
    assert "원달러 강세" not in out and "원화 약세" in out
    assert "자동 교정" not in out   # 내부 교정 내역은 사용자에게 노출하지 않는다


def test_zero_pct_index_is_hidden():
    blk = bf.build_market_block({}, {"kospi": {"current": 5668.0, "change_pct": 0.0}}, "pre_market")
    assert "5,668" in blk and "0.00%" not in blk


def test_contradictions_detected():
    c = bf.find_contradictions(REPORT, RAW)
    assert any("원달러 강세" in x for x in c) and any("선물 상승" in x for x in c)


def test_no_false_positive_when_consistent():
    ok = "선물 하락, 원화 약세로 외국인 이탈"
    assert bf.find_contradictions(ok, RAW) == []


def test_missing_data_is_safe():
    out, _ = bf.apply("본문", {}, {}, "pre_market")
    assert out.startswith("본문")
