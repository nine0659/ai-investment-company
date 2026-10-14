"""AI 추천 성적표 — '시장 대비'로 본 적중. 절대 수익률만 보면 시장 하락이 실력 부족으로 섞여 보인다.

각 추천을 같은 기간 KOSPI와 비교해(초과수익 = 종목 수익률 - KOSPI 수익률) 시장 요인과 종목 선택 요인을 분리한다.
또 추천이 몇 개 종목·며칠에 몰려 있는지 코드가 세어, 표본이 독립이 아님을 숨기지 않는다.
"""
import logging

from sqlalchemy import text

from db.database import get_conn

logger = logging.getLogger(__name__)


def load_recs() -> list[dict]:
    """추천 + 최신 추적가(없으면 None). [{id,date,code,name,entry,price}]"""
    try:
        with get_conn() as conn:
            rows = conn.execute(text(
                "SELECT r.id, r.date, r.code, r.name, r.entry_price, "
                "(SELECT t.current_price FROM recommendation_tracking t WHERE t.rec_id=r.id ORDER BY t.date DESC LIMIT 1), "
                "(SELECT t.date FROM recommendation_tracking t WHERE t.rec_id=r.id ORDER BY t.date DESC LIMIT 1) "
                "FROM stock_recommendations r WHERE r.entry_price > 0 ORDER BY r.date, r.id")).fetchall()
        return [{"id": r[0], "date": r[1], "code": r[2], "name": r[3], "entry": float(r[4]),
                 "price": float(r[5]) if r[5] else None, "price_date": r[6]} for r in rows]
    except Exception as e:
        logger.debug("[성적표] 추천 조회 실패: %s", e)
        return []


def _days_between(a: str, b: str) -> int:
    from datetime import date
    try:
        return abs((date.fromisoformat(b[:10]) - date.fromisoformat(a[:10])).days)
    except ValueError:
        return 0


def score(recs: list[dict], closes: dict[str, float], quote=None) -> dict:
    """반환: {rows, n, beat, avg_alpha, stocks, days, missing}. 현재가 없으면 quote(code)로 보강."""
    from services.account_perf import kospi_return_since
    rows, missing = [], 0
    asof = max(closes) if closes else ""
    for r in recs:
        price = r["price"]
        # 추적이 오래전에 끝난 추천의 마지막 가격은 '지금 가격'이 아니다 — 지수는 오늘까지인데 종목만 과거면 초과수익이 틀린다.
        pd_ = r.get("price_date")
        if price is not None and asof and pd_ and _days_between(pd_, asof) > 5:
            price = None
        if price is None and quote:
            try:
                q = quote(r["code"])
                price = q.get("price") if q else None
            except Exception:
                price = None
        mk = kospi_return_since(r["date"], closes)
        if price is None or mk is None:
            missing += 1
            continue
        ret = (price / r["entry"] - 1) * 100
        rows.append({"name": r["name"], "code": r["code"], "date": r["date"], "ret": ret, "mkt": mk, "alpha": ret - mk})
    n = len(rows)
    return {"rows": rows, "n": n, "beat": sum(1 for x in rows if x["alpha"] > 0),
            "avg_alpha": (sum(x["alpha"] for x in rows) / n) if n else 0.0,
            "stocks": len({x["code"] for x in rows}), "days": len({x["date"] for x in rows}), "missing": missing}


def format_scorecard(s: dict) -> str:
    if not s.get("n"):
        return ""
    lines = [f"[AI 추천 성적표 — 같은 기간 KOSPI 대비, 코드 계산] 총 {s['n']}건 중 시장을 이긴 것 {s['beat']}건 · 평균 초과수익 {s['avg_alpha']:+.1f}%p"]
    for x in s["rows"]:
        lines.append(f"  {x['date']} {x['name']}: 종목 {x['ret']:+.1f}% / KOSPI {x['mkt']:+.1f}% → 초과 {x['alpha']:+.1f}%p")
    lines.append(f"  ※ 사실상 독립 표본은 {s['stocks']}개 종목·{s['days']}개 추천일뿐 — 같은 종목 재추천은 한 번의 판단이 반복된 것이라 "
                 f"'N건 중 몇 건'을 실력의 증거로 읽으면 안 됨")
    if s.get("missing"):
        lines.append(f"  (현재가/지수 비교 불가 {s['missing']}건 제외)")
    return "\n".join(lines)
