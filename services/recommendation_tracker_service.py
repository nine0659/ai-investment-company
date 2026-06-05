"""
services/recommendation_tracker_service.py
AI 추천 종목 일별 성과 추적 서비스

- 매일 장마감 후 활성 추천 종목의 가격 스냅샷 기록
- 목표가/손절가 도달 시 자동 상태 전환
- 30 영업일 경과 시 만료 처리
- 추적 통계 및 텔레그램 리포트 생성
"""
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from db.database import get_conn
from sqlalchemy import text

logger = logging.getLogger(__name__)
_KST = ZoneInfo("Asia/Seoul")

# 추적 기간 (영업일 기준)
_TRACKING_DAYS = 30


def _get_price_yfinance(code: str) -> float | None:
    """yfinance로 현재가(최근 종가) 조회. 코스피/코스닥 자동 판별."""
    try:
        import yfinance as yf
        for suffix in [".KS", ".KQ"]:
            ticker = yf.Ticker(code + suffix)
            hist = ticker.history(period="5d")
            if not hist.empty:
                return float(hist["Close"].iloc[-1])
    except Exception as e:
        logger.debug("[Tracker] yfinance 가격 조회 실패 (%s): %s", code, e)
    return None


def _count_trading_days(start_date: str, end_date: str) -> int:
    """두 날짜 사이의 영업일 수 계산 (주말 + 한국 공휴일 제외)."""
    try:
        from utils.market_calendar import is_krx_trading_day
        start = datetime.strptime(start_date, "%Y-%m-%d")
        end   = datetime.strptime(end_date,   "%Y-%m-%d")
        count = 0
        cur   = start
        while cur <= end:
            if is_krx_trading_day(cur.date()):
                count += 1
            cur += timedelta(days=1)
        return max(0, count - 1)  # 당일 포함 → 경과일
    except Exception:
        # 공휴일 라이브러리 없으면 주말만 제외 폴백
        try:
            start = datetime.strptime(start_date, "%Y-%m-%d")
            end   = datetime.strptime(end_date,   "%Y-%m-%d")
            count = sum(1 for i in range((end - start).days + 1)
                        if (start + timedelta(days=i)).weekday() < 5)
            return max(0, count - 1)
        except Exception:
            return 0


def _get_active_recommendations() -> list[dict]:
    """추적 대상 추천 종목 조회 (최근 45일, 아직 만료되지 않은 것)."""
    try:
        cutoff = (datetime.now(_KST) - timedelta(days=45)).strftime("%Y-%m-%d")
        with get_conn() as conn:
            rows = conn.execute(
                text("""
                    SELECT id, date, code, name, entry_price, stop_price, target_price
                    FROM stock_recommendations
                    WHERE date >= :cutoff
                      AND entry_price IS NOT NULL
                      AND entry_price > 0
                    ORDER BY date DESC
                """),
                {"cutoff": cutoff},
            ).fetchall()
        return [
            {"id": r[0], "date": r[1], "code": r[2], "name": r[3],
             "entry_price": r[4], "stop_price": r[5], "target_price": r[6]}
            for r in rows
        ]
    except Exception as e:
        logger.warning("[Tracker] 추천 종목 조회 실패: %s", e)
        return []


def _get_today_tracking(today: str) -> set[int]:
    """오늘 이미 처리된 rec_id 집합 반환 (중복 방지)."""
    try:
        with get_conn() as conn:
            rows = conn.execute(
                text("SELECT rec_id FROM recommendation_tracking WHERE date=:today"),
                {"today": today},
            ).fetchall()
        return {r[0] for r in rows}
    except Exception:
        return set()


def _get_max_min_history(rec_id: int) -> tuple[float | None, float | None]:
    """기존 추적 이력에서 최고/최저 수익률 조회."""
    try:
        with get_conn() as conn:
            row = conn.execute(
                text("""
                    SELECT MAX(max_return), MIN(min_return)
                    FROM recommendation_tracking WHERE rec_id=:rid
                """),
                {"rid": rec_id},
            ).fetchone()
        if row:
            return row[0], row[1]
    except Exception:
        pass
    return None, None


