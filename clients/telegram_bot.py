"""
텔레그램 대화형 봇 — long-polling 기반
사용자가 텔레그램에서 명령을 입력하면 실시간으로 응답한다.

지원 명령어:
  /research [종목코드 또는 회사명]  — 기업 종합 투자 분석
  /search [검색어]                  — 회사명 검색 (코드 모를 때)
  /price [종목코드]                 — 현재가·밸류에이션 조회
  /balance                          — 계좌 잔고·보유종목 조회
  /holdings                         — 보유 종목 현황
  /portfolio                        — 포트폴리오 손익 현황
  /watchlist                        — 관심종목 목록
  /watchlist add CODE 회사명 [목표가] — 관심종목 추가
  /watchlist remove CODE            — 관심종목 제거
  /buy CODE QTY [PRICE]             — 매수 주문
  /sell CODE QTY [PRICE]            — 매도 주문
  /orders                           — 미체결 주문 조회
  /cancel ORDER_NO CODE SIDE QTY    — 주문 취소
  /history                          — 최근 주문 이력
  /thesis                           — 현재 월간 투자관
  /strategy                         — 현재 주간 전략
  /tracker                          — AI 추천 성과 추적 + 예측 적중률
  /auto                             — 자동 실행 현재 상태 + 오늘 자동 실행 내역
  /auto on/off                      — 자동 실행 전체 활성화/비활성화
  /auto stop_on/stop_off            — 자동 손절만 제어
  /auto buy_on/buy_off              — 자동 진입만 제어
  /exposure                         — 오늘 포지션 노출도 + 드로다운 현황
  /pause [일수]                     — 자동 실행 N일 일시 중단
  /help                             — 명령어 안내
  [자유 텍스트]                     — AI 투자 어드바이저 대화
"""
import logging
import time
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from config.settings import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

logger = logging.getLogger(__name__)

_BASE    = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
_KST     = ZoneInfo("Asia/Seoul")
_RUNNING = threading.Event()

# 허용된 chat_id (설정된 TELEGRAM_CHAT_ID만 수신)
_ALLOWED = {TELEGRAM_CHAT_ID} if TELEGRAM_CHAT_ID else set()


# ── 메시지 발송 ────────────────────────────────────────────────

def _send(chat_id: str, text: str) -> None:
    url = f"{_BASE}/sendMessage"
    max_len = 4096
    # 개행 기준 분할 → 마크다운 포맷 보존
    chunks: list[str] = []
    while text:
        if len(text) <= max_len:
            chunks.append(text)
            break
        split_at = text.rfind('\n', 0, max_len)
        if split_at <= 0:
            split_at = max_len
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip('\n')
    for chunk in chunks:
        for attempt in range(3):
            try:
                r = requests.post(
                    url,
                    json={"chat_id": chat_id, "text": chunk, "parse_mode": "Markdown"},
                    timeout=15,
                )
                if r.ok:
                    break
                if r.status_code == 400:
                    # Markdown 파싱 오류 → plain text 재시도
                    requests.post(url, json={"chat_id": chat_id, "text": chunk}, timeout=15)
                    break
                time.sleep(2 ** attempt)
            except Exception as e:
                logger.warning("[Bot] 발송 실패 (시도 %d): %s", attempt + 1, e)
                time.sleep(1)


def _typing(chat_id: str) -> None:
    """입력 중 표시."""
    try:
        requests.post(
            f"{_BASE}/sendChatAction",
            json={"chat_id": chat_id, "action": "typing"},
            timeout=5,
        )
    except Exception:
        pass


# ── 명령 핸들러 ────────────────────────────────────────────────

def _cmd_start(chat_id: str, _args: str) -> None:
    _send(
        chat_id,
        "👋 *AI 투자 분석 어시스턴트에 오신 것을 환영합니다*\n\n"
        "저는 한국 주식 시장을 분석하고 중장기 투자 방향을 제시하는 AI 투자 분석 시스템입니다.\n\n"
        "📌 *이용 안내*\n"
        "• 이 봇은 시장 분석·투자 참고 정보 제공 목적입니다\n"
        "• 실시간 매매 주문 실행은 지원하지 않습니다\n"
        "• 제공되는 분석은 투자 참고용이며, 최종 투자 결정은 본인 책임입니다\n\n"
        "아래 명령어 또는 자유롭게 질문해보세요:\n"
        "• `삼성전자 중장기 전망이 어때?`\n"
        "• `오늘 반도체 섹터 흐름은?`\n"
        "• `/research 005930`\n\n"
        "`/help` — 전체 명령어 보기",
    )


