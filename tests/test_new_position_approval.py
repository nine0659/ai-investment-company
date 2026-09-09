"""CIO 신규편입 승인 큐 회귀 테스트 (2026-09-09).

미래에셋증권에는 개인 개발자용 Open API가 없어 실계좌 자동집행·자동조회가
불가능하다고 확인됨 — 그래서 CEO의 신규편입 판단(=ceo_decisions.new_positions)을
자동매수 대신 텔레그램 버튼 승인/보류/기각 큐로 사람이 최종 확정하고, 그 반응을
학습 루프 데이터(stock_recommendations.user_action)로 남긴다.

_register_drafts(agents/ceo_agent.py)가 만든 status='draft' 행(수량/가격 0)을
승인 시 approve_draft_position이 실제 체결 수량/가격으로 전환한다. add_position은
기존 holding과 평단을 합산 병합하므로 이 용도에 맞지 않다(별도 함수로 분리한 이유).
"""
from db.database import init_db, get_conn
from sqlalchemy import text

from agents.ceo_agent import _track_recommendations
from services.portfolio_service import (
    approve_draft_position,
    reject_new_position,
    defer_new_position,
    register_draft_positions,
)


def setup_function(_):
    init_db()
    with get_conn() as conn:
        conn.execute(text("DELETE FROM portfolio_positions"))
        conn.execute(text("DELETE FROM stock_recommendations"))


def _insert_draft(code, date, name="테스트종목"):
    with get_conn() as conn:
        conn.execute(
            text(
                "INSERT INTO portfolio_positions "
                "(code, name, quantity, avg_price, entry_date, target_price, stop_price, status) "
                "VALUES (:c, :n, 0, 0, :d, 0, 0, 'draft')"
            ),
            {"c": code, "n": name, "d": date},
        )


def _insert_recommendation(code, date, name="테스트종목"):
    with get_conn() as conn:
        conn.execute(
            text(
                "INSERT INTO stock_recommendations "
                "(date, code, name, entry_price, stop_price, target_price, rationale) "
                "VALUES (:d, :c, :n, 100000, 85000, 137500, '테스트 근거')"
            ),
            {"d": date, "c": code, "n": name},
        )


# ── _track_recommendations가 recs를 반환하는지 ──────────────────────

def test_track_recommendations_returns_recs():
    decisions = {"new_positions": [
        {"code": "005930", "name": "삼성전자", "risk_reward": "3:1", "thesis": "메모리 업사이클"},
    ]}

    class _FakeKIS:
        def get_stock_price(self, code):
            return {"price": 100_000}

    import agents.ceo_agent as ceo_agent
    orig_kis_client = None
    import clients.kis_client as kis_module
    orig_kis_client = kis_module.KISClient
    kis_module.KISClient = lambda: _FakeKIS()
    try:
        recs = _track_recommendations("2026-09-09", decisions)
    finally:
        kis_module.KISClient = orig_kis_client

    assert len(recs) == 1
    assert recs[0]["code"] == "005930"
    assert recs[0]["entry_price"] == 100_000
    with get_conn() as conn:
        row = conn.execute(
            text("SELECT code FROM stock_recommendations WHERE date='2026-09-09'")
        ).fetchone()
    assert row is not None


def test_track_recommendations_empty_positions_returns_empty_list():
    assert _track_recommendations("2026-09-09", {"new_positions": []}) == []
    assert _track_recommendations("2026-09-09", {}) == []


# ── approve_draft_position ──────────────────────────────────────────

def test_approve_converts_draft_to_holding():
    _insert_draft("005930", "2026-09-09", "삼성전자")
    _insert_recommendation("005930", "2026-09-09", "삼성전자")

    result = approve_draft_position(
        code="005930", date="2026-09-09", qty=10, fill_price=71_500,
        target_price=137_500, stop_price=85_000,
    )
    assert result is not None
    assert result["qty"] == 10
    assert result["fill_price"] == 71_500

    with get_conn() as conn:
        row = conn.execute(
            text("SELECT quantity, avg_price, status FROM portfolio_positions WHERE code='005930'")
        ).fetchone()
        rec = conn.execute(
            text("SELECT user_action FROM stock_recommendations WHERE code='005930'")
        ).fetchone()
    assert row == (10, 71_500, "holding")
    assert rec[0] == "approved"


def test_approve_missing_draft_returns_none():
    assert approve_draft_position(
        code="005930", date="2026-09-09", qty=10, fill_price=71_500,
        target_price=0, stop_price=0,
    ) is None


def test_approve_twice_second_call_returns_none():
    _insert_draft("005930", "2026-09-09")
    first = approve_draft_position("005930", "2026-09-09", 10, 71_500, 0, 0)
    second = approve_draft_position("005930", "2026-09-09", 10, 71_500, 0, 0)
    assert first is not None
    assert second is None  # 이미 holding으로 바뀌어 draft 조건에 안 걸림


# ── reject_new_position ──────────────────────────────────────────────

def test_reject_marks_draft_rejected():
    _insert_draft("005930", "2026-09-09")
    _insert_recommendation("005930", "2026-09-09")

    assert reject_new_position("005930", "2026-09-09") is True
    with get_conn() as conn:
        status = conn.execute(
            text("SELECT status FROM portfolio_positions WHERE code='005930'")
        ).fetchone()[0]
        action = conn.execute(
            text("SELECT user_action FROM stock_recommendations WHERE code='005930'")
        ).fetchone()[0]
    assert status == "rejected"
    assert action == "rejected"


def test_reject_missing_draft_returns_false():
    assert reject_new_position("005930", "2026-09-09") is False


def test_reject_already_rejected_returns_false():
    _insert_draft("005930", "2026-09-09")
    assert reject_new_position("005930", "2026-09-09") is True
    assert reject_new_position("005930", "2026-09-09") is False


# ── defer_new_position ────────────────────────────────────────────────

def test_defer_leaves_draft_untouched_marks_recommendation():
    _insert_draft("005930", "2026-09-09")
    _insert_recommendation("005930", "2026-09-09")

    defer_new_position("005930", "2026-09-09")

    with get_conn() as conn:
        status = conn.execute(
            text("SELECT status FROM portfolio_positions WHERE code='005930'")
        ).fetchone()[0]
        action = conn.execute(
            text("SELECT user_action FROM stock_recommendations WHERE code='005930'")
        ).fetchone()[0]
    assert status == "draft"  # 보류는 draft를 그대로 둔다
    assert action == "deferred"


# ── register_draft_positions (2026-09-09 주간추천 확장 — PRE/weekly 공유) ─────

def test_register_draft_positions_inserts_new_rows():
    items = [{"code": "005930", "name": "삼성전자", "timeframe": "mid", "memo": "테스트"}]
    n = register_draft_positions("2026-09-09", items)
    assert n == 1
    with get_conn() as conn:
        row = conn.execute(
            text("SELECT quantity, avg_price, status FROM portfolio_positions WHERE code='005930'")
        ).fetchone()
    assert row == (0, 0.0, "draft")


def test_register_draft_positions_skips_existing_draft():
    items = [{"code": "005930", "name": "삼성전자", "timeframe": "mid", "memo": "테스트"}]
    assert register_draft_positions("2026-09-09", items) == 1
    assert register_draft_positions("2026-09-09", items) == 0  # 같은 (code,date) draft 중복 스킵


def test_register_draft_positions_skips_missing_code():
    items = [{"code": "", "name": "이름만있음", "timeframe": "mid", "memo": ""}]
    assert register_draft_positions("2026-09-09", items) == 0
