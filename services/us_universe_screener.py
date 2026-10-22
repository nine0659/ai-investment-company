"""미국 전 종목 스크리너 — 나스닥 공개 스크리너(전 종목 목록) + yfinance 상세로 후보를 코드가 뽑는다 (LLM 없음, 발송 없음).

기존 미국 추천은 고정 12종목+ETF 목록에서만 골랐다 → 목록 밖의 좋은 종목은 구조적으로 못 본다.
한국 스크리너(services/universe_screener.py)와 같은 구조: 전 종목 확보 → 시총 하한 → 상세 → 신호(코드 계산).
"""
import logging
import re
import statistics
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)
_KST = ZoneInfo("Asia/Seoul")

MIN_MCAP_USD = 3_000_000_000     # 시총 하한 ($30억)
MAX_DETAIL = 800                 # 상세 조회 상한 (시총 큰 순) — 늘리면 Yahoo 제한에 걸려 소요시간이 20분 넘게 늘었다(실측)
COMPLETE_MIN = 3000              # 목록이 이 수 미만이면 '불완전' (정상은 6,900여 개)

_SCREENER_URL = "https://api.nasdaq.com/api/screener/stocks?tableonly=true&limit=25&offset=0&download=true"
_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
       "Accept": "application/json"}
_JUNK = re.compile(r"Warrant|Rights?\b|\bUnits?\b|Preferred|Depositary|Notes? due|Acquisition|Trust\b|\bFund\b|ETF|ETN",
                   re.IGNORECASE)


def _f(v) -> float | None:
    """'$173.01'·'1.83%'·'48776511031.00' → 숫자. 빈 값·'N/A'는 None."""
    if v is None:
        return None
    m = re.search(r"-?\d[\d,]*\.?\d*", str(v))
    if not m:
        return None
    try:
        return float(m.group().replace(",", ""))
    except ValueError:
        return None


def _parse_row(s: dict) -> dict | None:
    sym = (s.get("symbol") or "").strip()
    name = (s.get("name") or "").strip()
    if not sym or "^" in sym or _JUNK.search(name):
        return None
    close, mcap = _f(s.get("lastsale")), _f(s.get("marketCap"))
    if not close or not mcap:
        return None
    return {"code": sym.replace("/", "-"), "name": name.replace(" Common Stock", "").strip(),
            "close": close, "chg_pct": _f(s.get("pctchange")), "mcap_usd": mcap,
            "sector": s.get("sector") or "", "country": s.get("country") or ""}


def fetch_universe() -> dict:
    """전 종목 1단계. {rows, fetched, complete}. 실패 시 rows=[] / complete=False."""
    try:
        import requests
        r = requests.get(_SCREENER_URL, headers=_UA, timeout=30)
        r.raise_for_status()
        raw = ((r.json() or {}).get("data") or {}).get("rows") or []
    except Exception as e:
        logger.warning("[미국스크린] 전 종목 목록 수신 실패: %s", e)
        return {"rows": [], "fetched": 0, "complete": False}
    rows = [x for x in (_parse_row(s) for s in raw) if x]
    return {"rows": rows, "fetched": len(raw), "complete": len(raw) >= COMPLETE_MIN}


def tradable(rows: list[dict]) -> list[dict]:
    return [r for r in rows if r["mcap_usd"] >= MIN_MCAP_USD]


def parse_info(i: dict) -> dict:
    """yfinance info → 신호 계산용 수치. 없는 값은 None (만들지 않는다)."""
    def g(k):
        v = i.get(k)
        return float(v) if isinstance(v, (int, float)) and v == v else None
    return {
        "fwd_pe": g("forwardPE"), "pe": g("trailingPE"), "pb": g("priceToBook"), "roe": g("returnOnEquity"),
        "margin": g("profitMargins"), "eps": g("trailingEps"), "fwd_eps": g("forwardEps"),
        "target": g("targetMeanPrice"), "recomm": g("recommendationMean"), "analysts": g("numberOfAnalystOpinions"),
        "high52": g("fiftyTwoWeekHigh"), "ma50": g("fiftyDayAverage"), "ma200": g("twoHundredDayAverage"),
        "short_pct": g("shortPercentOfFloat"), "debt_eq": g("debtToEquity"),
        "industry": str(i.get("industry") or ""),
    }


def _info(sym: str) -> dict:
    try:
        import yfinance as yf
        return yf.Ticker(sym).info or {}
    except Exception as e:
        logger.debug("[미국스크린] %s 상세 실패: %s", sym, e)
        return {}


def enrich(rows: list[dict], workers: int = 3) -> list[dict]:
    """상세 조회. Yahoo가 동시 요청에 401(Invalid Crumb)로 제한을 거므로 실패분은 속도를 낮춰 재시도한다."""
    import time

    def one(r, pause=0.0):
        if pause:
            time.sleep(pause)
        i = _info(r["code"])
        return {**r, "detail": parse_info(i)} if i.get("quoteType") or i.get("marketCap") else dict(r)

    done = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        done = list(ex.map(one, rows))
    for w, pause in ((2, 0.4),):                         # 재시도 1회: 느리게 (더 길게 끌면 소요시간만 늘었다)
        failed = [i for i, r in enumerate(done) if not r.get("detail")]
        if not failed:
            break
        time.sleep(60 if len(failed) > len(rows) * 0.3 else 3)   # 많이 막혔으면 제한이 풀리도록 길게 쉰다
        with ThreadPoolExecutor(max_workers=w) as ex:
            redo = list(ex.map(lambda i: one(rows[i], pause), failed))
        for i, r in zip(failed, redo):
            done[i] = r
    return done