def _cmd_help(chat_id: str, _args: str) -> None:
    msg = (
        "🤖 *AI 투자 어시스턴트 명령어*\n\n"
        "📊 *시장·종목 조회*\n"
        "`/research [코드 또는 회사명]` — 기업 종합 투자 분석\n"
        "`/search [검색어]` — 종목코드 검색\n"
        "`/price [종목코드]` — 현재가·PER·PBR\n\n"
        "💼 *계좌·포트폴리오*\n"
        "`/balance` — 예수금 + 보유종목\n"
        "`/holdings` — 보유종목 수익률\n"
        "`/portfolio` — 포트폴리오 손익 현황\n\n"
        "📋 *주문*\n"
        "`/buy CODE QTY [PRICE]` — 매수\n"
        "  예: `/buy 005930 10 80000` 또는 `/buy 005930 10` (시장가)\n"
        "`/sell CODE QTY [PRICE]` — 매도 (QTY=0 전량)\n"
        "  예: `/sell 005930 5` 또는 `/sell 005930 0` (전량)\n"
        "`/orders` — 미체결 주문 목록\n"
        "`/cancel ORDER_NO CODE SIDE QTY` — 주문 취소\n"
        "`/history` — 최근 주문 이력\n\n"
        "🔍 *심층 분석*\n"
        "`/insight` — 글로벌 시장 서사·전문가/텔레그램 채널 시각·종목 기술/수급 분석\n"
        "  (메인 브리핑에서 압축돼 빠진 분석 원문)\n\n"
        "📜 *전략·투자관*\n"
        "`/thesis` — 현재 월간 투자관\n"
        "`/strategy` — 현재 주간 전략\n"
        "`/nav` — 포트폴리오 NAV + Alpha 현황\n"
        "`/report` — 이번 주 성과 요약\n"
        "`/tracker` — AI 추천 성과 추적 + 예측 적중률\n\n"
        "👀 *관심종목*\n"
        "`/watchlist` — 관심종목 목록\n"
        "`/watchlist add CODE 회사명 [목표가]` — 추가\n"
        "`/watchlist remove CODE` — 제거\n\n"
        "🤖 *자동 실행 제어*\n"
        "`/auto` — 자동 실행 상태 + 오늘 내역\n"
        "`/auto on` / `/auto off` — 전체 활성화/비활성화\n"
        "`/auto buy_on` / `/auto buy_off` — 자동 매수만\n"
        "`/auto stop_on` / `/auto stop_off` — 자동 손절만\n"
        "`/exposure` — 오늘 노출도 + 드로다운 현황\n"
        "`/pause [일수]` — 자동 실행 N일 일시 중단\n\n"
        "💬 *AI 대화*\n"
        "명령어 없이 자유롭게 질문하세요!\n"
        "예: '삼성전자 지금 살만해?', '오늘 시장 어때?'"
    )
    _send(chat_id, msg)


def _cmd_research(chat_id: str, args: str) -> None:
    if not args:
        _send(chat_id, "❌ 종목코드 또는 회사명을 입력하세요.\n예: `/research 005930` 또는 `/research 삼성전자`")
        return

    _send(chat_id, f"🔍 *{args}* 리서치 시작... (30~60초 소요)")
    _typing(chat_id)

    try:
        from services.research_service import research_company
        report = research_company(args)
        _send(chat_id, report)
    except Exception as e:
        logger.error("[Bot] /research 오류: %s", e)
        _send(chat_id, f"❌ 분석 중 오류가 발생했습니다: {e}")


def _cmd_search(chat_id: str, args: str) -> None:
    if not args:
        _send(chat_id, "❌ 검색어를 입력하세요.\n예: `/search 삼성`")
        return

    try:
        from services.research_service import search_companies
        results = search_companies(args)
        if not results:
            _send(chat_id, f"❌ '{args}'와 일치하는 종목이 없습니다.")
            return
        lines = [f"🔍 *'{args}' 검색 결과*\n"]
        for r in results:
            lines.append(f"• `{r['code']}` {r['name']}")
        lines.append("\n종목코드로 분석: `/research 005930`")
        _send(chat_id, "\n".join(lines))
    except Exception as e:
        logger.error("[Bot] /search 오류: %s", e)
        _send(chat_id, f"❌ 검색 오류: {e}")


def _cmd_price(chat_id: str, args: str) -> None:
    code = args.strip().zfill(6) if args.strip().isdigit() else args.strip()
    if not code:
        _send(chat_id, "❌ 종목코드를 입력하세요.\n예: `/price 005930`")
        return

    try:
        from clients.kis_client import KISClient
        from services.research_service import resolve_code
        _typing(chat_id)

        # 이름 해석
        if not code.isdigit():
            resolved_code, name = resolve_code(code)
            if not resolved_code:
                _send(chat_id, f"❌ '{code}' 종목을 찾지 못했습니다.")
                return
            code, corp_name = resolved_code, name
        else:
            _, corp_name = resolve_code(code)

        kis  = KISClient()
        data = kis.get_stock_price(code, market=None)
        if not data:
            _send(chat_id, f"❌ {code} 가격 조회 실패 (장 마감 또는 API 오류)")
            return

        price  = data.get("price", 0)
        chg    = data.get("change_pct", 0)
        per    = data.get("per", 0)
        pbr    = data.get("pbr", 0)
        eps    = data.get("eps", 0)
        cap    = data.get("market_cap_억", 0)
        h52    = data.get("52w_high", 0)
        l52    = data.get("52w_low", 0)

        msg = (
            f"💰 *{corp_name}({code}) 현재가*\n\n"
            f"현재가: `{price:,}원`  ({chg:+.2f}%)\n"
            f"PER: `{per:.1f}배`  |  PBR: `{pbr:.1f}배`\n"
            f"EPS: `{eps:,}원`\n"
            f"시가총액: `{cap:,}억원`\n"
            f"52주 고: `{h52:,}원`  |  저: `{l52:,}원`\n"
            f"\n분석 리포트: `/research {code}`"
        )
        _send(chat_id, msg)
    except Exception as e:
        logger.error("[Bot] /price 오류: %s", e)
        _send(chat_id, f"❌ 가격 조회 오류: {e}")


def _cmd_balance(chat_id: str, _args: str) -> None:
    try:
        from clients.kis_client import KISClient
        from config.settings import KIS_IS_REAL
        _typing(chat_id)

        kis  = KISClient()
        data = kis.get_account_balance()
        mode = "실계좌" if KIS_IS_REAL else "모의계좌"

        cash     = data.get("cash", 0)
        total    = data.get("total_eval", 0)
        purchase = data.get("purchase_amt", 0)
        holdings = data.get("holdings", [])

        lines = [
            f"💼 *계좌 잔고 현황* ({mode})\n",
            f"예수금: `{cash:,}원`",
            f"보유 평가액: `{total:,}원`",
            f"매입 금액: `{purchase:,}원`",
        ]

        if holdings:
            lines.append(f"\n📊 *보유 종목 ({len(holdings)}개)*")
            for h in holdings:
                pnl_icon = "📈" if h.get("pnl_pct", 0) >= 0 else "📉"
                lines.append(
                    f"{pnl_icon} {h['name']}({h['code']})\n"
                    f"   {h['qty']:,}주 | 평균 {h['avg_price']:,}원 | "
                    f"현재 {h['eval_price']:,}원 | {h['pnl_pct']:+.2f}%"
                )
        else:
            lines.append("\n보유 종목 없음")

        _send(chat_id, "\n".join(lines))
    except Exception as e:
        logger.error("[Bot] /balance 오류: %s", e)
        _send(chat_id, f"❌ 잔고 조회 오류: {e}\nKIS API 연결을 확인해 주세요.")


