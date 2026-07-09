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

        # 데이터 가드 (2026-07-08 사고): 시세 조회 실패 종목이 current_val=0으로
        # 집계되면 총평가가 통째로 무너진다. 오염된 한 행이 90일 드로다운 계산을
        # 지배해 전량청산 오판까지 갔다. 이상치는 수정이 아니라 저장 거부.
        prev = get_latest_nav()
        suspicion = _nav_data_suspicious(pnl_data, (prev or {}).get("total_value"))
        if suspicion:
            logger.warning("[NAV][데이터가드] 오염 의심 — 저장 안 함: %s", suspicion)
            try:
                from clients.telegram_client import send_error_alert
                send_error_alert(
                    f"[NAV 데이터가드] 오늘 NAV 기록을 건너뜀\n{suspicion}\n"
                    f"KIS 시세/잔고 응답 이상 가능성 — 내일 자동 재시도됩니다."
                )
            except Exception:
                pass
            return None
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

        # 포트폴리오 추적 시작(올해 첫 기록) 대비 등락률.
        # 과거엔 첫 기록의 손익률을 그대로 nav_pct_ytd에 넣어 값이 영원히
        # 고정되고(주간 변화 항상 0.00%), KOSPI는 1월 기준이라 알파가
        # -71% 같은 무의미한 수치로 나왔다 (2026-07-05 리포트 오류).
        # 알파는 반드시 같은 시작점(추적 시작일)끼리 비교한다.
        baseline = _get_year_baseline()
        if baseline:
            nav_pct_ytd = round(total_pnl_pct - baseline["pnl_pct"], 2)
            if baseline["kospi_close"] > 0 and kospi_close > 0:
                kospi_since_start = round(
                    (kospi_close - baseline["kospi_close"]) / baseline["kospi_close"] * 100, 2
                )
            else:
                kospi_since_start = kospi_pct_ytd
        else:
            nav_pct_ytd = 0.0  # 오늘이 추적 시작일
            kospi_since_start = 0.0

        alpha_ytd = round(nav_pct_ytd - kospi_since_start, 2)

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
        logger.info("[NAV] 기록 완료: 총평가 %.0f원 | 손익 %+.2f%% | 알파 %+.2f%%",
                    total_value, total_pnl_pct, alpha_ytd)
        return result
    except Exception as e:
        logger.warning("[NAV] 기록 실패: %s", e)
        return None


def _nav_data_suspicious(pnl_data: list[dict], prev_total: float | None) -> str:
    """NAV 오염 신호 감지. 문제 없으면 빈 문자열, 있으면 사유 반환.

    - 시세 누락: 매입금액은 있는데 평가금액이 0 이하인 종목 존재
    - 총평가 급변: 직전 기록 대비 하루 ±30% 초과 (시장 변동으로는 불가능,
      시세 부분 누락·이중 집계·대규모 입출금 신호)
    """
    broken = [
        p for p in pnl_data
        if (p.get("invested") or 0) > 0 and (p.get("current_val") or 0) <= 0
    ]
    if broken:
        names = ", ".join(str(p.get("name") or p.get("code") or "?") for p in broken[:5])
        return f"시세 누락 종목 {len(broken)}건: {names}"

    if prev_total and prev_total > 0:
        total_value = sum(p.get("current_val", 0) for p in pnl_data)
        chg = abs(total_value - prev_total) / prev_total
        if chg > 0.30:
            return (f"총평가 급변 {chg * 100:.0f}% "
                    f"(직전 {prev_total:,.0f}원 → 오늘 {total_value:,.0f}원)")
    return ""


def _get_year_baseline() -> dict | None:
    """올해 첫 NAV 기록(추적 시작점) 반환 — 수익률·알파의 공통 기준점."""
    try:
        year_start = f"{datetime.now(_KST).year}-01-01"
        with get_conn() as conn:
            row = conn.execute(
                text("""
                    SELECT date, total_pnl_pct, kospi_close FROM portfolio_nav
                    WHERE date >= :ys ORDER BY date ASC LIMIT 1
                """),
                {"ys": year_start},
            ).fetchone()
        if row:
            return {
                "date": row[0],
                "pnl_pct": float(row[1] or 0),
                "kospi_close": float(row[2] or 0),
            }
    except Exception:
        pass
    return None


