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


def test_price_trend_signals_are_not_scored():
    """2024-10~2026-10 한국 2년 검증(scripts/backtest_price_signals.py): 상대강도는 효과 없음(t=-0.9), 추세회복은 시점의 21%에서만
    시장을 이겨 유의하게 부진(t=-2.8)했다 → 점수에 넣지 않는다. 강한 상대강도·추세회복 조건을 모두 갖춰도 신호가 생기면 안 된다."""
    strong = _trend_row(close=10000.0, ma50=9000.0, ret_1m=5.0, ret_3m=80.0)       # 고점 대비 -50%, 50일선 위, 3개월 +80%
    keys = {s["key"] for s in us.compute_signals(strong, {})}
    assert "상대강도" not in keys and "추세회복" not in keys
    scored = us.score_all([_trend_row(f"{i:06d}", ret_3m=float(i)) for i in range(10)])
    assert all(s["key"] not in ("상대강도", "추세회복") for r in scored for s in r["signals"])
    assert "상대강도" not in us._GROUP and "추세회복" not in us._GROUP


def test_trend_warnings_are_neutral_and_independent():
    below = _trend_row(close=10000.0, ma200=14000.0)                    # 200일선 대비 -29%
    hot = _trend_row(close=10000.0, ma200=9000.0, ret_1m=55.0)
    w1, w2 = us.trend_warnings(below), us.trend_warnings(hot)
    assert any("장기 추세 아래" in w for w in w1) and not any("급등" in w for w in w1)
    assert any("단기 급등" in w for w in w2) and not any("장기 추세 아래" in w for w in w2)
    assert not any("싸 보이는 이유" in w for w in w1)                    # 검증되지 않은 인과 주장을 하지 않는다
    assert us.trend_warnings({"detail": {"hist": None}, "close": 1.0}) == []


def test_job_fails_when_history_mostly_missing(monkeypatch):
    base = {"universe_complete": True, "fetched": 4300, "expected": 4317, "detailed": 1280, "detail_requested": 1284,
            "picks": [], "scored": [_trend_row()]}
    monkeypatch.setattr(us, "run_screen", lambda: {**base, "hist_ok": 100, "hist_requested": 1280})
    st, detail = us.run_daily_job()
    assert st == "fail" and "추세 이력" in detail


# ── 검증 통과 신호(✔) 표시·정렬 (2026-10-22 한국 재무 신호 17개월 검증: scripts/backtest_fundamental_signals.py) ────────────

def _vrow(code, cns_per=None, pbr=None, eps=1000.0, bps=5000.0, mcap=5000.0, target=None, recomm=None):
    infos = [{"code": "eps", "value": f"{eps:,.0f}원"}, {"code": "bps", "value": f"{bps:,.0f}원"}]
    if cns_per:
        infos.append({"code": "cnsPer", "value": f"{cns_per}배"})
    if pbr:
        infos.append({"code": "pbr", "value": f"{pbr}배"})
    d = us.parse_integration({"totalInfos": infos, "industryCode": "1",
                              "consensusInfo": {"priceTargetMean": f"{target:,.0f}" if target else None,
                                                "recommMean": f"{recomm}" if recomm else None},
                              "dealTrendInfos": []})
    return {"code": code, "name": code, "market": "KOSPI", "close": 10000.0, "chg_pct": 0.0, "volume": 1.0,
            "value_eok": 1.0, "mcap_eok": mcap, "detail": d}


def test_value_signals_are_marked_validated_and_roe_is_info_only():
    sig = us.compute_signals(_vrow("A", cns_per=6.0, pbr=0.8), {})
    by = {s["key"]: s for s in sig}
    assert by["저평가"]["validated"] is True and by["자산대비저평가"]["validated"] is True
    assert by["수익성"].get("info") is True and not by["수익성"].get("validated")
    assert us.score(sig) == 1                                      # 수익성은 점수 제외, 저평가·자산대비는 같은 '가치' 묶음
    assert us.validated_count(sig) == 1


def test_signal_labels_and_saved_string_roundtrip():
    sig = us.compute_signals(_vrow("A", cns_per=6.0, pbr=0.8), {})
    labels = [us.signal_label(s) for s in sig]
    assert "저평가✔" in labels and "자산대비저평가✔" in labels and "수익성(참고)" in labels
    assert us.validated_groups_in(",".join(labels)) == 1           # 가치 묶음 하나
    assert us.validated_groups_in("수익성(참고),컨센서스여력") == 0
    assert us.validated_groups_in("저평가✔,공시호재✔,쌍끌이매수") == 2
    assert us.validated_groups_in(None) == 0


def test_event_buyback_is_validated_but_contract_is_info():
    ev = [{"event_type": "자사주취득", "polarity": "호재", "summary": "", "metric": None, "date": "2026-10-21"},
          {"event_type": "공급계약", "polarity": "호재", "summary": "계약금액 500억원", "metric": 15.0, "date": "2026-10-21"}]
    sig = us.event_signals(ev)
    assert sig[0]["validated"] is True and sig[1].get("info") is True and not sig[1].get("validated")


def test_picks_ordered_by_validated_signals_before_plain_score():
    """같은 점수라도 검증된 신호(저평가·자산대비·공시호재)가 있는 종목이 먼저 — 시총이 작아도."""
    def scored(code, validated_keys, mcap):
        sig = [{"key": k, "validated": True, "text": ""} for k in validated_keys] + [{"key": "고점대비낙폭", "text": ""},
                                                                                     {"key": "쌍끌이매수", "text": ""}]
        return {"code": code, "name": code, "market": "KOSPI", "close": 1.0, "chg_pct": 0.0, "mcap_eok": mcap, "detail": {},
                "signals": sig, "score": us.score(sig), "warnings": []}
    a = scored("A", [], 90000.0)                                   # 검증 0, 큰 시총
    b = scored("B", ["저평가"], 2000.0)                             # 검증 1, 작은 시총
    c = scored("C", ["저평가"], 5000.0)
    b["score"] = c["score"] = a["score"] = 3
    out = us.pick_from([a, b, c])
    assert [p["code"] for p in out] == ["C", "B", "A"]


def test_load_picks_sorts_by_validated_groups():
    from db.database import get_conn, init_db
    from sqlalchemy import text
    init_db()
    with get_conn() as c:
        c.execute(text("DELETE FROM universe_screen_daily"))
        for code, sig, mcap in (("111111", "수익성(참고),쌍끌이매수,고점대비낙폭", 90000), ("222222", "저평가✔,쌍끌이매수,고점대비낙폭", 1500)):
            c.execute(text("INSERT INTO universe_screen_daily (date, code, name, market, close, mcap_eok, score, signals) "
                           "VALUES ('2026-10-22', :c, :c, 'KOSPI', 100, :m, 3, :s)"), {"c": code, "m": mcap, "s": sig})
    try:
        assert [p["code"] for p in us.load_picks(limit=5)] == ["222222", "111111"]
    finally:
        with get_conn() as c:
            c.execute(text("DELETE FROM universe_screen_daily"))
