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
CRASH_TRANCHES = 3          # 급락 직후엔 1/3씩 3회(pick_gate와 같은 기준)


def _won(x: float) -> str:
    return f"{x / 10000:,.0f}만원"


REV_WARN_PCT = -15.0        # 내년·올해 EPS 추정이 90일간 이 이상 깎였으면 경고(검증된 신호는 아님 — 위험 표시용)
_REV_CACHE: dict = {}


def revision_90d(code: str, market: str | None) -> float | None:
    """yfinance 이익 추정의 90일 변화율(%) 중 더 나쁜 쪽(내년·올해). 조회 실패는 None(경고 없이 진행). 6시간 캐시."""
    import time
    hit = _REV_CACHE.get(code)
    if hit and time.time() - hit[0] < 6 * 3600:
        return hit[1]
    val = None
    try:
        from services.stock_thesis import fetch_estimates, revision_view
        e = fetch_estimates(code + (".KQ" if (market or "").upper().startswith("KOSDAQ") else ".KS"))
        vs = [revision_view(e["trend"][k]).get("d90") for k in ("+1y", "0y") if e.get("trend", {}).get(k)]
        vs = [v for v in vs if v is not None]
        val = min(vs) if vs else None
    except Exception as ex:
        logger.debug("[후보행동안] %s 이익 추정 조회 실패: %s", code, ex)
    _REV_CACHE[code] = (time.time(), val)
    return val


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
    if c.get("rev90") is not None and c["rev90"] <= REV_WARN_PCT:
        reasons.append(f"증권사 이익 추정이 최근 90일 {c['rev90']:.0f}% 하향(검증 전 경고)")
    pct = BAND[v] if not reasons else 0.0
    out = {"code": c["code"], "name": c["name"], "pct": pct, "hold_reasons": reasons, "v": v}
    if pct and ctx["total"] > 0 and c["close"] > 0:
        gate = ctx.get("gate") or {}
        tr = CRASH_TRANCHES if gate.get("mode") == "crash" else TRANCHES
        amt = ctx["total"] * pct / 100
        qty = math.floor(amt / c["close"])
        if qty >= tr:
            out.update(amount=qty * c["close"], qty=qty, first=qty // tr, tranches=tr, stop=c["close"] * (1 - STOP_PCT / 100))
        else:
            out["pct"] = 0.0
            out["hold_reasons"] = reasons + ["투자금 대비 1주 가격이 커서 분할이 어려움"]
    return out


def format_line(d: dict, ctx: dict) -> str:
    if d["pct"]:
        s = (f"· {d['name']}({d['code']}): 소액 분할 후보 — 계좌 투자금의 약 {d['pct']:.0f}%({_won(d['amount'])}, {d['qty']}주)를 "
             f"{d['tranches']}회로 나눠 첫 {d['first']}주부터. 틀렸다고 볼 선: {d['stop']:,.0f}원(-{STOP_PCT:.0f}%).")
        if ctx.get("funding"):
            s += f" 자금: {ctx['funding']}"
        if (ctx.get("gate") or {}).get("mode") in ("crash", "heated"):
            s += f" ⚠시장 상태: {ctx['gate']['reason']} — {ctx['gate']['note']}"
        return s
    return f"· {d['name']}({d['code']}): 지금은 관찰만 — " + " · ".join(d["hold_reasons"])


HORIZON = 20
MIN_TRUSTED = 30


def record(date: str, ds: list[dict]) -> None:
    """오늘 행동안을 (날짜, 종목) 단위로 덮어써 기록 — 하루에 여러 번 불려도 중복되지 않는다."""
    from sqlalchemy import text
    from db.database import get_conn
    with get_conn() as conn:
        for d in ds:
            conn.execute(text("DELETE FROM candidate_log WHERE date=:d AND code=:c"), {"d": date, "c": d["code"]})
            conn.execute(text("INSERT INTO candidate_log (date, code, name, action, pct, close, reasons) "
                              "VALUES (:d,:c,:n,:a,:p,:cl,:r)"),
                         {"d": date, "c": d["code"], "n": d["name"], "a": "buy" if d["pct"] else "watch", "p": d["pct"],
                          "cl": d.get("close"), "r": " / ".join(d["hold_reasons"])[:300]})


def score_pending() -> int:
    """HORIZON 거래일이 지난 미채점 기록을 '종목 수익률 - 전 종목 중앙값'으로 채점. 채점 건수 반환."""
    import statistics
    from sqlalchemy import text
    from db.database import get_conn
    with get_conn() as conn:
        dates = [r[0] for r in conn.execute(text("SELECT DISTINCT date FROM universe_screen_daily ORDER BY date")).fetchall()]
        pend = conn.execute(text("SELECT id, date, code FROM candidate_log WHERE rel_ret IS NULL")).fetchall()
    n = 0
    for pid, d, code in pend:
        if d not in dates or dates.index(d) + HORIZON >= len(dates):
            continue
        d2 = dates[dates.index(d) + HORIZON]
        with get_conn() as conn:
            rows = conn.execute(text("SELECT a.code, a.close, b.close FROM universe_screen_daily a JOIN universe_screen_daily b "
                                     "ON a.code=b.code WHERE a.date=:a AND b.date=:b AND a.close>0 AND b.close>0"),
                                {"a": d, "b": d2}).fetchall()
        mine = [(c / a - 1) * 100 for k, a, c in rows if k == code]
        if len(rows) < 100 or not mine:
            continue
        rel = mine[0] - statistics.median([(c / a - 1) * 100 for _, a, c in rows])
        with get_conn() as conn:
            conn.execute(text("UPDATE candidate_log SET rel_ret=:r, scored_on=:s WHERE id=:i"), {"r": rel, "s": d2, "i": pid})
        n += 1
    return n


def scorecard_line() -> str:
    """행동안 성과 한 줄. 표본이 적으면 결론을 내지 않는다고 밝힌다."""
    from sqlalchemy import text
    from db.database import get_conn
    try:
        with get_conn() as conn:
            rows = conn.execute(text("SELECT action, rel_ret FROM candidate_log WHERE rel_ret IS NOT NULL")).fetchall()
    except Exception:
        return ""
    if not rows:
        return "이 행동안의 성과는 아직 채점 전입니다(기록 후 20거래일 뒤 시장 대비로 채점)."
    parts = []
    for act, label in (("buy", "분할 매수안"), ("watch", "관찰만")):
        v = [r for a, r in rows if a == act]
        if v:
            parts.append(f"{label} {len(v)}건 평균 {sum(v) / len(v):+.1f}%p(이긴 비율 {sum(x > 0 for x in v) / len(v) * 100:.0f}%)")
    tail = "" if len(rows) >= MIN_TRUSTED else f" — 표본 {MIN_TRUSTED}건 미만이라 결론 금지"
    return "지난 행동안의 시장 대비 20일 성과: " + ", ".join(parts) + tail


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
    gate = None
    try:
        from services import pick_gate
        from services.account_perf import _index_closes
        gate = pick_gate.market_gate(_index_closes())
    except Exception as e:
        logger.debug("[후보행동안] 시장 관문 조회 실패(없이 진행): %s", e)
    return {"gate": gate, "total": sum(p["value"] for p in positions), "held_inds": held_inds, "views": latest_views(),
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
    snap_date, picks, _ = mi.kr_picks(exclude, n=n)
    if not picks:
        return []
    ctx = _ctx(positions)
    evs = by_code(days=7)
    ind = {c: (i or None) for c, i in mi._query(
        "SELECT code, industry FROM universe_screen_daily WHERE date=(SELECT MAX(date) FROM universe_screen_daily)")}
    lines = []
    buys = 0
    decided: list[dict] = []
    gate = ctx.get("gate")
    max_buys = gate["max_picks"] if gate and gate.get("mode") in ("crash", "heated") else len(picks)
    for p in picks:
        p["industry"] = ind.get(p["code"])
        bad = [e for e in evs.get(p["code"], []) if e["polarity"] == "악재"][:1]
        p["bad_event"] = bad[0]["event_type"] if bad else None
        p["rev90"] = revision_90d(p["code"], p.get("market"))
        d = decide(p, ctx)
        if d["pct"]:
            if buys >= max_buys:      # 시장 상태가 정한 동시 진입 개수 상한
                d.update(pct=0.0, hold_reasons=[f"시장 상태({ctx['gate']['reason']})상 동시 진입은 {max_buys}종목까지"])
            else:
                buys += 1
        lines.append(format_line(d, ctx))
        d["close"] = p["close"]
        decided.append(d)
    try:
        record(snap_date, decided)
    except Exception as e:
        logger.warning("[후보행동안] 기록 실패 (본문은 정상): %s", e)
    sc = scorecard_line()
    if sc:
        lines.append("· " + sc)
    return lines
