"""한국 전 종목 스크리너 — 코스피·코스닥 전체를 훑어 후보를 코드로 뽑는다 (LLM 없음, 발송 없음).

기존 발굴은 KIS 순위표(외국인·기관·거래대금 상위)에 든 종목만 봤다 → 이미 오른 종목 위주이고
순위 밖 저평가 종목은 구조적으로 못 찾았다. 여기서는 ① 네이버 시총순 목록으로 전 종목 확보
② 거래 가능한 종목만 거른 뒤 ③ 종목 상세(PER·추정치·52주·수급·컨센서스)로 신호를 코드가 계산한다.
수치 계산은 전부 코드, 해석은 LLM에 넘기는 쪽 몫(절대원칙 2).
"""
import logging
import re
import statistics
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from zoneinfo import ZoneInfo

from clients import naver_client as nv

logger = logging.getLogger(__name__)
_KST = ZoneInfo("Asia/Seoul")

MIN_MCAP_EOK = 1000        # 시총 하한(억원)
MAX_DETAIL = 1800          # 상세 조회 상한 — 시총 상위부터 (장중에는 당일 거래대금이 작아 거래대금 기준이 부적합)
COMPLETE_RATIO = 0.9       # 목록을 이 비율 미만만 받으면 '불완전'으로 표시(조용한 부분 실패 방지)

_PREF = re.compile(r"우$|우[A-Z]$|우\(전환\)$")
_JUNK = re.compile(r"스팩|SPAC|제\d+호")


_NUM = re.compile(r"-?\d[\d,]*\.?\d*")


def _unit_num(v) -> float | None:
    """'5.75배'·'224,313원'·'48.79%' 처럼 단위가 붙은 값에서 숫자만. 'N/A'·빈 값은 None."""
    if v is None:
        return None
    m = _NUM.search(str(v))
    if not m:
        return None
    try:
        return float(m.group().replace(",", ""))
    except ValueError:
        return None


def _parse_row(s: dict, market: str) -> dict | None:
    if s.get("stockEndType") != "stock":
        return None                                   # ETF·ETN·리츠 등 제외
    name = (s.get("stockName") or "").strip()
    code = (s.get("itemCode") or "").strip()
    if not name or not code or _PREF.search(name) or _JUNK.search(name):
        return None
    if (s.get("tradeStopType") or {}).get("code") not in (None, "1"):
        return None                                   # 거래정지
    close, mcap = nv.num(s.get("closePrice")), nv.num(s.get("marketValue"))
    if not close or not mcap:
        return None
    value_mil = nv.num(s.get("accumulatedTradingValue")) or 0.0   # 백만원 단위
    return {
        "code": code, "name": name, "market": market, "close": close,
        "chg_pct": nv.num(s.get("fluctuationsRatio")), "volume": nv.num(s.get("accumulatedTradingVolume")),
        "value_eok": value_mil / 100.0, "mcap_eok": mcap,
    }


def fetch_universe(markets=("KOSPI", "KOSDAQ")) -> dict:
    """전 종목 1단계 데이터. {rows, expected, fetched, complete}."""
    rows: list[dict] = []
    expected = fetched = 0
    for market in markets:
        first = nv.market_value_page(market, 1)
        total = int(first.get("totalCount") or 0)
        if not total:
            continue
        expected += total
        pages = (total + 99) // 100
        with ThreadPoolExecutor(max_workers=4) as ex:
            results = list(ex.map(lambda p: first if p == 1 else nv.market_value_page(market, p),
                                  range(1, pages + 1)))
        for res in results:
            for s in res.get("stocks") or []:
                fetched += 1
                row = _parse_row(s, market)
                if row:
                    rows.append(row)
    complete = expected > 0 and fetched >= expected * COMPLETE_RATIO
    return {"rows": rows, "expected": expected, "fetched": fetched, "complete": complete}


def tradable(rows: list[dict]) -> list[dict]:
    """시총 하한을 넘는 종목. 거래대금은 장중엔 누적이 작아 거르는 기준으로 쓰지 않는다(표시용)."""
    return [r for r in rows if r["mcap_eok"] >= MIN_MCAP_EOK]


