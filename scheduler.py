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
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

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
from utils.market_calendar import is_krx_trading_day, get_holiday_name

_KST = ZoneInfo(TIMEZONE_STR)

# 브리핑 파이프라인 동시 실행 방지 (비싼 데이터 수집·LLM 호출 중복 방지)
_PIPELINE_LOCK = threading.Lock()
# 모니터 잡 동시 실행 방지
_MONITOR_LOCK = threading.Lock()

# 각 run_type별 허용 실행 시간 윈도우 (KST 기준, 시작~종료 분)
# 스케줄 시간 ±30분 이내에만 실제 브리핑 발송
_TIME_WINDOWS: dict[str, tuple[int, int]] = {
    RUN_TYPE_PRE:    (7 * 60 + 50,  9 * 60 + 30),   # 07:50 ~ 09:30
    RUN_TYPE_INTRA1: (9 * 60 + 30, 11 * 60),         # 09:30 ~ 11:00
    RUN_TYPE_INTRA2: (12 * 60 + 30, 14 * 60),        # 12:30 ~ 14:00
    RUN_TYPE_CLOSE:  (15 * 60 + 20, 17 * 60 + 30),  # 15:20 ~ 17:30
}

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


def _in_time_window(run_type: str) -> bool:
    """현재 KST 시각이 run_type의 허용 시간 윈도우 안에 있는지 확인."""
    window = _TIME_WINDOWS.get(run_type)
    if not window:
        return True  # 윈도우 미정의 run_type은 허용
    now = datetime.now(_KST)
    cur_min = now.hour * 60 + now.minute
    return window[0] <= cur_min <= window[1]


def _already_sent_today(run_type: str) -> bool:
    """오늘 해당 run_type 브리핑이 이미 성공적으로 발송됐는지 확인."""
    try:
        from services.report_service import already_ran_today
        from datetime import datetime
        today = datetime.now(_KST).strftime("%Y-%m-%d")
        return already_ran_today(today, run_type)
    except Exception:
        return False


def _run_safe(run_type: str):
    """에러 처리 + 중복 방지 + 시간 윈도우 검증 포함 파이프라인 실행"""
    from graph.investment_graph import run_pipeline

    # ① 공휴일·비거래일 체크 — KRX 휴장일에는 브리핑 전혀 발송하지 않음
    if not is_krx_trading_day():
        holiday = get_holiday_name()
        label = f" ({holiday})" if holiday else ""
        logger.info("📅 [공휴일 스킵] 오늘은 KRX 비거래일%s — %s 브리핑 발송 차단", label, run_type)
        return

    # ② 시간 윈도우 검증 — 예상 시간대가 아니면 경고만 하고 중단
    if not _in_time_window(run_type):
        now_str = datetime.now(_KST).strftime("%H:%M KST")
        logger.warning(
            "⏰ [시간 윈도우 초과] %s 는 %s 에 실행할 수 없습니다 — 발송 차단",
            run_type, now_str,
        )
        return

    # ② 당일 중복 방지 — 같은 run_type이 오늘 이미 완료됐으면 스킵
    if _already_sent_today(run_type):
        logger.info("⏭️ [중복 스킵] %s 브리핑이 오늘 이미 발송됐습니다", run_type)
        return

    if not _PIPELINE_LOCK.acquire(blocking=False):
        logger.warning("⏳ [파이프라인 잠금] 다른 브리핑이 실행 중 — %s 스킵", run_type)
        return

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
    finally:
        _PIPELINE_LOCK.release()


def job_pre_market():
    _run_safe(RUN_TYPE_PRE)

def job_intra1():
    _run_safe(RUN_TYPE_INTRA1)

def job_intra2():
    _run_safe(RUN_TYPE_INTRA2)

def job_close():
    _run_safe(RUN_TYPE_CLOSE)

def job_monitor():
    if not is_krx_trading_day():
        return
    if not _MONITOR_LOCK.acquire(blocking=False):
        logger.debug("⏳ [모니터 잠금] 모니터 실행 중 — 이번 실시간 모니터 스킵")
        return
    from agents.realtime_monitor_agent import run as monitor_run
    try:
        monitor_run()
    except Exception as e:
        logger.error("실시간 모니터 실패: %s", e)
    finally:
        _MONITOR_LOCK.release()


def job_emergency():
    """5분마다 실행 — KOSPI 급락·VIX 급등·환율 급등·지정학 뉴스 감지"""
    if not is_krx_trading_day():
        return
    if not _MONITOR_LOCK.acquire(blocking=False):
        logger.debug("⏳ [모니터 잠금] 모니터 실행 중 — 이번 긴급 모니터 스킵")
        return
    from agents.emergency_monitor_agent import run as emergency_run
    try:
        emergency_run()
    except Exception as e:
        logger.error("긴급 모니터 실패: %s", e)
    finally:
        _MONITOR_LOCK.release()


