"""
services/nav_service.py
포트폴리오 NAV(순자산가치) 일별 기록 및 벤치마크 비교 서비스

- 매일 장마감 후 현재 포트폴리오 가치를 기록
- KOSPI 대비 초과수익(Alpha) 추적
- 주간/월간/연간 자산 성장 리포트 생성
"""
import logging
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo

from db.database import get_conn
from sqlalchemy import text

logger = logging.getLogger(__name__)
_KST = ZoneInfo("Asia/Seoul")


def record_nav(kis=None) -> dict | None:
    """오늘 포트폴리오 NAV를 기록. 보유 종목 없으면 None 반환."""
    try:
        from services.portfolio_service import calculate_pnl
        import yfinance as yf

        today = datetime.now(_KST).strftime("%Y-%m-%d")

        # 포트폴리오 P&L 계산
        pnl_data = calculate_pnl(kis)
        if not pnl_data:
            logger.debug("[NAV] 보유 종목 없음 — NAV 기록 건너뜀")
            return None

        # calculate_pnl 반환 필드: current_val(평가금액), invested(매입금액)
        total_value = sum(p.get("current_val", 0) for p in pnl_data)
        total_cost  = sum(p.get("invested", 0) for p in pnl_data)
        total_pnl   = total_value - total_cost
        total_pnl_pct = (total_pnl / total_cost * 100) if total_cost > 0 else 0.0

        # KOSPI 현재 수준 및 연초 대비 등락률
        kospi_close = 0.0
        kospi_pct_ytd = 0.0
        try:
            h = yf.Ticker("^KS11").history(period="1y", interval="1d")
            if not h.empty:
                kospi_close = round(float(h.iloc[-1]["Close"]), 2)
                year_start = h[h.index.year == datetime.now(_KST).year]
                if not year_start.empty:
                    start_price = float(year_start.iloc[0]["Close"])
                    kospi_pct_ytd = round((kospi_close - start_price) / start_price * 100, 2)
        except Exception as e:
            logger.debug("[NAV] KOSPI 조회 실패: %s", e)

        # 포트폴리오 연초 대비 등락률
        nav_pct_ytd = _calc_ytd_pnl_pct()
        if nav_pct_ytd is None:
            nav_pct_ytd = round(total_pnl_pct, 2)

        alpha_ytd = round(nav_pct_ytd - kospi_pct_ytd, 2)

        with get_conn() as conn:
            conn.execute(
                text("""
                    INSERT INTO portfolio_nav
                    (date, total_value, total_cost, total_pnl, total_pnl_pct,
                     kospi_close, kospi_pct_ytd, nav_pct_ytd, alpha_ytd, position_count)
                    VALUES (:date, :tv, :tc, :tp, :tpp, :kc, :kpy, :npy, :ay, :pc)
                    ON CONFLICT(date) DO UPDATE SET
                      total_value=:tv, total_cost=:tc, total_pnl=:tp, total_pnl_pct=:tpp,
                      kospi_close=:kc, kospi_pct_ytd=:kpy, nav_pct_ytd=:npy,
                      alpha_ytd=:ay, position_count=:pc
                """),
                {
                    "date": today, "tv": round(total_value), "tc": round(total_cost),
                    "tp": round(total_pnl), "tpp": round(total_pnl_pct, 2),
                    "kc": kospi_close, "kpy": kospi_pct_ytd,
                    "npy": round(nav_pct_ytd, 2), "ay": alpha_ytd,
                    "pc": len(pnl_data),
                },
            )

        result = {
            "date": today, "total_value": total_value, "total_pnl_pct": total_pnl_pct,
            "kospi_pct_ytd": kospi_pct_ytd, "nav_pct_ytd": nav_pct_ytd, "alpha_ytd": alpha_ytd,
        }
        logger.info("[NAV] 기록 완료: 총평가 %,.0f원 | 손익 %+.2f%% | 알파 %+.2f%%",
                    total_value, total_pnl_pct, alpha_ytd)
        return result
    except Exception as e:
        logger.warning("[NAV] 기록 실패: %s", e)
        return None


