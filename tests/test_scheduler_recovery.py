"""daily_health 재시작 복구 — 2026-08-07 발견된 2주간 미실행 사고 회귀 테스트.

daily_health는 08:05 컨테이너 재시작과 겹치면 소리 없이 스킵됐고, 그 대상이
_recover_missed_briefings()의 복구 목록(pre_market/close_market)에 없어
아무도 대신 실행해주지 않았다. scheduler._recover_missed_daily_health()가
이 공백을 메운다.
"""
from datetime import datetime
from zoneinfo import ZoneInfo

import scheduler
from db.database import init_db
from services import job_ledger

_KST = ZoneInfo("Asia/Seoul")


def _reset_today_trace():
    from db.database import get_conn
    from sqlalchemy import text
    today = datetime.now(_KST).strftime("%Y-%m-%d")
    with get_conn() as conn:
        conn.execute(
            text("DELETE FROM job_runs WHERE date=:d AND job_name='daily_health'"),
            {"d": today},
        )


def test_before_0805_does_not_trigger(monkeypatch):
    init_db()
    _reset_today_trace()
    triggered = []
    monkeypatch.setattr(scheduler, "job_daily_health", lambda: triggered.append(True))

    early = datetime.now(_KST).replace(hour=7, minute=0)
    scheduler._recover_missed_daily_health(now=early)

    assert triggered == []


def test_after_0805_without_trace_triggers(monkeypatch):
    init_db()
    _reset_today_trace()
    triggered = []
    monkeypatch.setattr(scheduler, "job_daily_health", lambda: triggered.append(True))

    late = datetime.now(_KST).replace(hour=9, minute=0)
    scheduler._recover_missed_daily_health(now=late)

    # 백그라운드 스레드로 실행되므로 join으로 완료를 기다린다
    import threading
    for t in threading.enumerate():
        if t.name == "recover-daily_health":
            t.join(timeout=2)

    assert triggered == [True]


def test_after_0805_with_existing_trace_does_not_trigger(monkeypatch):
    init_db()
    _reset_today_trace()
    job_ledger.record_job("daily_health", "success", "이미 정상 실행됨")
    triggered = []
    monkeypatch.setattr(scheduler, "job_daily_health", lambda: triggered.append(True))

    late = datetime.now(_KST).replace(hour=9, minute=0)
    scheduler._recover_missed_daily_health(now=late)

    assert triggered == []