def parse_integration(j: dict) -> dict:
    """종목 상세 JSON → 신호 계산용 수치. 없는 값은 None (만들지 않는다)."""
    info = {i.get("code"): i.get("value") for i in (j.get("totalInfos") or [])}
    n = _unit_num
    out = {
        "per": n(info.get("per")), "pbr": n(info.get("pbr")), "eps": n(info.get("eps")),
        "bps": n(info.get("bps")), "cns_per": n(info.get("cnsPer")), "cns_eps": n(info.get("cnsEps")),
        "high52": n(info.get("highPriceOf52Weeks")), "low52": n(info.get("lowPriceOf52Weeks")),
        "div_yield": n(info.get("dividendYieldRatio")), "foreign_rate": n(info.get("foreignRate")),
        "industry": str(j.get("industryCode") or ""),
    }
    cons = j.get("consensusInfo") or {}
    out["target"] = n(cons.get("priceTargetMean"))
    out["recomm"] = n(cons.get("recommMean"))
    deals = (j.get("dealTrendInfos") or [])[:3]
    out["flow_days"] = len(deals)
    out["foreign_buy_days"] = sum(1 for d in deals if (n(d.get("foreignerPureBuyQuant")) or 0) > 0)
    out["organ_buy_days"] = sum(1 for d in deals if (n(d.get("organPureBuyQuant")) or 0) > 0)
    return out


def enrich(rows: list[dict], workers: int = 6) -> list[dict]:
    """상세 조회를 붙인다. 조회 실패 종목은 'detail'이 없어 신호 계산에서 빠진다."""
    def one(r):
        j = nv.stock_integration(r["code"])
        return {**r, "detail": parse_integration(j)} if j else dict(r)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        return list(ex.map(one, rows))


def _yf_tickers(rows: list[dict]) -> dict[str, str]:
    return {r["code"] + (".KS" if r["market"] == "KOSPI" else ".KQ"): r["code"] for r in rows}


def _hist_stats(cl) -> dict | None:
    """종가 시리즈(오래된 순) → 추세 수치. 60봉 미만이면 None."""
    n = len(cl)
    if n < 60:
        return None
    last = float(cl.iloc[-1])

    def ret(k):
        return (last / float(cl.iloc[-1 - k]) - 1) * 100 if n > k and float(cl.iloc[-1 - k]) > 0 else None
    return {"ret_1m": ret(21), "ret_3m": ret(63), "ret_6m": ret(126),
            "ma50": float(cl.tail(50).mean()), "ma200": float(cl.tail(200).mean()) if n >= 200 else None,
            "hi_hist": None}


def _download_part(part: list[str], tmap: dict[str, str], out: dict) -> int:
    """yfinance 일괄 다운로드 한 묶음 → out[code]에 채우고 성공 수를 반환. 실패하면 0."""
    import yfinance as yf
    real = len(part)
    dl = part + ["005930.KS"] if real < 2 else part          # 단일 종목은 열 구조가 달라 더미를 붙인다
    try:
        df = yf.download(dl, period="1y", interval="1d", group_by="ticker", progress=False,
                         threads=False, auto_adjust=False)
    except Exception as e:
        logger.warning("[전종목스크린] 이력 일괄 다운로드 실패: %s", e)
        return 0
    ok = 0
    for tk in part:
        try:
            st = _hist_stats(df[tk]["Close"].dropna())
            if st:
                st["hi_hist"] = float(df[tk]["High"].dropna().max())
                out[tmap[tk]] = st
                ok += 1
        except Exception:
            continue
    del df                                            # 묶음마다 즉시 해제 — 무료 플랜(512MB)에서 메모리 여유가 약 200MB뿐이다
    import gc
    gc.collect()
    return ok


def fetch_history(rows: list[dict], chunk: int = 100) -> dict[str, dict]:
    """{종목코드: {ret_1m, ret_3m, ret_6m, ma50, ma200}} — 종목별 호출이 아니라 일괄 다운로드(Yahoo 제한 회피).
    통째로 실패한 묶음은 20초 쉬고 한 번 더 시도한다."""
    import time
    tmap = _yf_tickers(rows)
    items = list(tmap)
    out: dict[str, dict] = {}
    failed = []
    from services import memory_watch
    for i in range(0, len(items), chunk):
        memory_watch.guard("한국 스크리너 이력 수집")          # 한도 근접 시 스스로 중단(잡 fail) — OOM 재시작 방지
        part = items[i:i + chunk]
        if _download_part(part, tmap, out) == 0:
            failed.append(part)
    if failed:
        time.sleep(20)
        for part in failed:
            _download_part(part, tmap, out)
    return out


