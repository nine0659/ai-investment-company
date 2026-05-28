"""
scheduler.py
APScheduler 기반 자동 스케줄 실행

실행:
  python scheduler.py

스케줄 (평일만):
  08:20 → 장전 브리핑
  10:00 → 장중 1차
  13:00 → 장중 2차
  15:50 → 장마감 복기
"""
import logging
import os
import signal
import sys
from datetime import datetime

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from rich.console import Console
from rich.logging import RichHandler

from config.settings import (
    TIMEZONE_STR,
    SCHEDULE_PRE_MARKET, SCHEDULE_INTRA_1, SCHEDULE_INTRA_2, SCHEDULE_CLOSE,
    RUN_TYPE_PRE, RUN_TYPE_INTRA1, RUN_TYPE_INTRA2, RUN_TYPE_CLOSE,
    validate_env,
)
from clients.telegram_client import send_error_alert

_LOG_DIR = os.path.join(os.path.dirname(__file__), "data", "logs")
os.makedirs(_LOG_DIR, exist_ok=True)
_LOG_FILE = os.path.join(_LOG_DIR, "scheduler.log")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        RichHandler(show_time=False),
        logging.FileHandler(_LOG_FILE, encoding="utf-8"),
    ],
)
# 민감정보 로그 차단
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.INFO)

logger = logging.getLogger(__name__)
console = Console()

scheduler = BlockingScheduler(timezone=TIMEZONE_STR)


def _run_safe(run_type: str):
    """에러 처리 포함 파이프라인 실행"""
    from graph.investment_graph import run_pipeline
    try:
        logger.info("스케줄 실행 시작: %s", run_type)
        run_pipeline(run_type)
        logger.info("스케줄 실행 완료: %s", run_type)
    except Exception as e:
        logger.error("스케줄 실행 실패 (%s): %s", run_type, e)
        try:
            send_error_alert(f"스케줄 실행 실패 ({run_type}): {str(e)[:200]}")
        except Exception:
            pass


def job_pre_market():
    _run_safe(RUN_TYPE_PRE)

def job_intra1():
    _run_safe(RUN_TYPE_INTRA1)

def job_intra2():
    _run_safe(RUN_TYPE_INTRA2)

def job_close():
    _run_safe(RUN_TYPE_CLOSE)

def job_monitor():
    from agents.realtime_monitor_agent import run as monitor_run
    try:
        monitor_run()
    except Exception as e:
        logger.error("실시간 모니터 실패: %s", e)


def job_emergency():
    """5분마다 실행 — KOSPI 급락·VIX 급등·환율 급등·지정학 뉴스 감지"""
    from agents.emergency_monitor_agent import run as emergency_run
    try:
        emergency_run()
    except Exception as e:
        logger.error("긴급 모니터 실패: %s", e)


def _parse_time(time_str: str) -> tuple[int, int]:
    h, m = time_str.split(":")
    return int(h), int(m)


def setup_jobs():
    """평일(월~금) 스케줄 등록"""
    for run_type, func, time_str in [
        (RUN_TYPE_PRE,    job_pre_market, SCHEDULE_PRE_MARKET),
        (RUN_TYPE_INTRA1, job_intra1,     SCHEDULE_INTRA_1),
        (RUN_TYPE_INTRA2, job_intra2,     SCHEDULE_INTRA_2),
        (RUN_TYPE_CLOSE,  job_close,      SCHEDULE_CLOSE),
    ]:
        h, m = _parse_time(time_str)
        scheduler.add_job(
            func,
            CronTrigger(day_of_week="mon-fri", hour=h, minute=m, timezone=TIMEZONE_STR),
            id=run_type,
            name=f"[{time_str}] {run_type}",
            misfire_grace_time=300,  # 5분 내 재실행 허용
            coalesce=True,
        )
        console.print(f"  [cyan]⏰ {time_str}[/cyan] {run_type}")

    # 실시간 모니터: 장중 9:00-15:30, 15분마다
    scheduler.add_job(
        job_monitor,
        CronTrigger(
            day_of_week="mon-fri",
            hour="9-15",
            minute="*/15",
            timezone=TIMEZONE_STR,
        ),
        id="realtime_monitor",
        name="[15분] 실시간 진입신호·손절·목표가 모니터",
        misfire_grace_time=120,
        coalesce=True,
    )
    console.print("  [cyan]⏰ 09:00-15:30 매 15분[/cyan] 실시간 모니터")

    # 긴급 모니터: 장중 9:00-15:30, 5분마다
    # KOSPI 급락 / VIX 급등 / 원달러 급등 / 지정학 뉴스 / 보유 종목 급락 체크
    scheduler.add_job(
        job_emergency,
        CronTrigger(
            day_of_week="mon-fri",
            hour="9-15",
            minute="*/5",
            timezone=TIMEZONE_STR,
        ),
        id="emergency_monitor",
        name="[5분] 긴급알림 — KOSPI급락·VIX·환율·지정학·보유종목",
        misfire_grace_time=60,
        coalesce=True,
    )
    console.print("  [cyan]⏰ 09:00-15:30 매 5분[/cyan] 긴급 알림 모니터")


def shutdown(signum, frame):
    console.print("\n[yellow]스케줄러 종료 중...[/yellow]")
    scheduler.shutdown(wait=False)
    sys.exit(0)


def main():
    # 환경변수 검증
    missing = validate_env()
    if missing:
        console.print(f"[red]❌ 누락된 환경변수: {', '.join(missing)}[/red]")
        sys.exit(1)

    # DB 초기화
    try:
        from db.database import init_db
        init_db()
    except Exception as e:
        logger.warning("DB 초기화 경고: %s", e)

    console.print("[bold cyan]📅 AI Investment Research Scheduler 시작[/bold cyan]")
    console.print(f"[cyan]타임존: {TIMEZONE_STR}[/cyan]")
    console.print("\n등록된 스케줄 (평일 Mon-Fri):")

    setup_jobs()

    # Graceful shutdown
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    console.print("\n[green]✅ 스케줄러 실행 중... (Ctrl+C로 종료)[/green]")
    scheduler.start()


if __name__ == "__main__":
    main()
