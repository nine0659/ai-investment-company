"""미국 전 종목 스크리너 — 단일 소스(Yahoo) 제한을 피해 여러 출처를 조합한다 (LLM 없음, 발송 없음).

  ① 나스닥 공개 스크리너: 전 종목 목록·시총·업종 (1회 호출)
  ② SEC EDGAR 일괄 재무(frames API): 순이익·자본·매출·부채·EPS → ROE·이익률·PER을 코드가 계산 (개별 종목 호출 없음)
  ③ yfinance 일괄 시세 다운로드: 52주 고점·50/200일선 (종목별 info 호출이 아니라 Yahoo 제한에 덜 걸린다)
  ④ 나스닥 애널리스트 API: 목표가·매수 비율·다음 결산 EPS 예상 — ①~③을 통과한 후보에만 조회

2026-10-22 실측: 종목별 yfinance info를 800~1,500개 호출하면 Yahoo가 401(Invalid Crumb)·IP 차단으로 62%만 받거나 0건이 됐다.
한국 스크리너(services/universe_screener.py)와 같은 신호·점수·저장 구조를 쓴다.
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
MAX_UNIVERSE = 1700              # 시세·재무 확인 상한 (시총 큰 순)
MAX_CONSENSUS = 250              # 애널리스트 조회 상한 (후보만)
COMPLETE_MIN = 3000              # 목록이 이 수 미만이면 '불완전' (정상은 6,900여 개)

_SCREENER_URL = "https://api.nasdaq.com/api/screener/stocks?tableonly=true&limit=25&offset=0&download=true"
_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
       "Accept": "application/json"}
_SEC = {"User-Agent": "ai-investment-research nine0659@gmail.com", "Accept-Encoding": "gzip, deflate"}
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
            "sector": s.get("sector") or "", "industry": s.get("industry") or "", "country": s.get("country") or ""}


def fetch_universe() -> dict:
    """① 전 종목 목록. {rows, fetched, complete}. 실패 시 rows=[] / complete=False."""
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


# ── ② SEC EDGAR 일괄 재무 ────────────────────────────────────────────────────

def _frame(concept: str, unit: str, period: str) -> dict[int, float]:
    """한 개념·기간의 전 종목 값 {cik: 값}. 실패 시 {}."""
    try:
        import requests
        r = requests.get(f"https://data.sec.gov/api/xbrl/frames/us-gaap/{concept}/{unit}/{period}.json",
                         headers=_SEC, timeout=60)
        r.raise_for_status()
        return {int(x["cik"]): float(x["val"]) for x in (r.json().get("data") or []) if x.get("val") is not None}
    except Exception as e:
        logger.warning("[미국스크린] EDGAR %s/%s 실패: %s", concept, period, e)
        return {}


def fetch_edgar() -> dict:
    """{by_ticker: {ticker: {cik, ni, equity, revenue, liabilities, eps}}, ok}. 못 받은 항목은 키가 없다."""
    try:
        import requests
        r = requests.get("https://www.sec.gov/files/company_tickers.json", headers=_SEC, timeout=30)
        r.raise_for_status()
        t2c = {v["ticker"].upper(): int(v["cik_str"]) for v in r.json().values()}
    except Exception as e:
        logger.warning("[미국스크린] EDGAR 티커 매핑 실패: %s", e)
        return {"by_ticker": {}, "ok": False}
    ni = _frame("NetIncomeLoss", "USD", "CY2025")
    eps = _frame("EarningsPerShareDiluted", "USD-per-shares", "CY2025")
    rev = _frame("Revenues", "USD", "CY2025")
    for c, v in _frame("RevenueFromContractWithCustomerExcludingAssessedTax", "USD", "CY2025").items():
        rev.setdefault(c, v)
    eq: dict[int, float] = {}
    liab: dict[int, float] = {}
    for per in ("CY2025Q4I", "CY2025Q3I", "CY2025Q2I"):    # 결산월이 다른 회사는 다른 분기말에 잡힌다
        for c, v in _frame("StockholdersEquity", "USD", per).items():
            eq.setdefault(c, v)
        for c, v in _frame("Liabilities", "USD", per).items():
            liab.setdefault(c, v)
    out = {}
    for tk, cik in t2c.items():
        rec = {k: m[cik] for k, m in (("ni", ni), ("eps", eps), ("revenue", rev), ("equity", eq), ("liabilities", liab))
               if cik in m}
        out[tk] = {**rec, "cik": cik}                       # 재무가 없어도 CIK는 내부자 거래 매칭에 쓴다
    return {"by_ticker": out, "ok": len(ni) >= 2000}        # 순이익 프레임이 이보다 적으면 수신 이상


# ── ③ 일괄 시세 ─────────────────────────────────────────────────────────────

def fetch_prices(tickers: list[str], chunk: int = 100) -> dict[str, dict]:
    """{ticker: {close, high52, ma50, ma200}}. 종목별 info가 아니라 일괄 시세 다운로드 사용."""
    out: dict[str, dict] = {}
    try:
        import yfinance as yf
    except Exception:
        return out
    for i in range(0, len(tickers), chunk):
        part = tickers[i:i + chunk]
        if len(part) < 2:
            part = part + ["SPY"]                              # 단일 종목은 열 구조가 달라 더미를 붙인다
        try:
            df = yf.download(part, period="1y", interval="1d", group_by="ticker", progress=False,
                             threads=False, auto_adjust=False)
        except Exception as e:
            logger.warning("[미국스크린] 시세 일괄 다운로드 실패(%d~): %s", i, e)
            continue
        for tk in part:
            try:
                sub = df[tk]
                cl = sub["Close"].dropna()
                if len(cl) < 60:
                    continue
                out[tk] = {"close": float(cl.iloc[-1]), "high52": float(sub["High"].dropna().max()),
                           "ma50": float(cl.tail(50).mean()),
                           "ma200": float(cl.tail(200).mean()) if len(cl) >= 200 else None}
            except Exception:
                continue
    return out


# ── ④ 나스닥 애널리스트 ──────────────────────────────────────────────────────

def _nasdaq_get(path: str) -> dict:
    try:
        import requests
        r = requests.get("https://api.nasdaq.com/api/" + path, headers=_UA, timeout=20)
        if r.status_code == 200:
            return (r.json() or {}).get("data") or {}
    except Exception as e:
        logger.debug("[미국스크린] 나스닥 %s 실패: %s", path, e)
    return {}


def fetch_consensus(sym: str) -> dict:
    """{target, buy, hold, sell, analysts, fwd_eps} — 못 받은 항목은 키가 없다."""
    s = sym.replace("-", ".")
    out = {}
    tp = (_nasdaq_get(f"analyst/{s}/targetprice").get("consensusOverview")) or {}
    if tp.get("priceTarget"):
        out["target"] = float(tp["priceTarget"])
        buy, hold, sell = (int(tp.get(k) or 0) for k in ("buy", "hold", "sell"))
        out.update(buy=buy, hold=hold, sell=sell, analysts=buy + hold + sell)
    rows = ((_nasdaq_get(f"analyst/{s}/earnings-forecast").get("yearlyForecast")) or {}).get("rows") or []
    if rows and rows[0].get("consensusEPSForecast") is not None:
        out["fwd_eps"] = float(rows[0]["consensusEPSForecast"])     # 아직 결산 안 된 가장 가까운 사업연도
    return out


# ── 조립·신호 ───────────────────────────────────────────────────────────────

def build_detail(row: dict, edgar: dict | None, price: dict | None, insider: dict | None = None) -> dict | None:
    """① 행 + ② 재무 + ③ 시세 → 신호 계산용 수치. 시세가 없으면 None."""
    if not price:
        return None
    close = price["close"]
    e = edgar or {}
    eps, ni, eq, rev, li = e.get("eps"), e.get("ni"), e.get("equity"), e.get("revenue"), e.get("liabilities")
    return {
        "close": close, "high52": price["high52"], "ma50": price["ma50"], "ma200": price["ma200"],
        "eps": eps,
        "pe": close / eps if eps and eps > 0 else None,
        "roe": ni / eq if ni is not None and eq and eq > 0 else None,
        "margin": ni / rev if ni is not None and rev and rev > 0 else None,
        "debt_eq": li / eq * 100 if li is not None and eq and eq > 0 else None,
        "industry": row.get("industry") or "", "sector": row.get("sector") or "",
        "target": None, "buy_ratio": None, "analysts": None, "fwd_eps": None, "fwd_pe": None,
        "insider": insider if insider and insider.get("buyers") else None,
    }


def apply_consensus(d: dict, c: dict) -> None:
    if c.get("target"):
        d["target"] = c["target"]
    if c.get("analysts"):
        d["analysts"] = float(c["analysts"])
        d["buy_ratio"] = c["buy"] / c["analysts"]
    if c.get("fwd_eps") is not None:
        d["fwd_eps"] = c["fwd_eps"]
        d["fwd_pe"] = d["close"] / c["fwd_eps"] if c["fwd_eps"] > 0 else None


def prefilter(rows: list[dict], cap: int = MAX_CONSENSUS) -> list[dict]:
    """애널리스트 조회 대상: 흑자·수익성 최소 요건을 넘고 (낙폭이 있거나 PER이 낮은) 종목."""
    out = []
    for r in rows:
        d = r.get("detail")
        if not d or not d["eps"] or d["eps"] <= 0 or (d["roe"] or 0) < 0.08:
            continue
        dd = d["close"] / d["high52"] - 1 if d["high52"] else 0
        if dd <= -0.15 or (d["pe"] and d["pe"] <= 18):
            out.append(r)
    out.sort(key=lambda r: -r["mcap_usd"])
    return out[:cap]


def enrich_consensus(rows: list[dict], workers: int = 5) -> int:
    """rows[*]['detail']에 애널리스트 정보를 제자리에서 채운다. 성공(목표가 받음) 수를 반환."""
    def one(r):
        c = fetch_consensus(r["code"])
        if not c:
            c = fetch_consensus(r["code"])                   # 1회 재시도
        if c:
            apply_consensus(r["detail"], c)
        return bool(c.get("target"))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        return sum(ex.map(one, rows))


def industry_medians(rows: list[dict], min_members: int = 8) -> dict[str, float]:
    """업종별 최근 연간 PER 중앙값 (표본 8개 미만 업종은 비교 불가라 제외)."""
    by: dict[str, list[float]] = {}
    for r in rows:
        d = r.get("detail")
        if d and d["industry"] and d["pe"] and 0 < d["pe"] < 100:
            by.setdefault(d["industry"], []).append(d["pe"])
    return {k: statistics.median(v) for k, v in by.items() if len(v) >= min_members}


def compute_signals(r: dict, ind_median: dict[str, float]) -> tuple[list[dict], list[str]]:
    """(신호 목록, 경고 목록). 근거 수치는 전부 코드가 계산."""
    d = r.get("detail")
    if not d or not d["eps"] or d["eps"] <= 0:
        return [], []                                   # 적자·이익 미확인 제외(질 하한)
    sig, warn = [], []
    close = d["close"]
    if d["roe"] is not None and d["roe"] >= 0.15 and (d["margin"] or 0) > 0:
        sig.append({"key": "수익성", "text": f"ROE {d['roe'] * 100:.0f}% · 순이익률 {d['margin'] * 100:.0f}% (최근 연간)"})
    if d["fwd_pe"] and 0 < d["fwd_pe"] <= 12:
        sig.append({"key": "저평가", "text": f"선행PER {d['fwd_pe']:.1f}배 (다음 결산 컨센서스 EPS {d['fwd_eps']:.2f})"})
    elif not d["fwd_pe"] and d["pe"] and 0 < d["pe"] <= 12:
        sig.append({"key": "저평가", "text": f"최근 연간 PER {d['pe']:.1f}배"})
    med = ind_median.get(d["industry"])
    if d["pe"] and med and 0 < d["pe"] <= med * 0.7:
        sig.append({"key": "업종대비저평가", "text": f"최근 연간 PER {d['pe']:.1f}배 vs 업종({d['industry']}) 중앙 {med:.1f}배"})
    if d["fwd_eps"] and d["fwd_eps"] > 0:
        g = d["fwd_eps"] / d["eps"] - 1
        if g >= 0.2:
            sig.append({"key": "이익증가기대", "text": f"다음 결산 컨센서스 EPS가 최근 연간 EPS 대비 {g * 100:+.0f}%"})
        if g >= 1.0:
            warn.append("이익 추정이 2배 이상 급증 — 사이클 업종이면 이익 정점에서 PER이 가장 낮아 보일 수 있음")
    if d["target"] and d["analysts"] and d["analysts"] >= 5 and (d["buy_ratio"] or 0) >= 0.6:
        up = d["target"] / close - 1
        if 0.2 <= up <= 1.5:
            sig.append({"key": "컨센서스여력",
                        "text": f"평균 목표가 대비 {up * 100:+.0f}% (애널리스트 {d['analysts']:.0f}명 중 매수 {d['buy_ratio'] * 100:.0f}%)"})
    dd = (close / d["high52"] - 1) if d["high52"] else None
    if dd is not None and dd <= -0.3:
        sig.append({"key": "고점대비낙폭", "text": f"52주 고점 대비 {dd * 100:.0f}%"})
    if dd is not None and dd <= -0.2 and d["ma50"] and close > d["ma50"]:
        sig.append({"key": "단기회복", "text": f"고점 대비 {dd * 100:.0f}% 구간에서 50일선({d['ma50']:.2f}) 위로 회복"})
    ins = d.get("insider")
    if ins and ((ins["buyers"] >= 2 and ins["value"] >= 100_000) or ins["value"] >= 500_000):
        sig.append({"key": "내부자매수",
                    "text": f"임원·이사 {ins['buyers']}명이 공개시장에서 ${ins['value']:,.0f} 매수 (마지막 {ins['last']}, 최근 90일)"})
    if d["roe"] is not None and d["roe"] >= 1.0:
        warn.append("ROE 100%+ — 자기자본이 작거나 자사주 매입 영향일 수 있어 수익성 지표로 과대평가될 수 있음")
    if d["debt_eq"] is not None and d["debt_eq"] >= 300 and d["sector"] != "Finance":
        warn.append(f"총부채/자기자본 {d['debt_eq']:.0f}%")
    if d["ma200"] and close < d["ma200"] * 0.8:
        warn.append("200일선 대비 20% 이상 아래(하락 추세 지속 가능)")
    return sig, warn


_GROUP = {"수익성": "질", "저평가": "가치", "업종대비저평가": "가치", "이익증가기대": "이익",
          "컨센서스여력": "이익", "고점대비낙폭": "낙폭", "단기회복": "회복", "내부자매수": "수급"}


def score(signals: list[dict]) -> int:
    return len({_GROUP[s["key"]] for s in signals})


def score_all(rows: list[dict]) -> list[dict]:
    med = industry_medians(rows)
    out = []
    for r in rows:
        sig, warn = compute_signals(r, med)
        out.append({**r, "signals": sig, "warnings": warn, "score": score(sig)})
    return out


def pick_from(scored: list[dict], exclude: set[str] | None = None, top: int = 20, min_score: int = 3) -> list[dict]:
    exclude = exclude or set()
    out = [r for r in scored if r["score"] >= min_score and r["code"] not in exclude]
    out.sort(key=lambda x: (-x["score"], -x["mcap_usd"]))
    return out[:top]


def run_screen(exclude: set[str] | None = None, top: int = 20) -> dict:
    uni = fetch_universe()
    pool = sorted(tradable(uni["rows"]), key=lambda r: -r["mcap_usd"])[:MAX_UNIVERSE]
    edgar = fetch_edgar()
    prices = fetch_prices([r["code"] for r in pool]) if pool else {}
    try:
        from clients.sec_insider_client import fetch_recent_purchases
        ins = fetch_recent_purchases()
    except Exception as e:
        logger.warning("[미국스크린] 내부자 거래 수신 실패 (이 신호 없이 진행): %s", e)
        ins = {"by_cik": {}, "data_through": None, "ok": False}
    rows = []
    for r in pool:
        e = edgar["by_ticker"].get(r["code"].upper())
        d = build_detail(r, e, prices.get(r["code"]), ins["by_cik"].get((e or {}).get("cik")))
        if d:
            rows.append({**r, "detail": d})
    cands = prefilter(rows)
    cons_ok = enrich_consensus(cands) if cands else 0
    scored = score_all(rows)
    return {
        "as_of": datetime.now(_KST).strftime("%Y-%m-%d %H:%M"),
        "fetched": uni["fetched"], "universe_complete": uni["complete"], "tradable": len(pool),
        "detailed": len(rows), "detail_requested": len(pool),
        "edgar_ok": edgar["ok"],
        "edgar_matched": sum(1 for r in rows if "eps" in edgar["by_ticker"].get(r["code"].upper(), {})),
        "insider_ok": ins["ok"], "insider_through": ins["data_through"],
        "insider_hits": sum(1 for r in rows if r["detail"]["insider"]),
        "consensus_ok": cons_ok, "consensus_requested": len(cands),
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
        rows.append({"date": date, "code": r["code"], "name": r["name"], "sector": r["sector"], "close": d["close"],
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
    if not res["edgar_ok"]:
        return "fail", "SEC EDGAR 재무 수신 이상(순이익 프레임 2,000건 미만)"
    if res["detail_requested"] and res["detailed"] < res["detail_requested"] * 0.8:
        return "fail", f"시세 확인 {res['detailed']}/{res['detail_requested']}만 성공 (80% 미만)"
    if res["consensus_requested"] and res["consensus_ok"] < res["consensus_requested"] * 0.6:
        return "fail", f"애널리스트 조회 {res['consensus_ok']}/{res['consensus_requested']}만 성공 (60% 미만)"
    n = save_daily(res)
    if n == 0:
        return "empty", "저장 0건"
    note = "" if res.get("insider_ok") else " · ⚠내부자거래 미수신(해당 신호 없이 저장)"
    return "success", (f"저장 {n}종목 · 신호 3묶음+ {len(res['picks'])}종목 · 목록 {res['fetched']} · "
                       f"재무매칭 {res['edgar_matched']} · 컨센서스 {res['consensus_ok']}/{res['consensus_requested']}{note}")


def format_screen(res: dict) -> str:
    lines = [f"=== 미국 전 종목 스크리닝 ({res['as_of']}) ===",
             f"전체 {res['fetched']}종목 수신" + ("" if res["universe_complete"] else " ⚠불완전")
             + f" → 시총 $30억+ {res['tradable']}종목 → 시세 확인 {res['detailed']}/{res['detail_requested']}"
             + f" · SEC 재무 매칭 {res['edgar_matched']} · 애널리스트 {res['consensus_ok']}/{res['consensus_requested']}"
             + (f" · 내부자 매수 {res.get('insider_hits', 0)}종목(데이터는 {res.get('insider_through')}까지, 분기 공개라 최근 몇 주 지연)"
                if res.get("insider_ok") else " · ⚠내부자거래 미수신")]
    if not res["picks"]:
        lines.append("신호가 3묶음 이상 겹치는 종목 없음")
    for p in res["picks"]:
        chg = f"{p['chg_pct']:+.2f}%" if p["chg_pct"] is not None else "N/A"
        lines.append(f"\n{p['name']}({p['code']}) ${p['detail']['close']:,.2f} · 시총 ${p['mcap_usd'] / 1e9:,.1f}B · "
                     f"{p['sector']} · 직전 {chg}  [신호 {p['score']}묶음]")
        for s in p["signals"]:
            lines.append(f"  · {s['key']}: {s['text']}")
        for w in p["warnings"]:
            lines.append(f"  ⚠ {w}")
    return "\n".join(lines)


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    print(format_screen(run_screen()))
