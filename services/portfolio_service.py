"""
portfolio_service.py
실제 보유 포트폴리오 관리 서비스

기능:
  - 포지션 추가/수정/종료
  - 실시간 P&L 계산
  - 섹터별 비중 분석
  - 포트폴리오 전체 현황 요약
"""
import logging
import os
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

_DB = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "data", "database.sqlite3"))
_TZ = ZoneInfo("Asia/Seoul")


def _conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(_DB), exist_ok=True)
    return sqlite3.connect(_DB)


# ── 포지션 추가 / 수정 ──────────────────────────────────────────

def add_position(code: str, name: str, quantity: int, avg_price: float,
                 entry_date: str = None, timeframe: str = "short",
                 sector: str = None, target_price: float = None,
                 stop_price: float = None, memo: str = None) -> int:
    """포트폴리오에 새 종목 추가. 이미 존재하면 수량/평균단가 업데이트(매수 추가)."""
    now = datetime.now(_TZ).strftime("%Y-%m-%d")
    entry_date = entry_date or now

    with _conn() as c:
        existing = c.execute(
            "SELECT id, quantity, avg_price FROM portfolio_positions WHERE code=? AND status='holding'",
            (code,)
        ).fetchone()

        if existing:
            old_qty, old_avg = existing[1], existing[2]
            new_qty = old_qty + quantity
            new_avg = round((old_qty * old_avg + quantity * avg_price) / new_qty, 0)
            c.execute(
                "UPDATE portfolio_positions SET quantity=?, avg_price=?, updated_at=?, "
                "target_price=COALESCE(?,target_price), stop_price=COALESCE(?,stop_price), "
                "memo=COALESCE(?,memo) WHERE code=? AND status='holding'",
                (new_qty, new_avg, now,
                 target_price, stop_price, memo, code)
            )
            row_id = existing[0]
            logger.info("포지션 추가매수: %s(%s) %d주 → 총 %d주 @평균 %,.0f원",
                        name, code, quantity, new_qty, new_avg)
        else:
            c.execute(
                "INSERT INTO portfolio_positions "
                "(code, name, quantity, avg_price, entry_date, timeframe, sector, "
                " target_price, stop_price, memo, status) "
                "VALUES (?,?,?,?,?,?,?,?,?,'holding')",
                (code, name, quantity, avg_price, entry_date, timeframe,
                 sector, target_price, stop_price)
            )
            # memo 추가
            row_id = c.lastrowid
            if memo:
                c.execute("UPDATE portfolio_positions SET memo=? WHERE id=?", (memo, row_id))
            logger.info("신규 포지션 추가: %s(%s) %d주 @%,.0f원 [%s]",
                        name, code, quantity, avg_price, timeframe)
    return row_id


def update_position(code: str, **kwargs) -> bool:
    """포지션 정보 업데이트 (목표가, 손절가, 메모 등)."""
    allowed = {"quantity", "avg_price", "target_price", "stop_price",
               "sector", "timeframe", "memo", "status"}
    updates = {k: v for k, v in kwargs.items() if k in allowed}
    if not updates:
        return False
    now = datetime.now(_TZ).strftime("%Y-%m-%d")
    updates["updated_at"] = now
    set_clause = ", ".join(f"{k}=?" for k in updates)
    with _conn() as c:
        c.execute(
            f"UPDATE portfolio_positions SET {set_clause} WHERE code=? AND status='holding'",
            (*updates.values(), code)
        )
    return True


