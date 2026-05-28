"""
watchlist_service.py
관심종목(워치리스트) 관리 서비스
"""
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from db.database import get_conn
from sqlalchemy import text

logger = logging.getLogger(__name__)
_TZ = ZoneInfo("Asia/Seoul")

TRIGGER_TYPES = {
    "price_below": "목표가 이하 도달",
    "rsi_oversold": "RSI 과매도(30 이하)",
    "breakout": "저항선 돌파",
    "pullback": "눌림목 진입",
    "manual": "수동 확인",
    "volume_surge": "거래량 급증",
}


# ── CRUD ──────────────────────────────────────────────────────

def add_to_watchlist(code: str, name: str, target_entry: float = None,
                     timeframe: str = "short", reason: str = None,
                     trigger_type: str = "price_below", trigger_value: float = None,
                     priority: str = "normal") -> int:
    """워치리스트에 종목 추가. 이미 있으면 업데이트."""
    now = datetime.now(_TZ).strftime("%Y-%m-%d")
    trigger_value = trigger_value or target_entry

    with get_conn() as conn:
        existing = conn.execute(
            text("SELECT id FROM watchlist_items WHERE code=:code"),
            {"code": code},
        ).fetchone()

        if existing:
            conn.execute(
                text(
                    "UPDATE watchlist_items SET name=:name, target_entry=:target, "
                    "timeframe=:tf, reason=:reason, trigger_type=:ttype, "
                    "trigger_value=:tval, priority=:priority, status='active', added_date=:now "
                    "WHERE code=:code"
                ),
                {"name": name, "target": target_entry, "tf": timeframe, "reason": reason,
                 "ttype": trigger_type, "tval": trigger_value, "priority": priority,
                 "now": now, "code": code},
            )
            row_id = existing[0]
            logger.info("워치리스트 업데이트: %s(%s) [%s] 목표진입 %s원",
                        name, code, priority, f"{target_entry:,.0f}" if target_entry else "미설정")
        else:
            result = conn.execute(
                text(
                    "INSERT INTO watchlist_items "
                    "(code, name, target_entry, timeframe, reason, trigger_type, trigger_value, priority, added_date) "
                    "VALUES (:code, :name, :target, :tf, :reason, :ttype, :tval, :priority, :now) RETURNING id"
                ),
                {"code": code, "name": name, "target": target_entry, "tf": timeframe,
                 "reason": reason, "ttype": trigger_type, "tval": trigger_value,
                 "priority": priority, "now": now},
            )
            row_id = result.scalar()
            logger.info("워치리스트 추가: %s(%s) [%s/%s] 목표진입 %s원",
                        name, code, timeframe, priority,
                        f"{target_entry:,.0f}" if target_entry else "미설정")
    return row_id


def remove_from_watchlist(code: str) -> bool:
    """워치리스트에서 종목 제거."""
    with get_conn() as conn:
        result = conn.execute(
            text("UPDATE watchlist_items SET status='removed' WHERE code=:code AND status='active'"),
            {"code": code},
        )
    removed = result.rowcount > 0
    if removed:
        logger.info("워치리스트 제거: %s", code)
    return removed


def get_watchlist(status: str = "active") -> list[dict]:
    """워치리스트 조회."""
    try:
        with get_conn() as conn:
            rows = conn.execute(
                text(
                    "SELECT code, name, target_entry, timeframe, reason, "
                    "trigger_type, trigger_value, priority, status, added_date "
                    "FROM watchlist_items WHERE status=:status "
                    "ORDER BY CASE priority WHEN 'urgent' THEN 1 WHEN 'normal' THEN 2 ELSE 3 END, added_date DESC"
                ),
                {"status": status},
            ).fetchall()
        return [
            {"code": r[0], "name": r[1], "target_entry": r[2], "timeframe": r[3],
             "reason": r[4], "trigger_type": r[5], "trigger_value": r[6],
             "priority": r[7], "status": r[8], "added_date": r[9]}
            for r in rows
        ]
    except Exception as e:
        logger.warning("워치리스트 조회 실패: %s", e)
        return []


# ── 트리거 감지 ────────────────────────────────────────────────

