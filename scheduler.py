"""
scheduler.py
APScheduler 기반 자동 스케줄 실행

실행:
  python scheduler.py

정기 브리핑 스케줄:
  월·수·금 08:20 → 장전 브리핑 (주 3회 — 한 주의 시작·중간·끝 매크로·보유 근거 점검)
  금     16:30 → 주간 마감 브리핑 (주 1회 — 한 주 정리 + 다음 주 보유 근거 점검)

장기 투자자 관점에서 매일 브리핑은 정보 과잉이다.
투자 근거 훼손·이상 신호는 15분 모니터(조건부)가 실시간 처리한다.
화·목 장전과 월~목 장마감은 자동 발송하지 않는다.

* GLOBAL(새벽 시황)은 PRE로 통합됨 (2026-06-22).
* INTRA1·INTRA2(장중)는 자동 스케줄에서 제외됨 (2026-06-23).
  python main.py --type intra1/intra2 로 수동 실행 가능.
* 실시간·긴급 모니터는 단일 15분 주기로 통합됨 (2026-06-23).
* PRE 주 5회→3회(월·수·금), CLOSE 주 5회→1회(금) 로 축소 (2026-06-26).
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
    SCHEDULE_PRE_MARKET, SCHEDULE_CLOSE,
    RUN_TYPE_PRE, RUN_TYPE_CLOSE,
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
    RUN_TYPE_CLOSE:  (16 * 60,      18 * 60),        # 16:00 ~ 18:00  (수급 집계 완료 후)
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

def job_close():
    _run_safe(RUN_TYPE_CLOSE)

def job_market_monitor():
    """15분마다 실행 — 실시간 모니터(워치리스트·포지션·추적종목) + 긴급모니터
    (KOSPI 급락·VIX 급등·환율 급등·지정학 뉴스)를 한 번에 체크.

    과거엔 15분/5분 두 잡이 따로 돌며 보유종목 급락·워치리스트 변동을
    중복 체크해 같은 사건에 메시지가 2건씩 나갔다 — 단일 잡으로 통합 (2026-06-23).
    """
    if not is_krx_trading_day():
        return
    if not _MONITOR_LOCK.acquire(blocking=False):
        logger.debug("⏳ [모니터 잠금] 모니터 실행 중 — 이번 회차 스킵")
        return
    try:
        from agents.realtime_monitor_agent import run as realtime_run
        try:
            realtime_run()
        except Exception as e:
            logger.error("실시간 모니터 실패: %s", e)

        from agents.emergency_monitor_agent import run as emergency_run
        try:
            emergency_run()
        except Exception as e:
            logger.error("긴급 모니터 실패: %s", e)
    finally:
        _MONITOR_LOCK.release()


def job_monthly_thesis():
    """매월 첫째 주 월요일 19:00 — 월간 투자관 수립 (모든 일일 판단의 헌법).
    APScheduler는 첫째 월요일을 직접 표현하기 어려우므로 내부에서 날짜 확인.
    """
    today = datetime.now(_KST)
    if not (1 <= today.day <= 7 and today.weekday() == 0):
        logger.debug("월간 투자관 스킵 — 첫째 주 월요일 아님 (%s)", today.strftime("%Y-%m-%d"))
        return
    from agents.thesis_agent import run_thesis
    try:
        logger.info("월간 투자관 수립 시작")
        run_thesis()
        logger.info("월간 투자관 수립 완료")
    except Exception as e:
        logger.error("월간 투자관 실패: %s", e)
        try:
            send_error_alert(f"월간 투자관 실패: {str(e)[:200]}")
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


def job_weekly_discovery():
    """매주 화요일 19:00 — 탑다운 종목 발굴 (시장 흐름→주도 산업→종목→워치리스트 등록)."""
    from agents.discovery_agent import run_discovery
    try:
        logger.info("종목 발굴 시작")
        run_discovery()
        logger.info("종목 발굴 완료")
    except Exception as e:
        logger.error("종목 발굴 실패: %s", e)
        try:
            send_error_alert(f"종목 발굴 실패: {str(e)[:200]}")
        except Exception:
            pass


def job_weekly_attribution():
    """매주 일요일 19:00 — 주간 성과 귀인 분석 (매크로·섹터·종목·타이밍·투자관부합)."""
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


def job_weekly_stats():
    """매주 일요일 20:00 — 주간 적중률 통계 (샤프비율·MDD·섹터 성과)."""
    from services.stats_service import send_weekly_report
    try:
        logger.info("주간 적중률 통계 시작")
        send_weekly_report()
        logger.info("주간 적중률 통계 완료")
    except Exception as e:
        logger.error("주간 통계 실패: %s", e)
        try:
            send_error_alert(f"주간 통계 실패: {str(e)[:200]}")
        except Exception:
            pass


def job_weekly_midterm():
    """매주 일요일 20:05 — 중기 투자 분석 (1~6개월 관점).

    GitHub Actions에도 같은 스케줄이 있으나 GH cron은 상시 수십 분 지연된다.
    Render가 정시에 먼저 실행하고, 늦게 도착한 GH 실행은 에이전트 내부의
    claim_report_slot 가드가 중복 발송을 차단한다.
    """
    from agents.midterm_agent import run_analysis
    try:
        logger.info("주간 중기분석 시작")
        run_analysis()
        logger.info("주간 중기분석 완료")
    except Exception as e:
        logger.error("주간 중기분석 실패: %s", e)
        try:
            send_error_alert(f"주간 중기분석 실패: {str(e)[:200]}")
        except Exception:
            pass


def job_weekly_us_invest():
    """매주 일요일 20:40 — 미국 주식 주간 추천."""
    from agents.us_invest_agent import run as us_run
    try:
        logger.info("미국 주식 추천 시작")
        us_run()
        logger.info("미국 주식 추천 완료")
    except Exception as e:
        logger.error("미국 주식 추천 실패: %s", e)
        try:
            send_error_alert(f"미국 주식 추천 실패: {str(e)[:200]}")
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


_kis_client_cache: object | None = None


def _get_kis() -> object | None:
    """KISClient 캐시 반환 — 장마감 후 잡들이 토큰을 공유해 재발급 최소화."""
    global _kis_client_cache
    try:
        from clients.kis_client import KISClient
        if _kis_client_cache is None:
            _kis_client_cache = KISClient()
        return _kis_client_cache
    except Exception as e:
        logger.warning("KIS 클라이언트 생성 실패: %s", e)
        return None


def job_daily_nav():
    """평일 16:10 — 장마감 후 포트폴리오 NAV 자동 기록 + 드로다운 방어 체크."""
    if not is_krx_trading_day():
        return
    try:
        from services.nav_service import record_nav
        nav = record_nav(_get_kis())
        if nav:
            logger.info("NAV 기록 완료: 총자산 %s원", f"{nav.get('total_value', 0):,}")
    except Exception as e:
        logger.warning("NAV 기록 실패 (무시): %s", e)

    # P4-2: 드로다운 방어 체크
    try:
        from services.nav_service import check_drawdown_defense
        from services.auto_execute_service import execute_drawdown_defense
        dd = check_drawdown_defense()
        action = dd.get("action", "none")
        if action in ("half", "all"):
            logger.warning("드로다운 방어 발동: %s — %s", action, dd.get("message", ""))
            execute_drawdown_defense(action, _get_kis())
        else:
            logger.info("드로다운 정상: %s", dd.get("message", ""))
    except Exception as e:
        logger.warning("드로다운 방어 체크 실패 (무시): %s", e)


def job_daily_tracker():
    """평일 16:20 — AI 추천 종목 일별 성과 추적 + 시장 예측 검증."""
    if not is_krx_trading_day():
        return
    try:
        from services.recommendation_tracker_service import run_daily_tracker
        from services.market_prediction_service import run_daily_verify
        stats    = run_daily_tracker(_get_kis())
        verified = run_daily_verify()
        logger.info(
            "성과 추적 완료: 처리 %d건 (목표 %d/손절 %d/만료 %d) | 예측 검증 %d건",
            stats.get("processed", 0), stats.get("target_hit", 0),
            stats.get("stop_hit", 0), stats.get("expired", 0), verified,
        )
    except Exception as e:
        logger.warning("성과 추적 실패 (무시): %s", e)


def _parse_time(time_str: str) -> tuple[int, int]:
    h, m = time_str.split(":")
    return int(h), int(m)


def setup_jobs():
    """정기 브리핑 스케줄 등록.

    PRE:   월·수·금 08:20 (주 3회)
    CLOSE: 금      16:30 (주 1회 — 주간 마감 랩업)

    장기 투자자에게 매일 브리핑은 정보 과잉이다. 화·목 장전과 월~목 장마감은
    자동 발송하지 않는다. 이상 신호는 15분 모니터(조건부)가 처리한다.
    """
    pre_h, pre_m = _parse_time(SCHEDULE_PRE_MARKET)
    scheduler.add_job(
        job_pre_market,
        CronTrigger(day_of_week="mon,wed,fri", hour=pre_h, minute=pre_m, timezone=TIMEZONE_STR),
        id=RUN_TYPE_PRE,
        name=f"[{SCHEDULE_PRE_MARKET} 월·수·금] {RUN_TYPE_PRE}",
        misfire_grace_time=300,
        coalesce=True,
    )
    console.print(f"  [cyan]⏰ {SCHEDULE_PRE_MARKET} 월·수·금[/cyan] {RUN_TYPE_PRE}")

    close_h, close_m = _parse_time(SCHEDULE_CLOSE)
    scheduler.add_job(
        job_close,
        CronTrigger(day_of_week="fri", hour=close_h, minute=close_m, timezone=TIMEZONE_STR),
        id=RUN_TYPE_CLOSE,
        name=f"[{SCHEDULE_CLOSE} 금] {RUN_TYPE_CLOSE}",
        misfire_grace_time=300,
        coalesce=True,
    )
    console.print(f"  [cyan]⏰ {SCHEDULE_CLOSE} 금[/cyan] {RUN_TYPE_CLOSE}")

    # 시장 모니터(실시간+긴급 통합): 장중 9:00-15:30, 15분마다
    # 워치리스트·포지션·추적종목 + KOSPI급락·VIX·환율·지정학 뉴스를 한 번에 체크
    scheduler.add_job(
        job_market_monitor,
        CronTrigger(
            day_of_week="mon-fri",
            hour="9-15",
            minute="*/15",
            timezone=TIMEZONE_STR,
        ),
        id="market_monitor",
        name="[15분] 시장 모니터 (실시간+긴급 통합 — 수급·지수·포지션·섹터 이상 감지)",
        misfire_grace_time=120,
        coalesce=True,
    )
    console.print("  [cyan]⏰ 09:00-15:30 매 15분[/cyan] 시장 모니터 (실시간+긴급 통합)")

    # 월간 투자관: 매월 첫째 주 월요일 19:00 (job 내부에서 날짜 재확인)
    scheduler.add_job(
        job_monthly_thesis,
        CronTrigger(day_of_week="mon", hour=19, minute=0, timezone=TIMEZONE_STR),
        id="monthly_thesis",
        name="[월1월 19:00] 월간 투자관 수립",
        misfire_grace_time=3600,
        coalesce=True,
    )
    console.print("  [cyan]⏰ 매월 첫째 월요일 19:00[/cyan] 월간 투자관 수립")

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
        job_weekly_discovery,
        CronTrigger(day_of_week="tue", hour=19, minute=0, timezone=TIMEZONE_STR),
        id="weekly_discovery",
        name="[화 19:00] 종목 발굴 (탑다운 — 시장→산업→종목→워치리스트)",
        misfire_grace_time=600,
        coalesce=True,
    )
    console.print("  [cyan]⏰ 화 19:00[/cyan] 종목 발굴 (탑다운)")

    scheduler.add_job(
        job_weekly_attribution,
        CronTrigger(day_of_week="sun", hour=19, minute=0, timezone=TIMEZONE_STR),
        id="weekly_attribution",
        name="[일 19:00] 주간 성과 귀인 분석",
        misfire_grace_time=1800,
        coalesce=True,
    )
    console.print("  [cyan]⏰ 매주 일요일 19:00[/cyan] 주간 성과 귀인 분석")

    # 주간 적중률 통계: 매주 일요일 20:00 (귀인분석 후)
    scheduler.add_job(
        job_weekly_stats,
        CronTrigger(day_of_week="sun", hour=20, minute=0, timezone=TIMEZONE_STR),
        id="weekly_stats",
        name="[일 20:00] 주간 적중률 통계 — 샤프비율·MDD·섹터 성과",
        misfire_grace_time=1800,
        coalesce=True,
    )
    console.print("  [cyan]⏰ 매주 일요일 20:00[/cyan] 주간 적중률 통계")

    # 주간 중기 분석: 매주 일요일 20:05 (적중률 통계 직후)
    scheduler.add_job(
        job_weekly_midterm,
        CronTrigger(day_of_week="sun", hour=20, minute=5, timezone=TIMEZONE_STR),
        id="weekly_midterm",
        name="[일 20:05] 중기 투자 분석 (1~6개월)",
        misfire_grace_time=1800,
        coalesce=True,
    )
    console.print("  [cyan]⏰ 매주 일요일 20:05[/cyan] 중기 투자 분석")

    # 미국 주식 주간 추천: 매주 일요일 20:40
    scheduler.add_job(
        job_weekly_us_invest,
        CronTrigger(day_of_week="sun", hour=20, minute=40, timezone=TIMEZONE_STR),
        id="weekly_us_invest",
        name="[일 20:40] 미국 주식 주간 추천",
        misfire_grace_time=1800,
        coalesce=True,
    )
    console.print("  [cyan]⏰ 매주 일요일 20:40[/cyan] 미국 주식 주간 추천")

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

    # NAV 기록: 평일 16:10 (장마감 후 10분)
    scheduler.add_job(
        job_daily_nav,
        CronTrigger(day_of_week="mon-fri", hour=16, minute=10, timezone=TIMEZONE_STR),
        id="daily_nav",
        name="[16:10] 포트폴리오 NAV 자동 기록",
        misfire_grace_time=600,
        coalesce=True,
    )
    console.print("  [cyan]⏰ 평일 16:10[/cyan] 포트폴리오 NAV 자동 기록")

    # 성과 추적: 평일 16:20 (NAV 기록 후)
    scheduler.add_job(
        job_daily_tracker,
        CronTrigger(day_of_week="mon-fri", hour=16, minute=20, timezone=TIMEZONE_STR),
        id="daily_tracker",
        name="[16:20] AI 추천 종목 성과 추적 + 시장 예측 검증",
        misfire_grace_time=600,
        coalesce=True,
    )
    console.print("  [cyan]⏰ 평일 16:20[/cyan] AI 추천 성과 추적 + 예측 검증")


def shutdown(signum, frame):
    console.print("\n[yellow]스케줄러 종료 중...[/yellow]")
    scheduler.shutdown(wait=False)
    sys.exit(0)


def _recover_missed_briefings():
    """스케줄러 재시작 후 당일 미발송 브리핑 복구.

    정상 시간 윈도우보다 넓은 복구 윈도우를 사용하여
    컨테이너 재시작으로 인한 누락을 자동으로 보정.
    """
    if not is_krx_trading_day():
        return

    now = datetime.now(_KST)
    cur_min = now.hour * 60 + now.minute

    weekday = now.weekday()  # 0=Mon … 4=Fri

    # (run_type, 예약분, 복구마감분, 허용요일집합)
    _RECOVERY: list[tuple[str, int, int, set[int]]] = [
        (RUN_TYPE_PRE,   8 * 60 + 20, 11 * 60, {0, 2, 4}),  # 월·수·금 08:20 → 마감 11:00
        (RUN_TYPE_CLOSE, 16 * 60 + 30, 20 * 60, {4}),        # 금      16:30 → 마감 20:00
    ]

    for run_type, sched_min, deadline_min, allowed_days in _RECOVERY:
        if weekday not in allowed_days:
            continue  # 오늘은 이 run_type 발송일 아님
        if cur_min < sched_min:
            continue  # 아직 예약 시간 전
        if cur_min > deadline_min:
            continue  # 복구 마감 초과
        if _already_sent_today(run_type):
            continue  # 이미 발송됨

        logger.warning(
            "🔄 [복구] %s 브리핑 누락 감지 — 지금 바로 실행 (예약 %02d:%02d, 현재 %02d:%02d)",
            run_type, sched_min // 60, sched_min % 60, now.hour, now.minute,
        )
        threading.Thread(
            target=_run_safe, args=(run_type,), name=f"recover-{run_type}", daemon=True
        ).start()


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

    # 재시작으로 인한 당일 미발송 브리핑 복구
    try:
        _recover_missed_briefings()
    except Exception as e:
        logger.warning("복구 실행 오류 (무시): %s", e)

    # 텔레그램 봇을 백그라운드 스레드로 함께 시작
    try:
        from clients.telegram_bot import run_bot
        bot_thread = threading.Thread(target=run_bot, name="telegram-bot", daemon=True)
        bot_thread.start()
        console.print("[green]✅ 텔레그램 봇 시작 (백그라운드)[/green]")
    except Exception as e:
        logger.warning("텔레그램 봇 시작 실패 (스케줄러는 계속 실행): %s", e)

    # Graceful shutdown
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    console.print("\n[green]✅ 스케줄러 실행 중... (Ctrl+C로 종료)[/green]")
    scheduler.start()


if __name__ == "__main__":
    main()
