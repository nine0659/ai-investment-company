"""잡 실행 대장 — 기대 목록과 실행 흔적 소스의 정합성 검증.

기대 목록에 있는 잡이 흔적을 남길 방법(job_runs 직접 기록 또는
report_claims 별칭)이 없으면 헬스체크가 매주 오탐 경보를 낸다.
"""
from services import job_ledger

# job_runs에 직접 기록하는 잡들 (scheduler.py에서 record_job 호출)
_DIRECT_RECORDERS = {"daily_nav", "daily_tracker", "weekly_picks", "daily_health", "rebound_screener", "discovery_weekly", "backtest_snapshot"}


def test_every_expected_job_has_a_trace_source():
    for weekday, jobs in job_ledger._EXPECTED_BY_WEEKDAY.items():
        for job in jobs:
            has_claim = job in job_ledger._CLAIM_ALIAS
            has_direct = job in _DIRECT_RECORDERS
            assert has_claim or has_direct, (
                f"{job}(요일 {weekday}): 실행 흔적을 남길 소스가 없음 — "
                f"_CLAIM_ALIAS에 추가하거나 record_job을 호출해야 함"
            )


def test_paused_jobs_not_expected():
    # 2026-07-06 축소로 중단된 잡들이 기대 목록에 남아있으면 매주 오탐 경보
    paused = {"weekly_attribution", "weekly_stats", "weekly_discovery", "weekly_strategy"}
    for jobs in job_ledger._EXPECTED_BY_WEEKDAY.values():
        assert not (paused & set(jobs)), f"중단된 잡이 기대 목록에 남음: {paused & set(jobs)}"


def test_saturday_expects_nothing():
    assert job_ledger._EXPECTED_BY_WEEKDAY[5] == []


def test_empty_status_is_flagged_by_health_check(monkeypatch):
    """예외 없이 산출 0건인 잡('조용한 스킵')은 흔적은 있어도 헬스체크가 경보해야 한다.

    2026-09-21(NAV 5주 공백)·09-29(tracker) 사고: job_runs엔 success만 찍혀 못 잡았다.
    """
    from datetime import datetime, timedelta
    from sqlalchemy import text
    from db.database import get_conn, init_db
    init_db()

    yesterday = datetime.now(job_ledger._KST) - timedelta(days=1)
    date_str = yesterday.strftime("%Y-%m-%d")
    monkeypatch.setitem(job_ledger._EXPECTED_BY_WEEKDAY, yesterday.weekday(), ["empty_probe_job"])
    with get_conn() as conn:
        conn.execute(
            text("INSERT INTO job_runs (date, job_name, status, detail) "
                 "VALUES (:d, 'empty_probe_job', 'empty', 'NAV 기록 0건')"),
            {"d": date_str},
        )
    problems = job_ledger.get_yesterday_problems()
    assert any("empty_probe_job" in p and "산출 0건" in p for p in problems)
    # 흔적으로는 인정 — '실행 흔적 없음'과 중복 경보하지 않는다
    assert not any("실행 흔적 없음" in p and "empty_probe_job" in p for p in problems)


def test_scheduler_records_empty_when_nav_or_tracker_produce_nothing():
    """daily_nav/daily_tracker가 산출 0건을 success로 기록하면 사각지대가 재발한다."""
    import inspect
    import scheduler
    assert '"daily_nav", "empty"' in inspect.getsource(scheduler.job_daily_nav)
    assert '"daily_tracker", "empty"' in inspect.getsource(scheduler.job_daily_tracker)


def test_has_trace_today_roundtrip():
    """GH 백업 실행기의 중복 방지 가드 — 기록 전 False, 기록 후 True."""
    import uuid
    from db.database import init_db
    init_db()

    name = f"testjob_{uuid.uuid4().hex[:8]}"
    assert job_ledger.has_trace_today(name) is False
    job_ledger.record_job(name, "success")
    assert job_ledger.has_trace_today(name) is True
    # 실패 기록은 흔적으로 치지 않는다 (백업이 대신 돌아야 함)
    fail_name = f"testjob_{uuid.uuid4().hex[:8]}"
    job_ledger.record_job(fail_name, "fail", "의도된 테스트 실패")
    assert job_ledger.has_trace_today(fail_name) is False