def check_triggers(kis=None) -> list[dict]:
    """워치리스트 종목의 진입 조건 충족 여부 확인."""
    items = get_watchlist("active")
    if not items:
        return []

    triggered = []
    for item in items:
        if not kis:
            continue
        try:
            data = kis.get_stock_price(item["code"], market=None)
            current_price = data.get("price", 0)
            if not current_price:
                continue

            item["current_price"] = current_price
            ttype = item.get("trigger_type", "price_below")
            tval  = item.get("trigger_value") or item.get("target_entry")

            fired = False
            trigger_msg = ""

            if ttype == "price_below" and tval and current_price <= tval:
                fired = True
                trigger_msg = f"현재가 {current_price:,.0f}원 ≤ 목표진입 {tval:,.0f}원"

            elif ttype == "rsi_oversold":
                from clients.market_data_client import fetch_kr_stock_technicals
                tech = fetch_kr_stock_technicals(f"{item['code']}.KS")
                if not tech:
                    tech = fetch_kr_stock_technicals(f"{item['code']}.KQ")
                if tech and tech.get("rsi14", 100) <= 30:
                    fired = True
                    trigger_msg = f"RSI {tech['rsi14']} ≤ 30 (과매도 진입 기회)"

            elif ttype == "pullback" and tval and current_price <= tval:
                fired = True
                trigger_msg = f"현재가 {current_price:,.0f}원 — 눌림목 {tval:,.0f}원 도달"

            elif ttype == "manual":
                item["trigger_msg"] = "수동 확인 필요"
                triggered.append(item)
                continue

            if fired:
                item["trigger_msg"] = trigger_msg
                triggered.append(item)
                logger.info("워치리스트 트리거: %s(%s) — %s",
                            item["name"], item["code"], trigger_msg)

        except Exception as e:
            logger.debug("트리거 체크 실패 (%s): %s", item["code"], e)

    return triggered


# ── 포맷 ──────────────────────────────────────────────────────

def format_watchlist_for_briefing(kis=None, include_triggered_only: bool = False) -> str:
    """브리핑/에이전트용 워치리스트 텍스트."""
    items = get_watchlist("active")
    if not items:
        return "관심 종목 없음"

    triggered = check_triggers(kis) if kis else []
    triggered_codes = {t["code"] for t in triggered}

    tf_map = {"short": "단기", "mid": "중기", "long": "장기"}
    pr_map = {"urgent": "🔴긴급", "normal": "🟡보통", "low": "🔵낮음"}

    items_to_show = [i for i in items if i["code"] in triggered_codes] if include_triggered_only else items

    if not items_to_show:
        return "조건 충족 종목 없음" if include_triggered_only else "관심 종목 없음"

    lines = [f"👀 관심종목 워치리스트 ({len(items)}개 모니터링중)"]

    if triggered:
        lines.append(f"\n🚨 진입 조건 충족 종목 ({len(triggered)}개):")
        for t in triggered:
            lines.append(
                f"  ✅ {t['name']}({t['code']}) [{tf_map.get(t['timeframe'], t['timeframe'])}]"
                f"\n     {t.get('trigger_msg', '')}"
                f"\n     주목 이유: {t.get('reason', '미기재')}"
            )

    if not include_triggered_only:
        remaining = [i for i in items if i["code"] not in triggered_codes]
        if remaining:
            lines.append(f"\n📋 대기 중 ({len(remaining)}개):")
            for i in remaining:
                pr_label = pr_map.get(i.get("priority", "normal"), "🟡보통")
                tf_label = tf_map.get(i.get("timeframe", "short"), "단기")
                entry_str = f" | 목표진입 {i['target_entry']:,.0f}원" if i.get("target_entry") else ""
                lines.append(
                    f"  {pr_label} {i['name']}({i['code']}) [{tf_label}]{entry_str}"
                    + (f"\n     {i['reason']}" if i.get("reason") else "")
                )

    return "\n".join(lines)


def format_watchlist_telegram() -> str:
    """텔레그램 직접 발송용 워치리스트 현황."""
    from clients.kis_client import KISClient
    try:
        kis = KISClient()
    except Exception:
        kis = None
    return format_watchlist_for_briefing(kis, include_triggered_only=False)
