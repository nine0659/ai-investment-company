"""
텔레그램 대화형 봇 — long-polling 기반
사용자가 텔레그램에서 명령을 입력하면 실시간으로 응답한다.

지원 명령어:
  /research [종목코드 또는 회사명]  — 기업 종합 투자 분석
  /search [검색어]                  — 회사명 검색 (코드 모를 때)
  /price [종목코드]                 — 현재가·밸류에이션 조회
  /balance                          — 계좌 잔고·보유종목 조회
  /holdings                         — 보유 종목 현황
  /watchlist                        — 관심종목 목록
  /help                             — 명령어 안내
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
        "`/research [코드 또는 회사명]`\n"
        "  → 기업 종합 투자 분석 리포트\n"
        "  예: `/research 005930` 또는 `/research 삼성전자`\n\n"
        "`/search [검색어]`\n"
        "  → 회사명으로 종목코드 검색\n"
        "  예: `/search 삼성`\n\n"
        "`/price [종목코드]`\n"
        "  → 현재가·PER·PBR·시가총액 조회\n"
        "  예: `/price 005930`\n\n"
        "`/balance`\n"
        "  → 계좌 예수금 + 보유종목 현황\n\n"
        "`/holdings`\n"
        "  → 보유종목 수익률 현황\n\n"
        "`/watchlist`\n"
        "  → 관심종목 목록\n\n"
        "`/help`\n"
        "  → 이 도움말"
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


# ── 라우터 ──────────────────────────────────────────────────────

_HANDLERS: dict = {
    "/help":      _cmd_help,
    "/research":  _cmd_research,
    "/search":    _cmd_search,
    "/price":     _cmd_price,
    "/balance":   _cmd_balance,
    "/holdings":  _cmd_holdings,
    "/watchlist": _cmd_watchlist,
}


def _dispatch(chat_id: str, text: str) -> None:
    """메시지를 파싱해 적절한 핸들러로 라우팅."""
    text = text.strip()
    if not text.startswith("/"):
        # 일반 텍스트 무시 (또는 도움말 안내)
        return

    parts    = text.split(None, 1)
    cmd_raw  = parts[0].lower()
    args     = parts[1].strip() if len(parts) > 1 else ""

    # "@botname" 접미사 제거 (그룹 채팅 대응)
    cmd = cmd_raw.split("@")[0]

    handler = _HANDLERS.get(cmd)
    if handler:
        try:
            handler(chat_id, args)
        except Exception as e:
            logger.error("[Bot] 핸들러 오류 (%s): %s", cmd, e)
            _send(chat_id, f"❌ 명령 처리 중 오류: {e}")
    else:
        _send(chat_id, f"❓ 알 수 없는 명령어: `{cmd}`\n`/help`로 명령어를 확인하세요.")


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
