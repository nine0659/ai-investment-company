"""
손절·목표가·급등락 자동 알림 에이전트
장중(09:10~15:20) 10분마다 실행 → 조건 충족 시 텔레그램 즉시 발송
"""
import logging
import os
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

from clients.kis_client import KISClient
from clients.telegram_client import send_message
from services.recommendation_service import get_recommendations

logger = logging.getLogger(__name__)
_KST = ZoneInfo("Asia/Seoul")

_DB = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "data", "database.sqlite3"))

# 알림 중복 방지 쿨다운 (분)
_ALERT_COOLDOWN_MIN = 60


def _conn():
    os.makedirs(os.path.dirname(_DB), exist_ok=True)
    return sqlite3.connect(_DB)


def _ensure_alert_log():
    with _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS price_alert_log (
                date    TEXT,
                code    TEXT,
                type    TEXT,
                sent_at TEXT DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (date, code, type)
            )
        """)


def _already_alerted(date: str, code: str, alert_type: str) -> bool:
    try:
        with _conn() as c:
            row = c.execute(
                "SELECT sent_at FROM price_alert_log WHERE date=? AND code=? AND type=?",
                (date, code, alert_type),
            ).fetchone()
        return row is not None
    except Exception:
        return False


def _mark_alerted(date: str, code: str, alert_type: str):
    try:
        with _conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO price_alert_log (date, code, type) VALUES (?,?,?)",
                (date, code, alert_type),
            )
    except Exception:
        pass


def _is_market_hours() -> bool:
    now = datetime.now(_KST)
    from datetime import time
    return now.weekday() < 5 and time(9, 10) <= now.time() <= time(15, 20)


def check_alerts() -> int:
    """추천 종목 가격 체크 → 조건 충족 시 알림. 발송 건수 반환."""
    if not _is_market_hours():
        logger.debug("[가격 알림] 장외 시간 스킵")
        return 0

    now  = datetime.now(_KST)
    date = now.strftime("%Y-%m-%d")
    _ensure_alert_log()

    recs = get_recommendations(date)
    if not recs:
        logger.debug("[가격 알림] 오늘 추천 종목 없음")
        return 0

    kis  = KISClient()
    sent = 0

    for rec in recs:
        code   = rec["code"]
        name   = rec["name"]
        entry  = rec.get("entry_price") or 0
        stop   = rec.get("stop_price") or 0
        target = rec.get("target_price") or 0

        try:
            price_data = kis.get_stock_price(code)
            cur = price_data.get("price", 0)
            if not cur:
                continue

            chg_from_entry = (cur - entry) / entry * 100 if entry else 0

            alerts: list[tuple[str, str]] = []

            if stop and cur <= stop and not _already_alerted(date, code, "stop"):
                alerts.append(("stop", f"⚠️ *손절선 도달!*\n{name}({code})\n현재가 {cur:,}원 ≤ 손절 {stop:,}원\n→ 즉시 손절 검토"))
            if target and cur >= target and not _already_alerted(date, code, "target"):
                alerts.append(("target", f"🎯 *목표가 도달!*\n{name}({code})\n현재가 {cur:,}원 ≥ 목표 {target:,}원\n→ 수익 실현 검토"))
            if chg_from_entry >= 5 and not _already_alerted(date, code, "surge"):
                alerts.append(("surge", f"🚀 *급등 중!*\n{name}({code})\n현재가 {cur:,}원  (+{chg_from_entry:.1f}%)\n→ 목표가 확인 후 분할 매도 검토"))
            if chg_from_entry <= -5 and not _already_alerted(date, code, "drop"):
                alerts.append(("drop", f"🔴 *급락!*\n{name}({code})\n현재가 {cur:,}원  ({chg_from_entry:.1f}%)\n→ 손절선 확인 후 대응"))

            for alert_type, msg in alerts:
                try:
                    send_message(msg)
                    _mark_alerted(date, code, alert_type)
                    sent += 1
                    logger.info("[가격 알림] %s %s (%s)", alert_type, name, code)
                except Exception as e:
                    logger.warning("[가격 알림] 발송 실패 (%s): %s", code, e)

        except Exception as e:
            logger.debug("[가격 알림] 가격 조회 실패 (%s): %s", code, e)

    return sent


def run():
    logger.info("[가격 알림] 시작")
    sent = check_alerts()
    logger.info("[가격 알림] 완료 — 알림 %d건", sent)


if __name__ == "__main__":
    import logging as _l
    _l.basicConfig(level=_l.INFO, format="%(asctime)s %(message)s")
    run()
