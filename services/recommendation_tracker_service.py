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
        # 2026-09-29 발견: 이 분기가 조용히(경보 없이) []를 반환하면 run_daily_tracker가
        # "추적 대상 없음"과 구분 못 하고 그냥 넘어간다 — job_runs엔 daily_tracker
        # "success"만 찍혀 daily_health가 못 잡는다. nav_service.record_nav()의
        # "보유 0개" 사각지대(2f3738b)와 동형 구조라 같은 방식으로 경보를 단다.
        try:
            from clients.telegram_client import send_error_alert
            send_error_alert(f"[Tracker] 추천 종목 조회 실패(DB) — 추적 스킵됨: {e}")
        except Exception:
            pass
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


def get_held_position(code: str) -> tuple[int, float] | None:
    """실제 보유 중이면 (수량, 평단), 아니면 None (조회 실패도 None)."""
    try:
        with get_conn() as conn:
            pos = conn.execute(
                text("SELECT quantity, avg_price FROM portfolio_positions "
                     "WHERE code=:c AND status='holding' AND quantity > 0"),
                {"c": code},
            ).fetchone()
        if pos and pos[1] and pos[1] > 0:
            return int(pos[0]), float(pos[1])
    except Exception:
        pass
    return None


def basis_line(code: str, current_price: float, entry_price: float | None) -> str:
    """알림용 손익 기준 문구. 실보유면 실제 평단 기준 손익, 아니면 가상 추적임을 명시.
    추천 진입가 기준 등락을 '내 수익률'처럼 보이게 하지 않는다(2026-10-02 사고)."""
    pos = get_held_position(code)
    ref = ""
    if entry_price:
        ref = (f"  (추천 기준가 {entry_price:,.0f}원 대비 {(current_price - entry_price) / entry_price * 100:+.1f}%"
               f" — 참고용, 실제 매매 아님)")
    if pos:
        qty, avg = pos
        return (f"  내 보유: {qty:,}주 @ 평단 {avg:,.0f}원 → *내 수익률 {(current_price - avg) / avg * 100:+.2f}%*\n" + ref).rstrip()
    if ref:
        return "  미보유 종목 — 아래는 내 계좌와 무관한 가상 추적 수치\n" + ref
    return "  미보유 종목 (가상 추적)"


