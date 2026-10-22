"""한국 재무 신호(저PER·저PBR·ROE·이익 증가) 과거 검증 (일회성 연구 스크립트 — CI 게이트 아님, 운영 코드에서 호출하지 않는다).

DART 다중회사 재무 API(fnlttMultiAcnt)로 사업보고서(FY2024·FY2025) 숫자를 받아, **보고서가 공개된 뒤의 시점에만** 그 숫자로 신호를 계산한다:
  FY2024 보고서 → 2025-04-01 ~ 2026-03-31 시점 / FY2025 보고서 → 2026-04-01 이후 시점  (사업보고서 법정 제출기한 3/31)
시총은 '현재 시총 × 그 시점 주가 / 현재 주가'로 근사한다(주식 수가 변하지 않았다고 가정 — 증자·분할이 큰 종목은 어긋남).
20·60거래일 뒤 수익률을 같은 시점 전 종목 중앙값과 비교하고 시점별 일관성(이긴 시점 비율·t값)으로 판정한다.

한계: ① 실제 점수에 쓰는 신호는 '추정'(컨센서스) 기반인데 여기선 '확정 실적' 기반 근사 ② 현재 시총 기준 유니버스(생존 편향)
③ 약 17개월(두 개 보고서 주기)이라 짧음 ④ 시점이 겹쳐 독립 표본이 아님. 방향성 참고용.
선행: python scripts/backtest_price_signals.py kr  (일봉 캐시 생성)
실행: PYTHONUTF8=1 python scripts/backtest_fundamental_signals.py
"""
import logging
import os
import pickle
import statistics
import sys
import tempfile
import time
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
logging.disable(logging.CRITICAL)

import numpy as np
import pandas as pd
import requests

from backtest_event_signals import HORIZONS, load_prices, summarize  # noqa: E402

_NI = ("당기순이익(손실)", "당기순이익", "연결당기순이익", "분기순이익")
_EQ = ("자본총계",)
_LIAB = ("부채총계",)
_SALES = ("매출액", "영업수익", "수익(매출액)")
_OP = ("영업이익", "영업이익(손실)")


def _amt(s) -> float | None:
    try:
        return float(str(s).replace(",", "").strip())
    except ValueError:
        return None


def fetch_financials(codes: list[str], years=(2024, 2025)) -> dict:
    """{종목코드: {연도: {ni, equity}}} — 사업보고서(11011), 연결(CFS) 우선·없으면 별도(OFS). 전년 값도 함께 채운다."""
    from config.settings import DART_API_KEY
    from clients.dart_client import _load_corp_map
    cm = _load_corp_map()
    code_by_corp = {cm[c]: c for c in codes if c in cm}
    corps = list(code_by_corp)
    out: dict[str, dict] = {}
    sess = requests.Session()
    for yr in years:
        for i in range(0, len(corps), 100):
            part = corps[i:i + 100]
            try:
                r = sess.get("https://opendart.fss.or.kr/api/fnlttMultiAcnt.json", timeout=60, params={
                    "crtfc_key": DART_API_KEY, "corp_code": ",".join(part), "bsns_year": str(yr), "reprt_code": "11011"}).json()
            except Exception:
                continue
            if r.get("status") != "000":
                continue
            by: dict[tuple, dict] = {}
            for x in r.get("list") or []:
                key = (x["stock_code"], x["fs_div"])
                d = by.setdefault(key, {})
                if x["sj_div"] == "IS" and x["account_nm"] in _NI and "ni" not in d:
                    d["ni"] = (_amt(x["thstrm_amount"]), _amt(x.get("frmtrm_amount")))
                if x["sj_div"] == "BS" and x["account_nm"] in _EQ and "eq" not in d:
                    d["eq"] = (_amt(x["thstrm_amount"]), _amt(x.get("frmtrm_amount")))
                if x["sj_div"] == "BS" and x["account_nm"] in _LIAB and "liab" not in d:
                    d["liab"] = (_amt(x["thstrm_amount"]), _amt(x.get("frmtrm_amount")))
                if x["sj_div"] in ("IS", "CIS") and x["account_nm"] in _SALES and "sales" not in d:
                    d["sales"] = (_amt(x["thstrm_amount"]), _amt(x.get("frmtrm_amount")))
                if x["sj_div"] in ("IS", "CIS") and x["account_nm"] in _OP and "op" not in d:
                    d["op"] = (_amt(x["thstrm_amount"]), _amt(x.get("frmtrm_amount")))
            for code in {k[0] for k in by}:
                d = by.get((code, "CFS")) or by.get((code, "OFS")) or {}
                if "ni" in d and "eq" in d:
                    rec = out.setdefault(code, {})
                    rec.setdefault(yr, {})["ni"], rec[yr]["eq"] = d["ni"][0], d["eq"][0]
                    for k in ("liab", "sales", "op"):
                        if k in d:
                            rec[yr][k] = d[k][0]
                    rec.setdefault(yr - 1, {}).setdefault("ni", d["ni"][1])
                    rec[yr - 1].setdefault("eq", d["eq"][1])
        print(f"  FY{yr} 재무 수신: 누적 {len(out)}개 회사", flush=True)
    return out


