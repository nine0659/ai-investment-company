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
        sig.append({"key": "수익성", "text": f"ROE 약 {d['eps'] / d['bps'] * 100:.0f}% (EPS/BPS)"})
    if cp and 0 < cp <= 10:
        sig.append({"key": "저평가", "text": f"추정PER {cp:.1f}배"})
    if d["pbr"] and 0 < d["pbr"] < 1:
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
    if d["flow_days"] >= 3 and d["foreign_buy_days"] >= 2 and d["organ_buy_days"] >= 2:
        sig.append({"key": "쌍끌이매수",
                    "text": f"최근 3일 중 외국인 {d['foreign_buy_days']}일·기관 {d['organ_buy_days']}일 순매수"})
    return sig


# 같은 성격의 신호가 겹쳐 점수가 부풀지 않게 묶음 단위로 센다
_GROUP = {"저평가": "가치", "자산대비저평가": "가치", "업종대비저평가": "가치",
          "이익증가기대": "이익", "컨센서스여력": "이익", "고점대비낙폭": "낙폭", "쌍끌이매수": "수급", "수익성": "질"}


def score(signals: list[dict]) -> int:
    return len({_GROUP[s["key"]] for s in signals})


def screen(enriched: list[dict], exclude: set[str] | None = None, top: int = 20, min_score: int = 3) -> list[dict]:
    exclude = exclude or set()
    med = industry_medians(enriched)
    out = []
    for r in enriched:
        if r["code"] in exclude:
            continue
        sig = compute_signals(r, med)
        sc = score(sig)
        if sc >= min_score:
            out.append({**r, "signals": sig, "score": sc})
    out.sort(key=lambda x: (-x["score"], -x["mcap_eok"]))
    return out[:top]


def run_screen(exclude: set[str] | None = None, top: int = 20, max_detail: int = MAX_DETAIL) -> dict:
    """전 종목 스크리닝 전체 실행. 결과 dict (발송·저장 없음)."""
    uni = fetch_universe()
    pool = tradable(uni["rows"])
    pool.sort(key=lambda r: -r["mcap_eok"])            # 시총 큰 종목부터 상세 조회
    detail_rows = enrich(pool[:max_detail])
    got = sum(1 for r in detail_rows if r.get("detail"))
    return {
        "as_of": datetime.now(_KST).strftime("%Y-%m-%d %H:%M"),
        "expected": uni["expected"], "fetched": uni["fetched"], "universe_complete": uni["complete"],
        "tradable": len(pool), "detailed": got, "detail_requested": len(detail_rows),
        "picks": screen(detail_rows, exclude, top),
    }


def format_screen(res: dict) -> str:
    lines = [f"=== 한국 전 종목 스크리닝 ({res['as_of']}) ===",
             f"전체 {res['fetched']}/{res['expected']}종목 수신"
             + ("" if res["universe_complete"] else " ⚠불완전(수신 90% 미만 — 결과 신뢰 낮음)")
             + f" → 거래 가능 {res['tradable']}종목 → 상세 확인 {res['detailed']}/{res['detail_requested']}종목"]
    if not res["picks"]:
        lines.append("신호가 3묶음 이상 겹치는 종목 없음")
    for p in res["picks"]:
        chg = f"{p['chg_pct']:+.2f}%" if p["chg_pct"] is not None else "N/A"
        lines.append(f"\n{p['name']}({p['code']}) [{p['market']}] {p['close']:,.0f}원 · 시총 {p['mcap_eok']:,.0f}억 · 오늘 {chg}  [신호 {p['score']}묶음]")
        for s in p["signals"]:
            lines.append(f"  · {s['key']}: {s['text']}")
    return "\n".join(lines)


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    print(format_screen(run_screen()))
