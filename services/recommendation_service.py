"""
추천 종목 DB 저장·조회·수익률 업데이트
"""
import logging
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from db.database import get_conn
from sqlalchemy import text

logger = logging.getLogger(__name__)
_TZ = ZoneInfo("Asia/Seoul")

# ── 파싱 ──────────────────────────────────────────────────────────

_HEADER_RE = re.compile(r"([가-힣A-Za-z()·&·\s]{1,25}?)\s*\((\d{6})\)")
# "진입①(즉시): XXXXX원" 또는 "진입②(눌림): XXXXX원" 형식 (현재 브리핑 포맷)
_ENTRY_NUMBERED_RE = re.compile(r"진입[①②][^\n]{0,30}?([\d,]{4,})\s*원")
# "즉시진입: XXXXX원" 또는 구형 "진입가: XXXXX원" 형식
# '진입' 부분문자열로 '즉시진입: XXX원' 도 매칭됨
_ENTRY_OLD_RE = re.compile(r"진입[가격：: ]{0,5}([\d,]{4,})\s*원")
_STOP_RE      = re.compile(r"손절[가격：: ·]{0,5}([\d,]{4,})\s*원")
_TARGET_RE    = re.compile(r"목표[가격：: ·]{0,5}([\d,]{4,})\s*원")
_RATIONALE_RE = re.compile(r"선택 이유[：:]\s*([^\n]{10,200})")


def _extract_price(pattern: re.Pattern, text_: str) -> int | None:
    m = pattern.search(text_)
    if m:
        try:
            return int(m.group(1).replace(",", ""))
        except ValueError:
            pass
    return None


def parse_recommendations(report_text: str) -> list[dict]:
    """CEO 브리핑 텍스트에서 추천 종목 파싱."""
    results: list[dict] = []
    seen_codes: set[str] = set()
    lines = report_text.split("\n")

    for i, line in enumerate(lines):
        m = _HEADER_RE.search(line)
        if not m:
            continue
        name = m.group(1).strip()
        code = m.group(2)
        if code in seen_codes:
            continue

        block = "\n".join(lines[i: i + 10])
        # 진입①(즉시): 우선, 즉시진입:/진입가: 차선으로 시도
        entry  = _extract_price(_ENTRY_NUMBERED_RE, block) or _extract_price(_ENTRY_OLD_RE, block)
        stop   = _extract_price(_STOP_RE, block)
        target = _extract_price(_TARGET_RE, block)

        if entry and stop and target and entry > 0 and stop > 0 and target > 0:
            seen_codes.add(code)
            rm = _RATIONALE_RE.search(block)
            rationale = rm.group(1).strip() if rm else ""
            results.append({
                "name": name, "code": code,
                "entry_price": entry, "stop_price": stop,
                "target_price": target, "rationale": rationale,
            })

    return results


# ── 저장 / 조회 ──────────────────────────────────────────────────

def save_recommendations(date: str, recs: list[dict]) -> int:
    """추천 종목 저장 (같은 날 기존 데이터는 삭제 후 재저장)."""
    if not recs:
        return 0
    with get_conn() as conn:
        conn.execute(
            text("DELETE FROM stock_recommendations WHERE date=:date"),
            {"date": date},
        )
        for r in recs:
            conn.execute(
                text(
                    "INSERT INTO stock_recommendations "
                    "(date, code, name, entry_price, stop_price, target_price, rationale) "
                    "VALUES (:date, :code, :name, :entry, :stop, :target, :rationale)"
                ),
                {"date": date, "code": r["code"], "name": r["name"],
                 "entry": r["entry_price"], "stop": r["stop_price"],
                 "target": r["target_price"], "rationale": r["rationale"]},
            )
    logger.info("추천 종목 저장 완료: %d건 (%s)", len(recs), date)
    return len(recs)


def get_recommendations(date: str) -> list[dict]:
    """특정 날짜 추천 종목 조회."""
    try:
        with get_conn() as conn:
            rows = conn.execute(
                text(
                    "SELECT code, name, entry_price, stop_price, target_price, "
                    "rationale, close_price, return_pct, result "
                    "FROM stock_recommendations WHERE date=:date"
                ),
                {"date": date},
            ).fetchall()
        return [
            {"code": r[0], "name": r[1], "entry_price": r[2],
             "stop_price": r[3], "target_price": r[4], "rationale": r[5],
             "close_price": r[6], "return_pct": r[7], "result": r[8]}
            for r in rows
        ]
    except Exception as e:
        logger.warning("추천 종목 조회 실패: %s", e)
        return []