def _cmd_holdings(chat_id: str, _args: str) -> None:
    _cmd_balance(chat_id, "")  # 잔고에 보유종목 포함


def _cmd_watchlist(chat_id: str, args: str) -> None:
    parts = args.strip().split()
    sub = parts[0].lower() if parts else ""

    # ── /watchlist add CODE 회사명 [목표가] ─────────────────────
    if sub == "add":
        if len(parts) < 3:
            _send(chat_id,
                "❌ 사용법: `/watchlist add CODE 회사명 [목표가]`\n"
                "예: `/watchlist add 005930 삼성전자 75000`")
            return
        code = parts[1].zfill(6)
        name = parts[2]
        price = None
        if len(parts) > 3:
            try:
                price = float(parts[3].replace(",", ""))
            except ValueError:
                pass
        try:
            from services.watchlist_service import add_to_watchlist
            add_to_watchlist(code, name, target_entry=price)
            msg = f"✅ 관심종목 추가: *{name}* ({code})"
            if price:
                msg += f"\n목표진입가: {price:,.0f}원"
            _send(chat_id, msg)
        except Exception as e:
            logger.error("[Bot] /watchlist add 오류: %s", e)
            _send(chat_id, f"❌ 추가 실패: {e}")
        return

    # ── /watchlist remove CODE ───────────────────────────────────
    if sub == "remove":
        if len(parts) < 2:
            _send(chat_id, "❌ 사용법: `/watchlist remove CODE`\n예: `/watchlist remove 005930`")
            return
        code = parts[1].zfill(6)
        try:
            from services.watchlist_service import remove_from_watchlist
            ok = remove_from_watchlist(code)
            _send(chat_id, f"{'✅ 관심종목 제거 완료' if ok else '❌ 해당 종목 없음'}: `{code}`")
        except Exception as e:
            logger.error("[Bot] /watchlist remove 오류: %s", e)
            _send(chat_id, f"❌ 제거 실패: {e}")
        return

    # ── 기본: 목록 조회 ──────────────────────────────────────────
    try:
        from services.watchlist_service import get_watchlist
        items = get_watchlist()
        if not items:
            _send(chat_id,
                "📋 관심종목이 없습니다.\n"
                "`/watchlist add CODE 회사명 [목표가]`으로 추가하세요.")
            return
        lines = ["📋 *관심종목*\n"]
        for w in items:
            target_str = f" → 목표 {w['target_entry']:,.0f}원" if w.get("target_entry") else ""
            lines.append(
                f"• {w['name']}({w['code']}){target_str}\n"
                f"  _{w.get('reason', '')}_ ({w.get('timeframe', '')})"
            )
        lines.append("\n`/watchlist add CODE 회사명` | `/watchlist remove CODE`")
        _send(chat_id, "\n".join(lines))
    except Exception as e:
        logger.error("[Bot] /watchlist 오류: %s", e)
        _send(chat_id, f"❌ 관심종목 조회 오류: {e}")


def _cmd_portfolio(chat_id: str, _args: str) -> None:
    try:
        from services.portfolio_service import format_portfolio_for_briefing
        _typing(chat_id)
        try:
            from clients.kis_client import KISClient
            kis = KISClient()
        except Exception:
            kis = None
        text = format_portfolio_for_briefing(kis)
        _send(chat_id, text or "📋 포트폴리오 없음")
    except Exception as e:
        logger.error("[Bot] /portfolio 오류: %s", e)
        _send(chat_id, f"❌ 포트폴리오 조회 오류: {e}")


def _cmd_buy(chat_id: str, args: str) -> None:
    """
    /buy CODE QTY [PRICE] [timeframe] [memo...]
    예: /buy 005930 10 80000
        /buy 005930 10 (시장가)
        /buy 005930 10 80000 mid 반도체 반등 기대
    """
    parts = args.split()
    if len(parts) < 2:
        _send(chat_id,
              "❌ 사용법: `/buy CODE QTY [PRICE]`\n"
              "예: `/buy 005930 10 80000` 또는 `/buy 005930 10` (시장가)")
        return

    code = parts[0].strip().zfill(6)
    try:
        qty = int(parts[1])
    except ValueError:
        _send(chat_id, "❌ 수량은 정수여야 합니다.")
        return

    price = 0
    idx = 2
    if len(parts) > 2:
        try:
            price = int(parts[2].replace(",", ""))
            idx = 3
        except ValueError:
            pass

    tf_map = {"short": "short", "mid": "mid", "long": "long",
              "단기": "short", "중기": "mid", "장기": "long"}
    timeframe = "short"
    if len(parts) > idx and parts[idx] in tf_map:
        timeframe = tf_map[parts[idx]]
        idx += 1

    memo = " ".join(parts[idx:]) if len(parts) > idx else ""

    _typing(chat_id)
    price_label = f"{price:,}원 지정가" if price > 0 else "시장가"
    _send(chat_id, f"📤 매수 주문 처리 중...\n{code} {qty:,}주 @{price_label}")

    try:
        from services.trading_service import execute_buy, TradingError
        result = execute_buy(code=code, qty=qty, price=price,
                             memo=memo, timeframe=timeframe)
        if result["success"]:
            _send(chat_id,
                  f"✅ *매수 완료*\n{result['name']}({code}) {qty:,}주\n"
                  f"주문번호: {result['order_no'] or '없음'}")
        else:
            _send(chat_id, f"❌ *매수 실패*\n{result['message']}")
    except Exception as e:
        logger.error("[Bot] /buy 오류: %s", e)
        _send(chat_id, f"❌ 매수 오류: {e}")