def industry_medians(rows: list[dict], min_members: int = 5) -> dict[str, float]:
    """업종별 추정PER 중앙값 (표본 5개 미만 업종은 비교 불가라 제외)."""
    by: dict[str, list[float]] = {}
    for r in rows:
        d = r.get("detail")
        if d and d["industry"] and d["cns_per"] and 0 < d["cns_per"] < 100:
            by.setdefault(d["industry"], []).append(d["cns_per"])
    return {k: statistics.median(v) for k, v in by.items() if len(v) >= min_members}


def compute_signals(r: dict, ind_median: dict[str, float]) -> list[dict]:
    """한 종목의 신호 목록 [{key, text}]. 근거 수치는 전부 상세 데이터에서 코드가 계산."""
    d = r.get("detail")
    if not d:
        return []
    if not d["eps"] or d["eps"] <= 0:
        return []                                      # 적자·이익 미확인 종목은 후보에서 제외(질 하한)
    sig = []
    close = r["close"]
    cp = d["cns_per"]
    if d["bps"] and d["bps"] > 0 and d["eps"] / d["bps"] >= 0.1:
        # 참고용(info): ROE 10%+ 단독은 한국 17개월 검증에서 효과 없음(60일 +0.13%p, t=+0.8) → 점수 제외 (scripts/backtest_fundamental_signals.py)
        sig.append({"key": "수익성", "info": True, "text": f"(참고) ROE 약 {d['eps'] / d['bps'] * 100:.0f}% (EPS/BPS)"})
    if cp and 0 < cp <= 10:
        # ✔검증(비금융만): 확정 실적 PER 10배 이하, 금융업을 뺀 한국 17개월 검증에서 60일 뒤 +1.36%p, 이긴 시점 53%, t=+2.1(약한 지지).
        # 금융 포함 수치(+2.14%p, t=+3.4)는 2025~26 금융주 랠리가 섞인 것이라 쓰지 않는다. 금융업·분류 불명(financial 없음)은 ✔ 없음.
        # (여기 PER은 컨센서스 추정 기준이라 근사)
        sig.append({"key": "저평가", "validated": d.get("financial") is False, "text": f"추정PER {cp:.1f}배"})
    if d["pbr"] and 0 < d["pbr"] < 1:
        # ✔ 없음: 금융을 빼면 PBR<1은 60일 +0.68%p, 이긴 시점 47%, t=+1.3으로 지지되지 않았다 (PBR<0.5는 t=+2.1로 약함)
        sig.append({"key": "자산대비저평가", "text": f"PBR {d['pbr']:.2f}배"})
    med = ind_median.get(d["industry"])
    if cp and med and 0 < cp <= med * 0.7:
        sig.append({"key": "업종대비저평가", "text": f"추정PER {cp:.1f}배 vs 업종 중앙 {med:.1f}배"})
    if d["eps"] and d["eps"] > 0 and d["cns_eps"]:
        g = d["cns_eps"] / d["eps"] - 1
        if g >= 0.2:
            sig.append({"key": "이익증가기대", "text": f"추정EPS가 현재 EPS 대비 {g * 100:+.0f}%"})
    if d["target"] and close and d["recomm"] and d["recomm"] >= 3.5:
        up = d["target"] / close - 1
        if 0.2 <= up <= 1.5:
            sig.append({"key": "컨센서스여력", "text": f"평균 목표가 대비 {up * 100:+.0f}% (투자의견 {d['recomm']:.1f}/5)"})
    if d["high52"] and close:
        dd = close / d["high52"] - 1
        if dd <= -0.3:
            sig.append({"key": "고점대비낙폭", "text": f"52주 고점 대비 {dd * 100:.0f}%"})
    # 가격 추세 신호(상대강도·추세회복)는 일부러 넣지 않는다: 2024-10~2026-10 한국 2년 검증에서 상대강도는 효과 없음(t=-0.9),
    # 추세회복(낙폭 큰 종목이 50일선 위로)은 시점의 21%에서만 시장을 이겨 유의하게 부진(t=-2.8)했다 (scripts/backtest_price_signals.py).
    # 이력(hist)은 업종 온도계·시장 폭·경고에만 쓴다.
    if d["flow_days"] >= 3 and d["foreign_buy_days"] >= 2 and d["organ_buy_days"] >= 2:
        sig.append({"key": "쌍끌이매수",
                    "text": f"최근 3일 중 외국인 {d['foreign_buy_days']}일·기관 {d['organ_buy_days']}일 순매수"})
    return sig


