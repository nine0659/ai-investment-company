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
        suspicion = _nav_data_suspicious(
            pnl_data,
            (prev or {}).get("total_value"),
            (prev or {}).get("total_cost"),
        )
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


def _nav_data_suspicious(
    pnl_data: list[dict],
    prev_total: float | None,
    prev_cost: float | None = None,
) -> str:
    """NAV 오염 신호 감지. 문제 없으면 빈 문자열, 있으면 사유 반환.

    - 시세 누락: 매입금액은 있는데 평가금액이 0 이하인 종목 존재
    - 평가배율 급변: 매입금 대비 평가배율(value/cost)이 직전 기록 대비 하루
      ±30% 초과. 총평가 원값을 비교하면 안 된다 — 매매·입출금으로 원금이
      바뀌면 총평가도 정당하게 크게 움직인다 (2026-07-09 오탐: 전날 SK하이닉스
      전량매도로 총평가 51.3M→30.5M이 정상인데 가드가 기록을 막았고, 같은
      비교식을 쓰던 드로다운 체크는 7/8에 -44.4%로 오판해 전량청산까지 갔다).
      시세 오염은 원금 변화 없이 배율만 무너뜨리므로 배율로 구분한다.
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
        total_cost = sum(p.get("invested", 0) for p in pnl_data)
        if prev_cost and prev_cost > 0 and total_cost > 0:
            prev_ratio = prev_total / prev_cost
            today_ratio = total_value / total_cost
            chg = abs(today_ratio - prev_ratio) / prev_ratio
            if chg > 0.30:
                return (f"평가배율 급변 {chg * 100:.0f}% "
                        f"(직전 {prev_ratio:.3f} → 오늘 {today_ratio:.3f}, "
                        f"총평가 {prev_total:,.0f}원 → {total_value:,.0f}원)")
        else:
            # 직전 기록에 매입금이 없으면 원값 비교로 폴백
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
                           nav_pct_ytd, alpha_ytd, kospi_close, total_cost
                    FROM portfolio_nav WHERE date >= :cutoff ORDER BY date
                """),
                {"cutoff": cutoff},
            ).fetchall()
        return [
            {"date": r[0], "total_value": r[1], "total_pnl_pct": r[2],
             "kospi_pct_ytd": r[3], "nav_pct_ytd": r[4], "alpha_ytd": r[5],
             "kospi_close": r[6], "total_cost": r[7]}
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

    기간 수익률은 total_pnl_pct 차이가 아니라 평가배율(value/cost) 변화로
    계산한다 — pnl% 차이는 기간 중 매매로 구성이 바뀌면 (하이닉스 매도 후
    +20.13%→+10.91%처럼) 실현·제외분이 주간 손실로 둔갑한다 (2026-07-10).
    KOSPI는 수집 실패일(kospi_close=0)을 제외한 유효 종가끼리 비교한다.
    """
    history = get_nav_history(days)
    if not history:
        return ""

    latest = history[-1]
    oldest = history[0]

    # 기간 내 변화 — 배율 시계열의 양끝 (같은 윈도우끼리 비교)
    ratios = [
        (h["total_value"] or 0) / h["total_cost"] for h in history
        if (h.get("total_cost") or 0) > 0 and (h.get("total_value") or 0) > 0
    ]
    if len(ratios) >= 2:
        period_nav_chg = round((ratios[-1] / ratios[0] - 1) * 100, 2)
    else:
        period_nav_chg = 0.0

    kospi_closes = [h["kospi_close"] for h in history if (h.get("kospi_close") or 0) > 0]
    lines = [
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"📈 포트폴리오 현황 ({latest['date']} 기준)",
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"💼 보유 손익(매수 후 누적): {latest.get('total_pnl_pct') or 0:+.2f}%",
        f"",
        f"[최근 {days}일: {oldest['date']} → {latest['date']}]",
        f"  포트폴리오: {period_nav_chg:+.2f}%",
    ]
    if len(kospi_closes) >= 2:
        period_kospi_chg = round((kospi_closes[-1] - kospi_closes[0]) / kospi_closes[0] * 100, 2)
        period_alpha = round(period_nav_chg - period_kospi_chg, 2)
        alpha_emoji = "✅" if period_alpha >= 0 else "⚠️"
        lines += [
            f"  KOSPI:      {period_kospi_chg:+.2f}%",
            f"  {alpha_emoji} 시장 대비: {period_alpha:+.2f}%p",
        ]
    else:
        lines.append(f"  KOSPI: 기간 내 지수 데이터 부족 — 비교 생략")

    return "\n".join(lines)


def _ratio_drawdown_pct(rows) -> float | None:
    """(date, total_value, total_cost) 시계열에서 평가배율 기준 낙폭(%) 계산.

    유효 행(평가·매입 모두 양수)이 2개 미만이면 None.

    배율 원값의 고점/최신 비교는 구성 변경을 낙폭으로 오인한다: 고점이 매도 전
    구성(2026-07-06 하이닉스 포함 1.201)에 찍힌 뒤 이익 난 종목을 팔면, 실현되어
    빠져나간 이익이 낙폭으로 둔갑한다 (2026-07-13 -15.4% 경보 — 체인 기준 실낙폭은
    -14.3%로 '전량' 아닌 '절반' 단계였다). 매입금이 같은(=매매 없는) 인접일의
    배율 변화만 곱해 성과지수를 만들고, 그 지수의 고점 대비 낙폭을 잰다.
    매입금이 바뀐 날은 성과와 매매를 분리할 수 없으므로 그날 변화는 0으로 둔다.
    """
    valid = [
        (r[1], r[2]) for r in rows
        if (r[2] or 0) > 0 and (r[1] or 0) > 0
    ]
    if len(valid) < 2:
        return None
    index = peak = 1.0
    prev_value, prev_cost = valid[0]
    for value, cost in valid[1:]:
        if abs(cost - prev_cost) / prev_cost <= 0.001:
            index *= (value / cost) / (prev_value / prev_cost)
            peak = max(peak, index)
        prev_value, prev_cost = value, cost
    return (peak - index) / peak * 100


def check_drawdown_defense() -> dict:
    """NAV 고점 대비 현재 낙폭을 계산해 방어 행동 지시.

    낙폭은 총평가 원값이 아니라 매입금 대비 평가배율(value/cost)로 계산한다.
    원값으로 재면 매매·입출금(예: 2026-07-07 SK하이닉스 전량매도 2,176만원)이
    낙폭으로 둔갑한다 — 2026-07-08 드로다운 -44.4% 오판·전량청산 사고의 실원인.

    Returns:
        {"action": "none"|"half"|"all", "message": str, "drawdown_pct": float}
    """
    try:
        with get_conn() as conn:
            # 최근 90일 NAV 이력 조회
            cutoff = (datetime.now(_KST) - timedelta(days=90)).strftime("%Y-%m-%d")
            rows = conn.execute(
                text("""
                    SELECT date, total_value, total_cost FROM portfolio_nav
                    WHERE date >= :cutoff ORDER BY date ASC
                """),
                {"cutoff": cutoff},
            ).fetchall()

        drawdown_pct = _ratio_drawdown_pct(rows)
        if drawdown_pct is None:
            return {"action": "none", "message": "NAV 데이터 부족", "drawdown_pct": 0.0}

        # 문구 주의: 자동매도는 2026-07-09 정책으로 영구 제거됐다. "청산"을
        # 단정하는 문구는 시스템이 매도한 것처럼 읽혀 혼란을 준다 — 권고형으로.
        if drawdown_pct >= 15.0:
            return {
                "action": "all",
                "message": (f"드로다운 -{drawdown_pct:.1f}% — 위험 단계(-15% 초과): "
                            f"포지션 전면 축소 검토 권고 (자동 매수 차단 중)"),
                "drawdown_pct": drawdown_pct,
            }
        elif drawdown_pct >= 10.0:
            return {
                "action": "half",
                "message": (f"드로다운 -{drawdown_pct:.1f}% — 경계 단계(-10% 초과): "
                            f"부분 축소 검토 권고 (자동 매수 차단 중)"),
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
                    SELECT date, total_value, total_pnl_pct, kospi_pct_ytd, nav_pct_ytd, alpha_ytd,
                           total_cost
                    FROM portfolio_nav ORDER BY date DESC LIMIT 1
                """)
            ).fetchone()
        if row:
            return {"date": row[0], "total_value": row[1],
                    "total_pnl_pct": row[2], "kospi_pct_ytd": row[3],
                    "nav_pct_ytd": row[4], "alpha_ytd": row[5],
                    "total_cost": row[6]}
    except Exception:
        pass
    return None