def _cmd_sell(chat_id: str, args: str) -> None:
    """
    /sell CODE QTY [PRICE] [memo...]
    QTY=0 이면 전량 매도
    예: /sell 005930 5 82000
        /sell 005930 0 (전량 시장가)
    """
    parts = args.split()
    if len(parts) < 2:
        _send(chat_id,
              "❌ 사용법: `/sell CODE QTY [PRICE]`\n"
              "예: `/sell 005930 5 82000` 또는 `/sell 005930 0` (전량 시장가)")
        return

    code = parts[0].strip().zfill(6)
    try:
        qty = int(parts[1])
    except ValueError:
        _send(chat_id, "❌ 수량은 정수여야 합니다. 전량 매도는 0을 입력하세요.")
        return

    price = 0
    idx = 2
    if len(parts) > 2:
        try:
            price = int(parts[2].replace(",", ""))
            idx = 3
        except ValueError:
            pass

    memo = " ".join(parts[idx:]) if len(parts) > idx else ""

    _typing(chat_id)
    qty_label = "전량" if qty == 0 else f"{qty:,}주"
    price_label = f"{price:,}원 지정가" if price > 0 else "시장가"
    _send(chat_id, f"📤 매도 주문 처리 중...\n{code} {qty_label} @{price_label}")

    try:
        from services.trading_service import execute_sell, TradingError
        result = execute_sell(code=code, qty=qty, price=price, memo=memo)
        if result["success"]:
            _send(chat_id,
                  f"✅ *매도 완료*\n{result['name']}({code}) {qty_label}\n"
                  f"주문번호: {result['order_no'] or '없음'}")
        else:
            _send(chat_id, f"❌ *매도 실패*\n{result['message']}")
    except Exception as e:
        logger.error("[Bot] /sell 오류: %s", e)
        _send(chat_id, f"❌ 매도 오류: {e}")


def _cmd_orders(chat_id: str, _args: str) -> None:
    """미체결 주문 조회."""
    _typing(chat_id)
    try:
        from services.trading_service import get_pending_orders
        orders = get_pending_orders()
        if not orders:
            _send(chat_id, "📋 미체결 주문 없음")
            return
        lines = [f"📋 *미체결 주문 ({len(orders)}건)*\n"]
        for o in orders:
            side_icon = "🟢 매수" if o["side"] == "buy" else "🔴 매도"
            lines.append(
                f"{side_icon} {o['name']}({o['code']})\n"
                f"  {o['qty']:,}주 @{o['price']:,}원\n"
                f"  주문번호: `{o['order_no']}`"
            )
        lines.append("\n취소: `/cancel ORDER_NO CODE SIDE QTY`")
        _send(chat_id, "\n".join(lines))
    except Exception as e:
        logger.error("[Bot] /orders 오류: %s", e)
        _send(chat_id, f"❌ 미체결 주문 조회 오류: {e}")


def _cmd_cancel(chat_id: str, args: str) -> None:
    """
    /cancel ORDER_NO CODE SIDE QTY [PRICE]
    예: /cancel 0012345 005930 buy 10
    """
    parts = args.split()
    if len(parts) < 4:
        _send(chat_id,
              "❌ 사용법: `/cancel ORDER_NO CODE SIDE QTY`\n"
              "예: `/cancel 0012345 005930 buy 10`\n"
              "`/orders` 로 주문번호 확인")
        return
    order_no = parts[0]
    code = parts[1].zfill(6)
    side = parts[2].lower()
    try:
        qty = int(parts[3])
    except ValueError:
        _send(chat_id, "❌ 수량은 정수여야 합니다.")
        return
    price = int(parts[4].replace(",", "")) if len(parts) > 4 else 0

    _typing(chat_id)
    try:
        from services.trading_service import cancel_order
        result = cancel_order(order_no, code, side, qty, price)
        if result.get("success"):
            _send(chat_id, f"✅ 주문 취소 완료\n주문번호: {order_no}")
        else:
            _send(chat_id, f"❌ 취소 실패: {result.get('message', '')}")
    except Exception as e:
        logger.error("[Bot] /cancel 오류: %s", e)
        _send(chat_id, f"❌ 취소 오류: {e}")


def _cmd_history(chat_id: str, _args: str) -> None:
    """최근 주문 이력."""
    try:
        from services.trading_service import format_order_history
        text = format_order_history(limit=10)
        _send(chat_id, text)
    except Exception as e:
        logger.error("[Bot] /history 오류: %s", e)
        _send(chat_id, f"❌ 주문 이력 조회 오류: {e}")


def _cmd_insight(chat_id: str, _args: str) -> None:
    """메인 브리핑에서 압축돼 잘려나간 분석 원문(글로벌 시장 서사·전문가/텔레그램
    채널 시각·빅피겨 발언·종목별 기술적·수급 분석) 조회."""
    _typing(chat_id)
    try:
        from services.report_service import get_latest_deep_report
        report = get_latest_deep_report()
        if not report or not report.get("content"):
            _send(chat_id, "🔍 아직 생성된 심층 리포트가 없습니다 — 다음 브리핑 이후 확인해주세요.")
            return
        header = f"🔍 *심층 분석* ({report['date']} {report['run_type']})\n\n"
        _send(chat_id, header + report["content"])
    except Exception as e:
        logger.error("[Bot] /insight 오류: %s", e)
        _send(chat_id, f"❌ 심층 리포트 조회 오류: {e}")


