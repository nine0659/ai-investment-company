"""종목발굴 워치리스트 승인 큐 회귀 테스트 (2026-09-09).

discovery_agent가 찾은 후보를 곧바로 status='active'로 등록하던 것을
status='candidate'로 스테이징 → 텔레그램 승인 버튼을 눌러야 active로 전환되도록
바꿨다. add_to_watchlist를 재사용하지 않는 이유: 그 함수의 UPDATE 분기가
status='active'를 하드코딩해 candidate를 즉시 덮어써버리기 때문(별도 함수로 분리).
"""
from db.database import init_db, get_conn
from sqlalchemy import text

from services.watchlist_service import (
    stage_watchlist_candidate,
    approve_watchlist_candidate,
    reject_watchlist_candidate,
    get_watchlist,
)


def setup_function(_):
    init_db()
    with get_conn() as conn:
        conn.execute(text("DELETE FROM watchlist_items"))


def test_stage_creates_candidate_row():
    row_id = stage_watchlist_candidate("005930", "삼성전자", target_entry=70000, reason="수급+반등")
    assert row_id is not None
    with get_conn() as conn:
        status = conn.execute(
            text("SELECT status FROM watchlist_items WHERE code='005930'")
        ).fetchone()[0]
    assert status == "candidate"
    assert get_watchlist("active") == []  # 승인 전엔 active 목록에 안 잡힘


def test_stage_skips_existing_code():
    assert stage_watchlist_candidate("005930", "삼성전자", 70000, "1차") is not None
    assert stage_watchlist_candidate("005930", "삼성전자", 71000, "2차 중복") is None


def test_approve_converts_candidate_to_active():
    stage_watchlist_candidate("005930", "삼성전자", 70000, "수급+반등")
    assert approve_watchlist_candidate("005930") is True

    with get_conn() as conn:
        status = conn.execute(
            text("SELECT status FROM watchlist_items WHERE code='005930'")
        ).fetchone()[0]
    assert status == "active"
    assert len(get_watchlist("active")) == 1


def test_approve_missing_candidate_returns_false():
    assert approve_watchlist_candidate("005930") is False


def test_approve_twice_second_call_returns_false():
    stage_watchlist_candidate("005930", "삼성전자", 70000, "수급+반등")
    assert approve_watchlist_candidate("005930") is True
    assert approve_watchlist_candidate("005930") is False


def test_reject_marks_removed():
    stage_watchlist_candidate("005930", "삼성전자", 70000, "수급+반등")
    assert reject_watchlist_candidate("005930") is True

    with get_conn() as conn:
        status = conn.execute(
            text("SELECT status FROM watchlist_items WHERE code='005930'")
        ).fetchone()[0]
    assert status == "removed"
    assert get_watchlist("active") == []


def test_reject_missing_candidate_returns_false():
    assert reject_watchlist_candidate("005930") is False


def test_reject_already_rejected_returns_false():
    stage_watchlist_candidate("005930", "삼성전자", 70000, "수급+반등")
    assert reject_watchlist_candidate("005930") is True
    assert reject_watchlist_candidate("005930") is False
