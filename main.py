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
  python main.py --type strategy     # 주간 종합 투자전략 (단기/중기/장기 통합)
  python main.py --type thesis       # 월간 투자관 수립 (경기사이클·6-12개월 전망·자산배분)
  python main.py --type attribution  # 주간 성과 귀인 분석 (매크로·섹터·종목·타이밍·투자관부합)
  python main.py --type dart         # DART 공시 알림 (즉시)
  python main.py --type price-alert  # 가격 알림 (즉시)
  python main.py --type weekly       # 주간 적중률 리포트
  python main.py --type trend        # 주간 KOSPI 추세 분석 (일요일 18:00 KST)
  python main.py --type monthly      # 월간 자기학습 분석
  python main.py --type us-invest    # 미국 주식 주간 추천

  # 기업 리서치 (종목코드 또는 회사명)
  python main.py --research 005930           # 삼성전자 종합 투자 분석
  python main.py --research "삼성전자"       # 회사명으로도 검색 가능
  python main.py --research 005930 --telegram  # 분석 후 텔레그램 발송

  # 텔레그램 대화형 봇 (명령 수신)
  python main.py --bot                       # 봇 시작 (/research /price /balance ...)

  # 포트폴리오 관리
  python main.py --portfolio list                              # 보유 종목 조회
  python main.py --portfolio add CODE NAME QTY PRICE          # 종목 추가 (단기)
  python main.py --portfolio add CODE NAME QTY PRICE mid      # 종목 추가 (중기)
  python main.py --portfolio add CODE NAME QTY PRICE long     # 종목 추가 (장기)
  python main.py --portfolio close CODE [EXIT_PRICE]          # 전량 매도
  python main.py --portfolio target CODE TARGET STOP          # 목표가/손절가 설정
  python main.py --portfolio memo CODE "메모 내용"             # 투자근거 기록

  # 워치리스트 관리
  python main.py --watchlist list                             # 관심종목 조회
  python main.py --watchlist add CODE NAME [TARGET_PRICE]     # 관심종목 추가
  python main.py --watchlist remove CODE                      # 관심종목 제거
  python main.py --watchlist check                            # 진입 조건 점검 (텔레그램 발송)

  # 주문 실행 (KIS API 연동)
  python main.py --order buy CODE QTY [PRICE] [timeframe] [memo...]  # 매수
  python main.py --order sell CODE QTY [PRICE] [memo...]             # 매도 (QTY=0 전량)
  python main.py --order pending                                     # 미체결 주문 조회
  python main.py --order cancel ORDER_NO CODE SIDE QTY [PRICE]      # 주문 취소
  python main.py --order history                                     # 최근 주문 이력

  python main.py --check             # 환경변수 검증만
  python main.py --init-db           # DB 초기화
  python main.py --tracker           # AI 추천 성과 추적 + 시장 예측 검증 (수동)
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


