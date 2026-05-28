"""
services/backtest_service.py
AI 추천 종목 백테스트 + 실제 매매 성과 분석

기능:
  - stock_recommendations 테이블의 AI 추천 종목 → yfinance로 N영업일 수익률 계산
  - portfolio_history 테이블의 실제 매매 이력 성과 분석
  - 월별 적중률, 평균 수익률, 손익 통계
"""
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import yfinance as yf

from db.database import get_conn
from sqlalchemy import text

logger = logging.getLogger(__name__)
_TZ = ZoneInfo("Asia/Seoul")


def _calc_return_pct(code: str, from_date: str, trading_days: int) -> float | None:
    """yfinance로 from_date 영업 개시일부터 trading_days 영업일 후 수익률 계산."""
    for suffix in ("KS", "KQ"):
        try:
            end_dt = datetime.strptime(from_date, "%Y-%m-%d") + timedelta(days=trading_days * 2 + 30)
            hist = yf.Ticker(f"{code}.{suffix}").history(
                start=from_date, end=end_dt.strftime("%Y-%m-%d"), interval="1d"
            )
            if hist.empty or len(hist) < 2:
                continue
            entry = float(hist.iloc[0]["Close"])
            idx   = min(trading_days, len(hist) - 1)
            exit_ = float(hist.iloc[idx]["Close"])
            return round((exit_ - entry) / entry * 100, 2)
        except Exception as e:
            logger.debug("수익률 계산 실패 (%s.%s): %s", code, suffix, e)
    return None


def get_recommendation_backtest(days: int = 20) -> dict:
    """AI 추천 종목 백테스트.
    days 영업일 이상 지난 추천 종목에 대해 days 영업일 후 수익률 계산.
    """
    cutoff = (datetime.now(_TZ) - timedelta(days=int(days * 1.5))).strftime("%Y-%m-%d")

    try:
        with get_conn() as conn:
            rows = conn.execute(
                text(
                    "SELECT id, date, code, name, entry_price, stop_price, target_price, rationale "
                    "FROM stock_recommendations "
                    "WHERE date <= :cutoff "
                    "ORDER BY date DESC LIMIT 100"
                ),
                {"cutoff": cutoff},
            ).fetchall()
    except Exception as e:
        logger.warning("추천 종목 조회 실패: %s", e)
        return {"error": str(e), "items": [], "stats": {}, "days": days}

    results = []
    for rec_id, date, code, name, entry_price, stop_price, target_price, rationale in rows:
        ret = _calc_return_pct(code, date, days)
        target_ret = ((target_price - entry_price) / entry_price * 100) if (target_price and entry_price) else None
        hit = bool(ret is not None and target_ret is not None and ret >= target_ret * 0.5)

        results.append({
            "id":           rec_id,
            "date":         date,
            "code":         code,
            "name":         name,
            "entry_price":  entry_price,
            "stop_price":   stop_price,
            "target_price": target_price,
            "rationale":    (rationale or "")[:120],
            "return_pct":   ret,
            "target_ret":   round(target_ret, 1) if target_ret else None,
            "hit_target":   hit,
        })

    valid  = [r for r in results if r["return_pct"] is not None]
    wins   = [r for r in valid if r["return_pct"] > 0]
    losses = [r for r in valid if r["return_pct"] <= 0]

    stats: dict = {
        "total":      len(results),
        "valid":      len(valid),
        "wins":       len(wins),
        "losses":     len(losses),
        "win_rate":   round(len(wins) / len(valid) * 100, 1) if valid else 0,
        "avg_return": round(sum(r["return_pct"] for r in valid) / len(valid), 2) if valid else 0,
        "avg_win":    round(sum(r["return_pct"] for r in wins) / len(wins), 2) if wins else 0,
        "avg_loss":   round(sum(r["return_pct"] for r in losses) / len(losses), 2) if losses else 0,
        "best":       max(valid, key=lambda r: r["return_pct"]) if valid else None,
        "worst":      min(valid, key=lambda r: r["return_pct"]) if valid else None,
    }

    return {"items": results, "stats": stats, "days": days}


def get_portfolio_performance() -> dict:
    """portfolio_history 기반 실제 매매 성과 분석."""
    try:
        with get_conn() as conn:
            rows = conn.execute(
                text(
                    "SELECT code, name, quantity, avg_price, exit_price, exit_date, return_pct, timeframe "
                    "FROM portfolio_history "
                    "ORDER BY exit_date DESC LIMIT 200"
                )
            ).fetchall()
    except Exception as e:
        logger.warning("매매이력 조회 실패: %s", e)
        return {"error": str(e), "trades": [], "stats": {}}

    trades = []
    for code, name, qty, avg_price, exit_price, exit_date, return_pct, timeframe in rows:
        pnl_amt = round((exit_price - avg_price) * qty) if (exit_price and avg_price and qty) else 0
        trades.append({
            "code":       code,
            "name":       name,
            "quantity":   qty,
            "avg_price":  avg_price,
            "exit_price": exit_price,
            "exit_date":  exit_date,
            "return_pct": return_pct,
            "pnl_amt":    pnl_amt,
            "timeframe":  timeframe,
        })

    valid  = [t for t in trades if t["return_pct"] is not None]
    wins   = [t for t in valid if t["return_pct"] > 0]
    losses = [t for t in valid if t["return_pct"] <= 0]

    # 월별 성과
    monthly: dict[str, dict] = {}
    for t in valid:
        month = (t["exit_date"] or "")[:7]
        if not month:
            continue
        if month not in monthly:
            monthly[month] = {"wins": 0, "losses": 0, "total": 0, "pnl": 0}
        monthly[month]["total"] += 1
        monthly[month]["pnl"]   += t["pnl_amt"]
        if t["return_pct"] > 0:
            monthly[month]["wins"] += 1
        else:
            monthly[month]["losses"] += 1

    stats: dict = {
        "total_trades": len(valid),
        "wins":         len(wins),
        "losses":       len(losses),
        "win_rate":     round(len(wins) / len(valid) * 100, 1) if valid else 0,
        "avg_return":   round(sum(t["return_pct"] for t in valid) / len(valid), 2) if valid else 0,
        "avg_win":      round(sum(t["return_pct"] for t in wins) / len(wins), 2) if wins else 0,
        "avg_loss":     round(sum(t["return_pct"] for t in losses) / len(losses), 2) if losses else 0,
        "total_pnl":    sum(t["pnl_amt"] for t in valid),
        "best":         max(valid, key=lambda t: t["return_pct"]) if valid else None,
        "worst":        min(valid, key=lambda t: t["return_pct"]) if valid else None,
        "monthly":      dict(sorted(monthly.items(), reverse=True)[:12]),
    }

    return {"trades": trades, "stats": stats}
