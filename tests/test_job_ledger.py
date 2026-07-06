"""잡 실행 대장 — 기대 목록과 실행 흔적 소스의 정합성 검증.

기대 목록에 있는 잡이 흔적을 남길 방법(job_runs 직접 기록 또는
report_claims 별칭)이 없으면 헬스체크가 매주 오탐 경보를 낸다.
"""
from services import job_ledger

# job_runs에 직접 기록하는 잡들 (scheduler.py에서 record_job 호출)
_DIRECT_RECORDERS = {"daily_nav", "daily_tracker", "weekly_picks", "daily_health"}


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
