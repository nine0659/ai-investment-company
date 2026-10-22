"""스크리너 성적표 — 과거 후보(신호 3묶음+)가 N거래일 뒤 시장을 이겼는지 매일 쌓이는 스냅샷으로 코드가 채점한다.

기준선은 같은 두 스냅샷 날짜 사이에 **전 종목(스냅샷에 있는 종목 전부) 수익률의 중앙값**이다 — 외부 지수가 필요 없고,
'후보로 뽑은 효과'만 분리해서 본다(시장 전체가 올랐다고 후보가 잘한 게 아니다). 표본이 적을 땐 그렇다고 밝힌다.
LLM 없음. 이 채점은 신호 가중치 조정의 근거이지 매매 신호가 아니다.
"""
import logging
import statistics

logger = logging.getLogger(__name__)

_TABLES = {"KR": "universe_screen_daily", "US": "us_screen_daily"}
HORIZONS = (5, 10, 20)             # 스냅샷(거래일) 개수 기준 보유 기간
MIN_OBS_TRUSTED = 30               # 이보다 적으면 '표본 부족'으로 표시


def _dates(table: str) -> list[str]:
    from db.database import get_conn
    from sqlalchemy import text
    with get_conn() as conn:
        return [r[0] for r in conn.execute(text(f"SELECT DISTINCT date FROM {table} ORDER BY date ASC")).fetchall()]


def _pair_rows(table: str, da: str, db: str) -> list[tuple]:
    """(code, 이전 종가, 이전 점수, 이후 종가) — 두 날짜 모두에 있고 종가가 양수인 종목."""
    from db.database import get_conn
    from sqlalchemy import text
    with get_conn() as conn:
        return conn.execute(text(
            f"SELECT a.code, a.close, a.score, b.close FROM {table} a JOIN {table} b ON a.code=b.code "
            "WHERE a.date=:da AND b.date=:db AND a.close>0 AND b.close>0"), {"da": da, "db": db}).fetchall()


def evaluate(market: str, horizon: int, min_score: int = 3) -> dict:
    """한 시장·한 보유기간의 성적. n_obs=(후보, 시작일) 관측 수 — 같은 종목이 여러 날 후보면 여러 번 센다."""
    table = _TABLES[market]
    try:
        dates = _dates(table)
    except Exception as e:
        logger.warning("[성적표] 날짜 조회 실패: %s", e)
        return {"market": market, "horizon": horizon, "n_obs": 0, "error": str(e)[:80]}
    excess, rets, benches, by_score = [], [], [], {}
    used_dates = 0
    for i in range(len(dates) - horizon):
        rows = _pair_rows(table, dates[i], dates[i + horizon])
        if len(rows) < 100:                                 # 비교 가능한 종목이 너무 적은 날(수집 이상)은 쓰지 않는다
            continue
        allr = [(c / a - 1) * 100 for _, a, _, c in rows]
        bench = statistics.median(allr)
        picked = [((c / a - 1) * 100, sc) for _, a, sc, c in rows if sc >= min_score]
        if not picked:
            continue
        used_dates += 1
        benches.append(bench)
        for r, sc in picked:
            rets.append(r)
            excess.append(r - bench)
            by_score.setdefault(min(sc, 5), []).append(r - bench)
    n = len(excess)
    return {
        "market": market, "horizon": horizon, "n_obs": n, "n_dates": used_dates,
        "avg_ret": statistics.fmean(rets) if n else None,
        "bench_avg": statistics.fmean(benches) if benches else None,
        "avg_excess": statistics.fmean(excess) if n else None,
        "median_excess": statistics.median(excess) if n else None,
        "hit_rate": sum(1 for x in excess if x > 0) / n * 100 if n else None,
        "by_score": {k: {"n": len(v), "avg_excess": statistics.fmean(v)} for k, v in sorted(by_score.items())},
        "trusted": n >= MIN_OBS_TRUSTED,
    }


def format_report(markets=("KR", "US"), horizons=HORIZONS) -> str:
    """사람이 읽는 성적표. 관측이 하나도 없으면 ''."""
    label = {"KR": "🇰🇷 한국", "US": "🇺🇸 미국"}
    lines = []
    for m in markets:
        for h in horizons:
            ev = evaluate(m, h)
            if not ev["n_obs"]:
                continue
            tag = "" if ev["trusted"] else f" ⚠표본 {ev['n_obs']}건(30건 미만 — 참고만)"
            lines.append(f"  · {label[m]} {h}거래일 뒤: 후보 평균 {ev['avg_ret']:+.1f}% vs 전 종목 중앙값 {ev['bench_avg']:+.1f}% "
                         f"→ 초과 {ev['avg_excess']:+.1f}%p · 이긴 비율 {ev['hit_rate']:.0f}% (후보일 {ev['n_dates']}일, 관측 {ev['n_obs']}건){tag}")
            if ev["trusted"] and len(ev["by_score"]) > 1:
                lines.append("      신호 묶음 수별 초과수익: " + " · ".join(
                    f"{k}묶음{'+' if k == 5 else ''} {v['avg_excess']:+.1f}%p({v['n']}건)" for k, v in ev["by_score"].items()))
    return ("🧪 스크리너 성적표 — 후보로 뽑은 종목이 이후 시장(전 종목 중앙값)을 이겼는가\n" + "\n".join(lines)) if lines else ""