# 같은 성격의 신호가 겹쳐 점수가 부풀지 않게 묶음 단위로 센다
_GROUP = {"저평가": "가치", "자산대비저평가": "가치", "업종대비저평가": "가치",
          "이익증가기대": "이익", "컨센서스여력": "이익", "고점대비낙폭": "낙폭", "쌍끌이매수": "수급", "수익성": "질", "공시호재": "사건"}


FIN_KEYWORDS = ("은행", "증권", "보험", "카드", "금융", "캐피탈", "창업투자", "저축", "리스")


def financial_industry_codes(groups: list[dict]) -> set[str]:
    """네이버 업종 그룹 중 금융업(은행·증권·보험 등) 업종 번호. 이름을 못 받았으면 빈 집합."""
    return {str(g["no"]) for g in groups or [] if any(k in str(g.get("name", "")) for k in FIN_KEYWORDS)}


def validated_count(signals: list[dict]) -> int:
    """과거 검증을 통과한(validated) 신호의 묶음 수 (참고용 info 신호는 제외)."""
    return len({_GROUP[x["key"]] for x in signals if x.get("validated") and not x.get("info")})


def validated_groups_in(signals_str: str | None) -> int:
    """저장된 신호 문자열('저평가✔,공시호재✔,수익성(참고)')에서 검증 통과 묶음 수."""
    keys = [k.split("(")[0].rstrip("✔") for k in (signals_str or "").split(",") if "✔" in k and "(참고" not in k]
    return len({_GROUP[k] for k in keys if k in _GROUP})


def signal_label(sig: dict) -> str:
    return sig["key"] + ("✔" if sig.get("validated") and not sig.get("info") else "") + ("(참고)" if sig.get("info") else "")


def score(signals: list[dict]) -> int:
    """신호 묶음 수. 참고용(info) 신호는 세지 않는다."""
    return len({_GROUP[s["key"]] for s in signals if not s.get("info")})


# 중대 악재 공시가 최근에 있는 종목은 신호가 아무리 좋아도 후보에서 뺀다 (상폐·횡령·불성실공시는 '싸 보이는 이유'일 수 있다)
_SEVERE_EVENTS = {"상폐·부도위험", "횡령·배임·회계위반", "불성실공시"}


def trend_warnings(r: dict) -> list[str]:
    d = r.get("detail") or {}
    h = d.get("hist")
    if not h:
        return []
    out = []
    if h.get("ma200") and r["close"] < h["ma200"] * 0.8:
        out.append(f"장기 추세 아래: 200일선({h['ma200']:,.0f}) 대비 {(r['close'] / h['ma200'] - 1) * 100:.0f}%")
    if (h.get("ret_1m") or 0) >= 40:
        out.append(f"단기 급등: 1개월 {h['ret_1m']:+.0f}% — 과거 2년 검증에서 이런 종목은 이후 시장보다 부진한 경향(약함)")
    return out


def event_signals(events: list[dict]) -> list[dict]:
    """최근 공시 사건 중 호재 신호. 수치는 공시 본문에서 코드가 뽑은 summary를 그대로 쓴다.

    점수에 넣는 건 **과거 검증을 통과한 것만**이다: 자사주 취득 결정은 접수일 다음 거래일 이후 20일 중앙 초과수익
    +1.62%p, 이긴 시점 64%(t=+3.2, 2024-10~2026-10 한국, scripts/backtest_event_signals.py dart).
    대형 공급계약·잠정실적 호조는 아직 검증 전이라 '(참고)'로만 표시하고 점수에서 뺀다.
    """
    out = []
    for e in events or []:
        t, m = e["event_type"], e.get("metric")
        if t == "자사주취득":
            out.append({"key": "공시호재", "validated": True, "text": f"자사주 취득 결정 ({e['date']})"})
        elif t in ("자사주소각", "배당결정"):                     # 검증 표본 0건(주요사항보고서 목록에 없는 유형) — 미검증
            out.append({"key": "공시호재", "info": True, "text": f"(참고·미검증) {t} 결정 ({e['date']})"})
        elif t == "공급계약" and m is not None and m >= 10:
            out.append({"key": "공시호재", "info": True, "text": f"(참고·미검증) 공급계약 {e['summary']} ({e['date']})"})
    out.sort(key=lambda x: bool(x.get("info")))              # 검증된 것 먼저
    return out[:2]


