"""자문 판단용 코드 계산 블록 모음 — 브리핑(CEO)과 텔레그램 봇이 같은 근거를 쓰도록 한 곳에서 조립.

각 블록은 독립적으로 try/except 처리한다(하나가 실패해도 나머지는 들어간다 — 전부-아니면-전무 방지).
무거운 계산(가격 이력·적정가치)은 6시간 캐시. 모든 수치는 코드 계산값이고 LLM은 서술만 한다.
"""
import logging
import time

logger = logging.getLogger(__name__)
_CACHE: dict[str, tuple[float, object]] = {}
_TTL = 6 * 3600


def _cached(key: str, fn, ttl: int = _TTL):
    now = time.time()
    hit = _CACHE.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    val = fn()
    if val:
        _CACHE[key] = (now, val)
    return val


def _safe(name: str, fn):
    try:
        return fn()
    except Exception as e:
        logger.warning("[자문컨텍스트] %s 실패 (건너뜀): %s", name, e)
        return None


def freshness_block(raw: dict) -> tuple[str, list[str]]:
    from services.data_freshness import audit
    stale = audit(raw)
    if not stale:
        return "", []
    txt = ("[⚠️ 데이터 신선도 경고 — 아래 지표는 직전 거래일보다 오래된 값. 이 수치로 '어제/전일' 서술 금지, 불확실함을 명시]\n"
           + "\n".join(f"  - {s}" for s in stale))
    return txt, stale


def build(raw: dict, kr_rt: dict, run_type: str, kis=None) -> dict:
    """반환: {blocks: [str], stale: [str], mode: {...}, flows: {...}, kospi_chg: float|None}"""
    blocks: list[str] = []
    kospi_chg = ((kr_rt or {}).get("kospi") or {}).get("change_pct")

    fresh = _safe("신선도", lambda: freshness_block(raw)) or ("", [])
    if fresh[0]:
        blocks.append(fresh[0])

    flows = _safe("수급", lambda: __import__("services.investor_flow", fromlist=["x"]).collect()) or {}
    if flows:
        from services.investor_flow import summarize
        t = _safe("수급요약", lambda: summarize(flows))
        if t:
            blocks.append(t)

    positions = _safe("보유", lambda: __import__("services.portfolio_risk", fromlist=["x"]).load_positions(kis)) or []

    if positions:
        from services import portfolio_risk
        t = _safe("계좌위험", lambda: _cached("risk", lambda: portfolio_risk.format_report(
            portfolio_risk.build_report(kis)), ttl=3600))
        if t:
            blocks.append(t)
        from services import account_perf
        t = _safe("계좌성과", lambda: _cached("perf", lambda: account_perf.build(kis), ttl=3600))
        if t:
            blocks.append(t)
        from services import fair_value
        t = _safe("가치위치", lambda: _cached("fair", lambda: fair_value.build(positions)))
        if t:
            blocks.append(t)

    violations, conc_alerts = [], []
    if positions:
        from services import concentration_rule
        violations = _safe("집중도", lambda: concentration_rule.evaluate(positions)) or []
        conc_alerts = [concentration_rule.format_alert(v) for v in violations]
        if conc_alerts:
            blocks.append(
                "[⚠️ 집중도 규칙 위반 — 코드 계산. 🧭 '선택'의 A안은 반드시 아래 분할 축소안으로 하고(수량·일수는 이 숫자만 인용), "
                "위반 종목의 추가 매수·신규 편입 권고는 금지]\n" + "\n".join(conc_alerts))

    from services import event_calendar
    earnings = _safe("실적일", lambda: _cached("earn", lambda: event_calendar.fetch_earnings(positions), ttl=12 * 3600)) or {}
    evs = _safe("일정", lambda: event_calendar.upcoming(earnings=earnings)) or []
    t = event_calendar.format_upcoming(evs) if evs else ""
    if t:
        blocks.append(t)

    from services import playbook
    blocks.append(playbook.context_block(kospi_chg))

    from services import calibration, decision_scoring
    t = _safe("확신도보정", lambda: _cached("calib", lambda: calibration.format_for_prompt(decision_scoring.score_all())))
    if t:
        blocks.append(t)

    from services import user_decisions
    t = _safe("내결정", lambda: user_decisions.build_context())
    if t:
        blocks.append(t)

    from services import market_ideas
    t = _safe("시장아이디어", lambda: _cached(
        "ideas", lambda: market_ideas.build_block({p["code"] for p in positions}), ttl=1800))
    if t:
        blocks.append(t)

    from services import sector_outlook
    t = _safe("업종전망", lambda: _cached("sector_outlook", sector_outlook.context_block, ttl=1800))
    if t:
        blocks.append(t)

    from services import briefing_mode
    pl = {p["name"]:(p["price"] / p["avg"] - 1) * 100 for p in positions if p.get("avg")}
    mode = briefing_mode.decide(kospi_chg, holding_pl=pl, events=evs)
    blocks.append(briefing_mode.context_line(mode))

    return {"blocks": blocks, "stale": fresh[1], "mode": mode, "flows": flows, "kospi_chg": kospi_chg,
            "concentration": conc_alerts, "violations": violations}
