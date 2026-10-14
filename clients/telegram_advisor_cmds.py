"""자문 판단 보조 명령 (2026-10-14): /risk /perf /whatif /calendar /playbook /did /ledger.

모든 계산은 services/ 의 코드 계산 모듈이 하고 여기서는 입출력만 한다.
`send` 는 telegram_bot._send 를 주입받는다(순환 import 방지).
"""
import logging

logger = logging.getLogger(__name__)

_PENDING_LEDGER: dict[str, dict] = {}


def _kis():
    try:
        from clients.kis_client import KISClient
        return KISClient()
    except Exception:
        return None


def cmd_risk(chat_id: str, _args: str, send) -> None:
    from services import portfolio_risk
    rep = portfolio_risk.build_report(_kis())
    send(chat_id, portfolio_risk.format_report(rep) or "보유 종목이 없어 계산할 수 없습니다.")


def cmd_perf(chat_id: str, _args: str, send) -> None:
    from services import account_perf
    send(chat_id, account_perf.build(_kis()) or "보유 종목이 없어 계산할 수 없습니다.")


def cmd_calendar(chat_id: str, _args: str, send) -> None:
    from services import event_calendar
    from services.portfolio_service import get_portfolio
    earn = event_calendar.fetch_earnings(get_portfolio())
    txt = event_calendar.format_upcoming(event_calendar.upcoming(days=14, earnings=earn))
    send(chat_id, txt or "앞으로 14일 내 확정된 주요 일정이 없습니다.")


def cmd_playbook(chat_id: str, args: str, send) -> None:
    """/playbook → 전체 보기 | /playbook shock_down 규칙1 ; 규칙2 → 확정 저장"""
    from services import playbook
    parts = args.split(None, 1)
    if not parts:
        send(chat_id, "📘 *국면별 행동 플레이북*\n\n" + playbook.format_all() +
             "\n\n수정: `/playbook shock_down 규칙1 ; 규칙2 ; 규칙3`\n국면 이름: " + ", ".join(playbook.REGIMES))
        return
    regime = parts[0]
    if regime not in playbook.REGIMES or len(parts) < 2:
        send(chat_id, "❌ 사용법: `/playbook shock_down 규칙1 ; 규칙2`\n국면: " + ", ".join(playbook.REGIMES))
        return
    rules = [r.strip() for r in parts[1].split(";") if r.strip()]
    ok = playbook.set_rules(regime, rules)
    send(chat_id, f"✅ {playbook.LABEL[regime]} 규칙 {len(rules)}개 확정 저장" if ok else "❌ 저장 실패")


def cmd_whatif(chat_id: str, args: str, send) -> None:
    """/whatif 종목명또는코드 매도% — 예: /whatif SK하이닉스 20"""
    from services import portfolio_risk, scenario_engine
    parts = args.split()
    if len(parts) < 2:
        send(chat_id, "❌ 사용법: `/whatif 종목명|코드 매도%`\n예: `/whatif SK하이닉스 20` (20%를 판다고 가정)")
        return
    try:
        pct = float(parts[-1].replace("%", ""))
    except ValueError:
        send(chat_id, "❌ 마지막은 매도 비율(숫자)이어야 합니다. 예: `/whatif SK하이닉스 20`")
        return
    key = " ".join(parts[:-1]).strip()
    positions = portfolio_risk.load_positions(_kis())
    p = next((x for x in positions if x["code"] == key or key.lower() in x["name"].lower()), None)
    if not p:
        send(chat_id, f"❌ 보유 종목에서 '{key}'를 찾지 못했습니다.")
        return
    rets = portfolio_risk.fetch_returns([p["code"]])
    sigma = scenario_engine.month_sigma(rets.get(p["code"]))
    total = sum(x["value"] for x in positions)
    is_etf = (p.get("sector") == "ETF")
    r = scenario_engine.what_if_sell(p, pct, sigma=sigma, account_total=total, is_etf=is_etf)
    send(chat_id, scenario_engine.format_what_if(r))