def _run_order_cmd(args):
    """주문 실행 명령 처리."""
    from db.database import init_db
    init_db()

    sub = args.order[0] if args.order else "help"

    if sub == "buy":
        if len(args.order) < 3:
            console.print("[red]사용법: --order buy CODE QTY [PRICE] [timeframe] [memo...][/red]")
            return
        code = args.order[1]
        try:
            qty = int(args.order[2])
        except ValueError:
            console.print("[red]수량은 정수여야 합니다.[/red]")
            return

        price = 0
        idx = 3
        if len(args.order) > 3:
            try:
                price = int(args.order[3].replace(",", ""))
                idx = 4
            except ValueError:
                pass

        tf_map = {"short": "short", "mid": "mid", "long": "long",
                  "단기": "short", "중기": "mid", "장기": "long"}
        timeframe = "short"
        if len(args.order) > idx and args.order[idx] in tf_map:
            timeframe = tf_map[args.order[idx]]
            idx += 1

        memo = " ".join(args.order[idx:]) if len(args.order) > idx else ""
        price_label = f"{price:,}원 지정가" if price > 0 else "시장가"
        console.print(f"[cyan]📤 매수 주문: {code} {qty:,}주 @{price_label} [{timeframe}][/cyan]")

        try:
            from services.trading_service import execute_buy, TradingError
            result = execute_buy(code=code, qty=qty, price=price, memo=memo, timeframe=timeframe)
            if result["success"]:
                console.print(
                    f"[green]✅ 매수 완료: {result['name']}({code}) {qty:,}주 "
                    f"| 주문번호: {result['order_no'] or '없음'}[/green]"
                )
            else:
                console.print(f"[red]❌ 매수 실패: {result['message']}[/red]")
        except Exception as e:
            console.print(f"[red]❌ 오류: {e}[/red]")

    elif sub == "sell":
        if len(args.order) < 3:
            console.print("[red]사용법: --order sell CODE QTY [PRICE] [memo...] (QTY=0 전량)[/red]")
            return
        code = args.order[1]
        try:
            qty = int(args.order[2])
        except ValueError:
            console.print("[red]수량은 정수여야 합니다. 전량은 0[/red]")
            return

        price = 0
        idx = 3
        if len(args.order) > 3:
            try:
                price = int(args.order[3].replace(",", ""))
                idx = 4
            except ValueError:
                pass

        memo = " ".join(args.order[idx:]) if len(args.order) > idx else ""
        qty_label = "전량" if qty == 0 else f"{qty:,}주"
        price_label = f"{price:,}원 지정가" if price > 0 else "시장가"
        console.print(f"[cyan]📤 매도 주문: {code} {qty_label} @{price_label}[/cyan]")

        try:
            from services.trading_service import execute_sell, TradingError
            result = execute_sell(code=code, qty=qty, price=price, memo=memo)
            if result["success"]:
                console.print(
                    f"[green]✅ 매도 완료: {result['name']}({code}) {qty_label} "
                    f"| 주문번호: {result['order_no'] or '없음'}[/green]"
                )
            else:
                console.print(f"[red]❌ 매도 실패: {result['message']}[/red]")
        except Exception as e:
            console.print(f"[red]❌ 오류: {e}[/red]")

    elif sub == "pending":
        try:
            from services.trading_service import get_pending_orders
            orders = get_pending_orders()
            if not orders:
                console.print("[yellow]미체결 주문 없음[/yellow]")
                return
            console.print(f"[cyan]📋 미체결 주문 {len(orders)}건:[/cyan]")
            for o in orders:
                side_label = "매수" if o["side"] == "buy" else "매도"
                console.print(
                    f"  [{side_label}] {o['name']}({o['code']}) "
                    f"{o['qty']:,}주 @{o['price']:,}원 | 주문번호: {o['order_no']}"
                )
            console.print("\n[dim]취소: --order cancel ORDER_NO CODE SIDE QTY[/dim]")
        except Exception as e:
            console.print(f"[red]❌ 조회 실패: {e}[/red]")

    elif sub == "cancel":
        if len(args.order) < 5:
            console.print("[red]사용법: --order cancel ORDER_NO CODE SIDE QTY [PRICE][/red]")
            return
        order_no = args.order[1]
        code = args.order[2]
        side = args.order[3].lower()
        try:
            qty = int(args.order[4])
        except ValueError:
            console.print("[red]수량은 정수여야 합니다.[/red]")
            return
        price = int(args.order[5].replace(",", "")) if len(args.order) > 5 else 0

        try:
            from services.trading_service import cancel_order
            result = cancel_order(order_no, code, side, qty, price)
            if result.get("success"):
                console.print(f"[green]✅ 주문 취소 완료: {order_no}[/green]")
            else:
                console.print(f"[red]❌ 취소 실패: {result.get('message', '')}[/red]")
        except Exception as e:
            console.print(f"[red]❌ 오류: {e}[/red]")

    elif sub == "history":
        try:
            from services.trading_service import format_order_history
            text = format_order_history(limit=20)
            console.print(text)
        except Exception as e:
            console.print(f"[red]❌ 이력 조회 실패: {e}[/red]")

    else:
        console.print(
            "[yellow]사용법:[/yellow]\n"
            "  --order buy CODE QTY [PRICE] [timeframe] [memo]\n"
            "  --order sell CODE QTY [PRICE] [memo]\n"
            "  --order pending\n"
            "  --order cancel ORDER_NO CODE SIDE QTY [PRICE]\n"
            "  --order history"
        )