def _determine_status(current_price: float, entry_price: float,
                      stop_price: float | None, target_price: float | None,
                      days_held: int) -> str:
    """현재 상태 결정."""
    if stop_price and current_price <= stop_price:
        return "stop_hit"
    if target_price and current_price >= target_price:
        return "target_hit"
    if days_held >= _TRACKING_DAYS:
        return "expired"
    return "tracking"


def _send_status_alert(
    status: str, name: str, code: str,
    entry_price: float, current_price: float, return_pct: float,
    target_price: float | None, stop_price: float | None,
    days_held: int, rec_date: str,
) -> None:
    """목표가/손절 도달 첫 발생 시 텔레그램 알림 발송 (하루 1회 중복 방지)."""
    try:
        # price_alert_log로 중복 발송 방지 (date + code + type 조합)
        today = datetime.now(_KST).strftime("%Y-%m-%d")
        alert_type = "track_target" if status == "target_hit" else "track_stop"
        with get_conn() as conn:
            exists = conn.execute(
                text("SELECT 1 FROM price_alert_log WHERE date=:d AND code=:c AND type=:t"),
                {"d": today, "c": code, "t": alert_type},
            ).fetchone()
            if exists:
                return  # 오늘 이미 발송됨
            conn.execute(
                text("INSERT INTO price_alert_log(date,code,type) VALUES(:d,:c,:t)"),
                {"d": today, "c": code, "t": alert_type},
            )

        if status == "target_hit":
            emoji, label = "🎯", "목표가 달성"
        else:
            emoji, label = "🛑", "손절선 도달"

        price_ref = target_price if status == "target_hit" else stop_price
        price_str = f"{price_ref:,.0f}원" if price_ref else "-"

        msg = (
            f"{emoji} *[추천 추적] {label}*\n\n"
            f"종목: {name} ({code})\n"
            f"추천일: {rec_date} ({days_held}일 경과)\n"
            f"진입가: {entry_price:,.0f}원\n"
            f"현재가: {current_price:,.0f}원\n"
            f"기준가: {price_str}\n"
            f"수익률: `{return_pct:+.2f}%`"
        )
        from clients.telegram_client import send_message
        send_message(msg)
        logger.info("[Tracker] %s 알림 발송: %s(%s) %+.2f%%", label, name, code, return_pct)
    except Exception as e:
        logger.debug("[Tracker] 알림 발송 실패: %s", e)