def get_recent_recommendations(days: int = 7) -> list[dict]:
    """최근 N일 추천 종목 전체 조회 (통계용)."""
    try:
        cutoff = (datetime.now(_TZ) - timedelta(days=days)).strftime("%Y-%m-%d")
        with get_conn() as conn:
            rows = conn.execute(
                text(
                    "SELECT date, code, name, entry_price, stop_price, target_price, "
                    "close_price, return_pct, result "
                    "FROM stock_recommendations "
                    "WHERE date >= :cutoff AND return_pct IS NOT NULL "
                    "ORDER BY date DESC"
                ),
                {"cutoff": cutoff},
            ).fetchall()
        return [
            {"date": r[0], "code": r[1], "name": r[2],
             "entry_price": r[3], "stop_price": r[4], "target_price": r[5],
             "close_price": r[6], "return_pct": r[7], "result": r[8]}
            for r in rows
        ]
    except Exception as e:
        logger.warning("최근 추천 종목 조회 실패: %s", e)
        return []


# ── 종가 업데이트 ────────────────────────────────────────────────

def _classify(return_pct: float, close: float = 0, stop_price: int = 0, target_price: int = 0) -> str:
    if stop_price and close and close <= stop_price:
        return "손절"
    if target_price and close and close >= target_price:
        return "목표달성"
    if return_pct >= 2.0:
        return "성공"
    if return_pct <= -2.0:
        return "실패"
    return "보통"


def update_close_prices(date: str, kis) -> list[dict]:
    """당일 추천 종목 종가 수집 → 수익률 계산 → DB 업데이트."""
    recs = get_recommendations(date)
    if not recs:
        logger.info("종가 업데이트: 당일 추천 종목 없음 (%s)", date)
        return []

    updated = []
    for rec in recs:
        try:
            price_data = kis.get_stock_price(rec["code"])
            close = price_data.get("price", 0)
            if not close or not rec["entry_price"]:
                continue
            ret = (close - rec["entry_price"]) / rec["entry_price"] * 100
            result = _classify(ret, close=close,
                               stop_price=rec.get("stop_price", 0),
                               target_price=rec.get("target_price", 0))
            with get_conn() as conn:
                conn.execute(
                    text(
                        "UPDATE stock_recommendations "
                        "SET close_price=:close, return_pct=:ret, result=:result "
                        "WHERE date=:date AND code=:code"
                    ),
                    {"close": close, "ret": round(ret, 2), "result": result,
                     "date": date, "code": rec["code"]},
                )
            updated.append({**rec, "close_price": close,
                             "return_pct": round(ret, 2), "result": result})
            logger.info("종가 업데이트: %s(%s) 진입 %s → 종가 %s (%.1f%% %s)",
                        rec["name"], rec["code"], rec["entry_price"], close, ret, result)
        except Exception as e:
            logger.warning("종가 업데이트 실패 (%s): %s", rec["code"], e)

    return updated


def get_performance_stats(days: int = 30) -> dict:
    """최근 N일 추천 성과 통계."""
    recs = get_recent_recommendations(days=days)
    empty = {"total": 0, "win": 0, "loss": 0, "neutral": 0,
             "win_rate": 0.0, "avg_return": 0.0, "max_loss": 0.0, "profit_factor": 0.0}
    if not recs:
        return empty
    returns = [r["return_pct"] for r in recs if r.get("return_pct") is not None]
    if not returns:
        return {**empty, "total": len(recs)}
    wins    = [r for r in returns if r >= 2.0]
    losses  = [r for r in returns if r <= -2.0]
    neutral = len(returns) - len(wins) - len(losses)
    avg_ret = sum(returns) / len(returns)
    max_loss = min(returns)
    total_profit = sum(r for r in returns if r > 0)
    total_loss   = abs(sum(r for r in returns if r < 0))
    profit_factor = round(total_profit / total_loss, 2) if total_loss > 0 else 0.0
    return {
        "total":         len(returns),
        "win":           len(wins),
        "loss":          len(losses),
        "neutral":       neutral,
        "win_rate":      round(len(wins) / len(returns) * 100, 1),
        "avg_return":    round(avg_ret, 2),
        "max_loss":      round(max_loss, 2),
        "profit_factor": profit_factor,
    }


def format_returns_for_report(results: list[dict]) -> str:
    """종가/수익률 결과를 텔레그램 메시지용 텍스트로 변환."""
    if not results:
        return "오늘 추천 종목 없음"
    lines = ["📊 오늘 추천 종목 결과:"]
    for r in results:
        emoji = "✅" if r["result"] == "성공" else ("❌" if r["result"] == "실패" else "➖")
        ret   = r.get("return_pct") or 0
        lines.append(
            f"{emoji} {r['name']}({r['code']}) "
            f"진입 {r['entry_price']:,}원 → 종가 {int(r.get('close_price') or 0):,}원 "
            f"({ret:+.1f}%) [{r['result']}]"
        )
    return "\n".join(lines)