def _cmd_thesis(chat_id: str, _args: str) -> None:
    """현재 월간 투자관 조회."""
    try:
        from services.thesis_service import get_active_thesis
        thesis = get_active_thesis()
        if not thesis:
            _send(chat_id,
                  "📜 저장된 투자관가 없습니다.\n"
                  "`python main.py --type thesis` 로 생성하세요.")
            return
        date = thesis.get("date", "?")
        cycle = thesis.get("cycle_stage", "")
        body = thesis.get("ceo_summary") or thesis.get("full_report", "")
        header = f"📜 *월간 투자관* ({date})\n경기 사이클: {cycle}\n\n"
        _send(chat_id, header + body[:3000])
    except Exception as e:
        logger.error("[Bot] /thesis 오류: %s", e)
        _send(chat_id, f"❌ 투자관 조회 오류: {e}")


def _cmd_strategy(chat_id: str, _args: str) -> None:
    """현재 주간 전략 조회."""
    try:
        from services.strategy_service import get_latest_strategy_report
        report = get_latest_strategy_report(report_type="weekly", max_days=10)
        if not report:
            _send(chat_id,
                  "🗓 저장된 주간 전략이 없습니다.\n"
                  "`python main.py --type strategy` 로 생성하세요.")
            return
        _send(chat_id, f"🗓 *주간 종합 전략*\n\n{report[:3500]}")
    except Exception as e:
        logger.error("[Bot] /strategy 오류: %s", e)
        _send(chat_id, f"❌ 주간 전략 조회 오류: {e}")


def _cmd_nav(chat_id: str, _args: str) -> None:
    """포트폴리오 NAV + Alpha 현황 조회."""
    _typing(chat_id)
    try:
        from services.nav_service import get_latest_nav, get_nav_history, generate_nav_report
        latest = get_latest_nav()
        if not latest:
            _send(chat_id,
                  "📈 NAV 기록이 없습니다.\n"
                  "장마감 후 16:10에 자동 기록되거나,\n"
                  "`python main.py --type close` 실행 후 확인하세요.")
            return
        report = generate_nav_report(days=7)
        _send(chat_id, f"📈 *포트폴리오 NAV 현황*\n\n{report}")
    except Exception as e:
        logger.error("[Bot] /nav 오류: %s", e)
        _send(chat_id, f"❌ NAV 조회 오류: {e}")


def _cmd_report(chat_id: str, _args: str) -> None:
    """이번 주 성과 요약 리포트."""
    _typing(chat_id)
    try:
        from services.recommendation_service import get_performance_stats, get_recent_recommendations
        from agents.attribution_agent import _get_kospi_weekly

        perf = get_performance_stats(days=7)
        kospi = _get_kospi_weekly()
        recs = get_recent_recommendations(days=7)

        lines = [
            "📊 *이번 주 성과 요약*\n",
            f"📅 기간: 최근 7일",
            f"📉 KOSPI 주간: `{kospi:+.2f}%`\n",
            f"🎯 *추천 성과*",
            f"  총 {perf.get('total', 0)}건 | 성공 {perf.get('win', 0)} | 실패 {perf.get('loss', 0)}",
            f"  승률: `{perf.get('win_rate', 0):.1f}%`",
            f"  평균 수익률: `{perf.get('avg_return', 0):+.2f}%`",
        ]

        if recs:
            lines.append(f"\n📋 *상세 추천 ({min(len(recs),5)}건)*")
            for r in recs[:5]:
                ret = r.get("return_pct")
                ret_str = f"`{ret:+.1f}%`" if ret is not None else "집계중"
                result_icon = "✅" if r.get("result") == "성공" else ("❌" if r.get("result") == "실패" else "⏳")
                lines.append(f"  {result_icon} {r.get('name','?')}({r.get('code','?')}) {ret_str}")

        # 귀인 분석 최신 교훈
        try:
            from agents.attribution_agent import get_recent_learnings
            learnings = get_recent_learnings(weeks=1)
            if learnings:
                lines.append(f"\n💡 *지난주 교훈*\n{learnings[:300]}")
        except Exception:
            pass

        _send(chat_id, "\n".join(lines))
    except Exception as e:
        logger.error("[Bot] /report 오류: %s", e)
        _send(chat_id, f"❌ 리포트 조회 오류: {e}")


def _cmd_tracker(chat_id: str, _args: str) -> None:
    """AI 추천 종목 성과 추적 + 시장 방향 예측 적중률 요약."""
    _typing(chat_id)
    try:
        from services.recommendation_tracker_service import format_tracker_report
        from services.market_prediction_service import format_prediction_report
        tracker_msg = format_tracker_report()
        pred_msg    = format_prediction_report(days=30)
        _send(chat_id, tracker_msg)
        _send(chat_id, pred_msg)
    except Exception as e:
        logger.error("[Bot] /tracker 오류: %s", e)
        _send(chat_id, f"❌ 성과 추적 조회 오류: {e}")


# ── 자동 실행 명령어 (P6-1) ──────────────────────────────────────