def event_warnings(events: list[dict]) -> list[str]:
    """최근 악재성 공시 (유상증자·메자닌·감자·횡령·상폐·불성실·적자 실적). 중복 유형은 한 번만."""
    seen, out = set(), []
    for e in events or []:
        if e["polarity"] != "악재" or e["event_type"] in seen:
            continue
        seen.add(e["event_type"])
        extra = f" — {e['summary']}" if e.get("summary") else ""
        out.append(f"{e['event_type']} 공시 ({e['date']}){extra}")
    return out


def score_all(enriched: list[dict]) -> list[dict]:
    """상세 데이터가 있는 모든 종목에 신호·점수를 붙인다 (신호 없는 종목은 score 0)."""
    med = industry_medians(enriched)
    out = []
    for r in enriched:
        sig = compute_signals(r, med)
        ev = (r.get("detail") or {}).get("events") or []
        if sig:                                            # 적자 등으로 후보 자격이 없는 종목(sig 빈 목록)엔 붙이지 않는다
            sig = sig + event_signals(ev)
        out.append({**r, "signals": sig, "warnings": trend_warnings(r) + event_warnings(ev), "score": score(sig),
                    "blocked": any(e["event_type"] in _SEVERE_EVENTS and e["polarity"] == "악재" for e in ev)})
    return out


def pick_from(scored: list[dict], exclude: set[str] | None = None, top: int = 20, min_score: int = 3) -> list[dict]:
    exclude = exclude or set()
    out = [r for r in scored if r["score"] >= min_score and r["code"] not in exclude and not r.get("blocked")]
    out.sort(key=lambda x: (-validated_count(x["signals"]), -x["score"], -x["mcap_eok"]))     # 검증된 신호가 많은 종목 먼저
    return out[:top]


def screen(enriched: list[dict], exclude: set[str] | None = None, top: int = 20, min_score: int = 3) -> list[dict]:
    return pick_from(score_all(enriched), exclude, top, min_score)


def _industry_board(scored: list[dict], groups: list[dict] | None = None) -> dict | None:
    """업종 온도계. 실패해도 스크리닝 자체는 계속한다(None) — 단 잡 상태 상세에 '업종 온도계 없음'이 남는다."""
    try:
        from services import industry_board
        groups = groups if groups is not None else nv.industry_groups()
        board = industry_board.build(scored, groups)
        board["names_ok"] = bool(groups)                    # 업종 이름을 못 받으면 '업종NNN'으로 폴백 — 잡 상세에 남긴다
        return board
    except Exception as e:
        logger.warning("[전종목스크린] 업종 온도계 실패 (스크리닝은 계속): %s", e)
        return None


def run_screen(exclude: set[str] | None = None, top: int = 20, max_detail: int = MAX_DETAIL) -> dict:
    """전 종목 스크리닝 전체 실행. 결과 dict (발송·저장 없음). 'scored'는 상세 확인된 전 종목."""
    uni = fetch_universe()
    pool = tradable(uni["rows"])
    pool.sort(key=lambda r: -r["mcap_eok"])            # 시총 큰 종목부터 상세 조회
    detail_rows = enrich(pool[:max_detail])
    got = sum(1 for r in detail_rows if r.get("detail"))
    try:
        from services.disclosure_events import by_code
        events = by_code(days=7)
    except Exception as e:
        logger.warning("[전종목스크린] 공시 사건 조회 실패 (이 신호 없이 진행): %s", e)
        events = {}
    with_detail = [r for r in detail_rows if r.get("detail")]
    hist = fetch_history(with_detail) if with_detail else {}
    for r in detail_rows:
        if r.get("detail"):
            r["detail"]["events"] = events.get(r["code"], [])
            r["detail"]["hist"] = hist.get(r["code"])
    groups = nv.industry_groups()
    fin_inds = financial_industry_codes(groups)
    for r in detail_rows:                                      # 금융업 여부(은행·증권·보험…): 업종 이름을 못 받으면 None → 검증 표시를 달지 않는다
        if r.get("detail"):
            r["detail"]["financial"] = (str(r["detail"].get("industry")) in fin_inds) if groups else None
    scored = score_all([r for r in detail_rows if r.get("detail")])
    return {
        "as_of": datetime.now(_KST).strftime("%Y-%m-%d %H:%M"),
        "expected": uni["expected"], "fetched": uni["fetched"], "universe_complete": uni["complete"],
        "tradable": len(pool), "detailed": got, "detail_requested": len(detail_rows),
        "hist_ok": len(hist), "hist_requested": len(with_detail),
        "event_codes": len(events), "blocked": sum(1 for r in scored if r.get("blocked")),
        "industry_board": _industry_board(scored, groups),
        "scored": scored, "picks": pick_from(scored, exclude, top),
    }