def _calc_ytd_pnl_pct() -> float | None:
    """올해 1월 1일 이후 포트폴리오 손익률 계산 (연초 기준 NAV가 있으면 사용)."""
    try:
        year_start = f"{datetime.now(_KST).year}-01-01"
        with get_conn() as conn:
            row = conn.execute(
                text("""
                    SELECT total_pnl_pct FROM portfolio_nav
                    WHERE date >= :ys ORDER BY date ASC LIMIT 1
                """),
                {"ys": year_start},
            ).fetchone()
        return float(row[0]) if row else None
    except Exception:
        return None


def get_nav_history(days: int = 30) -> list[dict]:
    """최근 N일 NAV 이력 반환."""
    try:
        cutoff = (datetime.now(_KST) - timedelta(days=days)).strftime("%Y-%m-%d")
        with get_conn() as conn:
            rows = conn.execute(
                text("""
                    SELECT date, total_value, total_pnl_pct, kospi_pct_ytd, nav_pct_ytd, alpha_ytd
                    FROM portfolio_nav WHERE date >= :cutoff ORDER BY date
                """),
                {"cutoff": cutoff},
            ).fetchall()
        return [
            {"date": r[0], "total_value": r[1], "total_pnl_pct": r[2],
             "kospi_pct_ytd": r[3], "nav_pct_ytd": r[4], "alpha_ytd": r[5]}
            for r in rows
        ]
    except Exception as e:
        logger.debug("[NAV] 이력 조회 실패: %s", e)
        return []


def generate_nav_report(days: int = 7) -> str:
    """주간 자산 성장 리포트 생성."""
    history = get_nav_history(days)
    if not history:
        return ""

    latest = history[-1]
    oldest = history[0]

    # 기간 내 NAV 변화
    period_nav_chg = round(latest["nav_pct_ytd"] - oldest["nav_pct_ytd"], 2)
    period_kospi_chg = round(latest["kospi_pct_ytd"] - oldest["kospi_pct_ytd"], 2)
    period_alpha = round(period_nav_chg - period_kospi_chg, 2)

    alpha_emoji = "✅" if latest["alpha_ytd"] >= 0 else "⚠️"
    alpha_label = "초과수익" if latest["alpha_ytd"] >= 0 else "시장 하회"

    lines = [
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"📈 포트폴리오 자산 성장 현황",
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"📅 기준: {latest['date']}",
        f"",
        f"💼 포트폴리오  연초대비 {latest['nav_pct_ytd']:+.2f}%",
        f"📊 KOSPI       연초대비 {latest['kospi_pct_ytd']:+.2f}%",
        f"{'━'*20}",
        f"{alpha_emoji} {alpha_label}(Alpha)  {latest['alpha_ytd']:+.2f}%",
        f"",
        f"[이번 주 변화 ({days}일)]",
        f"  포트폴리오: {period_nav_chg:+.2f}%",
        f"  KOSPI:      {period_kospi_chg:+.2f}%",
        f"  주간 Alpha: {period_alpha:+.2f}%",
    ]

    if len(history) >= 2:
        best_day = max(history, key=lambda x: x["alpha_ytd"])
        worst_day = min(history, key=lambda x: x["alpha_ytd"])
        lines += [
            f"",
            f"  최고 Alpha일: {best_day['date']} ({best_day['alpha_ytd']:+.2f}%)",
            f"  최저 Alpha일: {worst_day['date']} ({worst_day['alpha_ytd']:+.2f}%)",
        ]

    return "\n".join(lines)


def get_latest_nav() -> dict | None:
    """가장 최근 NAV 기록 반환."""
    try:
        with get_conn() as conn:
            row = conn.execute(
                text("""
                    SELECT date, total_value, total_pnl_pct, nav_pct_ytd, alpha_ytd
                    FROM portfolio_nav ORDER BY date DESC LIMIT 1
                """)
            ).fetchone()
        if row:
            return {"date": row[0], "total_value": row[1],
                    "total_pnl_pct": row[2], "nav_pct_ytd": row[3], "alpha_ytd": row[4]}
    except Exception:
        pass
    return None
