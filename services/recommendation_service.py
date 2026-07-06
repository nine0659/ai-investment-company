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

# "목표 +X% / 손절 -Y%" 패턴 — 현재 CIO 브리핑 포맷
_PCT_RE = re.compile(r"목표\s+\+?([\d.]+)%\s*/\s*손절\s+-?([\d.]+)%")


def recs_from_cio_decisions(
    decisions: dict,
    ceo_report: str,
    price_fn,  # Callable[[str], int] — 종목코드 → 현재가(원), 실패 시 0
) -> list[dict]:
    """ceo_decisions.new_positions + 브리핑 텍스트 % + 현재가 → stock_recommendations 레코드.

    진입가: price_fn으로 조회한 현재가
    목표가/손절가: 브리핑 텍스트 "목표 +X% / 손절 -Y%" 파싱 후 현재가에 적용
    """
    results: list[dict] = []
    seen: set[str] = set()

    for pos in decisions.get("new_positions", []):
        code = pos.get("code", "")
        name = pos.get("name", "")
        if not code or code in seen:
            continue

        # 브리핑 텍스트에서 해당 종목 주변 블록 추출 (코드 또는 이름 앞 4자 기준)
        target_pct = stop_pct = None
        for anchor in (code, name[:4] if name else ""):
            idx = ceo_report.find(anchor)
            if idx < 0:
                continue
            nearby = ceo_report[max(0, idx - 10): idx + 140]
            m = _PCT_RE.search(nearby)
            if m:
                try:
                    target_pct = float(m.group(1))
                    stop_pct   = float(m.group(2))
                except ValueError:
                    pass
                break

        entry  = price_fn(code) if price_fn else 0
        target = int(entry * (1 + target_pct / 100)) if entry and target_pct else 0
        stop   = int(entry * (1 - stop_pct   / 100)) if entry and stop_pct   else 0

        seen.add(code)
        results.append({
            "name":         name,
            "code":         code,
            "entry_price":  entry,
            "stop_price":   stop,
            "target_price": target,
            "rationale":    pos.get("thesis", ""),
        })

    return results


# "1. 종목명 (005930) — 현재가 175,100원 → 목표가 210,000원" 패턴 — 주간 추천 포맷
_PICK_RE = re.compile(
    r"^\s*\d+\.\s*(?P<name>[^\n(]+?)\s*\((?P<code>\d{6})\)\s*"
    r"[—–\-]+\s*현재가\s*(?P<entry>[\d,]+)\s*원\s*"
    r"(?:→|->)\s*(?:목표가|적정가치[^\d]*)\s*(?P<target>[\d,]+)\s*원",
    re.MULTILINE,
)


def recs_from_weekly_picks(
    report: str,
    price_lookup: dict[str, float],
    rationale_prefix: str = "주간 추천(중기)",
) -> list[dict]:
    """주간 추천 브리핑 텍스트 → stock_recommendations 레코드 (환각 교차검증 포함).

    이 함수가 있어야 추천→추적→적중률→개선의 학습 루프가 돈다. 과거엔
    주간 추천이 어디에도 기록되지 않아 적중률 리포트가 만년 '데이터 없음'이었다.

    환각 차단 규칙:
      1) 종목코드가 실제 분석에 쓰인 데이터(price_lookup)에 없으면 폐기
      2) 진입가는 브리핑 텍스트가 아니라 항상 실데이터 가격을 쓴다
         (텍스트 가격이 실데이터와 5% 이상 어긋나면 경고 로그)
      3) 목표가가 진입가의 0.7~3.0배 범위 밖이면 폐기 (비현실 목표)
      4) "(지난 추천 유지)" 종목은 저장하지 않는다 — 최초 추천일 기준으로
         이미 추적 중이므로, 다시 저장하면 성과 측정 기준일이 오염된다
    """
    results: list[dict] = []
    seen: set[str] = set()

    for m in _PICK_RE.finditer(report):
        code = m.group("code")
        name = m.group("name").strip()
        if code in seen:
            continue

        line_block = report[m.start(): m.start() + 300]
        if "지난 추천 유지" in line_block.split("\n")[0]:
            logger.info("[추천파싱] %s(%s) 지난 추천 유지 — 기존 추적 계속", name, code)
            continue

        actual_price = price_lookup.get(code)
        if not actual_price:
            logger.warning("[추천파싱] %s(%s) 분석 데이터에 없는 종목 — 환각 의심, 폐기", name, code)
            continue

        try:
            text_entry = float(m.group("entry").replace(",", ""))
            target     = float(m.group("target").replace(",", ""))
        except ValueError:
            continue

        if abs(text_entry - actual_price) / actual_price > 0.05:
            logger.warning(
                "[추천파싱] %s(%s) 브리핑 현재가 %s원 ≠ 실데이터 %s원 — 실데이터 사용",
                name, code, f"{text_entry:,.0f}", f"{actual_price:,.0f}",
            )

        if not (0.7 <= target / actual_price <= 3.0):
            logger.warning("[추천파싱] %s(%s) 목표가 %s원 비현실적 (현재가 대비 %.1f배) — 폐기",
                           name, code, f"{target:,.0f}", target / actual_price)
            continue

        # "왜:" 한 줄을 근거로 첨부
        rationale = rationale_prefix
        why = re.search(r"왜[^:：]*[:：]\s*(.+)", line_block)
        if why:
            rationale = f"{rationale_prefix} — {why.group(1).strip()[:150]}"

        seen.add(code)
        results.append({
            "name":         name,
            "code":         code,
            "entry_price":  int(actual_price),
            "stop_price":   0,
            "target_price": int(target),
            "rationale":    rationale,
        })

    return results


def has_open_recommendation(code: str, days: int = 30) -> bool:
    """최근 N일 내 같은 종목의 미결(추적 가능) 추천이 있는지 — 중복 추적 방지."""
    try:
        cutoff = (datetime.now(_TZ) - timedelta(days=days)).strftime("%Y-%m-%d")
        with get_conn() as conn:
            row = conn.execute(
                text("SELECT 1 FROM stock_recommendations "
                     "WHERE code=:code AND date >= :cutoff LIMIT 1"),
                {"code": code, "cutoff": cutoff},
            ).fetchone()
        return row is not None
    except Exception:
        return False


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