def _cmd_auto(chat_id: str, args: str) -> None:
    """
    /auto                   — 자동 실행 현재 상태 + 오늘 자동 실행 내역
    /auto on                — 모두 활성화
    /auto off               — 모두 비활성화
    /auto stop_on/stop_off  — 자동 손절만 제어
    /auto buy_on/buy_off    — 자동 진입만 제어
    """
    import os
    sub = args.strip().lower()

    # ── 상태 토글 (DB에 저장 → 모든 프로세스에서 즉시 반영) ──────
    from config.settings import set_auto_setting

    if sub in ("on", "off"):
        active = (sub == "on")
        set_auto_setting("AUTO_EXECUTE_BUY",         active)
        set_auto_setting("AUTO_EXECUTE_STOP",        active)
        set_auto_setting("AUTO_EXECUTE_TARGET_HALF", active)
        icon = "✅" if active else "⏸️"
        state = "ON" if active else "OFF"
        _send(chat_id, f"{icon} 자동 실행 전체 {'활성화' if active else '비활성화'} 완료\n"
              f"  매수: {state}  |  손절: {state}  |  목표익절: {state}\n"
              f"  _(재시작 없이 스케줄러에 즉시 반영됩니다)_")
        return

    if sub == "stop_on":
        set_auto_setting("AUTO_EXECUTE_STOP", True)
        _send(chat_id, "✅ 자동 손절 활성화 (DB 저장 완료)")
        return
    if sub == "stop_off":
        set_auto_setting("AUTO_EXECUTE_STOP", False)
        _send(chat_id, "⏸️ 자동 손절 비활성화 (DB 저장 완료)")
        return
    if sub == "buy_on":
        set_auto_setting("AUTO_EXECUTE_BUY", True)
        _send(chat_id, "✅ 자동 매수 진입 활성화 (DB 저장 완료)")
        return
    if sub == "buy_off":
        set_auto_setting("AUTO_EXECUTE_BUY", False)
        _send(chat_id, "⏸️ 자동 매수 진입 비활성화 (DB 저장 완료)")
        return

    # ── 현재 상태 + 오늘 내역 (DB에서 실시간 조회) ──────────────
    _typing(chat_id)
    try:
        from config.settings import get_auto_setting
        import config.settings as _s
        buy_on  = get_auto_setting("AUTO_EXECUTE_BUY")
        stop_on = get_auto_setting("AUTO_EXECUTE_STOP")
        half_on = get_auto_setting("AUTO_EXECUTE_TARGET_HALF")
        max_exp = getattr(_s, "AUTO_MAX_DAILY_EXPOSURE", 0.10)
    except Exception:
        buy_on = stop_on = half_on = False
        max_exp = 0.10

    # 오늘 자동 실행 내역
    today_lines = []
    try:
        from db.database import get_conn
        from sqlalchemy import text
        from datetime import datetime
        today = datetime.now(_KST).strftime("%Y-%m-%d")
        with get_conn() as conn:
            rows = conn.execute(text("""
                SELECT side, name, code, qty, price, success, memo
                FROM order_history
                WHERE rec_id IS NOT NULL AND created_at >= :today
                ORDER BY id DESC LIMIT 10
            """), {"today": today + " 00:00:00"}).fetchall()
        for r in rows:
            icon = "✅" if r[5] else "❌"
            side_str = "매수" if r[0] == "buy" else "매도"
            today_lines.append(f"  {icon} {side_str} {r[1]}({r[2]}) {r[3]:,}주 @{r[4]:,}원")
    except Exception as _e:
        logger.debug("[Bot] /auto 내역 조회 실패: %s", _e)

    lines = [
        "🤖 *자동 실행 현황*\n",
        f"📥 자동 매수 진입: {'✅ ON' if buy_on else '⏸️ OFF'}",
        f"🛑 자동 손절:      {'✅ ON' if stop_on else '⏸️ OFF'}",
        f"💰 목표 절반 익절: {'✅ ON' if half_on else '⏸️ OFF'}",
        f"📊 일일 최대 노출: {max_exp*100:.0f}%",
    ]
    if today_lines:
        lines.append(f"\n📋 오늘 자동 실행 ({len(today_lines)}건):")
        lines.extend(today_lines)
    else:
        lines.append("\n📋 오늘 자동 실행 내역 없음")

    lines.append("\n`/auto on` | `/auto off` | `/auto buy_on` | `/auto stop_on`")
    _send(chat_id, "\n".join(lines))


def _cmd_exposure(chat_id: str, _args: str) -> None:
    """오늘 포지션 노출도 + 드로다운 현황."""
    _typing(chat_id)
    try:
        from services.auto_execute_service import get_daily_exposure, get_consecutive_loss_days
        from services.nav_service import check_drawdown_defense, get_latest_nav

        nav = get_latest_nav()
        total_assets = int(nav.get("total_value", 0)) if nav else 0
        daily_exp = get_daily_exposure()
        consec_loss = get_consecutive_loss_days()
        dd_info = check_drawdown_defense()

        dd_action = dd_info.get("action", "none")
        dd_msg = dd_info.get("message", "정상")
        dd_icon = "✅" if dd_action == "none" else ("⚠️" if dd_action == "half" else "🚨")

        lines = [
            "📊 *노출도 & 드로다운 현황*\n",
            f"💼 총 자산: {total_assets:,}원" if total_assets else "💼 총 자산: NAV 없음",
            f"📥 오늘 신규 노출: {daily_exp*100:.1f}%",
            f"📉 연속 손실일: {consec_loss}일",
            f"\n{dd_icon} 드로다운 상태: {dd_msg}",
        ]
        if dd_action != "none":
            lines.append(f"⚠️ 조치 필요: {dd_action.upper()}")
        _send(chat_id, "\n".join(lines))
    except Exception as e:
        logger.error("[Bot] /exposure 오류: %s", e)
        _send(chat_id, f"❌ 노출도 조회 오류: {e}")


def _cmd_pause(chat_id: str, args: str) -> None:
    """/pause [일수] — 자동 실행 N일 일시 중단."""
    try:
        days = int(args.strip()) if args.strip().isdigit() else 1
    except ValueError:
        days = 1
    try:
        from services.auto_execute_service import pause_auto_execute
        pause_auto_execute(f"봇 명령 일시중단 ({days}일)", days=days)
        _send(chat_id, f"⏸️ 자동 실행 {days}일 일시 중단 완료\n`/auto on` 으로 재개 가능")
    except Exception as e:
        logger.error("[Bot] /pause 오류: %s", e)
        _send(chat_id, f"❌ 일시중단 오류: {e}")


