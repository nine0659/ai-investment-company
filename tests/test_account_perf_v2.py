"""2026-10-14 계좌성과 개편 회귀: 시장/종목 분해, 가짜 매도 제외, 기준점 재설정, 추천 성적표."""
import pytest

from services import account_perf, rec_scorecard

CLOSES = {"2026-09-01": 100.0, "2026-10-14": 90.0}
POS = [
    {"code": "A", "name": "에이", "qty": 10, "avg": 1000.0, "price": 800.0, "value": 8000.0},   # -20%
    {"code": "B", "name": "비", "qty": 10, "avg": 1000.0, "price": 950.0, "value": 9500.0},     # -5%
]


def test_decomposition_adds_up_and_shares():
    c = account_perf.compute(POS, CLOSES, {"A": "2026-09-01", "B": "2026-09-01"}, {"A": 1.5, "B": 0.5})
    a = next(r for r in c["rows"] if r["name"] == "에이")
    assert a["mkt_won"] == pytest.approx(10000 * 1.5 * -10 / 100)           # 베타1.5 × 지수 -10%
    assert c["mkt_part"] + c["sel_part"] == pytest.approx(c["pnl"])        # 시장 몫+종목 몫 = 총손익
    assert a["share"] > next(r for r in c["rows"] if r["name"] == "비")["share"]
    assert sum(r["share"] for r in c["rows"]) == pytest.approx(100.0)


def test_missing_entry_is_flagged_not_guessed():
    c = account_perf.compute(POS, CLOSES, {"A": "2026-09-01"}, {"A": 1.0})
    assert not c["complete"] and c["decomp_n"] == 1


def test_realized_excludes_kis_sync_artifacts():
    rows = [
        {"quantity": 10, "avg_price": 100, "exit_price": 120, "memo": ""},
        {"quantity": 10, "avg_price": 100, "exit_price": 80, "memo": None},
        {"quantity": 3, "avg_price": 100, "exit_price": 50, "memo": " [2026-08-14 KIS 동기화: 실계좌에서 확인 안 됨]"},
    ]
    r = account_perf.realized_stats(rows)
    assert r["n"] == 2 and r["excluded"] == 1 and r["wins"] == 1
    assert r["total"] == pytest.approx(10 * 20 + 10 * -20)


def test_since_last_reset_ignores_earlier_history():
    nav = [
        {"date": "2026-08-13", "total_value": 35.6e6, "total_cost": 44.8e6, "kospi_close": 6800.0},
        {"date": "2026-08-14", "total_value": 44.8e6, "total_cost": 44.8e6, "kospi_close": 7000.0},   # 재설정
        {"date": "2026-10-14", "total_value": 33.6e6, "total_cost": 44.8e6, "kospi_close": 6300.0},
    ]
    r = account_perf.since_last_reset(nav)
    assert r["since"] == "2026-08-14"
    assert r["acct"] == pytest.approx((33.6 / 44.8 - 1) * 100)
    assert r["kospi"] == pytest.approx(-10.0)


def test_scorecard_alpha_and_independence_note():
    recs = [
        {"id": 1, "date": "2026-09-01", "code": "A", "name": "에이", "entry": 100.0, "price": 80.0, "price_date": "2026-10-13"},
        {"id": 2, "date": "2026-09-01", "code": "A", "name": "에이", "entry": 100.0, "price": 80.0, "price_date": "2026-10-13"},
        {"id": 3, "date": "2026-09-01", "code": "B", "name": "비", "entry": 100.0, "price": 95.0, "price_date": "2026-10-14"},
    ]
    s = rec_scorecard.score(recs, CLOSES)
    assert s["n"] == 3 and s["stocks"] == 2 and s["days"] == 1
    assert s["beat"] == 1                          # KOSPI -10%: A -20%(졌음), B -5%(+5%p로 이김)
    assert "독립 표본" in rec_scorecard.format_scorecard(s)


def test_scorecard_stale_price_not_used():
    recs = [{"id": 1, "date": "2026-09-01", "code": "A", "name": "에이", "entry": 100.0,
             "price": 99.0, "price_date": "2026-07-20"}]          # 석 달 전 마지막 추적가
    assert rec_scorecard.score(recs, CLOSES)["n"] == 0            # 현재가 없으면 제외(오래된 값으로 계산 금지)
    s = rec_scorecard.score(recs, CLOSES, quote=lambda c: {"price": 85.0})
    assert s["n"] == 1 and s["rows"][0]["ret"] == pytest.approx(-15.0)
