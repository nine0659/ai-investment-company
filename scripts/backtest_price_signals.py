"""가격 기반 신호 과거 검증 (일회성 연구 스크립트 — CI 게이트 아님, 운영 코드에서 호출하지 않는다).

한국 스크리너의 가격 신호(상대강도·추세회복·낙폭·200일선 훼손)가 과거에 실제로 시장(전 종목 중앙값)을 이겼는지
2년치 일봉으로 10거래일 간격 시점마다 신호를 다시 계산하고 20거래일 뒤 수익률과 비교한다.

한계(결과를 읽을 때 반드시 감안): ① 현재 시총 1,000억+ 종목만 본 생존 편향 ② 재무·컨센서스·수급 신호는 과거 스냅샷이 없어 검증 불가
③ 시점들이 겹쳐 독립 표본이 아님 ④ 2년은 한 사이클도 안 되는 짧은 기간.
실행: PYTHONUTF8=1 python scripts/backtest_price_signals.py [kr|us]   (기본 kr)
"""
import logging
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
logging.disable(logging.CRITICAL)

import numpy as np
import pandas as pd

FWD = 20          # 보유 기간(거래일)
STEP = 10         # 시점 간격(거래일)
WARM = 130        # 신호 계산에 필요한 최소 이력


def load_closes(period: str = "2y", market: str = "kr") -> tuple[pd.DataFrame, pd.DataFrame]:
    """일봉 다운로드(약 7분). 하루 안에 다시 실행하면 임시 폴더의 캐시를 쓴다."""
    import os
    import pickle
    import tempfile
    import time
    cache = os.path.join(tempfile.gettempdir(), f"{market}_prices_2y.pkl")
    if os.path.exists(cache) and time.time() - os.path.getmtime(cache) < 86400:
        with open(cache, "rb") as f:
            return pickle.load(f)
    out = _download(period, market)
    with open(cache, "wb") as f:
        pickle.dump(out, f)
    return out


def _universe(market: str) -> dict[str, str]:
    """{yfinance 티커: 종목 키}"""
    if market == "us":
        from services import us_universe_screener as uus
        pool = sorted(uus.tradable(uus.fetch_universe()["rows"]), key=lambda r: -r["mcap_usd"])[:uus.MAX_UNIVERSE]
        return {r["code"]: r["code"] for r in pool}
    from services import universe_screener as us
    return us._yf_tickers(us.tradable(us.fetch_universe()["rows"]))


def _download(period: str, market: str = "kr") -> tuple[pd.DataFrame, pd.DataFrame]:
    import yfinance as yf
    tmap = _universe(market)
    items = list(tmap)
    closes, highs = {}, {}
    for i in range(0, len(items), 100):
        part = items[i:i + 100]
        try:
            df = yf.download(part, period=period, interval="1d", group_by="ticker", progress=False,
                             threads=False, auto_adjust=False)
        except Exception as e:
            print("묶음 실패", i, e, flush=True)
            continue
        for tk in part:
            try:
                c = df[tk]["Close"].dropna()
                if len(c) >= WARM + FWD + 20:
                    closes[tmap[tk]] = c
                    highs[tmap[tk]] = df[tk]["High"].reindex(c.index)
            except Exception:
                continue
        print(f"  다운로드 {min(i + 100, len(items))}/{len(items)} (유효 {len(closes)})", flush=True)
    return pd.DataFrame(closes).sort_index(), pd.DataFrame(highs).sort_index()