def close_position(code: str, exit_price: float = None, exit_date: str = None,
                   partial_qty: int = None) -> dict | None:
    """포지션 종료(매도). exit_price 없으면 avg_price 기준. 부분 매도 지원."""
    now = datetime.now(_TZ).strftime("%Y-%m-%d")
    exit_date = exit_date or now

    with _conn() as c:
        row = c.execute(
            "SELECT id, name, quantity, avg_price, timeframe, memo FROM portfolio_positions "
            "WHERE code=? AND status='holding'",
            (code,)
        ).fetchone()
        if not row:
            return None

        pos_id, name, qty, avg_price, timeframe, memo = row
        exit_price = exit_price or avg_price
        sell_qty = partial_qty or qty
        ret = round((exit_price - avg_price) / avg_price * 100, 2)

        # 이력 저장
        c.execute(
            "INSERT INTO portfolio_history "
            "(code, name, quantity, avg_price, exit_price, exit_date, return_pct, timeframe, memo) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (code, name, sell_qty, avg_price, exit_price, exit_date, ret, timeframe, memo)
        )

        if partial_qty and partial_qty < qty:
            # 부분 매도: 수량만 줄임
            c.execute(
                "UPDATE portfolio_positions SET quantity=?, updated_at=? WHERE code=? AND status='holding'",
                (qty - partial_qty, now, code)
            )
            logger.info("부분 매도: %s(%s) %d주 @%,.0f원 (%.2f%%)",
                        name, code, sell_qty, exit_price, ret)
        else:
            # 전량 매도: 상태 변경
            c.execute(
                "UPDATE portfolio_positions SET status='sold', updated_at=? WHERE code=? AND status='holding'",
                (now, code)
            )
            logger.info("전량 매도: %s(%s) %d주 @%,.0f원 (%.2f%%)",
                        name, code, qty, exit_price, ret)

    return {"code": code, "name": name, "sell_qty": sell_qty,
            "avg_price": avg_price, "exit_price": exit_price, "return_pct": ret}


# ── 조회 ──────────────────────────────────────────────────────

def get_portfolio() -> list[dict]:
    """현재 보유 중인 모든 포지션 반환."""
    try:
        with _conn() as c:
            rows = c.execute(
                "SELECT code, name, quantity, avg_price, entry_date, timeframe, "
                "sector, target_price, stop_price, memo "
                "FROM portfolio_positions WHERE status='holding' ORDER BY entry_date DESC"
            ).fetchall()
        return [
            {"code": r[0], "name": r[1], "quantity": r[2], "avg_price": r[3],
             "entry_date": r[4], "timeframe": r[5], "sector": r[6],
             "target_price": r[7], "stop_price": r[8], "memo": r[9]}
            for r in rows
        ]
    except Exception as e:
        logger.warning("포트폴리오 조회 실패: %s", e)
        return []


def get_portfolio_history(days: int = 90) -> list[dict]:
    """최근 N일 매도 이력 조회."""
    try:
        with _conn() as c:
            rows = c.execute(
                "SELECT code, name, quantity, avg_price, exit_price, exit_date, return_pct, timeframe "
                "FROM portfolio_history WHERE exit_date >= date('now', ?) ORDER BY exit_date DESC",
                (f"-{days} days",)
            ).fetchall()
        return [
            {"code": r[0], "name": r[1], "quantity": r[2], "avg_price": r[3],
             "exit_price": r[4], "exit_date": r[5], "return_pct": r[6], "timeframe": r[7]}
            for r in rows
        ]
    except Exception as e:
        logger.warning("포트폴리오 이력 조회 실패: %s", e)
        return []


# ── P&L 계산 ──────────────────────────────────────────────────

def calculate_pnl(kis=None) -> list[dict]:
    """실시간(또는 전일 종가) 기준 P&L 계산.
    kis: KISClient 인스턴스 (None이면 현재가 없이 avg_price 기준)
    """
    positions = get_portfolio()
    results = []
    for p in positions:
        current_price = p["avg_price"]  # fallback
        price_label = "평균단가(기준)"
        if kis:
            try:
                data = kis.get_stock_price(p["code"], market=None)
                cp = data.get("price", 0)
                if cp:
                    current_price = cp
                    price_label = "현재가"
            except Exception as e:
                logger.debug("현재가 조회 실패 (%s): %s", p["code"], e)

        invested = p["avg_price"] * p["quantity"]
        current_val = current_price * p["quantity"]
        pnl_amt = current_val - invested
        pnl_pct = round((current_price - p["avg_price"]) / p["avg_price"] * 100, 2)

        # 목표가/손절가 대비 상태
        status_flag = "보유중"
        if p.get("stop_price") and current_price <= p["stop_price"]:
            status_flag = "🔴손절선도달"
        elif p.get("target_price") and current_price >= p["target_price"]:
            status_flag = "🟢목표가도달"
        elif pnl_pct >= 5:
            status_flag = "✅수익중"
        elif pnl_pct <= -3:
            status_flag = "⚠️손실중"

        results.append({
            **p,
            "current_price": current_price,
            "price_label": price_label,
            "invested": round(invested),
            "current_val": round(current_val),
            "pnl_amt": round(pnl_amt),
            "pnl_pct": pnl_pct,
            "status_flag": status_flag,
        })
    return results


