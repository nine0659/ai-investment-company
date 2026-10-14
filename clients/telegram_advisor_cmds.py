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


def _applied(h: str) -> bool:
    from sqlalchemy import text
    from db.database import get_conn
    try:
        with get_conn() as conn:
            return conn.execute(text("SELECT 1 FROM system_settings WHERE key=:k"),
                                {"k": f"ledger.applied.{h}"}).fetchone() is not None
    except Exception:
        return False


def _mark_applied(h: str) -> None:
    from sqlalchemy import text
    from db.database import get_conn
    try:
        with get_conn() as conn:
            conn.execute(text("DELETE FROM system_settings WHERE key=:k"), {"k": f"ledger.applied.{h}"})
            conn.execute(text("INSERT INTO system_settings (key, value) VALUES (:k, '1')"),
                         {"k": f"ledger.applied.{h}"})
    except Exception as e:
        logger.debug("[Bot] 반영 이력 기록 실패: %s", e)


def propose_fills(chat_id: str, fills: list[dict], send, source: str = "문자") -> bool:
    """검증된 체결 목록으로 원장 갱신안을 제시(여러 건 가능). 하나도 쓸 수 없으면 False."""
    from services import fill_parser, fill_ocr
    from services.portfolio_service import get_portfolio
    real = get_portfolio()
    work = {h["code"]: dict(h) for h in real}      # 같은 종목 여러 건은 순서대로 누적 반영해 계획
    items, lines, skipped = [], [], []
    for f in fills:
        if not f.get("ok", True):
            skipped.append(f"{f.get('name') or '?'}: {f.get('reason')}")
            continue
        base = [work[f["code"]]] if f["code"] in work else []
        pl = fill_parser.plan(f, base)
        if not pl["ok"]:
            skipped.append(f"{f['name']}: {pl['reason']}")
            continue
        warns = list(f.get("warnings", []))
        h = fill_ocr.fill_hash(f)
        if _applied(h):
            warns.append("이미 반영한 체결과 같은 내용입니다(중복 입력 주의)")
        warns += _behavior_notes(f, real)
        items.append({"fill": f, "plan": pl, "hash": h})
        work[f["code"]] = {"code": f["code"], "name": f["name"], "quantity": pl["new_q"], "avg_price": pl["new_avg"]}
        side = "매수" if f["side"] == "buy" else "매도"
        extra = "".join("\n   " + w for w in warns)
        lines.append(f"• *{f['name']}* {f['qty']:,}주 {side} @{f['price']:,.0f}원\n"
                     f"   {pl['old_q']:,}주 @{pl['old_avg']:,.0f} → {pl['new_q']:,}주 @{pl['new_avg']:,.0f}"
                     f"{' (전량 정리)' if pl['close'] else ''}" + extra)
    if not items:
        if skipped:
            send(chat_id, "❌ 읽은 체결을 원장에 반영할 수 없습니다:\n" + "\n".join("- " + x for x in skipped))
            return True
        return False
    _PENDING_LEDGER[chat_id] = {"items": items}
    msg = f"📒 원장 갱신안 ({source} {len(items)}건)\n\n" + "\n".join(lines)
    if skipped:
        msg += "\n\n⚠️ 제외된 항목:\n" + "\n".join("- " + x for x in skipped)
    msg += "\n\n맞으면 `/ledger ok`, 아니면 `/ledger no` (확인 전에는 원장을 바꾸지 않습니다)"
    send(chat_id, msg)
    return True


def propose_ledger(chat_id: str, text: str, send) -> bool:
    """체결 문자로 보이면 원장 갱신안을 제시하고 True. 아니면 False(다른 처리로 넘김)."""
    from services import fill_parser
    from services.portfolio_service import get_portfolio
    fill = fill_parser.parse(text, get_portfolio())
    if not fill["ok"]:
        return False
    fill["warnings"] = []
    return propose_fills(chat_id, [fill], send, source="문자")


def propose_from_image(chat_id: str, image_bytes: bytes, mime: str, send) -> None:
    """체결내역 캡처 이미지 → 읽기 → 갱신안. 실패하면 이유와 대안을 안내."""
    from services import fill_ocr
    from services.portfolio_service import get_portfolio
    res = fill_ocr.extract(image_bytes, mime, get_portfolio())
    if not res["ok"]:
        send(chat_id, f"❌ {res['error']}\n체결 문자를 `/ledger 삼성전자 10주 매수 체결 72,000원` 형식으로 보내거나, "
                      f"캡처를 '파일'로 보내면(압축 안 됨) 더 잘 읽습니다.")
        return
    if not propose_fills(chat_id, res["fills"], send, source="캡처"):
        send(chat_id, "❌ 캡처에서 체결 건을 찾지 못했습니다.")


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


def cmd_more(chat_id: str, _args: str, send) -> None:
    from services.briefing_facts import load_more
    m = load_more()
    if not m:
        send(chat_id, "보관된 상세 근거가 없습니다.")
        return
    send(chat_id, f"📎 {m.get('date', '')} {m.get('run_type', '')} 상세 근거\n\n{m['more']}")


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
        from services.portfolio_service import add_position, close_position, get_portfolio
        from services.profile_service import confirm_holdings
        done, failed = [], []
        for it in pend["items"]:
            f = it["fill"]
            try:
                if f["side"] == "buy":
                    add_position(f["code"], f["name"], f["qty"], f["price"], timeframe="mid")
                else:
                    cur = next((h for h in get_portfolio() if h["code"] == f["code"]), None)
                    whole = bool(cur and f["qty"] >= cur["quantity"])
                    close_position(f["code"], exit_price=f["price"], partial_qty=None if whole else f["qty"])
                _mark_applied(it["hash"])
                done.append(f"{f['name']} {'매수' if f['side'] == 'buy' else '매도'} {f['qty']:,}주")
            except Exception as e:
                logger.error("[Bot] 원장 반영 오류: %s", e)
                failed.append(f"{f['name']}: {e}")
        if done:
            confirm_holdings()
        out = ("✅ 원장 반영: " + ", ".join(done)) if done else ""
        if failed:
            out += "\n❌ 실패: " + "; ".join(failed)
        send(chat_id, out)
        return
    if not args.strip() or not propose_ledger(chat_id, args, send):
        send(chat_id, "체결 문자를 `/ledger 삼성전자 10주 매수 체결 72,000원` 처럼 보내거나, "
                      "체결내역 캡처 이미지를 그냥 보내주세요(문자는 종목명이 보유 종목과 같거나 6자리 코드가 있어야 읽힙니다).")
