"""job_rebound_screener 스케줄러 래퍼 — 거래일 스킵·중복방지·기록 검증.

2026-08-07: /rebound의 KIS TR_ID 버그를 고친 뒤, 주 1회(금 15:00) 자동
발송으로 전환하면서 CLAUDE.md 절대원칙 4(신규 발송 경로는 claim_report_slot
가드 필수)에 따라 추가한 잡. 회귀 방지용 테스트.
"""
from datetime import datetime
from zoneinfo import ZoneInfo

import agents.rebound_screener_agent as rebound_screener_agent
import scheduler
from db.database import get_conn, init_db
from services import job_ledger
from sqlalchemy import text

_KST = ZoneInfo("Asia/Seoul")


def _clear_today_traces():
    today = datetime.now(_KST).strftime("%Y-%m-%d")
    with get_conn() as conn:
        conn.execute(
            text("DELETE FROM job_runs WHERE date=:d AND job_name='rebound_screener'"),
            {"d": today},
        )
        conn.execute(
            text("DELETE FROM report_claims WHERE date=:d AND run_type='rebound_screener'"),
            {"d": today},
        )


def test_skips_on_non_trading_day(monkeypatch):
    init_db()
    _clear_today_traces()
    monkeypatch.setattr(scheduler, "is_krx_trading_day", lambda: False)
    called = []
    monkeypatch.setattr(rebound_screener_agent, "run_rebound_screen", lambda send=True: called.append(1))

    scheduler.job_rebound_screener()

    assert called == []
    assert job_ledger.has_trace_today("rebound_screener") is True


def test_runs_on_trading_day(monkeypatch):
    init_db()
    _clear_today_traces()
    monkeypatch.setattr(scheduler, "is_krx_trading_day", lambda: True)
    called = []
    monkeypatch.setattr(rebound_screener_agent, "run_rebound_screen", lambda send=True: called.append(1))

    scheduler.job_rebound_screener()

    assert called == [1]
    assert job_ledger.has_trace_today("rebound_screener") is True


def test_second_trigger_same_day_is_skipped(monkeypatch):
    """Render 재시작 등으로 같은 날 잡이 두 번 뜨는 상황을 흉내 — claim 가드가 막아야 함."""
    init_db()
    _clear_today_traces()
    monkeypatch.setattr(scheduler, "is_krx_trading_day", lambda: True)
    calls = []
    monkeypatch.setattr(rebound_screener_agent, "run_rebound_screen", lambda send=True: calls.append(1))

    scheduler.job_rebound_screener()
    scheduler.job_rebound_screener()

    assert len(calls) == 1
