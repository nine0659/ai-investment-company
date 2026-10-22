"""미국 전 종목 스크리너 회귀 테스트 — 파싱, 질 하한(적자 제외), 경고(이익 정점·ROE 100%+), 불완전 수신 은폐 방지."""
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
            "marketCap": "5000000000", "sector": "Technology", "country": "United States"}
    base.update(kw)
    assert us._parse_row(base) is None


def test_parse_row_normalizes():
    r = us._parse_row({"symbol": "BRK/B", "name": "Berkshire Hathaway Common Stock", "lastsale": "$480.5",
                       "pctchange": "0.5%", "marketCap": "1000000000000", "sector": "Finance"})
    assert r["code"] == "BRK-B" and r["name"] == "Berkshire Hathaway" and r["mcap_usd"] == 1e12


def _info(**over):
    i = {"forwardPE": 10.0, "trailingPE": 15.0, "priceToBook": 2.0, "returnOnEquity": 0.2, "profitMargins": 0.15,
         "trailingEps": 5.0, "forwardEps": 7.0, "targetMeanPrice": 150.0, "recommendationMean": 1.8,
         "numberOfAnalystOpinions": 20, "fiftyTwoWeekHigh": 200.0, "fiftyDayAverage": 90.0,
         "twoHundredDayAverage": 110.0, "shortPercentOfFloat": 0.02, "debtToEquity": 50.0, "industry": "Software"}
    i.update(over)
    return i


def _row(close=100.0, **over):
    return {"code": "AAA", "name": "Alpha", "close": close, "mcap_usd": 5e9, "chg_pct": 1.0, "sector": "Tech",
            "detail": us.parse_info(_info(**over))}


def test_full_signal_set():
    sig, warn = us.compute_signals(_row(), {})
    keys = {s["key"] for s in sig}
    assert {"수익성", "저평가", "이익증가기대", "컨센서스여력", "고점대비낙폭", "단기회복"} <= keys
    assert warn == []


def test_loss_making_excluded():
    assert us.compute_signals(_row(trailingEps=-1.0), {}) == ([], [])


def test_cycle_peak_and_roe_warnings():
    _, warn = us.compute_signals(_row(forwardEps=15.0, returnOnEquity=1.3), {})
    assert any("2배 이상" in w for w in warn) and any("ROE 100%" in w for w in warn)


def test_short_interest_and_debt_warnings():
    _, warn = us.compute_signals(_row(shortPercentOfFloat=0.2, debtToEquity=300.0), {})
    assert any("공매도" in w for w in warn) and any("부채비율" in w for w in warn)


def test_consensus_requires_enough_analysts():
    sig, _ = us.compute_signals(_row(numberOfAnalystOpinions=3), {})
    assert "컨센서스여력" not in {s["key"] for s in sig}


def test_score_counts_groups():
    sigs = [{"key": "저평가"}, {"key": "업종대비저평가"}]
    assert us.score(sigs) == 1


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
    return {"code": code, "name": code, "sector": "Tech", "close": 10.0, "chg_pct": 1.0, "mcap_usd": 5e9,
            "detail": us.parse_info(_info()), "signals": [{"key": "저평가", "text": ""}], "warnings": [], "score": score}


def _res(rows, complete=True, fetched=6900, detailed=None, requested=None):
    return {"scored": rows, "picks": [r for r in rows if r["score"] >= 3], "universe_complete": complete,
            "fetched": fetched, "detailed": len(rows) if detailed is None else detailed,
            "detail_requested": len(rows) if requested is None else requested}


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
    picks = us.load_picks()
    assert [p["code"] for p in picks] == ["AAA"]


@pytest.mark.parametrize("res,status", [
    (_res([_scored()], complete=False, fetched=10), "fail"),
    (_res([_scored()], detailed=50, requested=100), "fail"),
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
