"""전 종목 스크리닝 일별 스냅샷·잡·발굴 연동 회귀 테스트.

잡 상태 규약(job_runs): 수신 불완전/상세 80% 미만=fail, 저장 0건=empty — 조용한 부분 실패를 success로 찍지 않는다.
"""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text

import scheduler
from agents import discovery_agent
from db.database import get_conn, init_db
from services import job_ledger
from services import universe_screener as us

_KST = ZoneInfo("Asia/Seoul")


def _scored(code="111111", score=3):
    keys = ["저평가", "컨센서스여력", "쌍끌이매수"][:score] or []
    return {"code": code, "name": f"종목{code}", "market": "KOSPI", "close": 10000.0, "chg_pct": 1.0,
            "mcap_eok": 5000.0, "value_eok": 10.0,
            "detail": {"per": 8.0, "pbr": 0.8, "cns_per": 6.0, "eps": 1000.0, "target": 15000.0, "high52": 20000.0},
            "signals": [{"key": k, "text": ""} for k in keys], "score": score}


@pytest.fixture
def clean_db():
    init_db()
    with get_conn() as c:
        c.execute(text("DELETE FROM universe_screen_daily"))
    yield
    with get_conn() as c:
        c.execute(text("DELETE FROM universe_screen_daily"))


def _res(rows, complete=True, fetched=4000, expected=4000, detailed=None, requested=None):
    return {"scored": rows, "picks": [r for r in rows if r["score"] >= 3], "universe_complete": complete,
            "fetched": fetched, "expected": expected,
            "detailed": len(rows) if detailed is None else detailed,
            "detail_requested": len(rows) if requested is None else requested}


def test_save_and_load_roundtrip(clean_db):
    n = us.save_daily(_res([_scored("111111", 3), _scored("222222", 1)]), date="2026-10-22")
    assert n == 2
    picks = us.load_picks()
    assert [p["code"] for p in picks] == ["111111"]            # 3묶음 이상만
    assert picks[0]["date"] == "2026-10-22" and "저평가" in picks[0]["signals"]


def test_save_replaces_same_day(clean_db):
    us.save_daily(_res([_scored("111111", 3)]), date="2026-10-22")
    us.save_daily(_res([_scored("333333", 3)]), date="2026-10-22")
    assert [p["code"] for p in us.load_picks()] == ["333333"]


def test_streaks_counts_consecutive_days(clean_db):
    for d, codes in (("2026-10-20", ["111111"]), ("2026-10-21", ["111111", "222222"]), ("2026-10-22", ["111111"])):
        us.save_daily(_res([_scored(c, 3) for c in codes]), date=d)
    st = us.streaks([{"code": "111111"}, {"code": "222222"}])
    assert st["111111"] == 3
    assert st["222222"] == 0            # 최신일(10-22)에 빠졌으니 연속 0


def test_load_picks_empty_when_no_snapshot(clean_db):
    assert us.load_picks() == []


@pytest.mark.parametrize("res,status", [
    (_res([_scored()], complete=False, fetched=100, expected=4000), "fail"),
    (_res([_scored()], detailed=50, requested=100), "fail"),
    (_res([]), "empty"),
    (_res([_scored()]), "success"),
])
def test_run_daily_job_status(clean_db, monkeypatch, res, status):
    monkeypatch.setattr(us, "run_screen", lambda: res)
    assert us.run_daily_job()[0] == status


def test_scheduler_job_skips_non_trading_day(monkeypatch):
    rec = []
    monkeypatch.setattr(scheduler, "is_krx_trading_day", lambda: False)
    monkeypatch.setattr("services.job_ledger.record_job", lambda n, s, d="": rec.append((n, s)))
    scheduler.job_universe_screen()
    assert rec == [("universe_screen", "skipped")]


def test_scheduler_job_records_failure_without_raising(monkeypatch):
    rec = []
    monkeypatch.setattr(scheduler, "is_krx_trading_day", lambda: True)
    monkeypatch.setattr("services.job_ledger.record_job", lambda n, s, d="": rec.append((n, s)))
    monkeypatch.setattr(us, "run_daily_job", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    scheduler.job_universe_screen()
    assert rec == [("universe_screen", "fail")]


def test_job_is_in_ledger_every_weekday():
    for wd in range(5):
        assert "universe_screen" in job_ledger._EXPECTED_BY_WEEKDAY[wd]


def test_discovery_uses_screener_picks(monkeypatch):
    today = datetime.now(_KST).strftime("%Y-%m-%d")
    picks = [{"date": today, "code": "111111", "name": "A", "market": "KOSPI", "score": 4, "signals": ""},
             {"date": today, "code": "999999", "name": "보유중", "market": "KOSPI", "score": 4, "signals": ""}]
    monkeypatch.setattr(us, "load_picks", lambda **kw: picks)
    monkeypatch.setattr(us, "streaks", lambda p: {"111111": 3})
    out = discovery_agent._screener_candidates(exclude={"999999"})
    assert [c["code"] for c in out] == ["111111"]
    assert "3일 연속" in out[0]["signals"][0]


def test_discovery_ignores_stale_snapshot(monkeypatch):
    old = (datetime.now(_KST) - timedelta(days=10)).strftime("%Y-%m-%d")
    monkeypatch.setattr(us, "load_picks", lambda **kw: [{"date": old, "code": "1", "name": "A", "market": "KOSPI",
                                                         "score": 4, "signals": ""}])
    assert discovery_agent._screener_candidates(set()) == []


def test_discovery_screener_failure_falls_back(monkeypatch):
    def boom(**kw):
        raise RuntimeError("db down")
    monkeypatch.setattr(us, "load_picks", boom)
    assert discovery_agent._screener_candidates(set()) == []