def job_monthly_thesis():
    """매월 첫째 주 월요일 19:00 — 월간 투자 테제 수립 (모든 일일 판단의 헌법).
    APScheduler는 첫째 월요일을 직접 표현하기 어려우므로 내부에서 날짜 확인.
    """
    today = datetime.now(_KST)
    if not (1 <= today.day <= 7 and today.weekday() == 0):
        logger.debug("월간 테제 스킵 — 첫째 주 월요일 아님 (%s)", today.strftime("%Y-%m-%d"))
        return
    from agents.thesis_agent import run_thesis
    try:
        logger.info("월간 투자 테제 수립 시작")
        run_thesis()
        logger.info("월간 투자 테제 수립 완료")
    except Exception as e:
        logger.error("월간 투자 테제 실패: %s", e)
        try:
            send_error_alert(f"월간 투자 테제 실패: {str(e)[:200]}")
        except Exception:
            pass


def job_weekly_strategy():
    """매주 수요일 20:00 — 주간 종합 투자전략 (단기·중기·장기 통합)."""
    from agents.strategy_agent import run_strategy
    try:
        logger.info("주간 전략 시작")
        run_strategy()
        logger.info("주간 전략 완료")
    except Exception as e:
        logger.error("주간 전략 실패: %s", e)
        try:
            send_error_alert(f"주간 전략 실패: {str(e)[:200]}")
        except Exception:
            pass


def job_weekly_attribution():
    """매주 일요일 19:00 — 주간 성과 귀인 분석 (매크로·섹터·종목·타이밍·테제정합)."""
    from agents.attribution_agent import run_attribution
    try:
        logger.info("주간 귀인 분석 시작")
        run_attribution()
        logger.info("주간 귀인 분석 완료")
    except Exception as e:
        logger.error("주간 귀인 분석 실패: %s", e)
        try:
            send_error_alert(f"주간 귀인 분석 실패: {str(e)[:200]}")
        except Exception:
            pass


def job_monthly_longterm():
    """매월 첫째 주 일요일 20:30 — 장기 가치투자 분석 (1년+ 관점).
    APScheduler CronTrigger는 첫째 주 일요일을 직접 표현하기 어려워
    job 내부에서 날짜를 확인한다.
    """
    today = datetime.now(_KST)
    # 첫째 주 일요일 = 당월 1~7일 중 일요일
    if not (1 <= today.day <= 7 and today.weekday() == 6):
        logger.debug("월간 장기분석 스킵 — 첫째 주 일요일 아님 (%s)", today.strftime("%Y-%m-%d"))
        return
    from agents.longterm_agent import run as longterm_run
    try:
        logger.info("월간 장기분석 시작")
        longterm_run({})
        logger.info("월간 장기분석 완료")
    except Exception as e:
        logger.error("월간 장기분석 실패: %s", e)
        try:
            send_error_alert(f"월간 장기분석 실패: {str(e)[:200]}")
        except Exception:
            pass


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

    # 월간 투자 테제: 매월 첫째 주 월요일 19:00 (job 내부에서 날짜 재확인)
    scheduler.add_job(
        job_monthly_thesis,
        CronTrigger(day_of_week="mon", hour=19, minute=0, timezone=TIMEZONE_STR),
        id="monthly_thesis",
        name="[월1월 19:00] 월간 투자 테제 수립",
        misfire_grace_time=3600,
        coalesce=True,
    )
    console.print("  [cyan]⏰ 매월 첫째 월요일 19:00[/cyan] 월간 투자 테제 수립")

    # 주간 종합 투자전략: 매주 수요일 20:00
    scheduler.add_job(
        job_weekly_strategy,
        CronTrigger(day_of_week="wed", hour=20, minute=0, timezone=TIMEZONE_STR),
        id="weekly_strategy",
        name="[수 20:00] 주간 종합 투자전략 — 단기·중기·장기 통합",
        misfire_grace_time=1800,  # 30분 내 재실행 허용
        coalesce=True,
    )
    console.print("  [cyan]⏰ 매주 수요일 20:00[/cyan] 주간 종합 투자전략")

    # 주간 귀인 분석: 매주 일요일 19:00
    scheduler.add_job(
        job_weekly_attribution,
        CronTrigger(day_of_week="sun", hour=19, minute=0, timezone=TIMEZONE_STR),
        id="weekly_attribution",
        name="[일 19:00] 주간 성과 귀인 분석",
        misfire_grace_time=1800,
        coalesce=True,
    )
    console.print("  [cyan]⏰ 매주 일요일 19:00[/cyan] 주간 성과 귀인 분석")

    # 월간 장기 분석: 매월 첫째 주 일요일 20:30 (job 내부에서 날짜 재확인)
    scheduler.add_job(
        job_monthly_longterm,
        CronTrigger(day_of_week="sun", hour=20, minute=30, timezone=TIMEZONE_STR),
        id="monthly_longterm",
        name="[월1일요 20:30] 월간 장기 가치투자 분석",
        misfire_grace_time=3600,  # 1시간 내 재실행 허용
        coalesce=True,
    )
    console.print("  [cyan]⏰ 매월 첫째 일요일 20:30[/cyan] 월간 장기 가치투자 분석")


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