def run_daily_tracker(kis=None) -> dict:
    """
    매일 장마감 후 실행 — 모든 활성 추천 종목 일별 스냅샷 기록.
    Returns 요약 통계 dict.
    """
    today = datetime.now(_KST).strftime("%Y-%m-%d")
    recs  = _get_active_recommendations()
    if not recs:
        logger.info("[Tracker] 추적 대상 없음")
        return {"processed": 0, "target_hit": 0, "stop_hit": 0, "expired": 0}

    already_done  = _get_today_tracking(today)
    stats = {"processed": 0, "target_hit": 0, "stop_hit": 0, "expired": 0, "errors": 0}

    for rec in recs:
        rec_id = rec["id"]
        if rec_id in already_done:
            continue

        code         = rec["code"]
        entry_price  = rec["entry_price"]
        rec_date     = rec["date"]

        # 현재가 조회 (KIS 우선, yfinance 폴백)
        current_price = None
        if kis:
            try:
                price_data    = kis.get_stock_price(code)
                current_price = float(price_data.get("price", 0) or 0) or None
            except Exception:
                pass
        if not current_price:
            current_price = _get_price_yfinance(code)

        if not current_price or current_price <= 0:
            logger.debug("[Tracker] 가격 조회 실패: %s", code)
            stats["errors"] += 1
            continue

        # 수익률 계산
        return_pct = round((current_price - entry_price) / entry_price * 100, 2)
        days_held  = _count_trading_days(rec_date, today)

        # 최고/최저 수익률 갱신
        hist_max, hist_min = _get_max_min_history(rec_id)
        max_return = max(return_pct, hist_max) if hist_max is not None else return_pct
        min_return = min(return_pct, hist_min) if hist_min is not None else return_pct

        # 상태 결정
        status = _determine_status(
            current_price, entry_price,
            rec.get("stop_price"), rec.get("target_price"),
            days_held,
        )

        # 스냅샷 저장
        try:
            with get_conn() as conn:
                conn.execute(
                    text("""
                        INSERT INTO recommendation_tracking
                        (rec_id, date, code, name, rec_date,
                         entry_price, stop_price, target_price,
                         current_price, return_pct, max_return, min_return,
                         days_held, status)
                        VALUES
                        (:rid, :date, :code, :name, :rec_date,
                         :entry, :stop, :target,
                         :cur, :ret, :maxr, :minr, :days, :status)
                    """),
                    {
                        "rid": rec_id, "date": today, "code": code,
                        "name": rec["name"], "rec_date": rec_date,
                        "entry": entry_price, "stop": rec.get("stop_price"),
                        "target": rec.get("target_price"),
                        "cur": round(current_price, 0), "ret": return_pct,
                        "maxr": round(max_return, 2), "minr": round(min_return, 2),
                        "days": days_held, "status": status,
                    },
                )
            stats["processed"] += 1
            if status in stats:
                stats[status] += 1

            logger.info(
                "[Tracker] %s(%s) %s | 진입 %,.0f → 현재 %,.0f (%+.2f%%) | %d일 | %s",
                rec["name"], code, today,
                entry_price, current_price, return_pct, days_held, status,
            )

            # 목표가/손절 도달 첫 발생 시 텔레그램 알림
            if status in ("target_hit", "stop_hit"):
                _send_status_alert(
                    status=status,
                    name=rec["name"], code=code,
                    entry_price=entry_price,
                    current_price=current_price,
                    return_pct=return_pct,
                    target_price=rec.get("target_price"),
                    stop_price=rec.get("stop_price"),
                    days_held=days_held,
                    rec_date=rec_date,
                )

        except Exception as e:
            logger.warning("[Tracker] 스냅샷 저장 실패 (%s): %s", code, e)
            stats["errors"] += 1

    logger.info("[Tracker] 완료: %s", stats)
    return stats


# ── 조회 함수 ───────────────────────────────────────────────────────