def industry_medians(rows: list[dict], min_members: int = 5) -> dict[str, float]:
    by: dict[str, list[float]] = {}
    for r in rows:
        d = r.get("detail")
        if d and d["industry"] and d["fwd_pe"] and 0 < d["fwd_pe"] < 100:
            by.setdefault(d["industry"], []).append(d["fwd_pe"])
    return {k: statistics.median(v) for k, v in by.items() if len(v) >= min_members}


def compute_signals(r: dict, ind_median: dict[str, float]) -> tuple[list[dict], list[str]]:
    """(신호 목록, 경고 목록). 근거 수치는 전부 코드가 상세 데이터에서 계산."""
    d = r.get("detail")
    if not d or not d["eps"] or d["eps"] <= 0:
        return [], []                                   # 적자·이익 미확인 제외(질 하한)
    sig, warn = [], []
    close = r["close"]
    fp = d["fwd_pe"]
    if d["roe"] is not None and d["roe"] >= 0.15 and (d["margin"] or 0) > 0:
        sig.append({"key": "수익성", "text": f"ROE {d['roe'] * 100:.0f}% · 순이익률 {d['margin'] * 100:.0f}%"})
    if fp and 0 < fp <= 12:
        sig.append({"key": "저평가", "text": f"선행PER {fp:.1f}배"})
    med = ind_median.get(d["industry"])
    if fp and med and 0 < fp <= med * 0.7:
        sig.append({"key": "업종대비저평가", "text": f"선행PER {fp:.1f}배 vs 업종({d['industry']}) 중앙 {med:.1f}배"})
    if d["fwd_eps"] and d["eps"] > 0:
        g = d["fwd_eps"] / d["eps"] - 1
        if g >= 0.2:
            sig.append({"key": "이익증가기대", "text": f"선행EPS가 현재 EPS 대비 {g * 100:+.0f}%"})
    if d["target"] and d["recomm"] and d["analysts"] and d["analysts"] >= 5 and d["recomm"] <= 2.5:
        up = d["target"] / close - 1
        if 0.2 <= up <= 1.5:
            sig.append({"key": "컨센서스여력",
                        "text": f"평균 목표가 대비 {up * 100:+.0f}% (애널리스트 {d['analysts']:.0f}명, 의견 {d['recomm']:.1f}/5 — 낮을수록 매수)"})
    dd = (close / d["high52"] - 1) if d["high52"] else None
    if dd is not None and dd <= -0.3:
        sig.append({"key": "고점대비낙폭", "text": f"52주 고점 대비 {dd * 100:.0f}%"})
    if dd is not None and dd <= -0.2 and d["ma50"] and close > d["ma50"]:
        sig.append({"key": "단기회복", "text": f"고점 대비 {dd * 100:.0f}% 구간에서 50일선({d['ma50']:.2f}) 위로 회복"})
    if d["fwd_eps"] and d["eps"] > 0 and d["fwd_eps"] / d["eps"] - 1 >= 1.0:
        warn.append("이익 추정이 2배 이상 급증 — 사이클 업종이면 이익 정점에서 PER이 가장 낮아 보일 수 있음")
    if d["roe"] is not None and d["roe"] >= 1.0:
        warn.append("ROE 100%+ — 자기자본이 작거나 자사주 매입 영향일 수 있어 수익성 지표로 과대평가될 수 있음")
    if d["short_pct"] is not None and d["short_pct"] >= 0.15:
        warn.append(f"공매도 비율 {d['short_pct'] * 100:.0f}% (유통주식 대비)")
    if d["debt_eq"] is not None and d["debt_eq"] >= 200:
        warn.append(f"부채비율 {d['debt_eq']:.0f}%")
    if d["ma200"] and close < d["ma200"] * 0.8:
        warn.append("200일선 대비 20% 이상 아래(하락 추세 지속 가능)")
    return sig, warn


_GROUP = {"수익성": "질", "저평가": "가치", "업종대비저평가": "가치", "이익증가기대": "이익",
          "컨센서스여력": "이익", "고점대비낙폭": "낙폭", "단기회복": "회복"}


def score(signals: list[dict]) -> int:
    return len({_GROUP[s["key"]] for s in signals})


def score_all(enriched: list[dict]) -> list[dict]:
    med = industry_medians(enriched)
    out = []
    for r in enriched:
        sig, warn = compute_signals(r, med)
        out.append({**r, "signals": sig, "warnings": warn, "score": score(sig)})
    return out