def get_portfolio_summary(pnl_data: list[dict] = None, kis=None) -> dict:
    """포트폴리오 전체 요약 통계."""
    if pnl_data is None:
        pnl_data = calculate_pnl(kis)
    if not pnl_data:
        return {"count": 0, "total_invested": 0, "total_current": 0,
                "total_pnl_amt": 0, "total_pnl_pct": 0.0, "sector_breakdown": {}}

    total_invested = sum(p["invested"] for p in pnl_data)
    total_current  = sum(p["current_val"] for p in pnl_data)
    total_pnl_amt  = total_current - total_invested
    total_pnl_pct  = round(total_pnl_amt / total_invested * 100, 2) if total_invested else 0.0

    sector_map: dict[str, dict] = {}
    for p in pnl_data:
        s = p.get("sector") or "기타"
        if s not in sector_map:
            sector_map[s] = {"invested": 0, "current": 0, "count": 0}
        sector_map[s]["invested"] += p["invested"]
        sector_map[s]["current"]  += p["current_val"]
        sector_map[s]["count"]    += 1

    sector_breakdown = {}
    for s, v in sector_map.items():
        weight = round(v["invested"] / total_invested * 100, 1) if total_invested else 0
        ret    = round((v["current"] - v["invested"]) / v["invested"] * 100, 2) if v["invested"] else 0
        sector_breakdown[s] = {"weight_pct": weight, "return_pct": ret, "count": v["count"]}

    timeframe_map = {"short": 0, "mid": 0, "long": 0}
    for p in pnl_data:
        tf = p.get("timeframe", "short")
        timeframe_map[tf] = timeframe_map.get(tf, 0) + p["invested"]

    return {
        "count":          len(pnl_data),
        "total_invested": total_invested,
        "total_current":  total_current,
        "total_pnl_amt":  total_pnl_amt,
        "total_pnl_pct":  total_pnl_pct,
        "sector_breakdown": sector_breakdown,
        "timeframe_breakdown": {
            k: round(v / total_invested * 100, 1) if total_invested else 0
            for k, v in timeframe_map.items()
        },
    }


# ── 포맷 ──────────────────────────────────────────────────────

def format_portfolio_for_briefing(kis=None) -> str:
    """브리핑/에이전트 컨텍스트용 포트폴리오 현황 텍스트."""
    pnl_data = calculate_pnl(kis)
    if not pnl_data:
        return "보유 포지션 없음"

    summary = get_portfolio_summary(pnl_data)
    tf_map  = {"short": "단기", "mid": "중기", "long": "장기"}

    lines = [
        f"📂 포트폴리오 현황 ({len(pnl_data)}종목)",
        f"총 투자금: {summary['total_invested']:,.0f}원 | 평가금액: {summary['total_current']:,.0f}원",
        f"총 손익: {summary['total_pnl_amt']:+,.0f}원 ({summary['total_pnl_pct']:+.2f}%)",
    ]

    # 섹터 비중
    if summary["sector_breakdown"]:
        sec_parts = [f"{s}:{v['weight_pct']}%" for s, v in summary["sector_breakdown"].items()]
        lines.append(f"섹터 비중: {' | '.join(sec_parts)}")

    lines.append("\n[종목별 현황]")
    for p in pnl_data:
        tf_label = tf_map.get(p.get("timeframe", "short"), "단기")
        target_line = ""
        if p.get("target_price"):
            target_to_go = round((p["target_price"] - p["current_price"]) / p["current_price"] * 100, 1)
            target_line = f" | 목표가 {p['target_price']:,.0f}원(+{target_to_go}%)"
        stop_line = ""
        if p.get("stop_price"):
            stop_line = f" | 손절 {p['stop_price']:,.0f}원"

        lines.append(
            f"  {p['status_flag']} {p['name']}({p['code']}) [{tf_label}]"
            f"\n    {p['quantity']}주 @평균 {p['avg_price']:,.0f}원 → {p['price_label']} {p['current_price']:,.0f}원"
            f" ({p['pnl_pct']:+.2f}%){target_line}{stop_line}"
        )
        if p.get("memo"):
            lines.append(f"    ※ {p['memo']}")

    return "\n".join(lines)


def format_portfolio_telegram() -> str:
    """텔레그램 직접 발송용 포트폴리오 현황."""
    from clients.kis_client import KISClient
    try:
        kis = KISClient()
    except Exception:
        kis = None
    return format_portfolio_for_briefing(kis)
