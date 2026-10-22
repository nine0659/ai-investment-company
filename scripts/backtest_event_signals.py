"""사건 기반 신호 과거 검증 (일회성 연구 스크립트 — CI 게이트 아님, 운영 코드에서 호출하지 않는다).

  insider : 미국 임원·이사 공개시장 매수 군집(SEC Form 4 분기 데이터셋 8분기) 이후 수익률 vs 같은 시점 전 종목 중앙값
  dart    : 한국 DART 공시 사건(자사주취득·소각·배당 = 호재 / 유상증자·메자닌·감자 = 악재) 이후 수익률 vs 전 종목 중앙값

룩어헤드 방지: 내부자는 *제출일*이 시점 이전인 거래만, DART 사건은 접수일 **다음 거래일 종가**에 진입한다.
한계: 현재 시총 기준 유니버스(생존 편향), 사건이 겹치는 종목/시점은 독립 표본이 아님, 2년은 한 사이클이 안 됨.
선행 조건: scripts/backtest_price_signals.py를 market(kr/us)별로 한 번 실행해 일봉 캐시(임시 폴더 *_prices_2y.pkl)를 만들어 둘 것.
실행: PYTHONUTF8=1 python scripts/backtest_event_signals.py insider|dart
"""
import logging
import os
import pickle
import re
import statistics
import sys
import tempfile
import time
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
logging.disable(logging.CRITICAL)

import numpy as np
import pandas as pd

HORIZONS = (5, 20, 60)


def load_prices(market: str) -> pd.DataFrame:
    path = os.path.join(tempfile.gettempdir(), f"{market}_prices_2y.pkl")
    if not os.path.exists(path):
        sys.exit(f"일봉 캐시가 없습니다: 먼저 `python scripts/backtest_price_signals.py {market}` 를 실행하세요.")
    with open(path, "rb") as f:
        return pickle.load(f)[0]


def fwd_excess(closes: pd.DataFrame, code: str, entry_pos: int, h: int) -> float | None:
    """entry_pos 거래일 종가 → h거래일 뒤 수익률(%) − 같은 구간 전 종목 중앙값."""
    if code not in closes.columns or entry_pos + h >= len(closes):
        return None
    a, b = closes[code].iloc[entry_pos], closes[code].iloc[entry_pos + h]
    if not (a > 0 and b > 0) or np.isnan(a) or np.isnan(b):
        return None
    allr = (closes.iloc[entry_pos + h] / closes.iloc[entry_pos] - 1) * 100
    return float((b / a - 1) * 100 - np.nanmedian(allr))


def summarize(name: str, by_h: dict[int, list[float]], per_date: dict[int, dict]) -> str:
    out = []
    for h in HORIZONS:
        v = by_h.get(h, [])
        if len(v) < 20:
            out.append(f"{name:<40}{h:>4}일  관측 {len(v)}건 (20건 미만 — 판단 불가)")
            continue
        pdm = [statistics.median(x) for x in per_date.get(h, {}).values() if len(x) >= 3]
        t = ""
        if len(pdm) >= 5 and statistics.pstdev(pdm) > 0:
            tv = statistics.fmean(pdm) / (statistics.pstdev(pdm) / len(pdm) ** 0.5)
            t = f"  시점수 {len(pdm)} · +인 시점 {sum(1 for x in pdm if x > 0) / len(pdm) * 100:.0f}% · t={tv:+.1f}"
        out.append(f"{name:<40}{h:>4}일  관측 {len(v):>5}건 · 중앙 초과 {statistics.median(v):>+6.2f}%p · "
                   f"이긴 비율 {sum(1 for x in v if x > 0) / len(v) * 100:>3.0f}%{t}")
    return "\n".join(out)


# ── 미국 내부자 매수 ─────────────────────────────────────────────────────────

