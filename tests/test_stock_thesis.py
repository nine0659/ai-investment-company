"""종목 판단서 회귀 테스트 (2026-10-21).

숫자는 전부 코드가 계산하고, LLM은 그 위에서 서술만 한다 — 계산 부분을 고정한다.
"""
from services import stock_thesis as st


def test_pct_and_per():
    assert st.pct(110, 100) == 10.0
    assert st.pct(None, 100) is None and st.pct(5, 0) is None
    assert st.per_at(1_300_000, 467_761) == 2.78
    assert st.per_at(1_000, -5) is None           # 적자 EPS로 PER을 만들지 않는다


def test_revision_view():
    v = st.revision_view({"current": 110.0, "7daysAgo": 100.0, "30daysAgo": 105.0, "90daysAgo": 88.0})
    assert v == {"d7": 10.0, "d30": 4.8, "d90": 25.0}
    assert st.revision_view({}) == {"d7": None, "d30": None, "d90": None}


def test_scenario_table_skips_missing_eps():
    rows = st.scenario_table(None, 200.0, 300.0, 1000.0, pers=(3, 5))
    assert [r["label"][:2] for r in rows] == ["기본", "상방"]
    assert rows[0]["cells"][0] == {"per": 3, "price": 600.0, "vs_now": -40.0}


def test_consensus_skew_needs_enough_analysts():
    assert st.consensus_skew(38, 1, 0)["one_sided"] is True
    assert st.consensus_skew(5, 0, 0)["one_sided"] is False       # 표본 8명 미만이면 쏠림이라 부르지 않는다
    assert st.consensus_skew(0, 0, 0) == {}


def _closes():
    # 2018: 고점 2018-05-15의 100 → 연말 65 (이익 정점 해), 2019: 이익이 급감했는데 주가는 반등
    return [("2017-01-15", 50.0), ("2017-06-15", 60.0), ("2017-12-20", 70.0),
            ("2018-01-15", 90.0), ("2018-05-15", 100.0), ("2018-12-20", 65.0),
            ("2019-01-15", 60.0), ("2019-06-15", 80.0), ("2019-12-20", 100.0)]


def test_cycle_peaks_finds_earnings_peak_and_drawdowns():
    ni = {2017: 10.0, 2018: 20.0, 2019: 4.0}
    rows = st.cycle_peaks(ni, _closes(), shares=2.0)
    assert [r["year"] for r in rows] == [2018]
    r = rows[0]
    assert r["hi_date"] == "2018-05-15" and r["hi_price"] == 100
    assert r["dd_to_ye"] == -35.0                       # 100 → 연말 65
    assert r["per_ye"] == 6.5                           # 연말가 65 / EPS(20/2=10)
    assert r["ni_next_yoy"] == -80.0
    assert r["ret_next_year"] == 53.8                   # 65 → 100


def test_cycle_peaks_needs_neighbors_and_shares():
    assert st.cycle_peaks({2018: 5.0}, _closes(), 2.0) == []
    assert st.cycle_peaks({2017: 1.0, 2018: 5.0, 2019: 1.0}, _closes(), 0) == []
    assert st.cycle_peaks({2017: -1.0, 2018: -0.5, 2019: -3.0}, _closes(), 2.0) == []     # 적자 해는 정점이 아니다


def test_find_signals_price_vs_estimates_and_quarters():
    me = {"ret_3m": -29.0, "recs": {"buy": 38, "hold": 1, "sell": 0},
          "trend": {"0y": {"current": 111.0, "90daysAgo": 100.0},
                    "0q": {"current": 96.0, "90daysAgo": 100.0}, "+1q": {"current": 97.0, "90daysAgo": 100.0}}}
    text = "\n".join(st.find_signals(me, {"마이크론": {"ret_3m": -6.0, "trend": {"0y": {"current": 120.0, "90daysAgo": 100.0}}}}))
    assert "가격↔이익추정 괴리" in text and "연간 vs 분기 엇갈림" in text and "의견 쏠림 속 가격 약세" in text
    assert "동종 대비" in text and "동종 이익추정 방향" in text


def test_find_signals_quiet_when_nothing_diverges():
    me = {"ret_3m": 5.0, "recs": {"buy": 3, "hold": 3, "sell": 3},
          "trend": {"0y": {"current": 101.0, "90daysAgo": 100.0}}}
    assert st.find_signals(me, {}) == []


def test_account_context_rejects_when_prices_failed(monkeypatch):
    import services.portfolio_service as ps
    monkeypatch.setattr(ps, "calculate_pnl", lambda kis=None: [
        {"code": "000660", "name": "SK하이닉스", "quantity": 19, "avg_price": 100.0, "current_price": 100.0,
         "current_val": 1900.0, "pnl_pct": 0.0}])
    assert st.account_context("000660") == {}           # 시세 조회 실패(현재가=평단)를 '손익 0%'로 판단하지 않는다


def test_account_context_trim_when_over_cap(monkeypatch):
    import services.portfolio_service as ps
    monkeypatch.setattr(ps, "calculate_pnl", lambda kis=None: [
        {"code": "000660", "name": "A", "quantity": 10, "avg_price": 150.0, "current_price": 100.0,
         "current_val": 1000.0, "pnl_pct": -33.3},
        {"code": "005380", "name": "B", "quantity": 10, "avg_price": 60.0, "current_price": 50.0,
         "current_val": 100.0, "pnl_pct": -16.0}])
    a = st.account_context("000660")
    assert a["weight"] == 90.9 and a["trim"]["sell_cash_qty"] > 0
