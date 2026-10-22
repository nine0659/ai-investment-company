"""미국 전 종목 스크리너 회귀 테스트 — 파싱, 질 하한(적자 제외), 경고(이익 정점·ROE 100%+), 불완전 수신 은폐 방지,
다중 출처 조립(EDGAR 재무 + 일괄 시세 + 나스닥 애널리스트)."""
import pytest
from sqlalchemy import text

import scheduler
from db.database import get_conn, init_db
from services import job_ledger
from services import us_universe_screener as us


def test_f_parses_prefixed_values():
    assert us._f("$173.01") == 173.01
    assert us._f("-2.112%") == -2.112
    assert us._f("48776511031.00") == 48776511031.0
    assert us._f("") is None and us._f("N/A") is None and us._f(None) is None


@pytest.mark.parametrize("kw", [
    {"name": "Foo Acquisition Corp"}, {"name": "Bar Warrant"}, {"name": "Baz Depositary Shares"},
    {"symbol": "BAC^L"}, {"lastsale": "N/A"}, {"marketCap": ""},
])
def test_parse_row_excludes_non_candidates(kw):
    base = {"symbol": "AAA", "name": "Alpha Inc. Common Stock", "lastsale": "$10.00", "pctchange": "1%",
            "marketCap": "5000000000", "sector": "Technology", "industry": "Software", "country": "United States"}
    base.update(kw)
    assert us._parse_row(base) is None


def test_parse_row_normalizes():
    r = us._parse_row({"symbol": "BRK/B", "name": "Berkshire Hathaway Common Stock", "lastsale": "$480.5",
                       "pctchange": "0.5%", "marketCap": "1000000000000", "sector": "Finance", "industry": "Insurance"})
    assert r["code"] == "BRK-B" and r["name"] == "Berkshire Hathaway" and r["mcap_usd"] == 1e12
    assert r["industry"] == "Insurance"


_EDGAR = {"eps": 5.0, "ni": 200.0, "equity": 1000.0, "revenue": 1300.0, "liabilities": 2000.0}
_PRICE = {"close": 100.0, "high52": 200.0, "ma50": 90.0, "ma200": 110.0}


def _row(edgar=_EDGAR, price=_PRICE, cons=None, **rowover):
    row = {"code": "AAA", "name": "Alpha", "mcap_usd": 5e9, "chg_pct": 1.0, "sector": "Technology",
           "industry": "Software", **rowover}
    d = us.build_detail(row, edgar, price)
    if d and cons:
        us.apply_consensus(d, cons)
    return {**row, "detail": d}


_CONS = {"target": 150.0, "buy": 16, "hold": 3, "sell": 1, "analysts": 20, "fwd_eps": 10.0}


def test_build_detail_computes_ratios_in_code():
    d = _row()["detail"]
    assert d["roe"] == pytest.approx(0.2) and d["margin"] == pytest.approx(200 / 1300)
    assert d["pe"] == pytest.approx(20.0) and d["debt_eq"] == pytest.approx(200.0)


def test_build_detail_none_without_price():
    assert us.build_detail({"industry": "x"}, _EDGAR, None) is None


def test_negative_equity_gives_no_roe():
    d = _row(edgar={**_EDGAR, "equity": -50.0})["detail"]
    assert d["roe"] is None and d["debt_eq"] is None


def test_apply_consensus_computes_forward_pe():
    d = _row(cons=_CONS)["detail"]
    assert d["fwd_pe"] == pytest.approx(10.0) and d["buy_ratio"] == pytest.approx(0.8)


def test_full_signal_set():
    sig, warn = us.compute_signals(_row(cons=_CONS), {})
    keys = {s["key"] for s in sig}
    assert {"수익성", "저평가", "이익증가기대", "컨센서스여력", "고점대비낙폭"} <= keys
    assert "단기회복" not in keys and "단기회복" not in us._GROUP        # 2년 검증에서 효과 없어 점수 제외
    assert any("2배 이상" in w for w in warn)          # EPS 5→10은 정확히 +100%라 이익 정점 경고 대상


def test_loss_making_excluded():
    assert us.compute_signals(_row(edgar={**_EDGAR, "eps": -1.0}), {}) == ([], [])


def test_missing_edgar_excluded():
    assert us.compute_signals(_row(edgar=None), {}) == ([], [])


def test_cycle_peak_and_roe_warnings():
    _, warn = us.compute_signals(_row(edgar={**_EDGAR, "ni": 1500.0}, cons={**_CONS, "fwd_eps": 12.0}), {})
    assert any("2배 이상" in w for w in warn) and any("ROE 100%" in w for w in warn)


def test_debt_warning_skips_finance():
    heavy = {**_EDGAR, "liabilities": 5000.0}
    assert any("총부채" in w for w in us.compute_signals(_row(edgar=heavy), {})[1])
    assert not any("총부채" in w for w in us.compute_signals(_row(edgar=heavy, sector="Finance"), {})[1])