def cmd_did(chat_id: str, args: str, send) -> None:
    """/did 코드 따름|무시|일부 [이유]"""
    from services import user_decisions
    parts = args.split(None, 2)
    if len(parts) < 2:
        send(chat_id, "❌ 사용법: `/did 코드 따름|무시|일부 [이유]`\n예: `/did 000660 무시 반등 기다려보기로`")
        return
    code = parts[0].zfill(6) if parts[0].isdigit() else parts[0]
    ok = user_decisions.record(code, parts[1], parts[2] if len(parts) > 2 else "")
    send(chat_id, "✅ 결정 기록 완료 — 나중에 '따랐을 때 vs 무시했을 때'를 비교해 드립니다." if ok
         else "❌ 기록 실패 — 두 번째 값은 따름/무시/일부 중 하나여야 합니다.")


def propose_ledger(chat_id: str, text: str, send) -> bool:
    """체결 문자로 보이면 원장 갱신안을 제시하고 True. 아니면 False(다른 처리로 넘김)."""
    from services import fill_parser
    from services.portfolio_service import get_portfolio
    holdings = get_portfolio()
    fill = fill_parser.parse(text, holdings)
    if not fill["ok"]:
        return False
    pl = fill_parser.plan(fill, holdings)
    if not pl["ok"]:
        send(chat_id, f"❌ {pl['reason']}")
        return True
    notes = _behavior_notes(fill, holdings)
    _PENDING_LEDGER[chat_id] = {"fill": fill, "plan": pl}
    side = "매수" if fill["side"] == "buy" else "매도"
    msg = (f"📒 원장 갱신안: *{fill['name']}* {fill['qty']:,}주 {side} @{fill['price']:,.0f}원\n"
           f"  보유 {pl['old_q']:,}주 @{pl['old_avg']:,.0f} → {pl['new_q']:,}주 @{pl['new_avg']:,.0f}"
           f"{' (전량 정리)' if pl['close'] else ''}\n")
    if notes:
        msg += "\n" + "\n".join(notes) + "\n"
    msg += "\n맞으면 `/ledger ok`, 아니면 `/ledger no` (자동 반영하지 않습니다)"
    send(chat_id, msg)
    return True


def _behavior_notes(fill: dict, holdings: list[dict]) -> list[str]:
    try:
        from services import behavior_check
        cur = next((h for h in holdings if h["code"] == fill["code"]), None)
        pos = {"name": cur["name"], "qty": cur["quantity"], "avg": cur["avg_price"]} if cur else None
        if fill["side"] == "buy":
            return behavior_check.check_buy(pos, fill["price"], fill["qty"])
        kospi = None
        try:
            from clients.market_data_client import fetch_kr_index_realtime
            kospi = (fetch_kr_index_realtime().get("kospi") or {}).get("change_pct")
        except Exception:
            pass
        sell_all = bool(cur and fill["qty"] >= cur["quantity"])
        return behavior_check.check_sell(pos, fill["price"], market_chg=kospi, sell_all=sell_all)
    except Exception as e:
        logger.debug("[Bot] 습관 점검 실패: %s", e)
        return []


def cmd_ledger(chat_id: str, args: str, send) -> None:
    """/ledger ok | no | (본문: 체결 문자 붙여넣기)"""
    sub = args.strip().lower()
    if sub in ("ok", "no"):
        pend = _PENDING_LEDGER.pop(chat_id, None)
        if not pend:
            send(chat_id, "대기 중인 원장 갱신안이 없습니다.")
            return
        if sub == "no":
            send(chat_id, "취소했습니다. 원장은 그대로입니다.")
            return
        f = pend["fill"]
        from services.portfolio_service import add_position, close_position
        from services.profile_service import confirm_holdings
        try:
            if f["side"] == "buy":
                add_position(f["code"], f["name"], f["qty"], f["price"], timeframe="mid")
            else:
                close_position(f["code"], exit_price=f["price"],
                               partial_qty=None if pend["plan"]["close"] else f["qty"])
            confirm_holdings()
            send(chat_id, f"✅ 원장 반영: {f['name']} {'매수' if f['side']=='buy' else '매도'} {f['qty']:,}주")
        except Exception as e:
            logger.error("[Bot] 원장 반영 오류: %s", e)
            send(chat_id, f"❌ 반영 실패: {e}")
        return
    if not args.strip() or not propose_ledger(chat_id, args, send):
        send(chat_id, "체결 문자를 `/ledger 삼성전자 10주 매수 체결 72,000원` 처럼 보내주세요 "
                      "(종목명이 보유 종목과 같거나 6자리 코드를 포함해야 합니다).")