def insider_study() -> str:
    import requests
    from clients.sec_insider_client import _SEC, latest_zip_urls, parse_zip
    closes = load_prices("us")
    t2c = {v["ticker"].upper(): int(v["cik_str"]) for v in requests.get(
        "https://www.sec.gov/files/company_tickers.json", headers=_SEC, timeout=30).json().values()}
    c2t = {c: t for t, c in t2c.items() if t in closes.columns}
    rows = []
    for u in latest_zip_urls(8):
        r = requests.get(u, headers=_SEC, timeout=180)
        r.raise_for_status()
        rows += parse_zip(r.content)
        print("  내부자 분기 로드:", u.rsplit("/", 1)[-1], len(rows), flush=True)
    rows = [r for r in rows if r["cik"] in c2t and r["date"] and r["filed"] and not r["plan"]
            and re.search(r"Officer|Director", r["role"], re.I)]
    first = rows and min(r["filed"] for r in rows)
    print(f"  유니버스 내 임원·이사 매수 {len(rows)}건, 가장 이른 제출일 {first}", flush=True)
    idx = closes.index
    dates = list(range(130, len(idx) - 5, 10))
    variants = {
        "군집 매수(2명+ & $10만+)": lambda n, v: n >= 2 and v >= 100_000,
        "강한 군집(3명+ & $50만+)": lambda n, v: n >= 3 and v >= 500_000,
        "단독 대형 매수($100만+)": lambda n, v: v >= 1_000_000,
        "소액 단독($10만 미만 1명)": lambda n, v: n == 1 and v < 100_000,
    }
    by = {k: {h: [] for h in HORIZONS} for k in variants}
    pdt = {k: {h: {} for h in HORIZONS} for k in variants}
    for pos in dates:
        d = idx[pos].date()
        lo = d - timedelta(days=90)
        agg: dict[int, dict] = {}
        for r in rows:
            if r["filed"] <= d and lo < r["date"] <= d:
                a = agg.setdefault(r["cik"], {"owners": set(), "value": 0.0})
                a["owners"].add(r["owner"])
                a["value"] += r["value"]
        for name, cond in variants.items():
            for cik, a in agg.items():
                if cond(len(a["owners"]), a["value"]):
                    for h in HORIZONS:
                        e = fwd_excess(closes, c2t[cik], pos, h)
                        if e is not None:
                            by[name][h].append(e)
                            pdt[name][h].setdefault(pos, []).append(e)
    return "\n".join(summarize(k, by[k], pdt[k]) for k in variants)


# ── 한국 DART 공시 ───────────────────────────────────────────────────────────

def dart_events(months: int = 24) -> list[dict]:
    import requests
    from config.settings import DART_API_KEY
    from services.disclosure_events import classify
    end, out = date.today(), []
    start = end - timedelta(days=months * 30)
    cur = start
    while cur < end:
        win_end = min(cur + timedelta(days=80), end)
        for cls in ("Y", "K"):
            p = 1
            while True:
                r = requests.get("https://opendart.fss.or.kr/api/list.json", params={
                    "crtfc_key": DART_API_KEY, "bgn_de": cur.strftime("%Y%m%d"), "end_de": win_end.strftime("%Y%m%d"),
                    "pblntf_ty": "B", "corp_cls": cls, "page_no": p, "page_count": 100}, timeout=30).json()
                if r.get("status") not in ("000",):
                    break
                for it in r.get("list") or []:
                    c = classify(it.get("report_nm"))
                    if c and it.get("stock_code"):
                        out.append({"code": it["stock_code"], "date": datetime.strptime(it["rcept_dt"], "%Y%m%d").date(),
                                    "type": c[0], "pol": c[1]})
                if p >= int(r.get("total_page") or 1):
                    break
                p += 1
        print(f"  DART {cur}~{win_end}: 누적 {len(out)}건", flush=True)
        cur = win_end + timedelta(days=1)
    return out


def dart_study() -> str:
    closes = load_prices("kr")
    evs = dart_events()
    idx = pd.DatetimeIndex(closes.index).tz_localize(None).normalize()
    groups = {"자사주취득": ["자사주취득"], "자사주소각": ["자사주소각"], "배당결정": ["배당결정"],
              "유상증자": ["유상증자"], "메자닌발행": ["메자닌발행"], "감자": ["감자"],
              "호재 묶음(취득·소각·배당)": ["자사주취득", "자사주소각", "배당결정"],
              "악재 묶음(증자·메자닌·감자)": ["유상증자", "메자닌발행", "감자"]}
    by = {k: {h: [] for h in HORIZONS} for k in groups}
    pdt = {k: {h: {} for h in HORIZONS} for k in groups}
    seen = set()
    for e in evs:
        key = (e["code"], e["date"], e["type"])
        if key in seen or e["code"] not in closes.columns:
            continue
        seen.add(key)
        pos = idx.searchsorted(pd.Timestamp(e["date"]), side="right")      # 접수일 *다음* 거래일 (룩어헤드 방지)
        if pos >= len(idx):
            continue
        for g, types in groups.items():
            if e["type"] in types:
                for h in HORIZONS:
                    x = fwd_excess(closes, e["code"], pos, h)
                    if x is not None:
                        by[g][h].append(x)
                        pdt[g][h].setdefault(pos // 10, []).append(x)         # 약 2주 단위 시점 묶음
    return "\n".join(summarize(k, by[k], pdt[k]) for k in groups)


if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else "insider"
    t0 = time.time()
    if which == "insider":
        print("미국 내부자 매수 군집 이후 수익률 vs 같은 시점 전 종목 중앙값 (제출일 이후 정보만 사용)", flush=True)
        print(insider_study())
    elif which == "dart":
        print("한국 DART 공시 사건 접수일 다음 거래일 이후 수익률 vs 전 종목 중앙값", flush=True)
        print(dart_study())
    else:
        sys.exit("사용법: insider | dart")
    print(f"\n※ 생존 편향·겹치는 표본·짧은 기간 — 방향성 참고용. ({time.time() - t0:.0f}초)")
