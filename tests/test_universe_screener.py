"""전 종목 스크리너 회귀 테스트 — 단위 글자 파싱 누락(PER '5.75배'가 None이 돼 신호가 통째로 사라진 사고)과
적자 종목 유입·불완전 수신 은폐를 막는다."""
import pytest

from services import universe_screener as us


def _stock(**kw):
    base = {"stockEndType": "stock", "stockName": "테스트기업", "itemCode": "123456",
            "closePrice": "10,000", "marketValue": "5,000", "accumulatedTradingValue": "30,000",
            "fluctuationsRatio": "1.5", "accumulatedTradingVolume": "1,000",
            "tradeStopType": {"code": "1"}}
    base.update(kw)
    return base


def test_unit_num_strips_units():
    assert us._unit_num("5.75배") == 5.75
    assert us._unit_num("224,313원") == 224313.0
    assert us._unit_num("48.79%") == 48.79
    assert us._unit_num("-3.2%") == -3.2
    assert us._unit_num("N/A") is None
    assert us._unit_num(None) is None
    assert us._unit_num("-") is None


@pytest.mark.parametrize("kw", [
    {"stockEndType": "etf"},
    {"stockName": "삼성전자우"},
    {"stockName": "현대차2우B"},
    {"stockName": "엔에이치스팩32호"},
    {"tradeStopType": {"code": "2"}},
    {"closePrice": "N/A"},
])
def test_parse_row_excludes_non_candidates(kw):
    assert us._parse_row(_stock(**kw), "KOSPI") is None


def test_parse_row_ok_and_value_unit():
    r = us._parse_row(_stock(), "KOSDAQ")
    assert r["code"] == "123456" and r["market"] == "KOSDAQ"
    assert r["value_eok"] == 300.0          # 30,000 백만원 = 300억원
    assert r["mcap_eok"] == 5000.0


def _integration(**over):
    infos = {"per": "8.0배", "pbr": "0.80배", "eps": "1,000원", "bps": "9,000원", "cnsPer": "6.0배",
             "cnsEps": "1,400원", "highPriceOf52Weeks": "20,000", "lowPriceOf52Weeks": "8,000"}
    infos.update(over.pop("infos", {}))
    j = {"totalInfos": [{"code": k, "value": v} for k, v in infos.items()],
         "industryCode": "100",
         "consensusInfo": {"priceTargetMean": "15,000", "recommMean": "4.0"},
         "dealTrendInfos": [{"foreignerPureBuyQuant": "+10", "organPureBuyQuant": "+5"}] * 3}
    j.update(over)
    return j


def test_parse_integration_reads_unit_values():
    d = us.parse_integration(_integration())
    assert d["per"] == 8.0 and d["pbr"] == 0.8 and d["eps"] == 1000.0 and d["cns_per"] == 6.0
    assert d["target"] == 15000.0 and d["foreign_buy_days"] == 3 and d["organ_buy_days"] == 3


def _row(**over):
    r = {"code": "123456", "name": "테스트", "market": "KOSPI", "close": 10000.0, "chg_pct": 1.0,
         "volume": 1.0, "value_eok": 10.0, "mcap_eok": 5000.0, "detail": us.parse_integration(_integration())}
    r.update(over)
    return r


def test_signals_full_set():
    keys = {s["key"] for s in us.compute_signals(_row(), {})}
    assert {"저평가", "자산대비저평가", "이익증가기대", "컨센서스여력", "고점대비낙폭", "쌍끌이매수"} <= keys


def test_loss_making_company_has_no_signals():
    d = us.parse_integration(_integration(infos={"eps": "-500원"}))
    assert us.compute_signals(_row(detail=d), {}) == []


def test_missing_detail_has_no_signals():
    assert us.compute_signals({"code": "1", "close": 1.0}, {}) == []


def test_score_counts_groups_not_signals():
    sigs = [{"key": "저평가", "text": ""}, {"key": "자산대비저평가", "text": ""}, {"key": "업종대비저평가", "text": ""}]
    assert us.score(sigs) == 1             # 가치 묶음 하나로만 센다


def test_screen_excludes_and_thresholds():
    rows = [_row(code="111111"), _row(code="222222")]
    assert {p["code"] for p in us.screen(rows)} == {"111111", "222222"}
    assert {p["code"] for p in us.screen(rows, exclude={"111111"})} == {"222222"}
    assert us.screen(rows, min_score=99) == []


def test_fetch_universe_flags_incomplete(monkeypatch):
    def page(market, p, size=100):
        if p == 1:
            return {"totalCount": 300, "stocks": [_stock(itemCode=f"{i:06d}") for i in range(100)]}
        return {}                           # 나머지 페이지 전부 실패
    monkeypatch.setattr(us.nv, "market_value_page", page)
    res = us.fetch_universe(markets=("KOSPI",))
    assert res["expected"] == 300 and res["fetched"] == 100 and res["complete"] is False
    assert "불완전" in us.format_screen({"as_of": "x", "expected": 300, "fetched": 100, "universe_complete": False,
                                        "tradable": 0, "detailed": 0, "detail_requested": 0, "picks": []})