def test_consensus_requires_enough_analysts_and_buy_ratio():
    few = {**_CONS, "buy": 2, "hold": 1, "sell": 0, "analysts": 3}
    assert "컨센서스여력" not in {s["key"] for s in us.compute_signals(_row(cons=few), {})[0]}
    bearish = {**_CONS, "buy": 5, "hold": 10, "sell": 5, "analysts": 20}
    assert "컨센서스여력" not in {s["key"] for s in us.compute_signals(_row(cons=bearish), {})[0]}


def test_score_counts_groups():
    assert us.score([{"key": "저평가"}, {"key": "업종대비저평가"}]) == 1


def test_prefilter_requires_profit_and_quality():
    good = _row()
    loss = _row(edgar={**_EDGAR, "eps": -1.0})
    lowroe = _row(edgar={**_EDGAR, "ni": 20.0})
    assert us.prefilter([good, loss, lowroe]) == [good]


def test_fetch_consensus_parses_nasdaq_shapes(monkeypatch):
    def fake(path):
        if "targetprice" in path:
            return {"consensusOverview": {"priceTarget": 323.45, "buy": 33, "hold": 1, "sell": 0}}
        return {"yearlyForecast": {"rows": [{"fiscalEnd": "Jan 2027", "consensusEPSForecast": 9.3}]}}
    monkeypatch.setattr(us, "_nasdaq_get", fake)
    c = us.fetch_consensus("BRK-B")
    assert c == {"target": 323.45, "buy": 33, "hold": 1, "sell": 0, "analysts": 34, "fwd_eps": 9.3}


def test_fetch_consensus_empty_on_failure(monkeypatch):
    monkeypatch.setattr(us, "_nasdaq_get", lambda p: {})
    assert us.fetch_consensus("AAA") == {}


def test_fetch_universe_incomplete_flag(monkeypatch):
    import requests

    class R:
        def raise_for_status(self): pass
        def json(self): return {"data": {"rows": [{"symbol": "A"}] * 10}}
    monkeypatch.setattr(requests, "get", lambda *a, **k: R())
    res = us.fetch_universe()
    assert res["complete"] is False and res["fetched"] == 10


def test_fetch_universe_network_failure(monkeypatch):
    import requests

    def boom(*a, **k):
        raise RuntimeError("net")
    monkeypatch.setattr(requests, "get", boom)
    assert us.fetch_universe() == {"rows": [], "fetched": 0, "complete": False}


def _scored(code="AAA", score=3):
    r = _row(cons=_CONS, code=code)
    return {**r, "signals": [{"key": "저평가", "text": ""}], "warnings": [], "score": score}


def _res(rows, complete=True, fetched=6900, detailed=None, requested=None, edgar_ok=True,
         cons_ok=None, cons_req=None):
    return {"scored": rows, "picks": [r for r in rows if r["score"] >= 3], "universe_complete": complete,
            "fetched": fetched, "edgar_ok": edgar_ok, "edgar_matched": len(rows),
            "detailed": len(rows) if detailed is None else detailed,
            "detail_requested": len(rows) if requested is None else requested,
            "consensus_ok": len(rows) if cons_ok is None else cons_ok,
            "consensus_requested": len(rows) if cons_req is None else cons_req}


@pytest.fixture
def clean_db():
    init_db()
    with get_conn() as c:
        c.execute(text("DELETE FROM us_screen_daily"))
    yield
    with get_conn() as c:
        c.execute(text("DELETE FROM us_screen_daily"))


def test_save_load_roundtrip(clean_db):
    assert us.save_daily(_res([_scored("AAA", 3), _scored("BBB", 1)]), date="2026-10-22") == 2
    assert [p["code"] for p in us.load_picks()] == ["AAA"]


@pytest.mark.parametrize("res,status", [
    (_res([_scored()], complete=False, fetched=10), "fail"),
    (_res([_scored()], edgar_ok=False), "fail"),
    (_res([_scored()], detailed=50, requested=100), "fail"),
    (_res([_scored()], cons_ok=10, cons_req=100), "fail"),
    (_res([]), "empty"),
    (_res([_scored()]), "success"),
])
def test_run_daily_job_status(clean_db, monkeypatch, res, status):
    monkeypatch.setattr(us, "run_screen", lambda: res)
    assert us.run_daily_job()[0] == status


def test_scheduler_job_failure_does_not_raise(monkeypatch):
    rec = []
    monkeypatch.setattr("services.job_ledger.record_job", lambda n, s, d="": rec.append((n, s)))
    monkeypatch.setattr(us, "run_daily_job", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    scheduler.job_us_universe_screen()
    assert rec == [("us_universe_screen", "fail")]


def test_job_in_ledger_every_weekday():
    for wd in range(5):
        assert "us_universe_screen" in job_ledger._EXPECTED_BY_WEEKDAY[wd]
