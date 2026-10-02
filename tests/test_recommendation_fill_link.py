"""추천 → 내 실제 체결 연결 회귀 테스트 (2026-10-02).

사고: 추천 시점 가상가 기준 +10.59%가 내 실제 손익(-5.6%)처럼 알림으로 나갔다.
설계: 내가 매수한 추천은 내 체결가 기준으로 추적, 미채택은 가상 성과로 분리.
"""
from datetime import datetime, timedelta

from sqlalchemy import text

import services.recommendation_tracker_service as tracker
from db.database import init_db, get_conn
from services.portfolio_service import (
    approve_draft_position,
    link_recommendation_fill,
    unlink_recommendation_fill,
)


def _d(offset=0):
    return (datetime.now() + timedelta(days=offset)).strftime("%Y-%m-%d")


def setup_function(_):
    init_db()
    with get_conn() as conn:
        for t in ("portfolio_positions", "stock_recommendations", "recommendation_tracking"):
            conn.execute(text(f"DELETE FROM {t}"))


def _rec(code="000660", date=None, action=None):
    with get_conn() as conn:
        conn.execute(text(
            "INSERT INTO stock_recommendations (date, code, name, entry_price, stop_price, "
            "target_price, user_action) VALUES (:d,:c,'SK하이닉스',1653000,1487700,1820000,:a)"),
            {"d": date or _d(-5), "c": code, "a": action})


def _row(code="000660"):
    with get_conn() as conn:
        return conn.execute(text(
            "SELECT user_action, fill_price, fill_qty FROM stock_recommendations WHERE code=:c"),
            {"c": code}).fetchone()


def test_link_attaches_my_fill_to_recent_recommendation():
    _rec()
    r = link_recommendation_fill("000660", 1_742_000, 3, _d())
    assert r and r["fill_price"] == 1_742_000
    assert tuple(_row()) == ("approved", 1_742_000, 3)


def test_link_ignores_unrelated_cases():
    _rec(date=_d(-60))                      # 45일 초과
    assert link_recommendation_fill("000660", 1_700_000, 1, _d()) is None
    _rec(code="005380", action="rejected")  # 기각한 추천
    assert link_recommendation_fill("005380", 400_000, 1, _d()) is None
    _rec(code="009150", date=_d(3))         # 체결일보다 미래의 추천
    assert link_recommendation_fill("009150", 1_795_000, 1, _d()) is None
    assert link_recommendation_fill("999999", 1, 1, _d()) is None


def test_link_is_one_shot_and_unlinkable():
    _rec()
    assert link_recommendation_fill("000660", 1_742_000, 3, _d())
    assert link_recommendation_fill("000660", 1_900_000, 1, _d()) is None  # 이미 연결됨
    assert unlink_recommendation_fill("000660") is True
    assert tuple(_row()) == (None, None, None)


def test_approve_draft_records_fill():
    d = _d(-1)
    _rec(date=d)
    with get_conn() as conn:
        conn.execute(text(
            "INSERT INTO portfolio_positions (code,name,quantity,avg_price,entry_date,"
            "target_price,stop_price,status) VALUES ('000660','SK하이닉스',0,0,:d,0,0,'draft')"),
            {"d": d})
    approve_draft_position("000660", d, 2, 1_700_000, 1_820_000, 1_487_700)
    assert tuple(_row()) == ("approved", 1_700_000, 2)


def test_tracker_uses_my_fill_as_baseline_only_when_adopted():
    _rec(code="000660")
    _rec(code="005380")
    link_recommendation_fill("000660", 1_742_000, 3, _d())
    recs = {r["code"]: r for r in tracker._get_active_recommendations()}
    assert recs["000660"]["entry_price"] == 1_742_000 and recs["000660"]["adopted"]
    assert recs["005380"]["entry_price"] == 1653000 and not recs["005380"]["adopted"]


def test_report_splits_adopted_from_virtual(monkeypatch):
    monkeypatch.setattr(tracker, "get_tracking_summary", lambda days=30: {
        "total": 2, "win_rate": 50.0, "avg_return": 1.0, "target_rate": 0.0,
        "items": [
            {"code": "000660", "name": "SK하이닉스", "rec_date": "2026-08-30",
             "return_pct": -5.58, "days_held": 21, "status": "tracking", "adopted": True},
            {"code": "005380", "name": "현대차", "rec_date": "2026-08-30",
             "return_pct": 10.0, "days_held": 21, "status": "target_hit", "adopted": False},
        ]})
    monkeypatch.setattr(tracker, "_get_held_codes", lambda: {"000660"})
    out = tracker.format_tracker_report()
    assert "내가 매수한 추천 (1건)" in out and "내 체결가 기준" in out
    assert "미채택 추천 (1건)" in out and "가상 성과" in out
    assert out.index("내가 매수한 추천") < out.index("SK하이닉스") < out.index("미채택 추천") < out.index("현대차")
