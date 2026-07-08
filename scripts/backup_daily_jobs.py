"""
scripts/backup_daily_jobs.py — 평일 16:10/16:20 잡의 GH Actions 백업 실행기

배경(2026-07-08 사고): Render 컨테이너가 16:10~16:20 사이 재시작되면 APScheduler는
지나간 실행을 기억하지 못해 daily_tracker가 통째로 증발한다. 이를 대비한 백업
nav-tracker.yml은 YAML 문법 오류로 생성일(2026-06-08)부터 한 번도 돈 적이 없었다.

동작: 오늘(KST) job_runs에 실행 흔적이 이미 있으면 스킵, 없으면 scheduler.py의
잡 함수를 그대로 호출한다(비거래일 스킵·record_job 기록 포함 동일 동작).
GH cron 지연(수십 분)은 문제 없다 — 장마감 후 데이터라 시점에 둔감하다.

수동 실행: python scripts/backup_daily_jobs.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def main() -> None:
    from db.database import init_db
    init_db()

    import scheduler  # 잡 함수 재사용 (import만으로는 스케줄러가 기동되지 않음)
    from services.job_ledger import has_trace_today

    jobs = [
        ("daily_nav", scheduler.job_daily_nav),
        ("daily_tracker", scheduler.job_daily_tracker),
    ]
    for job_name, fn in jobs:
        if has_trace_today(job_name):
            print(f"{job_name}: 오늘 실행 흔적 있음 (Render 정상) — 백업 스킵")
            continue
        print(f"{job_name}: 오늘 실행 흔적 없음 — 백업 실행")
        fn()


if __name__ == "__main__":
    main()