_FIN_KEYWORDS = ("은행", "증권", "보험", "카드", "금융", "캐피탈", "창업투자", "저축", "리스")


def financial_codes(codes: list[str]) -> set[str]:
    """금융업 종목 코드 집합 — 네이버 업종 이름에 금융 키워드가 들어간 종목. 부채비율이 구조적으로 높고(은행·증권·보험)
    2025~26 한국 금융주 랠리가 '가치/고부채' 결과를 왜곡할 수 있어 따로 떼어 본다. 결과는 임시 폴더에 하루 캐시."""
    from concurrent.futures import ThreadPoolExecutor
    from clients import naver_client as nv
    cache = os.path.join(tempfile.gettempdir(), "kr_fin_codes.pkl")
    if os.path.exists(cache) and time.time() - os.path.getmtime(cache) < 86400:
        with open(cache, "rb") as f:
            return pickle.load(f)
    names = {str(g["no"]): g.get("name", "") for g in nv.industry_groups()}
    def one(c):
        ind = str((nv.stock_integration(c) or {}).get("industryCode") or "")
        return c, names.get(ind, "")
    with ThreadPoolExecutor(max_workers=6) as ex:
        res = list(ex.map(one, codes))
    fin = {c for c, nm in res if any(k in nm for k in _FIN_KEYWORDS)}
    print(f"  금융업 판정 {len(fin)}개 / {len(codes)}개 (업종 이름 {sorted({nm for c, nm in res if c in fin})[:12]})", flush=True)
    with open(cache, "wb") as f:
        pickle.dump(fin, f)
    return fin