def _run_portfolio_cmd(args):
    """포트폴리오 관리 명령 처리."""
    from services.portfolio_service import (
        add_position, close_position, update_position,
        get_portfolio, calculate_pnl, format_portfolio_for_briefing
    )
    from db.database import init_db
    init_db()

    sub = args.portfolio[0] if args.portfolio else "list"

    if sub == "list":
        try:
            from clients.kis_client import KISClient
            kis = KISClient()
        except Exception:
            kis = None
        text = format_portfolio_for_briefing(kis)
        console.print(text)

    elif sub == "add":
        if len(args.portfolio) < 5:
            console.print("[red]사용법: --portfolio add CODE NAME QTY PRICE [timeframe=short/mid/long][/red]")
            return
        code     = args.portfolio[1]
        name     = args.portfolio[2]
        qty      = int(args.portfolio[3])
        price    = float(args.portfolio[4].replace(",", ""))
        timeframe = args.portfolio[5] if len(args.portfolio) > 5 else "short"
        tf_map   = {"단기": "short", "중기": "mid", "장기": "long",
                    "short": "short", "mid": "mid", "long": "long"}
        timeframe = tf_map.get(timeframe, "short")
        add_position(code, name, qty, price, timeframe=timeframe)
        console.print(f"[green]✅ 포지션 추가: {name}({code}) {qty}주 @{price:,.0f}원 [{timeframe}][/green]")

    elif sub == "close":
        if len(args.portfolio) < 2:
            console.print("[red]사용법: --portfolio close CODE [EXIT_PRICE][/red]")
            return
        code = args.portfolio[1]
        exit_price = float(args.portfolio[2].replace(",", "")) if len(args.portfolio) > 2 else None
        result = close_position(code, exit_price)
        if result:
            console.print(
                f"[green]✅ 매도 완료: {result['name']}({code}) "
                f"{result['sell_qty']}주 @{result['exit_price']:,.0f}원 "
                f"({result['return_pct']:+.2f}%)[/green]"
            )
        else:
            console.print(f"[red]❌ 보유 중인 {code} 없음[/red]")

    elif sub == "target":
        if len(args.portfolio) < 4:
            console.print("[red]사용법: --portfolio target CODE TARGET_PRICE STOP_PRICE[/red]")
            return
        code   = args.portfolio[1]
        target = float(args.portfolio[2].replace(",", ""))
        stop   = float(args.portfolio[3].replace(",", ""))
        update_position(code, target_price=target, stop_price=stop)
        console.print(f"[green]✅ {code} 목표가 {target:,.0f}원 | 손절가 {stop:,.0f}원 설정 완료[/green]")

    elif sub == "memo":
        if len(args.portfolio) < 3:
            console.print("[red]사용법: --portfolio memo CODE \"메모 내용\"[/red]")
            return
        code = args.portfolio[1]
        memo = " ".join(args.portfolio[2:])
        update_position(code, memo=memo)
        console.print(f"[green]✅ {code} 투자근거 기록 완료[/green]")

    else:
        console.print(f"[red]알 수 없는 명령: {sub}[/red]")


def _run_watchlist_cmd(args):
    """워치리스트 관리 명령 처리."""
    from services.watchlist_service import (
        add_to_watchlist, remove_from_watchlist,
        get_watchlist, check_triggers, format_watchlist_for_briefing
    )
    from db.database import init_db
    from clients.telegram_client import send_message
    init_db()

    sub = args.watchlist[0] if args.watchlist else "list"

    if sub == "list":
        try:
            from clients.kis_client import KISClient
            kis = KISClient()
        except Exception:
            kis = None
        text = format_watchlist_for_briefing(kis)
        console.print(text)

    elif sub == "add":
        if len(args.watchlist) < 3:
            console.print("[red]사용법: --watchlist add CODE NAME [TARGET_PRICE] [timeframe] [reason...][/red]")
            return
        code   = args.watchlist[1]
        name   = args.watchlist[2]
        target = float(args.watchlist[3].replace(",", "")) if len(args.watchlist) > 3 else None
        tf_raw = args.watchlist[4] if len(args.watchlist) > 4 else "short"
        tf_map = {"단기": "short", "중기": "mid", "장기": "long",
                  "short": "short", "mid": "mid", "long": "long"}
        timeframe = tf_map.get(tf_raw, "short")
        reason = " ".join(args.watchlist[5:]) if len(args.watchlist) > 5 else None
        add_to_watchlist(code, name, target_entry=target, timeframe=timeframe, reason=reason)
        console.print(
            f"[green]✅ 워치리스트 추가: {name}({code})"
            + (f" | 목표진입 {target:,.0f}원" if target else "") + f" [{timeframe}][/green]"
        )

    elif sub == "remove":
        if len(args.watchlist) < 2:
            console.print("[red]사용법: --watchlist remove CODE[/red]")
            return
        code = args.watchlist[1]
        ok = remove_from_watchlist(code)
        if ok:
            console.print(f"[green]✅ {code} 워치리스트에서 제거[/green]")
        else:
            console.print(f"[red]❌ {code} 워치리스트에 없음[/red]")

    elif sub == "check":
        try:
            from clients.kis_client import KISClient
            kis = KISClient()
        except Exception:
            kis = None
        triggered = check_triggers(kis)
        if triggered:
            msg_lines = ["🚨 워치리스트 진입 조건 충족 종목:"]
            for t in triggered:
                msg_lines.append(
                    f"✅ {t['name']}({t['code']}) — {t.get('trigger_msg', '')}"
                    + (f"\n   주목 이유: {t['reason']}" if t.get("reason") else "")
                )
            msg = "\n".join(msg_lines)
            console.print(msg)
            try:
                send_message(msg)
                console.print("[green]텔레그램 발송 완료[/green]")
            except Exception as e:
                console.print(f"[yellow]텔레그램 발송 실패: {e}[/yellow]")
        else:
            console.print("[yellow]진입 조건 충족 종목 없음[/yellow]")

    else:
        console.print(f"[red]알 수 없는 명령: {sub}[/red]")