def get_tracking_summary(days: int = 30) -> dict:
    """최근 N일 추천 성과 요약 통계."""
    try:
        cutoff = (datetime.now(_KST) - timedelta(days=days)).strftime("%Y-%m-%d")
        with get_conn() as conn:
            rows = conn.execute(
                text("""
                    SELECT rt.code, rt.name, rt.rec_date, rt.entry_price,
                           rt.target_price, rt.stop_price,
                           rt.return_pct, rt.max_return, rt.min_return,
                           rt.days_held, rt.status
                    FROM recommendation_tracking rt
                    INNER JOIN (
                        SELECT rec_id, MAX(date) AS max_date
                        FROM recommendation_tracking
                        GROUP BY rec_id
                    ) latest ON rt.rec_id = latest.rec_id AND rt.date = latest.max_date
                    WHERE rt.rec_date >= :cutoff
                    ORDER BY rt.rec_date DESC
                """),
                {"cutoff": cutoff},
            ).fetchall()

        items = [
            {
                "code": r[0], "name": r[1], "rec_date": r[2],
                "entry_price": r[3], "target_price": r[4], "stop_price": r[5],
                "return_pct": r[6], "max_return": r[7], "min_return": r[8],
                "days_held": r[9], "status": r[10],
            }
            for r in rows
        ]

        if not items:
            return {"items": [], "total": 0, "win_rate": 0.0,
                    "avg_return": 0.0, "target_rate": 0.0}

        total       = len(items)
        target_hits = sum(1 for i in items if i["status"] == "target_hit")
        stop_hits   = sum(1 for i in items if i["status"] == "stop_hit")
        returns     = [i["return_pct"] for i in items if i["return_pct"] is not None]
        avg_return  = round(sum(returns) / len(returns), 2) if returns else 0.0
        wins        = sum(1 for r in returns if r > 0)
        win_rate    = round(wins / len(returns) * 100, 1) if returns else 0.0
        target_rate = round(target_hits / total * 100, 1)

        return {
            "items":       items,
            "total":       total,
            "target_hits": target_hits,
            "stop_hits":   stop_hits,
            "win_rate":    win_rate,
            "avg_return":  avg_return,
            "target_rate": target_rate,
        }
    except Exception as e:
        logger.warning("[Tracker] 요약 조회 실패: %s", e)
        return {"items": [], "total": 0, "win_rate": 0.0,
                "avg_return": 0.0, "target_rate": 0.0}


def get_active_tracking_list() -> list[dict]:
    """현재 추적 중인 종목 목록 (최신 스냅샷)."""
    try:
        with get_conn() as conn:
            rows = conn.execute(
                text("""
                    SELECT rt.code, rt.name, rt.rec_date, rt.entry_price,
                           rt.target_price, rt.stop_price,
                           rt.current_price, rt.return_pct, rt.max_return,
                           rt.days_held, rt.status, rt.date AS last_update
                    FROM recommendation_tracking rt
                    INNER JOIN (
                        SELECT rec_id, MAX(date) AS max_date
                        FROM recommendation_tracking
                        GROUP BY rec_id
                    ) latest ON rt.rec_id = latest.rec_id AND rt.date = latest.max_date
                    WHERE rt.status = 'tracking'
                    ORDER BY rt.rec_date DESC
                """)
            ).fetchall()
        return [
            {
                "code": r[0], "name": r[1], "rec_date": r[2],
                "entry_price": r[3], "target_price": r[4], "stop_price": r[5],
                "current_price": r[6], "return_pct": r[7], "max_return": r[8],
                "days_held": r[9], "status": r[10], "last_update": r[11],
            }
            for r in rows
        ]
    except Exception as e:
        logger.warning("[Tracker] 활성 추적 목록 조회 실패: %s", e)
        return []


def format_tracker_report() -> str:
    """추적 리포트 텔레그램 메시지 포맷."""
    summary = get_tracking_summary(days=30)
    items   = summary.get("items", [])
    if not items:
        return "📊 최근 30일 추천 종목 추적 데이터 없음"

    status_map = {
        "tracking":   "📍 추적중",
        "target_hit": "🎯 목표달성",
        "stop_hit":   "🛑 손절",
        "expired":    "⏰ 만료",
    }

    lines = [
        "━━━━━━━━━━━━━━━━━━━━━━━━━━",
        "📊 AI 추천 종목 성과 추적 (최근 30일)",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"총 {summary['total']}건 | 승률 {summary['win_rate']}% | "
        f"평균수익 {summary['avg_return']:+.2f}% | 목표달성 {summary['target_rate']}%",
        "",
    ]
    for item in items[:15]:  # 최대 15건
        st    = status_map.get(item["status"], item["status"])
        ret   = item["return_pct"] or 0
        emoji = "🔺" if ret > 0 else ("🔻" if ret < 0 else "➖")
        lines.append(
            f"{st} {item['name']}({item['code']}) "
            f"{emoji}{ret:+.2f}% ({item['days_held']}일) "
            f"[{item['rec_date']}]"
        )

    return "\n".join(lines)