def get_nav_history(days: int = 30) -> list[dict]:
    """최근 N일 NAV 이력 반환."""
    try:
        cutoff = (datetime.now(_KST) - timedelta(days=days)).strftime("%Y-%m-%d")
        with get_conn() as conn:
            rows = conn.execute(
                text("""
                    SELECT date, total_value, total_pnl_pct, kospi_pct_ytd,
                           nav_pct_ytd, alpha_ytd, kospi_close
                    FROM portfolio_nav WHERE date >= :cutoff ORDER BY date
                """),
                {"cutoff": cutoff},
            ).fetchall()
        return [
            {"date": r[0], "total_value": r[1], "total_pnl_pct": r[2],
             "kospi_pct_ytd": r[3], "nav_pct_ytd": r[4], "alpha_ytd": r[5],
             "kospi_close": r[6]}
            for r in rows
        ]
    except Exception as e:
        logger.debug("[NAV] 이력 조회 실패: %s", e)
        return []


def generate_nav_report(days: int = 7) -> str:
    """주간 자산 현황 리포트 생성.

    포트폴리오와 KOSPI를 반드시 같은 기간(리포트 윈도우)끼리 비교한다.
    과거처럼 '포트폴리오 추적 시작 대비'와 'KOSPI 연초 대비'를 섞어서
    빼면 -71% 같은 무의미한 알파가 나온다.
    """
    history = get_nav_history(days)
    if not history:
        return ""

    latest = history[-1]
    oldest = history[0]

    # 기간 내 변화 (같은 윈도우끼리 비교)
    period_nav_chg = round(
        (latest.get("total_pnl_pct") or 0) - (oldest.get("total_pnl_pct") or 0), 2
    )
    k_old, k_new = oldest.get("kospi_close") or 0, latest.get("kospi_close") or 0
    period_kospi_chg = round((k_new - k_old) / k_old * 100, 2) if k_old > 0 else 0.0
    period_alpha = round(period_nav_chg - period_kospi_chg, 2)
    alpha_emoji = "✅" if period_alpha >= 0 else "⚠️"

    lines = [
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"📈 포트폴리오 현황 ({latest['date']} 기준)",
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"💼 보유 손익(매수 후 누적): {latest.get('total_pnl_pct') or 0:+.2f}%",
        f"",
        f"[최근 {days}일: {oldest['date']} → {latest['date']}]",
        f"  포트폴리오: {period_nav_chg:+.2f}%",
        f"  KOSPI:      {period_kospi_chg:+.2f}%",
        f"  {alpha_emoji} 시장 대비: {period_alpha:+.2f}%p",
    ]

    return "\n".join(lines)


def check_drawdown_defense() -> dict:
    """NAV 고점 대비 현재 낙폭을 계산해 방어 행동 지시.

    Returns:
        {"action": "none"|"half"|"all", "message": str, "drawdown_pct": float}
    """
    try:
        with get_conn() as conn:
            # 최근 90일 NAV 이력 조회
            cutoff = (datetime.now(_KST) - timedelta(days=90)).strftime("%Y-%m-%d")
            rows = conn.execute(
                text("""
                    SELECT date, total_value FROM portfolio_nav
                    WHERE date >= :cutoff ORDER BY date ASC
                """),
                {"cutoff": cutoff},
            ).fetchall()

        if not rows or len(rows) < 2:
            return {"action": "none", "message": "NAV 데이터 부족", "drawdown_pct": 0.0}

        peak = max(r[1] for r in rows)
        latest_value = rows[-1][1]
        if peak <= 0:
            return {"action": "none", "message": "고점 NAV 이상", "drawdown_pct": 0.0}

        drawdown_pct = (peak - latest_value) / peak * 100

        if drawdown_pct >= 15.0:
            return {
                "action": "all",
                "message": f"드로다운 -{drawdown_pct:.1f}% — 전량 청산 + 1주일 차단",
                "drawdown_pct": drawdown_pct,
            }
        elif drawdown_pct >= 10.0:
            return {
                "action": "half",
                "message": f"드로다운 -{drawdown_pct:.1f}% — 전 포지션 50% 강제 청산",
                "drawdown_pct": drawdown_pct,
            }
        else:
            return {"action": "none", "message": f"정상 (낙폭 {drawdown_pct:.1f}%)", "drawdown_pct": drawdown_pct}
    except Exception as e:
        logger.debug("[드로다운] 계산 실패: %s", e)
        return {"action": "none", "message": f"계산 오류: {e}", "drawdown_pct": 0.0}


def get_latest_nav() -> dict | None:
    """가장 최근 NAV 기록 반환."""
    try:
        with get_conn() as conn:
            row = conn.execute(
                text("""
                    SELECT date, total_value, total_pnl_pct, kospi_pct_ytd, nav_pct_ytd, alpha_ytd
                    FROM portfolio_nav ORDER BY date DESC LIMIT 1
                """)
            ).fetchone()
        if row:
            return {"date": row[0], "total_value": row[1],
                    "total_pnl_pct": row[2], "kospi_pct_ytd": row[3],
                    "nav_pct_ytd": row[4], "alpha_ytd": row[5]}
    except Exception:
        pass
    return None
