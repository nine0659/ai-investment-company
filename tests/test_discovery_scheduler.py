"""job_discovery_weekly 스케줄러 래퍼 — 거래일 스킵·중복방지·조건부발송 검증.

2026-08-07: 종목발굴(discovery_agent)은 2026-07-06 신뢰성 회복 계획으로
자동 스케줄에서 빠졌던 잡이다. 사용자가 "완전 자동 발송은 아니고, 진짜
좋은 종목일 때만 보내는 조건부라면 지금 재개해도 된다"고 명시적으로
승인해 조건부(silent_if_empty) 방식으로 재개했다 — 이 조건이 실제로
지켜지는지 검증하는 회귀 테스트.
"""
from datetime import datetime
from zoneinfo import ZoneInfo

import agents.discovery_agent as discovery_agent
import scheduler
from db.database import get_conn, init_db
from services import job_ledger
from sqlalchemy import text

_KST = ZoneInfo("Asia/Seoul")


def _clear_today_traces():
    today = datetime.now(_KST).strftime("%Y-%m-%d")
    with get_conn() as conn:
        conn.execute(
            text("DELETE FROM job_runs WHERE date=:d AND job_name='discovery_weekly'"),
            {"d": today},
        )
        conn.execute(
            text("DELETE FROM report_claims WHERE date=:d AND run_type='discovery_weekly'"),
            {"d": today},
        )


def test_skips_on_non_trading_day(monkeypatch):
    init_db()
    _clear_today_traces()
    monkeypatch.setattr(scheduler, "is_krx_trading_day", lambda: False)
    called = []
    monkeypatch.setattr(discovery_agent, "run_discovery", lambda **kw: called.append(kw))

    scheduler.job_discovery_weekly()

    assert called == []
    assert job_ledger.has_trace_today("discovery_weekly") is True


def test_runs_with_silent_if_empty_true(monkeypatch):
    """자동 경로는 반드시 silent_if_empty=True로 호출해야 한다 — 공허한 리포트 반복 방지."""
    init_db()
    _clear_today_traces()
    monkeypatch.setattr(scheduler, "is_krx_trading_day", lambda: True)
    calls = []
    monkeypatch.setattr(discovery_agent, "run_discovery", lambda **kw: calls.append(kw))

    scheduler.job_discovery_weekly()

    assert len(calls) == 1
    assert calls[0].get("send") is True
    assert calls[0].get("silent_if_empty") is True
    assert job_ledger.has_trace_today("discovery_weekly") is True


def test_second_trigger_same_day_is_skipped(monkeypatch):
    init_db()
    _clear_today_traces()
    monkeypatch.setattr(scheduler, "is_krx_trading_day", lambda: True)
    calls = []
    monkeypatch.setattr(discovery_agent, "run_discovery", lambda **kw: calls.append(kw))

    scheduler.job_discovery_weekly()
    scheduler.job_discovery_weekly()

    assert len(calls) == 1