def format_target_alert(
    name: str, code: str, rec_date: str, days_held: int,
    entry_price: float, current_price: float, return_pct: float, price_str: str,
    held_qty: int | None, held_avg: float | None,
) -> str:
    """목표가 달성 알림 본문. 실보유면 실제 평단 기준 손익을 먼저, 추천가 기준은 참고로."""
    ref = (
        f"추천 기준가(참고용, 실제 매매 아님): {entry_price:,.0f}원 → {return_pct:+.2f}%\n"
        f"추천일 {rec_date} ({days_held}일 경과) · 추천 목표가 {price_str}"
    )
    if held_qty and held_avg and held_avg > 0:
        my_pct = (current_price - held_avg) / held_avg * 100
        pnl = (current_price - held_avg) * held_qty
        note = (
            "추천 목표가에는 도달했지만 내 평단 기준으로는 아직 손실 구간입니다."
            if my_pct < 0 else "추천 목표가에 도달했고 내 평단 기준으로도 수익 구간입니다."
        )
        return (
            f"🎯 *[추천 목표가 도달] {name}({code})*\n\n"
            f"▶ *내 실제 보유*: {held_qty:,}주 @ 평단 {held_avg:,.0f}원\n"
            f"▶ 현재가 {current_price:,.0f}원 → *내 수익률 {my_pct:+.2f}%* "
            f"({pnl:+,.0f}원)\n"
            f"{note}\n\n{ref}"
        )
    return (
        f"🎯 *[추천 목표가 도달 — 미보유·가상 추적] {name}({code})*\n\n"
        f"실제로 보유하지 않은 종목입니다. 아래 수익률은 내 계좌와 무관한 "
        f"추천 시점 기준 가상 수치입니다.\n"
        f"현재가 {current_price:,.0f}원\n\n{ref}"
    )


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

        if status == "stop_hit":
            # 실제 보유 중이면 수량 기반 권고(결론·행동 우선), 미보유면 신규진입 금지 안내
            with get_conn() as conn:
                pos = conn.execute(
                    text("SELECT quantity, avg_price FROM portfolio_positions "
                         "WHERE code=:c AND status='holding' AND quantity > 0"),
                    {"c": code},
                ).fetchone()
            from clients.telegram_client import send_message
            from services.stop_advice import build_advice, format_stop_alert
            if pos:
                adv = build_advice(pos[0], pos[1], current_price)
                if adv:
                    send_message(format_stop_alert(name, code, pos[0], pos[1], current_price, adv))
                    logger.info("[Tracker] 손절 권고(보유) 발송: %s(%s) %+.2f%%", name, code, return_pct)
                    return
            else:
                send_message(
                    f"🛑 *[추천 손절선 도달] {name}({code})  {return_pct:+.1f}%*\n\n"
                    f"▶ 결론: 추천 진입가 {entry_price:,.0f}원 → 현재 {current_price:,.0f}원, "
                    f"손절선({price_str})을 이탈했습니다. 이 추천은 종료합니다.\n"
                    f"▶ 행동: 미보유 종목이므로 *신규 진입 금지*. 새 근거가 나오기 전에는 "
                    f"반등해도 재진입하지 마세요.\n"
                    f"추천일 {rec_date} ({days_held}일 경과)"
                )
                logger.info("[Tracker] 손절 안내(미보유) 발송: %s(%s) %+.2f%%", name, code, return_pct)
                return

        # 2026-10-02: 목표가 알림이 시스템이 기록한 '추천 진입가' 기준 수익률만 보여줘
        # 실제 평단(-5.6% 손실)과 무관한 +10.6%를 자축하듯 발송한 사고 — 실보유는
        # 반드시 portfolio_positions의 실제 평단으로 보여준다(손절 분기와 동일 원칙).
        with get_conn() as conn:
            pos = conn.execute(
                text("SELECT quantity, avg_price FROM portfolio_positions "
                     "WHERE code=:c AND status='holding' AND quantity > 0"),
                {"c": code},
            ).fetchone()
        msg = format_target_alert(
            name, code, rec_date, days_held, entry_price, current_price,
            return_pct, price_str, pos[0] if pos else None, pos[1] if pos else None,
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
        # 2026-09-29 발견: daily_tracker는 학습 루프의 핵심 잡(추천→추적→
        # recommendation_tracking→귀인분석 재개 기준)인데, 이 분기가 조용히
        # {"processed":0,...}만 반환하면 job_runs엔 "success"만 찍혀 daily_health가
        # 학습 루프가 몇 주째 멈춰있어도 못 잡는다. nav_service.record_nav()의
        # "보유 0개" 사각지대(2f3738b)와 같은 이유로 경보를 단다.
        try:
            from clients.telegram_client import send_error_alert
            send_error_alert(
                "[Tracker] 추적 대상 추천 종목이 0건 — 학습 루프(추천→추적) 중단 여부 점검 필요"
            )
        except Exception:
            pass
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
                "[Tracker] %s(%s) %s | 진입 %.0f → 현재 %.0f (%+.2f%%) | %d일 | %s",
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


def _get_held_codes() -> set[str]:
    """실제 보유 중인 종목코드 집합 (조회 실패 시 빈 집합)."""
    try:
        with get_conn() as conn:
            rows = conn.execute(
                text("SELECT code FROM portfolio_positions WHERE status='holding' AND quantity > 0")
            ).fetchall()
        return {r[0] for r in rows}
    except Exception:
        return set()


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
        "⚠️ 추천 시점 가격 기준 *가상 성과*입니다. 내 실제 매매 손익이 아닙니다.",
        "",
    ]
    held = _get_held_codes()
    for item in items[:15]:  # 최대 15건
        st    = status_map.get(item["status"], item["status"])
        ret   = item["return_pct"] or 0
        emoji = "🔺" if ret > 0 else ("🔻" if ret < 0 else "➖")
        lines.append(
            f"{st} {item['name']}({item['code']}) "
            f"{emoji}{ret:+.2f}% ({item['days_held']}일) "
            f"[{item['rec_date']}]" + (" 📌실보유(내 평단 기준 아님)" if item["code"] in held else "")
        )

    return "\n".join(lines)