def test_tradable_uses_market_cap_not_intraday_value():
    rows = [_row(mcap_eok=500.0), _row(mcap_eok=2000.0, value_eok=0.0)]
    assert len(us.tradable(rows)) == 1      # 장중엔 거래대금이 작아도 탈락시키지 않는다


# ── 추세·상대강도 (2026-10-22: 시장 전체가 고점 대비 -40~60%라 낙폭·저평가 신호만으로는 변별이 안 됐다) ──────

def _hist(ret_3m=20.0, ret_1m=5.0, ma50=9000.0, ma200=11000.0, hi_hist=20000.0):
    return {"ret_1m": ret_1m, "ret_3m": ret_3m, "ret_6m": 10.0, "ma50": ma50, "ma200": ma200, "hi_hist": hi_hist}


def _trend_row(code="111111", close=10000.0, **h):
    d = us.parse_integration({"totalInfos": [{"code": "eps", "value": "1,000원"}, {"code": "highPriceOf52Weeks", "value": "20,000"}],
                              "industryCode": "1", "consensusInfo": {}, "dealTrendInfos": []})
    d["hist"] = _hist(**h)
    return {"code": code, "name": code, "market": "KOSPI", "close": close, "chg_pct": 0.0, "volume": 1.0,
            "value_eok": 1.0, "mcap_eok": 5000.0, "detail": d}


def test_hist_stats_computes_returns_and_averages():
    import pandas as pd
    cl = pd.Series([100.0 + i for i in range(220)])              # 꾸준한 상승
    st = us._hist_stats(cl)
    assert st["ret_1m"] == pytest.approx((319 / 298 - 1) * 100)
    assert st["ret_3m"] == pytest.approx((319 / 256 - 1) * 100)
    assert st["ma50"] == pytest.approx(sum(range(270, 320)) / 50) and st["ma200"] is not None


def test_hist_stats_none_when_too_short():
    import pandas as pd
    assert us._hist_stats(pd.Series([1.0] * 30)) is None
    assert us._hist_stats(pd.Series([1.0] * 100))["ma200"] is None          # 200봉 미만이면 200일선은 비움


def test_rs_percentiles_ranks_within_universe():
    rows = [_trend_row(c, ret_3m=r) for c, r in (("A", -30.0), ("B", 0.0), ("C", 40.0))]
    rs = us.rs_percentiles(rows)
    assert rs == {"A": 0.0, "B": 50.0, "C": 100.0}


def test_rs_percentiles_skips_missing_history():
    r = _trend_row("A")
    r["detail"]["hist"] = None
    assert us.rs_percentiles([r]) == {}


def test_relative_strength_signal_only_for_top_30pct():
    rows = [_trend_row(f"{i:06d}", ret_3m=float(i)) for i in range(10)]
    scored = us.score_all(rows)
    keys = {r["code"]: {s["key"] for s in r["signals"]} for r in scored}
    assert "상대강도" in keys["000009"] and "상대강도" in keys["000007"]
    assert "상대강도" not in keys["000005"]


def test_trend_recovery_requires_drawdown_and_close_above_ma50_and_positive_month():
    ok = us.compute_signals(_trend_row(close=10000.0, ma50=9000.0, ret_1m=5.0), {})
    assert "추세회복" in {s["key"] for s in ok}
    below = us.compute_signals(_trend_row(close=10000.0, ma50=11000.0), {})
    assert "추세회복" not in {s["key"] for s in below}
    falling = us.compute_signals(_trend_row(close=10000.0, ma50=9000.0, ret_1m=-3.0), {})
    assert "추세회복" not in {s["key"] for s in falling}
    shallow = us.compute_signals(_trend_row(close=19000.0, ma50=9000.0), {})
    assert "추세회복" not in {s["key"] for s in shallow}                # 고점 대비 -5%는 '회복'이 아니다


def test_trend_warnings():
    broken = _trend_row(close=10000.0, ma200=14000.0)                  # 200일선 대비 -29%
    hot = _trend_row(close=10000.0, ma200=9000.0, ret_1m=55.0)
    w1, w2 = us.trend_warnings(broken), us.trend_warnings(hot)
    assert any("추세 훼손" in w for w in w1) and not any("과열" in w for w in w1)
    assert any("단기 과열" in w for w in w2) and not any("추세 훼손" in w for w in w2)
    assert us.trend_warnings({"detail": {"hist": None}, "close": 1.0}) == []


def test_new_signals_count_as_their_own_groups():
    assert us.score([{"key": "상대강도"}, {"key": "추세회복"}, {"key": "저평가"}]) == 3


def test_job_fails_when_history_mostly_missing(monkeypatch):
    base = {"universe_complete": True, "fetched": 4300, "expected": 4317, "detailed": 1280, "detail_requested": 1284,
            "picks": [], "scored": [_trend_row()]}
    monkeypatch.setattr(us, "run_screen", lambda: {**base, "hist_ok": 100, "hist_requested": 1280})
    st, detail = us.run_daily_job()
    assert st == "fail" and "추세 이력" in detail