def run(closes: pd.DataFrame, highs: pd.DataFrame) -> dict:
    n = len(closes)
    groups: dict[str, list[float]] = {}
    per_date: dict[str, list[float]] = {}          # 시점별 '그 시점 후보들의 중앙 초과수익' — 일관성 확인용
    for t in range(WARM, n - FWD, STEP):
        w = closes.iloc[: t + 1]
        last = w.iloc[-1]
        valid = last.notna() & closes.iloc[t + FWD].notna()
        fwd = (closes.iloc[t + FWD] / last - 1) * 100
        bench = float(np.nanmedian(fwd[valid]))
        ret3 = (last / w.iloc[-64] - 1) * 100
        ret1 = (last / w.iloc[-22] - 1) * 100
        ma50 = w.iloc[-50:].mean()
        ma200 = w.iloc[-200:].mean() if len(w) >= 200 else pd.Series(np.nan, index=last.index)
        hi = highs.iloc[max(0, t - 251): t + 1].max()
        dd = last / hi - 1
        rs_cut = np.nanpercentile(ret3[valid], 70)
        masks = {
            "상대강도(3개월 상위30%)": ret3 >= rs_cut,
            "추세회복(낙폭≥20%·50일선 위·1개월 +)": (dd <= -0.2) & (last > ma50) & (ret1 > 0),
            "상대강도 AND 추세회복": (ret3 >= rs_cut) & (dd <= -0.2) & (last > ma50) & (ret1 > 0),
            "낙폭≥30%(추세 무시)": dd <= -0.3,
            "낙폭≥30% AND 200일선 -20% 미만(떨어지는 칼날)": (dd <= -0.3) & (last < ma200 * 0.8),
            "낙폭≥30% AND 50일선 위(반등 확인)": (dd <= -0.3) & (last > ma50),
            "200일선 위 AND 상대강도": (last > ma200) & (ret3 >= rs_cut),
            "과열(1개월 +40%↑)": ret1 >= 40,
            "낙폭≥30% AND 3개월 하위30%(약한 종목)": (dd <= -0.3) & (ret3 <= np.nanpercentile(ret3[valid], 30)),
            "200일선 -20% 미만(추세 훼손)": last < ma200 * 0.8,
            "3개월 하위30%(역추세)": ret3 <= np.nanpercentile(ret3[valid], 30),
        }
        for name, m in masks.items():
            sel = (m & valid).fillna(False)
            vals = [float(v) - bench for v in fwd[sel]]
            groups.setdefault(name, []).extend(vals)
            if len(vals) >= 5:
                per_date.setdefault(name, []).append(statistics.median(vals))
        groups.setdefault("전체(기준선)", []).extend([float(v) - bench for v in fwd[valid].sample(min(200, int(valid.sum())), random_state=t)])
    return {"obs": groups, "per_date": per_date}


def report(res: dict) -> str:
    """관측 전체의 중앙 초과/이긴 비율 + 시점별 일관성(시점 중앙 초과수익이 +인 시점 비율, 평균, t값).
    평균 초과수익은 '무작위 종목 평균 − 중앙값'이 구조적으로 +3~4%p(수익률 분포가 오른쪽으로 치우침)라 쓰지 않는다."""
    groups, per_date = res["obs"], res["per_date"]
    lines = [f"{'신호':<44}{'관측':>7}{'중앙 초과':>9}{'이긴 비율':>8}{'시점수':>6}{'시점 평균':>9}{'+인 시점':>8}{'t값':>6}"]
    for name, v in sorted(groups.items(), key=lambda kv: -statistics.median(kv[1]) if kv[1] else 0):
        pd_ = per_date.get(name, [])
        if len(v) < 30 or len(pd_) < 5:
            continue
        m, sd = statistics.fmean(pd_), statistics.pstdev(pd_)
        t = m / (sd / len(pd_) ** 0.5) if sd > 0 else 0.0
        lines.append(f"{name:<44}{len(v):>7}{statistics.median(v):>+9.2f}{sum(1 for x in v if x > 0) / len(v) * 100:>7.0f}%"
                     f"{len(pd_):>6}{m:>+9.2f}{sum(1 for x in pd_ if x > 0) / len(pd_) * 100:>7.0f}%{t:>+6.1f}")
    return "\n".join(lines)


if __name__ == "__main__":
    mk = (sys.argv[1] if len(sys.argv) > 1 else "kr").lower()
    print(f"{'미국' if mk == 'us' else '한국'} 가격 신호 과거 검증 — 20거래일 뒤 수익률 vs 같은 시점 전 종목 중앙값", flush=True)
    c, h = load_closes(market=mk)
    print(f"종목 {c.shape[1]}개 · 거래일 {c.shape[0]}일 ({c.index[0].date()}~{c.index[-1].date()})", flush=True)
    print(report(run(c, h)))
    print("\n※ 생존 편향·겹치는 시점·짧은 기간 때문에 '방향성 참고'일 뿐 확정 근거가 아닙니다.")
