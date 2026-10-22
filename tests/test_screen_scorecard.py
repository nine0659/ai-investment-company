"""스크리너 성적표 회귀 테스트 — 기준선은 같은 기간 전 종목 중앙값, 표본 부족 표시, 수집 이상 날 제외."""
import pytest
from sqlalchemy import text

from db.database import get_conn, init_db
from services import screen_scorecard as sc


@pytest.fixture
def db():
    init_db()
    with get_conn() as c:
        c.execute(text("DELETE FROM universe_screen_daily"))
    yield
    with get_conn() as c:
        c.execute(text("DELETE FROM universe_screen_daily"))


def _day(date, closes: dict, scores: dict | None = None):
    scores = scores or {}
    with get_conn() as c:
        for code, close in closes.items():
            c.execute(text(
                "INSERT INTO universe_screen_daily (date, code, name, market, close, mcap_eok, score, signals) "
                "VALUES (:d, :c, :c, 'KOSPI', :cl, 5000, :s, '')"), {"d": date, "c": code, "cl": close, "s": scores.get(code, 0)})


def _universe(n=120):
    return {f"{i:06d}": 100.0 for i in range(n)}


def test_excess_return_is_relative_to_universe_median(db):
    base = _universe()
    d1 = dict(base)
    d2 = {c: 110.0 for c in base}                         # 전 종목 +10% (시장 상승)
    d2["000000"] = 130.0                                  # 후보 하나만 +30%
    _day("2026-10-01", d1, {"000000": 4})
    _day("2026-10-02", d2)
    ev = sc.evaluate("KR", 1)
    assert ev["n_obs"] == 1 and ev["avg_ret"] == pytest.approx(30.0)
    assert ev["bench_avg"] == pytest.approx(10.0) and ev["avg_excess"] == pytest.approx(20.0)
    assert ev["hit_rate"] == 100.0


def test_market_wide_rally_is_not_credited_to_picks(db):
    base = _universe()
    _day("2026-10-01", base, {"000000": 4})
    _day("2026-10-02", {c: 110.0 for c in base})          # 후보도 시장과 같이 +10%
    assert sc.evaluate("KR", 1)["avg_excess"] == pytest.approx(0.0)


def test_low_score_stocks_are_not_counted(db):
    base = _universe()
    _day("2026-10-01", base, {"000000": 2})
    _day("2026-10-02", {c: 120.0 for c in base})
    assert sc.evaluate("KR", 1)["n_obs"] == 0


def test_small_sample_flagged_untrusted(db):
    base = _universe()
    _day("2026-10-01", base, {"000000": 4})
    _day("2026-10-02", {c: 105.0 for c in base})
    ev = sc.evaluate("KR", 1)
    assert ev["trusted"] is False
    assert "30건 미만" in sc.format_report(("KR",), (1,))


def test_day_with_too_few_comparable_stocks_is_skipped(db):
    _day("2026-10-01", {f"{i:06d}": 100.0 for i in range(20)}, {"000000": 4})
    _day("2026-10-02", {f"{i:06d}": 110.0 for i in range(20)})
    assert sc.evaluate("KR", 1)["n_obs"] == 0              # 수집 이상으로 종목이 적은 날은 기준선이 믿을 수 없다


def test_horizon_uses_snapshot_count(db):
    base = _universe()
    for i, price in enumerate((100, 101, 102, 103)):
        _day(f"2026-10-0{i + 1}", {c: float(price) for c in base}, {"000000": 4} if i == 0 else None)
    assert sc.evaluate("KR", 2)["n_obs"] == 1              # 첫 날 후보 → 2스냅샷 뒤
    assert sc.evaluate("KR", 3)["n_obs"] == 1
    assert sc.evaluate("KR", 5)["n_obs"] == 0              # 아직 5스냅샷 뒤 데이터 없음


def test_by_score_breakdown_only_when_trusted(db):
    base = _universe(150)
    scores = {f"{i:06d}": (3 if i % 3 == 0 else 5) for i in range(40)}
    for d in range(1, 4):
        _day(f"2026-10-0{d}", {c: 100.0 + d for c in base}, scores)
    ev = sc.evaluate("KR", 1)
    assert ev["trusted"] and set(ev["by_score"]) == {3, 5}
    assert "신호 묶음 수별" in sc.format_report(("KR",), (1,))


def test_empty_report_when_no_observations(db):
    assert sc.format_report() == ""


def test_evaluate_survives_db_error(monkeypatch):
    monkeypatch.setattr(sc, "_dates", lambda t: (_ for _ in ()).throw(RuntimeError("db")))
    assert sc.evaluate("KR", 5)["n_obs"] == 0