def run() -> str:
    from services import universe_screener as us
    closes = load_prices("kr")
    uni = us.fetch_universe()
    mcap_now = {r["code"]: r["mcap_eok"] * 1e8 for r in us.tradable(uni["rows"])}      # 원
    cache = os.path.join(tempfile.gettempdir(), "kr_fin2_2y.pkl")
    if os.path.exists(cache) and time.time() - os.path.getmtime(cache) < 86400:
        with open(cache, "rb") as f:
            fin = pickle.load(f)
    else:
        fin = fetch_financials([c for c in closes.columns if c in mcap_now])
        with open(cache, "wb") as f:
            pickle.dump(fin, f)
    fin_set = financial_codes([c for c in closes.columns if c in mcap_now])
    idx = pd.DatetimeIndex(closes.index).tz_localize(None).normalize()
    last = closes.iloc[-1]
    by = {}
    pdt = {}
    for pos in range(len(idx) - 20):
        d = idx[pos].date()
        if d < date(2025, 4, 1) or (pos % 10):
            continue
        fy = 2024 if d < date(2026, 4, 1) else 2025
        rows = []
        for code in closes.columns:
            f = (fin.get(code) or {}).get(fy)
            px, now = closes[code].iloc[pos], last.get(code)
            if not f or not mcap_now.get(code) or not (px > 0 and now > 0):
                continue
            ni, eq = f.get("ni"), f.get("eq")
            prev = (fin.get(code) or {}).get(fy - 1, {}).get("ni")
            if ni is None or not eq or eq <= 0:
                continue
            mc = mcap_now[code] * px / now
            liab, sales, op = f.get("liab"), f.get("sales"), f.get("op")
            rows.append({"code": code, "fin": code in fin_set, "per": mc / ni if ni > 0 else None, "pbr": mc / eq, "roe": ni / eq, "mc": mc,
                         "debt": liab / eq * 100 if liab is not None else None,
                         "opm": op / sales * 100 if op is not None and sales and sales > 0 else None,
                         "growth": (ni / prev - 1) * 100 if prev and prev > 0 and ni > 0 else None})
        if len(rows) < 150:
            continue
        df = pd.DataFrame(rows).set_index("code")
        per_q = df["per"].quantile([0.2, 0.4]).tolist() if df["per"].notna().sum() > 50 else [None, None]
        masks = {
            "저PER(상위 20%, PER>0)": df["per"].notna() & (df["per"] <= per_q[0]),
            "PER 10배 이하": df["per"].notna() & (df["per"] <= 10),
            "PBR 1배 미만": df["pbr"] < 1,
            "PBR 0.5배 미만": df["pbr"] < 0.5,
            "ROE 10%+": df["roe"] >= 0.10,
            "ROE 10%+ AND PBR<1": (df["roe"] >= 0.10) & (df["pbr"] < 1),
            "ROE 10%+ AND PER≤10": (df["roe"] >= 0.10) & df["per"].notna() & (df["per"] <= 10),
            "순이익 +20%+ (실현)": df["growth"].notna() & (df["growth"] >= 20),
            "순이익 +20%+ AND PER≤10": df["growth"].notna() & (df["growth"] >= 20) & df["per"].notna() & (df["per"] <= 10),
            "ROE 하위(<3%) 또는 적자": (df["roe"] < 0.03),
            "PER≤10 AND 부채비율 100% 이하": df["per"].notna() & (df["per"] <= 10) & df["debt"].notna() & (df["debt"] <= 100),
            "PER≤10 AND 부채비율 300%+": df["per"].notna() & (df["per"] <= 10) & df["debt"].notna() & (df["debt"] >= 300),
            "PER≤10 AND 영업이익률 10%+": df["per"].notna() & (df["per"] <= 10) & df["opm"].notna() & (df["opm"] >= 10),
            "PER≤10 AND 영업이익률 5% 미만": df["per"].notna() & (df["per"] <= 10) & df["opm"].notna() & (df["opm"] < 5),
            "PER≤10 AND PBR<1": df["per"].notna() & (df["per"] <= 10) & (df["pbr"] < 1),
            "PER≤10 — 소형(시총 하위 1/3)": df["per"].notna() & (df["per"] <= 10) & (df["mc"] <= df["mc"].quantile(1 / 3)),
            "PER≤10 — 중형(중간 1/3)": df["per"].notna() & (df["per"] <= 10) & (df["mc"] > df["mc"].quantile(1 / 3)) & (df["mc"] <= df["mc"].quantile(2 / 3)),
            "PER≤10 — 대형(상위 1/3)": df["per"].notna() & (df["per"] <= 10) & (df["mc"] > df["mc"].quantile(2 / 3)),
            "부채비율 300%+ (가치 무관)": df["debt"].notna() & (df["debt"] >= 300),
            # ── 금융업을 떼어 본다 (금융주 랠리가 결과를 왜곡하는지) ──
            "[비금융] PER≤10": ~df["fin"] & df["per"].notna() & (df["per"] <= 10),
            "[비금융] PBR<1": ~df["fin"] & (df["pbr"] < 1),
            "[비금융] PBR<0.5": ~df["fin"] & (df["pbr"] < 0.5),
            "[비금융] 부채비율 300%+": ~df["fin"] & df["debt"].notna() & (df["debt"] >= 300),
            "[비금융] PER≤10 AND 부채비율 100% 이하": ~df["fin"] & df["per"].notna() & (df["per"] <= 10) & df["debt"].notna() & (df["debt"] <= 100),
            "[비금융] 대형 PER≤10": ~df["fin"] & df["per"].notna() & (df["per"] <= 10) & (df["mc"] > df["mc"].quantile(2 / 3)),
            "[금융] 전체": df["fin"],
            "[금융] PER≤10": df["fin"] & df["per"].notna() & (df["per"] <= 10),
        }
        for h in HORIZONS:
            if h == 5 or pos + h >= len(idx):
                continue
            allr = (closes.iloc[pos + h] / closes.iloc[pos] - 1) * 100
            bench = float(np.nanmedian(allr))
            for name, m in masks.items():
                vals = [float(allr[c]) - bench for c in df.index[m.values] if c in allr.index and not np.isnan(allr[c])]
                by.setdefault(name, {}).setdefault(h, []).extend(vals)
                pdt.setdefault(name, {}).setdefault(h, {}).setdefault(pos, []).extend(vals)
    for k in by:
        for h in HORIZONS:
            by[k].setdefault(h, [])
            pdt[k].setdefault(h, {})
    return "\n".join(summarize(k, by[k], pdt[k]) for k in by)


if __name__ == "__main__":
    t0 = time.time()
    print("한국 재무 신호(확정 실적 기준, 공개 시점 이후만) 과거 검증 — 20·60거래일 뒤 vs 같은 시점 전 종목 중앙값", flush=True)
    print(run())
    print(f"\n※ 약 17개월·현재 시총 기준 유니버스·시총 근사 — 방향성 참고용. ({time.time() - t0:.0f}초)")