def main():
    parser = argparse.ArgumentParser(description="AI Investment Research Company")
    parser.add_argument(
        "--type",
        choices=["pre", "intra1", "intra2", "close", "midterm", "longterm",
                 "strategy", "thesis", "attribution",
                 "dart", "price-alert", "weekly", "trend", "monthly", "us-invest"],
        default="pre",
        help="실행 타입 (기본: pre)",
    )
    parser.add_argument("--check", action="store_true", help="환경변수 검증")
    parser.add_argument("--init-db", action="store_true", help="DB 초기화")
    parser.add_argument("--log-level", default="INFO", help="로그 레벨")
    parser.add_argument(
        "--research", metavar="CODE_OR_NAME",
        help="기업 종합 투자 분석 (종목코드 또는 회사명). 예: --research 005930 또는 --research 삼성전자"
    )
    parser.add_argument(
        "--telegram", action="store_true",
        help="--research 결과를 텔레그램으로도 발송"
    )
    parser.add_argument(
        "--bot", action="store_true",
        help="텔레그램 대화형 봇 시작 (long-polling). /research /price /balance 명령 수신"
    )
    parser.add_argument(
        "--portfolio", nargs="+", metavar="CMD",
        help="포트폴리오 관리: list | add CODE NAME QTY PRICE [timeframe] | close CODE [PRICE] | target CODE TARGET STOP | memo CODE TEXT"
    )
    parser.add_argument(
        "--watchlist", nargs="+", metavar="CMD",
        help="워치리스트 관리: list | add CODE NAME [TARGET] [timeframe] [reason] | remove CODE | check"
    )
    parser.add_argument(
        "--order", nargs="+", metavar="CMD",
        help="주문 실행: buy CODE QTY [PRICE] [timeframe] [memo] | sell CODE QTY [PRICE] [memo] | pending | cancel ORDER_NO CODE SIDE QTY [PRICE] | history"
    )
    parser.add_argument(
        "--tracker", action="store_true",
        help="AI 추천 종목 성과 추적 + 시장 예측 검증 (수동 실행)"
    )
    args = parser.parse_args()

    setup_logging(args.log_level)

    # ── 성과 추적 (수동 실행) ───────────────────────────────────
    if args.tracker:
        console.print("[bold cyan]📡 AI 추천 성과 추적 시작[/bold cyan]")
        try:
            from db.database import init_db
            init_db()
            from services.recommendation_tracker_service import run_daily_tracker
            from services.market_prediction_service import run_daily_verify
            from clients.kis_client import KISClient
            try:
                kis = KISClient()
            except Exception:
                kis = None
            stats = run_daily_tracker(kis)
            verified = run_daily_verify()
            console.print(
                f"[green]✅ 추적 완료: {stats.get('processed',0)}건 처리 "
                f"(목표달성 {stats.get('target_hit',0)} / 손절 {stats.get('stop_hit',0)} / 만료 {stats.get('expired',0)})"
                f" | 예측 검증 {verified}건[/green]"
            )
        except Exception as e:
            console.print(f"[red]❌ 추적 실패: {e}[/red]")
            sys.exit(1)
        return

    # 포트폴리오/워치리스트/주문 명령 (환경변수 없어도 실행 가능)
    if args.portfolio:
        _run_portfolio_cmd(args)
        return
    if args.watchlist:
        _run_watchlist_cmd(args)
        return
    if args.order:
        _run_order_cmd(args)
        return

    # ── 기업 리서치 ─────────────────────────────────────────────
    if args.research:
        from services.research_service import research_company
        query = args.research
        console.print(f"[bold cyan]🔍 기업 리서치 시작: {query}[/bold cyan]")
        try:
            report = research_company(query)
            console.print(report)
            if args.telegram:
                from clients.telegram_client import send_message
                send_message(report)
                console.print("[green]✅ 텔레그램 발송 완료[/green]")
        except Exception as e:
            console.print(f"[red]❌ 리서치 실패: {e}[/red]")
            sys.exit(1)
        return

    # ── 텔레그램 봇 ─────────────────────────────────────────────
    if args.bot:
        console.print("[bold cyan]🤖 텔레그램 봇 시작...[/bold cyan]")
        console.print("[yellow]Ctrl+C 로 종료[/yellow]")
        from clients.telegram_bot import run_bot
        try:
            run_bot()
        except KeyboardInterrupt:
            console.print("\n[yellow]봇 종료[/yellow]")
        return

    # 환경변수 검증 (run_type 전달 → KIS 불필요 타입은 KIS 검증 스킵)
    from config.settings import validate_env
    missing = validate_env(run_type=args.type if not args.check and not args.init_db else "")
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
        from db.database import init_db
        init_db()
        console.print("[green]✅ DB 초기화 완료[/green]")
        return

    # DB 초기화 (없으면 자동 생성)
    try:
        from db.database import init_db
        init_db()
    except Exception as e:
        logger.warning("DB 초기화 경고: %s", e)

    # ── 투자관 / 귀인 분석 (독립 실행) ─────────────────────
    if args.type == "thesis":
        console.print("[bold cyan]📜 월간 투자관 수립 시작[/bold cyan]")
        from agents.thesis_agent import run_thesis
        try:
            run_thesis()
            console.print("[green]✅ 투자관 수립 완료[/green]")
        except Exception as e:
            console.print(f"[red]❌ 투자관 실패: {e}[/red]")
            sys.exit(1)
        return

    if args.type == "attribution":
        console.print("[bold cyan]📊 주간 성과 귀인 분석 시작[/bold cyan]")
        from agents.attribution_agent import run_attribution
        try:
            run_attribution()
            console.print("[green]✅ 귀인 분석 완료[/green]")
        except Exception as e:
            console.print(f"[red]❌ 귀인 분석 실패: {e}[/red]")
            sys.exit(1)
        return

    # ── 중기 / 장기 분석 (독립 실행) ──────────────────────────
    if args.type == "strategy":
        console.print("[bold cyan]🗓 주간 종합 투자전략 수립 시작[/bold cyan]")
        from agents.strategy_agent import run_strategy
        try:
            run_strategy()
            console.print("[green]✅ 주간 전략 발송 완료[/green]")
        except Exception as e:
            console.print(f"[red]❌ 주간 전략 실패: {e}[/red]")
            sys.exit(1)
        return

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
        console.print("[bold cyan]📢 DART 공시 조회 시작 (브리핑 통합 방식)[/bold cyan]")
        from agents.dart_alert_agent import run as dart_run
        try:
            dart_run()
            console.print("[green]✅ DART 조회 완료 (텔레그램 발송은 브리핑에 통합됩니다)[/green]")
        except Exception as e:
            console.print(f"[red]❌ DART 조회 실패: {e}[/red]")
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

    if args.type == "trend":
        console.print("[bold cyan]📊 KOSPI 주간 추세 분석 시작[/bold cyan]")
        from services.market_trend_service import send_trend_report
        try:
            send_trend_report()
            console.print("[green]✅ KOSPI 추세 분석 발송 완료[/green]")
        except Exception as e:
            console.print(f"[red]❌ KOSPI 추세 분석 실패: {e}[/red]")
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