# ── 콜백 쿼리 핸들러 (인라인 버튼 클릭) ─────────────────────────

def _handle_callback(chat_id: str, data: str, callback_query_id: str = "") -> None:
    """인라인 버튼 callback_data 처리.

    data 형식:
      "buy:CODE:QTY:PRICE"       — 즉시 매수
      "sell:CODE:QTY:PRICE"      — 즉시 매도
      "sell_half:CODE"           — 절반 매도 (보유량 // 2)
      "ignore:CODE"              — 무시 (알림만 닫기)
      "auto_on"                  — 자동 실행 ON
      "auto_off"                 — 자동 실행 OFF
    """
    from clients.telegram_client import answer_callback_query
    if callback_query_id:
        answer_callback_query(callback_query_id, "처리 중...")

    try:
        parts = data.split(":")
        action = parts[0].lower()

        if action == "ignore":
            code = parts[1] if len(parts) > 1 else ""
            _send(chat_id, f"⏭️ {code} 알림 무시됨")
            return

        if action in ("auto_on", "auto_off"):
            _cmd_auto(chat_id, "on" if action == "auto_on" else "off")
            return

        if action == "buy":
            if len(parts) < 3:
                _send(chat_id, "❌ buy 콜백 데이터 오류")
                return
            code  = parts[1].zfill(6)
            qty   = int(parts[2]) if parts[2].isdigit() else 0
            price = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0
            if qty <= 0:
                _send(chat_id, "❌ 수량 오류 (0주 이하)")
                return
            _send(chat_id, f"📤 매수 처리 중... {code} {qty:,}주")
            try:
                from services.trading_service import execute_buy
                result = execute_buy(code=code, qty=qty, price=price, memo="인라인버튼 즉시매수")
                if result["success"]:
                    _send(chat_id, f"✅ 매수 완료: {result['name']}({code}) {qty:,}주")
                else:
                    _send(chat_id, f"❌ 매수 실패: {result['message']}")
            except Exception as e:
                _send(chat_id, f"❌ 매수 오류: {e}")
            return

        if action == "sell":
            if len(parts) < 3:
                _send(chat_id, "❌ sell 콜백 데이터 오류")
                return
            code  = parts[1].zfill(6)
            qty   = int(parts[2]) if parts[2].isdigit() else 0
            price = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0
            _send(chat_id, f"📤 매도 처리 중... {code} {qty or '전량'}")
            try:
                from services.trading_service import execute_sell
                result = execute_sell(code=code, qty=qty, price=price, memo="인라인버튼 즉시매도")
                if result["success"]:
                    _send(chat_id, f"✅ 매도 완료: {result['name']}({code})")
                else:
                    _send(chat_id, f"❌ 매도 실패: {result['message']}")
            except Exception as e:
                _send(chat_id, f"❌ 매도 오류: {e}")
            return

        if action == "sell_half":
            if len(parts) < 2:
                _send(chat_id, "❌ sell_half 콜백 데이터 오류")
                return
            code = parts[1].zfill(6)
            _send(chat_id, f"📤 절반 매도 처리 중... {code}")
            try:
                from clients.kis_client import KISClient
                from services.trading_service import execute_sell
                _kis = KISClient()
                holdings = _kis.get_holdings()
                holding  = next((h for h in holdings if h.get("code") == code), None)
                if not holding:
                    _send(chat_id, f"❌ {code} 보유 내역 없음")
                    return
                owned = holding.get("qty", 0)
                half_qty = owned // 2
                if half_qty <= 0:
                    _send(chat_id, f"❌ {code} 절반 매도 불가 (보유 {owned}주)")
                    return
                result = execute_sell(code=code, qty=half_qty, price=0, memo="인라인버튼 절반익절")
                if result["success"]:
                    _send(chat_id, f"✅ 절반 매도 완료: {result['name']}({code}) {half_qty:,}주")
                else:
                    _send(chat_id, f"❌ 절반 매도 실패: {result['message']}")
            except Exception as e:
                _send(chat_id, f"❌ 절반 매도 오류: {e}")
            return

        _send(chat_id, f"❓ 알 수 없는 콜백: {data}")
    except Exception as e:
        logger.error("[Bot] 콜백 처리 오류 (%s): %s", data, e)
        _send(chat_id, f"❌ 버튼 처리 오류: {e}")


def _cmd_ai_chat(chat_id: str, text: str) -> None:
    """자유형식 텍스트 → AI 투자 어드바이저 응답."""
    _typing(chat_id)

    # 현재 투자관·전략 컨텍스트 수집
    context_parts = []
    try:
        from services.thesis_service import get_thesis_ceo_summary
        thesis = get_thesis_ceo_summary()
        if thesis:
            context_parts.append(f"[현재 투자관]\n{thesis}")
    except Exception:
        pass

    try:
        from services.strategy_service import get_latest_strategy
        s = get_latest_strategy()
        if s:
            body = (s.get("summary") or s.get("full_text", ""))[:500]
            context_parts.append(f"[현재 주간 전략 요약]\n{body}")
    except Exception:
        pass

    context = "\n\n".join(context_parts)

    system = (
        "당신은 전문 AI 투자 어드바이저입니다.\n"
        "Ray Dalio의 매크로 관점, Charlie Munger의 정성 분석, Warren Buffett의 가치투자 원칙을 바탕으로 "
        "간결하고 실용적인 투자 조언을 한국어로 제공합니다.\n"
        "수치와 근거를 포함하고, 확실하지 않은 사항은 솔직히 인정하세요.\n"
        "답변은 3~5문장으로 핵심만 말하세요."
    )

    if context:
        system += f"\n\n{context}"

    try:
        from clients.openai_client import chat
        reply = chat(system, text, max_tokens=600)
        _send(chat_id, f"🤖 {reply}")
    except Exception as e:
        logger.error("[Bot] AI 대화 오류: %s", e)
        _send(chat_id, "❌ AI 응답 오류가 발생했습니다. 잠시 후 다시 시도해주세요.")


