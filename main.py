"""
main.py
수동 실행 진입점

사용법:
  python main.py --type pre          # 장전 브리핑
  python main.py --type intra1       # 장중 1차 (10:00)
  python main.py --type intra2       # 장중 2차 (13:00)
  python main.py --type close        # 장마감 복기
  python main.py --type midterm      # 중기 분석 (1~6개월)
  python main.py --type longterm     # 장기 분석 (1년+)
  python main.py --type dart         # DART 공시 알림 (즉시)
  python main.py --type price-alert  # 가격 알림 (즉시)
  python main.py --type weekly       # 주간 적중률 리포트
  python main.py --type monthly      # 월간 자기학습 분석
  python main.py --type us-invest    # 미국 주식 주간 추천
  python main.py --check             # 환경변수 검증만
  python main.py --init-db           # DB 초기화
"""
import argparse
import logging
import sys
import io
from rich.console import Console
from rich.logging import RichHandler

# Windows cp949 콘솔에서 이모지/한글 깨짐 방지
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# 로깅 설정
logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    handlers=[RichHandler(rich_tracebacks=True)],
)
logger = logging.getLogger(__name__)
console = Console()


def setup_logging(level: str = "INFO"):
    logging.getLogger().setLevel(getattr(logging, level.upper(), logging.INFO))
    # 민감정보 로그 차단
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)


def main():
    parser = argparse.ArgumentParser(description="AI Investment Research Company")
    parser.add_argument(
        "--type",
        choices=["pre", "intra1", "intra2", "close", "midterm", "longterm",
                 "dart", "price-alert", "weekly", "monthly", "us-invest"],
        default="pre",
        help="실행 타입 (기본: pre)",
    )
    parser.add_argument("--check", action="store_true", help="환경변수 검증")
    parser.add_argument("--init-db", action="store_true", help="DB 초기화")
    parser.add_argument("--log-level", default="INFO", help="로그 레벨")
    args = parser.parse_args()

    setup_logging(args.log_level)

    # 환경변수 검증
    from config.settings import validate_env
    missing = validate_env()
    if missing:
        console.print(f"[red]❌ 누락된 환경변수: {', '.join(missing)}[/red]")
        console.print("[yellow].env 파일에 해당 값을 설정하세요.[/yellow]")
        if args.check:
            return
        sys.exit(1)

    if args.check:
        console.print("[green]✅ 환경변수 검증 완료[/green]")
        return

    # DB 초기화
    if args.init_db:
        from services.review_service import init_db
        init_db()
        console.print("[green]✅ DB 초기화 완료[/green]")
        return

    # DB 초기화 (없으면 자동 생성)
    try:
        from services.review_service import init_db
        init_db()
    except Exception as e:
        logger.warning("DB 초기화 경고: %s", e)

    # ── 중기 / 장기 분석 (독립 실행) ──────────────────────────
    if args.type == "midterm":
        console.print("[bold cyan]📊 중기 투자 분석 시작 (1~6개월)[/bold cyan]")
        from agents.midterm_agent import run_analysis
        try:
            run_analysis()
            console.print("[green]✅ 중기 분석 완료[/green]")
        except Exception as e:
            console.print(f"[red]❌ 중기 분석 실패: {e}[/red]")
            sys.exit(1)
        return

    if args.type == "longterm":
        console.print("[bold cyan]🏦 장기 투자 분석 시작 (1년+)[/bold cyan]")
        from agents.longterm_agent import run_analysis
        try:
            run_analysis()
            console.print("[green]✅ 장기 분석 완료[/green]")
        except Exception as e:
            console.print(f"[red]❌ 장기 분석 실패: {e}[/red]")
            sys.exit(1)
        return

    if args.type == "dart":
        console.print("[bold cyan]📢 DART 공시 알림 체크 시작[/bold cyan]")
        from agents.dart_alert_agent import run as dart_run
        try:
            dart_run()
            console.print("[green]✅ DART 알림 완료[/green]")
        except Exception as e:
            console.print(f"[red]❌ DART 알림 실패: {e}[/red]")
            sys.exit(1)
        return

    if args.type == "price-alert":
        console.print("[bold cyan]🔔 가격 알림 체크 시작[/bold cyan]")
        from agents.price_alert_agent import run as price_run
        try:
            price_run()
            console.print("[green]✅ 가격 알림 완료[/green]")
        except Exception as e:
            console.print(f"[red]❌ 가격 알림 실패: {e}[/red]")
            sys.exit(1)
        return

    if args.type == "weekly":
        console.print("[bold cyan]📊 주간 적중률 리포트 생성 시작[/bold cyan]")
        from services.stats_service import send_weekly_report
        try:
            send_weekly_report()
            console.print("[green]✅ 주간 리포트 발송 완료[/green]")
        except Exception as e:
            console.print(f"[red]❌ 주간 리포트 실패: {e}[/red]")
            sys.exit(1)
        return

    if args.type == "monthly":
        console.print("[bold cyan]🧠 월간 자기학습 분석 시작[/bold cyan]")
        from services.learning_service import run_monthly_analysis
        try:
            run_monthly_analysis()
            console.print("[green]✅ 월간 학습 완료[/green]")
        except Exception as e:
            console.print(f"[red]❌ 월간 학습 실패: {e}[/red]")
            sys.exit(1)
        return

    if args.type == "us-invest":
        console.print("[bold cyan]🇺🇸 미국 주식 주간 추천 시작[/bold cyan]")
        from agents.us_invest_agent import run as us_run
        try:
            us_run()
            console.print("[green]✅ 미국 주식 추천 완료[/green]")
        except Exception as e:
            console.print(f"[red]❌ 미국 주식 추천 실패: {e}[/red]")
            sys.exit(1)
        return

    # ── 일간 파이프라인 ────────────────────────────────────────
    from config.settings import RUN_TYPE_PRE, RUN_TYPE_INTRA1, RUN_TYPE_INTRA2, RUN_TYPE_CLOSE
    type_map = {
        "pre":    RUN_TYPE_PRE,
        "intra1": RUN_TYPE_INTRA1,
        "intra2": RUN_TYPE_INTRA2,
        "close":  RUN_TYPE_CLOSE,
    }
    run_type = type_map[args.type]

    console.print(f"[bold cyan]🚀 AI Investment Research Company 시작[/bold cyan]")
    console.print(f"[cyan]실행 타입: {run_type}[/cyan]")

    from graph.investment_graph import run_pipeline
    try:
        final_state = run_pipeline(run_type)

        errors = final_state.get("errors", [])
        if errors:
            console.print(f"[yellow]⚠️ 일부 오류 발생: {len(errors)}건[/yellow]")
            for e in errors:
                console.print(f"  - {e}")

        console.print(f"[green]✅ 완료! 텔레그램으로 리포트가 발송되었습니다.[/green]")

    except Exception as e:
        console.print(f"[red]❌ 파이프라인 실패: {e}[/red]")
        sys.exit(1)


if __name__ == "__main__":
    main()