# ── 일별 스냅샷 저장·조회 (쌓아야 '새로 켜진 신호'를 알 수 있다) ──────────────────

def save_daily(res: dict, date: str | None = None) -> int:
    """오늘 스냅샷을 날짜 단위로 통째 교체 저장. 저장 행 수 반환."""
    from db.database import get_conn
    from sqlalchemy import text
    date = date or datetime.now(_KST).strftime("%Y-%m-%d")
    rows = []
    for r in res.get("scored") or []:
        d = r["detail"]
        h = d.get("hist") or {}
        rows.append({
            "date": date, "code": r["code"], "name": r["name"], "market": r["market"], "close": r["close"],
            "chg_pct": r["chg_pct"], "mcap_eok": r["mcap_eok"], "per": d["per"], "pbr": d["pbr"],
            "cns_per": d["cns_per"], "eps": d["eps"], "target": d["target"], "high52": d["high52"],
            # 중대 악재 공시 종목은 후보 조회(load_picks)에서 빠지게 0점으로 저장
            "score": 0 if r.get("blocked") else r["score"],
            "signals": ",".join(signal_label(s) for s in r["signals"]),
            # 추정치·의견 시계열 (이익 리비전 등 앞으로 검증할 신호의 재료)
            "cns_eps": d.get("cns_eps"), "recomm": d.get("recomm"), "bps": d.get("bps"),
            "foreign_rate": d.get("foreign_rate"), "ret_1m": h.get("ret_1m"), "ret_3m": h.get("ret_3m"),
        })
    with get_conn() as conn:
        conn.execute(text("DELETE FROM universe_screen_daily WHERE date=:d"), {"d": date})
        if rows:
            conn.execute(text(
                "INSERT INTO universe_screen_daily (date, code, name, market, close, chg_pct, mcap_eok, per, pbr, "
                "cns_per, eps, target, high52, score, signals, cns_eps, recomm, bps, foreign_rate, ret_1m, ret_3m) "
                "VALUES (:date, :code, :name, :market, :close, :chg_pct, :mcap_eok, :per, :pbr, :cns_per, :eps, :target, "
                ":high52, :score, :signals, :cns_eps, :recomm, :bps, :foreign_rate, :ret_1m, :ret_3m)"), rows)
    return len(rows)


def load_picks(date: str | None = None, min_score: int = 3, limit: int = 20) -> list[dict]:
    """저장된 스냅샷에서 신호 묶음 min_score 이상 종목. date 생략 시 가장 최근 저장일. 없으면 []."""
    from db.database import get_conn
    from sqlalchemy import text
    try:
        with get_conn() as conn:
            if not date:
                date = conn.execute(text("SELECT max(date) FROM universe_screen_daily")).scalar()
            if not date:
                return []
            rows = conn.execute(text(
                "SELECT date, code, name, market, close, chg_pct, mcap_eok, score, signals FROM universe_screen_daily "
                "WHERE date=:d AND score>=:m ORDER BY score DESC, mcap_eok DESC LIMIT :n"),
                {"d": date, "m": min_score, "n": max(limit * 5, 100)}).fetchall()
        picks = [dict(zip(("date", "code", "name", "market", "close", "chg_pct", "mcap_eok", "score", "signals"), r)) for r in rows]
        picks.sort(key=lambda x: (-validated_groups_in(x["signals"]), -x["score"], -x["mcap_eok"]))   # 검증된 신호가 많은 종목 먼저
        return picks[:limit]
    except Exception as e:
        logger.warning("[전종목스크린] 스냅샷 조회 실패: %s", e)
        return []


