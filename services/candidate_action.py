"""발굴 후보 → '내 계좌 기준 행동안' (전부 코드 계산, LLM 없음) (2026-10-23).

배경: 후보 목록은 '볼 만한 종목'에서 멈춰 있었다. 사용자가 원하는 건 "그래서 내 계좌에서 얼마를, 어떻게, 언제 틀렸다고 볼지"다.
규칙은 CEO 헌장·집중도 규칙과 같은 기준을 쓴다:
- 검증된(✔) 근거 묶음 수로 비중 밴드 결정: 0개=관찰만(0%), 1개=하(2%), 2개+=중(3%) — 확신도별 밴드(하 1~3%·중 3~5%)의 보수적 하단
- 손절선 -15%(헌장의 재검토 의무 기준), 2회 분할 진입
- 보류 사유: 최근 악재 공시 / 이미 보유한 업종과 겹침 / 업종 전망이 비우호 / 금융업이라 검증 밖
- 집중도 상한을 넘는 보유 종목이 있으면 그 감축분 재투자를 자금원으로 제시
제안일 뿐 자동 매매하지 않는다.
"""
import logging
import math

logger = logging.getLogger(__name__)

BAND = {0: 0.0, 1: 2.0, 2: 3.0}        # 검증 묶음 수 → 계좌 투자금 대비 %
STOP_PCT = 15.0
TRANCHES = 2


def _won(x: float) -> str:
    return f"{x / 10000:,.0f}만원"


def decide(c: dict, ctx: dict) -> dict:
    """c: kr_picks 후보(+ 'industry','ind_name','bad_event'), ctx: total(투자금), held_inds(set), views(dict 업종번호→전망), fin(set 코드), funding(str|None)."""
    from services.universe_screener import validated_groups_in
    v = min(validated_groups_in(c.get("signals")), 2)
    reasons = []
    if c.get("bad_event"):
        reasons.append(f"최근 악재 공시({c['bad_event']})")
    if c["code"] in ctx["fin"]:
        reasons.append("금융업이라 과거 검증 밖")
    if c.get("industry") and c["industry"] in ctx["held_inds"]:
        reasons.append("이미 보유한 업종과 겹침")
    if ctx["views"].get(c.get("industry")) == "비우호":
        reasons.append("업종 전망이 시장보다 못할 것으로 기록됨")
    if v == 0:
        reasons.append("과거 검증을 통과한 근거가 없음")
    pct = BAND[v] if not reasons else 0.0
    out = {"code": c["code"], "name": c["name"], "pct": pct, "hold_reasons": reasons, "v": v}
    if pct and ctx["total"] > 0 and c["close"] > 0:
        amt = ctx["total"] * pct / 100
        qty = math.floor(amt / c["close"])
        if qty >= TRANCHES:
            out.update(amount=qty * c["close"], qty=qty, first=qty // TRANCHES, stop=c["close"] * (1 - STOP_PCT / 100))
        else:
            out["pct"] = 0.0
            out["hold_reasons"] = reasons + ["투자금 대비 1주 가격이 커서 분할이 어려움"]
    return out


def format_line(d: dict, ctx: dict) -> str:
    if d["pct"]:
        s = (f"· {d['name']}({d['code']}): 소액 분할 후보 — 계좌 투자금의 약 {d['pct']:.0f}%({_won(d['amount'])}, {d['qty']}주)를 "
             f"{TRANCHES}회로 나눠 첫 {d['first']}주부터. 틀렸다고 볼 선: {d['stop']:,.0f}원(-{STOP_PCT:.0f}%).")
        if ctx.get("funding"):
            s += f" 자금: {ctx['funding']}"
        return s
    return f"· {d['name']}({d['code']}): 지금은 관찰만 — " + " · ".join(d["hold_reasons"])


def _ctx(positions: list[dict]) -> dict:
    from services import concentration_rule as cr
    from services.briefing_sections import _financial_codes
    from services.sector_outlook import latest_views
    from services.market_ideas import _query
    held = [p["code"] for p in positions]
    held_inds = set()
    if held:
        d = _query("SELECT MAX(date) FROM universe_screen_daily WHERE industry IS NOT NULL AND industry<>''")[0][0]
        if d:
            rows = _query("SELECT code, industry FROM universe_screen_daily WHERE date=:d", {"d": d})
            held_inds = {i for c, i in rows if c in set(held) and i}
    funding = None
    try:
        viol = [x for x in cr.evaluate(positions) if x["kind"] == "stock" and x["plan"]]
        if viol:
            v0 = viol[0]
            funding = f"{v0['name']} 비중이 상한을 넘어 줄이는 분량({_won(v0['plan']['sell_reinvest_won'])})을 옮기는 방식"
    except Exception as e:
        logger.debug("[후보행동안] 집중도 조회 실패: %s", e)
    return {"total": sum(p["value"] for p in positions), "held_inds": held_inds, "views": latest_views(),
            "fin": _financial_codes(), "funding": funding}


def build_lines(exclude: set[str], positions: list[dict] | None = None, n: int = 3) -> list[str]:
    """브리핑 섹션 줄. 계좌 정보를 못 얻으면 빈 목록(없는 숫자를 만들지 않는다)."""
    from services import market_ideas as mi
    from services.disclosure_events import by_code
    if positions is None:
        from services.portfolio_risk import load_positions
        positions = load_positions()
    if not positions or sum(p["value"] for p in positions) <= 0:
        return []
    _, picks, _ = mi.kr_picks(exclude, n=n)
    if not picks:
        return []
    ctx = _ctx(positions)
    evs = by_code(days=7)
    ind = {c: (i or None) for c, i in mi._query(
        "SELECT code, industry FROM universe_screen_daily WHERE date=(SELECT MAX(date) FROM universe_screen_daily)")}
    lines = []
    for p in picks:
        p["industry"] = ind.get(p["code"])
        bad = [e for e in evs.get(p["code"], []) if e["polarity"] == "악재"][:1]
        p["bad_event"] = bad[0]["event_type"] if bad else None
        lines.append(format_line(decide(p, ctx), ctx))
    return lines
