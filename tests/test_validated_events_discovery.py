"""검증 통과 공시(자사주 취득·소각·배당 결정) 종목이 종목 발굴 후보 풀에 직접 편입되는지 — 점수가 낮아도, 시총 기준·중복·실패 폴백 포함."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text

from agents import discovery_agent as da
from db.database import get_conn, init_db
from services import disclosure_events as de

_KST = ZoneInfo("Asia/Seoul")


def _d(n=0):
    return (datetime.now(_KST) - timedelta(days=n)).strftime("%Y-%m-%d")


@pytest.fixture
def db():
    init_db()
    with get_conn() as c:
        c.execute(text("DELETE FROM dart_events"))
        c.execute(text("DELETE FROM universe_screen_daily"))
    yield
    with get_conn() as c:
        c.execute(text("DELETE FROM dart_events"))
        c.execute(text("DELETE FROM universe_screen_daily"))


def _snap(code, mcap, score=0):
    with get_conn() as c:
        c.execute(text("INSERT INTO universe_screen_daily (date, code, name, market, close, mcap_eok, score, signals) "
                       "VALUES (:d, :c, :n, 'KOSPI', 100, :m, :s, '')"), {"d": _d(0), "c": code, "n": f"종목{code}", "m": mcap, "s": score})


def _ev(no, code, typ, date=None, pol="호재"):
    with get_conn() as c:
        c.execute(text("INSERT INTO dart_events (rcept_no, date, stock_code, corp_name, market, report_nm, event_type, polarity, "
                       "summary, metric) VALUES (:n, :d, :c, :nm, 'KOSPI', 'x', :t, :p, '', NULL)"),
                  {"n": no, "d": date or _d(0), "c": code, "nm": f"종목{code}", "t": typ, "p": pol})


def test_validated_stocks_filtered_by_type_mcap_and_recency(db):
    _snap("111111", 50000); _snap("222222", 5000); _snap("333333", 1500); _snap("444444", 8000)
    _ev("A1", "111111", "자사주취득")
    _ev("A2", "222222", "자사주취득")
    _ev("A3", "333333", "자사주취득")                 # 시총 3,000억 미만 → 제외
    _ev("A4", "444444", "유상증자", pol="악재")        # 검증된 호재 유형이 아님
    _ev("A5", "111111", "자사주취득")                 # 같은 종목 중복 → 한 번만
    _ev("A8", "444444", "배당결정")                   # 소각·배당은 검증 표본 0건이라 검증 유형이 아니다
    _ev("A6", "222222", "자사주취득", date=_d(9))     # 오래됨(3일 밖)
    _ev("A7", "999999", "자사주취득")                 # 스냅샷에 없는 종목(유니버스 밖) → 제외
    got = de.validated_event_stocks(days=3)
    assert [g["code"] for g in got] == ["111111", "222222"]          # 시총 큰 순
    assert got[0]["mcap_eok"] == 50000


def test_no_validated_events_returns_empty(db):
    _snap("111111", 50000)
    assert de.validated_event_stocks() == []


def test_discovery_adds_validated_event_stocks_with_label(db):
    _snap("111111", 50000); _ev("B1", "111111", "자사주취득")
    out = da._validated_event_candidates(set())
    assert len(out) == 1 and out[0]["code"] == "111111" and out[0]["signals"][0].startswith("검증공시:자사주취득")


def test_discovery_excludes_holdings_and_caps_share(db):
    for i in range(8):
        code = f"10000{i}"
        _snap(code, 90000 - i * 1000); _ev(f"C{i}", code, "자사주취득")
    out = da._validated_event_candidates({"100000"})
    assert len(out) == da._VALIDATED_EVENT_SHARE and all(c["code"] != "100000" for c in out)


def test_discovery_validated_failure_falls_back(monkeypatch):
    monkeypatch.setattr(de, "validated_event_stocks", lambda **k: (_ for _ in ()).throw(RuntimeError("db")))
    assert da._validated_event_candidates(set()) == []


def test_pool_merges_validated_with_screener_and_kis(db, monkeypatch):
    """점수가 낮아 스크리너에 없던 검증 사건 종목도 후보 풀에 들어오고, 이미 스크리너 후보면 신호가 합쳐진다."""
    monkeypatch.setattr(da, "_screener_candidates", lambda ex: [{"code": "111111", "name": "A", "market": "KOSPI", "signals": ["전종목스크린4묶음(2일 연속)"]}])
    monkeypatch.setattr(da, "_validated_event_candidates", lambda ex: [
        {"code": "111111", "name": "A", "market": "KOSPI", "signals": ["검증공시:자사주취득(10-22)"]},
        {"code": "222222", "name": "B", "market": "KOSPI", "signals": ["검증공시:배당결정(10-22)"]}])

    class Kis:
        def get_foreign_buy_rank(self, m): return []
        get_institution_buy_rank = get_amount_rank = get_foreign_buy_rank
    pool = da._build_candidate_pool(Kis(), set())
    by = {c["code"]: c for c in pool}
    assert set(by) == {"111111", "222222"}
    assert len(by["111111"]["signals"]) == 2                       # 신호 합쳐짐