# ── 라우터 ──────────────────────────────────────────────────────

_HANDLERS: dict = {
    "/start":     _cmd_start,
    "/help":      _cmd_help,
    "/research":  _cmd_research,
    "/search":    _cmd_search,
    "/price":     _cmd_price,
    "/balance":   _cmd_balance,
    "/holdings":  _cmd_holdings,
    "/portfolio": _cmd_portfolio,
    "/watchlist": _cmd_watchlist,
    "/buy":       _cmd_buy,
    "/sell":      _cmd_sell,
    "/orders":    _cmd_orders,
    "/cancel":    _cmd_cancel,
    "/history":   _cmd_history,
    "/insight":   _cmd_insight,
    "/thesis":    _cmd_thesis,
    "/strategy":  _cmd_strategy,
    "/nav":       _cmd_nav,
    "/report":    _cmd_report,
    "/tracker":   _cmd_tracker,
    "/auto":      _cmd_auto,
    "/exposure":  _cmd_exposure,
    "/pause":     _cmd_pause,
}


def _dispatch(chat_id: str, text: str) -> None:
    """메시지를 파싱해 적절한 핸들러로 라우팅. 슬래시 명령이 아니면 AI 대화."""
    text = text.strip()
    if not text:
        return

    if text.startswith("/"):
        parts   = text.split(None, 1)
        cmd_raw = parts[0].lower()
        args    = parts[1].strip() if len(parts) > 1 else ""
        cmd     = cmd_raw.split("@")[0]  # "@botname" 접미사 제거

        handler = _HANDLERS.get(cmd)
        if handler:
            try:
                handler(chat_id, args)
            except Exception as e:
                logger.error("[Bot] 핸들러 오류 (%s): %s", cmd, e)
                _send(chat_id, f"❌ 명령 처리 중 오류: {e}")
        else:
            _send(chat_id, f"❓ 알 수 없는 명령어: `{cmd}`\n`/help`로 명령어를 확인하세요.")
    else:
        # 자유형식 텍스트 → AI 투자 어드바이저
        try:
            _cmd_ai_chat(chat_id, text)
        except Exception as e:
            logger.error("[Bot] AI 대화 오류: %s", e)
            _send(chat_id, f"❌ 오류: {e}")


# ── Long-polling 루프 ──────────────────────────────────────────

def run_bot(allowed_chats: set[str] | None = None) -> None:
    """텔레그램 봇 long-polling 루프. 블로킹 함수."""
    allowed = allowed_chats or _ALLOWED
    offset  = 0
    logger.info("[Bot] 텔레그램 봇 시작 (허용 채팅: %s)", allowed or "전체")
    _RUNNING.set()

    # 시작 알림
    for cid in allowed:
        _send(cid,
              "🤖 *AI 투자 어시스턴트 봇 시작*\n"
              "`/help` 로 명령어를 확인하세요.")

    while _RUNNING.is_set():
        try:
            r = requests.get(
                f"{_BASE}/getUpdates",
                params={
                    "offset": offset, "timeout": 30,
                    "allowed_updates": ["message", "callback_query"],
                },
                timeout=35,
            )
            if not r.ok:
                logger.warning("[Bot] getUpdates 실패: %s", r.status_code)
                time.sleep(5)
                continue

            updates = r.json().get("result", [])
            for upd in updates:
                offset = upd["update_id"] + 1

                # ── 일반 메시지 처리 ─────────────────────────────────
                msg = upd.get("message", {})
                if msg:
                    chat_id = str(msg.get("chat", {}).get("id", ""))
                    text    = msg.get("text", "")
                    if chat_id and text:
                        if allowed and chat_id not in allowed:
                            logger.debug("[Bot] 차단된 채팅 무시: %s", chat_id)
                        else:
                            now_str = datetime.now(_KST).strftime("%H:%M")
                            logger.info("[Bot] [%s] %s → %s", now_str, chat_id, text[:60])
                            t = threading.Thread(
                                target=_dispatch,
                                args=(chat_id, text),
                                daemon=True,
                            )
                            t.start()
                    continue

                # ── 인라인 버튼 콜백 처리 ────────────────────────────
                cbq = upd.get("callback_query", {})
                if cbq:
                    cbq_id  = cbq.get("id", "")
                    cdata   = cbq.get("data", "")
                    from_ch = cbq.get("message", {}).get("chat", {})
                    chat_id = str(from_ch.get("id", "") or cbq.get("from", {}).get("id", ""))
                    if chat_id and cdata:
                        if allowed and chat_id not in allowed:
                            logger.debug("[Bot] 차단된 콜백 무시: %s", chat_id)
                        else:
                            now_str = datetime.now(_KST).strftime("%H:%M")
                            logger.info("[Bot] [콜백] [%s] %s → %s", now_str, chat_id, cdata[:60])
                            t = threading.Thread(
                                target=_handle_callback,
                                args=(chat_id, cdata, cbq_id),
                                daemon=True,
                            )
                            t.start()

        except requests.exceptions.Timeout:
            pass  # long-polling timeout — 정상
        except Exception as e:
            logger.error("[Bot] 루프 오류: %s", e)
            time.sleep(5)


def stop_bot() -> None:
    """봇 루프 종료 신호."""
    _RUNNING.clear()
    logger.info("[Bot] 봇 종료 신호 전송")