def streaks(picks: list[dict], lookback_days: int = 10) -> dict[str, int]:
    """picks 각 종목이 최근 며칠 연속(저장된 날 기준) 신호 3묶음 이상이었는지. {code: 연속일수}"""
    from db.database import get_conn
    from sqlalchemy import text
    if not picks:
        return {}
    try:
        with get_conn() as conn:
            dates = [r[0] for r in conn.execute(text(
                "SELECT DISTINCT date FROM universe_screen_daily ORDER BY date DESC LIMIT :n"), {"n": lookback_days})]
            hit = conn.execute(text(
                "SELECT date, code FROM universe_screen_daily WHERE score>=3 AND date>=:oldest"),
                {"oldest": dates[-1]}).fetchall() if dates else []
    except Exception as e:
        logger.warning("[전종목스크린] 연속일수 조회 실패: %s", e)
        return {}
    by_code: dict[str, set[str]] = {}
    for d, c in hit:
        by_code.setdefault(c, set()).add(d)
    out = {}
    for p in picks:
        n = 0
        for d in dates:                                 # 최신일부터 끊기기 전까지
            if d in by_code.get(p["code"], ()):
                n += 1
            else:
                break
        out[p["code"]] = n
    return out


def run_daily_job() -> tuple[str, str]:
    """매일 장마감 후 무발송 잡 본체. (status, detail) — job_runs 규약: 0건=empty, 부분실패=fail."""
    res = run_screen()
    if not res["universe_complete"]:
        return "fail", f"목록 수신 불완전 {res['fetched']}/{res['expected']}"
    if res["detail_requested"] and res["detailed"] < res["detail_requested"] * 0.8:
        return "fail", f"상세 조회 {res['detailed']}/{res['detail_requested']}만 성공 (80% 미만)"
    hist_req, hist_ok = res.get("hist_requested", 0), res.get("hist_ok", 0)
    if hist_req and hist_ok < hist_req * 0.5:
        return "fail", f"추세 이력 {hist_ok}/{hist_req}만 수신 (50% 미만 — 추세 신호 없이 저장하지 않음)"
    n = save_daily(res)
    if n == 0:
        return "empty", "저장 0건"
    board_note = ""
    board = res.get("industry_board")
    if board and board.get("industries"):
        try:
            from services import industry_board
            board_note = f" · 업종 {industry_board.save_daily(board)}개"
            if not board.get("names_ok", True):
                board_note += " ⚠업종 이름 미수신(번호로 표시)"
        except Exception as e:
            logger.warning("[전종목스크린] 업종 온도계 저장 실패: %s", e)
            board_note = " · ⚠업종 온도계 저장 실패"
    elif "industry_board" in res:
        board_note = " · ⚠업종 온도계 없음"
    return "success", (f"저장 {n}종목 · 신호 3묶음+ {len(res['picks'])}종목 · 수신 {res['fetched']}/{res['expected']}"
                       f" · 추세 {hist_ok}/{hist_req}{board_note}")


def format_screen(res: dict) -> str:
    lines = [f"=== 한국 전 종목 스크리닝 ({res['as_of']}) ===",
             f"전체 {res['fetched']}/{res['expected']}종목 수신"
             + ("" if res["universe_complete"] else " ⚠불완전(수신 90% 미만 — 결과 신뢰 낮음)")
             + f" → 거래 가능 {res['tradable']}종목 → 상세 확인 {res['detailed']}/{res['detail_requested']}종목"
             + f" · 추세 이력 {res.get('hist_ok', 0)}/{res.get('hist_requested', 0)}"
             + f" · 최근 7일 공시 사건 {res.get('event_codes', 0)}종목 · 중대 악재로 제외 {res.get('blocked', 0)}종목"]
    if not res["picks"]:
        lines.append("신호가 3묶음 이상 겹치는 종목 없음")
    for p in res["picks"]:
        chg = f"{p['chg_pct']:+.2f}%" if p["chg_pct"] is not None else "N/A"
        lines.append(f"\n{p['name']}({p['code']}) [{p['market']}] {p['close']:,.0f}원 · 시총 {p['mcap_eok']:,.0f}억 · 오늘 {chg}  [신호 {p['score']}묶음]")
        for s in p["signals"]:
            lines.append(f"  · {signal_label(s)}: {s['text']}")
    return "\n".join(lines)


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    print(format_screen(run_screen()))