def pick_from(scored: list[dict], exclude: set[str] | None = None, top: int = 20, min_score: int = 3) -> list[dict]:
    exclude = exclude or set()
    out = [r for r in scored if r["score"] >= min_score and r["code"] not in exclude]
    out.sort(key=lambda x: (-x["score"], -x["mcap_usd"]))
    return out[:top]


def run_screen(exclude: set[str] | None = None, top: int = 20, max_detail: int = MAX_DETAIL) -> dict:
    uni = fetch_universe()
    pool = sorted(tradable(uni["rows"]), key=lambda r: -r["mcap_usd"])
    detail_rows = enrich(pool[:max_detail])
    got = [r for r in detail_rows if r.get("detail")]
    scored = score_all(got)
    return {
        "as_of": datetime.now(_KST).strftime("%Y-%m-%d %H:%M"),
        "fetched": uni["fetched"], "universe_complete": uni["complete"], "tradable": len(pool),
        "detailed": len(got), "detail_requested": len(detail_rows),
        "scored": scored, "picks": pick_from(scored, exclude, top),
    }


# ── 일별 스냅샷 ────────────────────────────────────────────────────────────

def save_daily(res: dict, date: str | None = None) -> int:
    from db.database import get_conn
    from sqlalchemy import text
    date = date or datetime.now(_KST).strftime("%Y-%m-%d")
    rows = []
    for r in res.get("scored") or []:
        d = r["detail"]
        rows.append({"date": date, "code": r["code"], "name": r["name"], "sector": r["sector"], "close": r["close"],
                     "chg_pct": r["chg_pct"], "mcap_usd": r["mcap_usd"], "fwd_pe": d["fwd_pe"], "roe": d["roe"],
                     "target": d["target"], "high52": d["high52"], "score": r["score"],
                     "signals": ",".join(s["key"] for s in r["signals"])})
    with get_conn() as conn:
        conn.execute(text("DELETE FROM us_screen_daily WHERE date=:d"), {"d": date})
        if rows:
            conn.execute(text(
                "INSERT INTO us_screen_daily (date, code, name, sector, close, chg_pct, mcap_usd, fwd_pe, roe, target, "
                "high52, score, signals) VALUES (:date, :code, :name, :sector, :close, :chg_pct, :mcap_usd, :fwd_pe, "
                ":roe, :target, :high52, :score, :signals)"), rows)
    return len(rows)


def load_picks(date: str | None = None, min_score: int = 3, limit: int = 20) -> list[dict]:
    from db.database import get_conn
    from sqlalchemy import text
    try:
        with get_conn() as conn:
            if not date:
                date = conn.execute(text("SELECT max(date) FROM us_screen_daily")).scalar()
            if not date:
                return []
            rows = conn.execute(text(
                "SELECT date, code, name, sector, close, chg_pct, mcap_usd, score, signals FROM us_screen_daily "
                "WHERE date=:d AND score>=:m ORDER BY score DESC, mcap_usd DESC LIMIT :n"),
                {"d": date, "m": min_score, "n": limit}).fetchall()
        return [dict(zip(("date", "code", "name", "sector", "close", "chg_pct", "mcap_usd", "score", "signals"), r))
                for r in rows]
    except Exception as e:
        logger.warning("[미국스크린] 스냅샷 조회 실패: %s", e)
        return []


def run_daily_job() -> tuple[str, str]:
    """무발송 잡 본체. (status, detail) — 0건=empty, 부분 실패=fail."""
    res = run_screen()
    if not res["universe_complete"]:
        return "fail", f"전 종목 목록 수신 불완전 ({res['fetched']}종목)"
    if res["detail_requested"] and res["detailed"] < res["detail_requested"] * 0.8:
        return "fail", f"상세 조회 {res['detailed']}/{res['detail_requested']}만 성공 (80% 미만)"
    n = save_daily(res)
    if n == 0:
        return "empty", "저장 0건"
    return "success", f"저장 {n}종목 · 신호 3묶음+ {len(res['picks'])}종목 · 목록 {res['fetched']}종목"


def format_screen(res: dict) -> str:
    lines = [f"=== 미국 전 종목 스크리닝 ({res['as_of']}) ===",
             f"전체 {res['fetched']}종목 수신" + ("" if res["universe_complete"] else " ⚠불완전")
             + f" → 시총 $30억+ {res['tradable']}종목 → 상세 확인 {res['detailed']}/{res['detail_requested']}종목"]
    if not res["picks"]:
        lines.append("신호가 3묶음 이상 겹치는 종목 없음")
    for p in res["picks"]:
        chg = f"{p['chg_pct']:+.2f}%" if p["chg_pct"] is not None else "N/A"
        lines.append(f"\n{p['name']}({p['code']}) ${p['close']:,.2f} · 시총 ${p['mcap_usd'] / 1e9:,.1f}B · {p['sector']} · 직전 {chg}  [신호 {p['score']}묶음]")
        for s in p["signals"]:
            lines.append(f"  · {s['key']}: {s['text']}")
        for w in p["warnings"]:
            lines.append(f"  ⚠ {w}")
    return "\n".join(lines)


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    print(format_screen(run_screen()))
