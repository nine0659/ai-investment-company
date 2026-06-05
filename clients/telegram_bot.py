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
  /buy CODE QTY [PRICE]             — 매수 주문
  /sell CODE QTY [PRICE]            — 매도 주문
  /orders                           — 미체결 주문 조회
  /cancel ORDER_NO CODE SIDE QTY    — 주문 취소
  /history                          — 최근 주문 이력
  /thesis                           — 현재 월간 투자관
  /strategy                         — 현재 주간 전략
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
    chunks = [text[i:i + max_len] for i in range(0, len(text), max_len)]
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
        "📜 *전략·투자관*\n"
        "`/thesis` — 현재 월간 투자관\n"
        "`/strategy` — 현재 주간 전략\n"
        "`/watchlist` — 관심종목 목록\n\n"
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


def _cmd_watchlist(chat_id: str, _args: str) -> None:
    try:
        from services.watchlist_service import get_watchlist
        items = get_watchlist()
        if not items:
            _send(chat_id, "📋 관심종목이 없습니다.\n`python main.py --watchlist add CODE NAME`으로 추가하세요.")
            return
        lines = ["📋 *관심종목*\n"]
        for w in items:
            target_str = f" → 목표 {w['target_price']:,}원" if w.get("target_price") else ""
            lines.append(
                f"• {w['name']}({w['code']}){target_str}\n"
                f"  _{w.get('reason', '')}_ ({w.get('timeframe', '')})"
            )
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
    "/thesis":    _cmd_thesis,
    "/strategy":  _cmd_strategy,
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
                params={"offset": offset, "timeout": 30, "allowed_updates": ["message"]},
                timeout=35,
            )
            if not r.ok:
                logger.warning("[Bot] getUpdates 실패: %s", r.status_code)
                time.sleep(5)
                continue

            updates = r.json().get("result", [])
            for upd in updates:
                offset = upd["update_id"] + 1
                msg    = upd.get("message", {})
                if not msg:
                    continue

                chat_id = str(msg.get("chat", {}).get("id", ""))
                text    = msg.get("text", "")

                if not chat_id or not text:
                    continue

                # 허용된 채팅만 처리
                if allowed and chat_id not in allowed:
                    logger.debug("[Bot] 차단된 채팅 무시: %s", chat_id)
                    continue

                now_str = datetime.now(_KST).strftime("%H:%M")
                logger.info("[Bot] [%s] %s → %s", now_str, chat_id, text[:60])

                # 별도 스레드로 처리 (긴 리서치도 봇 응답 차단 없음)
                t = threading.Thread(
                    target=_dispatch,
                    args=(chat_id, text),
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
