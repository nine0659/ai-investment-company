"""
추천 종목 DB 저장·조회·수익률 업데이트
"""
import logging
import os
import re
import sqlite3

logger = logging.getLogger(__name__)

_DB = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "data", "database.sqlite3"))


def _conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(_DB), exist_ok=True)
    return sqlite3.connect(_DB)


# ── 파싱 ──────────────────────────────────────────────────────────

_REC_PATTERN = re.compile(
    r"([가-힣A-Za-z·&\s]{1,20}?)\s*\((\d{6})\)"   # 종목명(코드)
    r"[^\n]*?진입[가격]*\s*[：:]?\s*([\d,]+)\s*원?"  # 진입가
    r"[^\n]*?손절[가격]*\s*[：:]?\s*([\d,]+)\s*원?"  # 손절가
    r"[^\n]*?목표[가격]*\s*[：:]?\s*([\d,]+)\s*원?"  # 목표가
    r"(?:[^\n]*?\|\s*([^\n|]{1,80}))?",             # 근거(선택)
    re.DOTALL,
)


def parse_recommendations(report_text: str) -> list[dict]:
    """CEO 브리핑 텍스트에서 추천 종목 파싱"""
    results = []
    for m in _REC_PATTERN.finditer(report_text):
        try:
            name    = m.group(1).strip()
            code    = m.group(2)
            entry   = int(m.group(3).replace(",", ""))
            stop    = int(m.group(4).replace(",", ""))
            target  = int(m.group(5).replace(",", ""))
            reason  = (m.group(6) or "").strip()
            if entry > 0 and stop > 0 and target > 0:
                results.append({
                    "name": name, "code": code,
                    "entry_price": entry, "stop_price": stop,
                    "target_price": target, "rationale": reason,
                })
        except (ValueError, AttributeError):
            continue
    return results


# ── 저장 / 조회 ──────────────────────────────────────────────────

def save_recommendations(date: str, recs: list[dict]) -> int:
    """추천 종목 저장 (같은 날 기존 데이터는 삭제 후 재저장). 저장 건수 반환."""
    if not recs:
        return 0
    with _conn() as c:
        c.execute("DELETE FROM stock_recommendations WHERE date=?", (date,))
        c.executemany(
            "INSERT INTO stock_recommendations "
            "(date, code, name, entry_price, stop_price, target_price, rationale) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(date, r["code"], r["name"],
              r["entry_price"], r["stop_price"], r["target_price"], r["rationale"])
             for r in recs],
        )
    logger.info("추천 종목 저장 완료: %d건 (%s)", len(recs), date)
    return len(recs)


def get_recommendations(date: str) -> list[dict]:
    """특정 날짜 추천 종목 조회"""
    try:
        with _conn() as c:
            rows = c.execute(
                "SELECT code, name, entry_price, stop_price, target_price, "
                "rationale, close_price, return_pct, result "
                "FROM stock_recommendations WHERE date=?",
                (date,),
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
    """최근 N일 추천 종목 전체 조회 (통계용)"""
    try:
        with _conn() as c:
            rows = c.execute(
                "SELECT date, code, name, entry_price, stop_price, target_price, "
                "close_price, return_pct, result "
                "FROM stock_recommendations "
                "WHERE date >= date('now', ?) AND return_pct IS NOT NULL "
                "ORDER BY date DESC",
                (f"-{days} days",),
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

def _classify(return_pct: float) -> str:
    if return_pct >= 2.0:
        return "성공"
    if return_pct <= -2.0:
        return "실패"
    return "보통"


def update_close_prices(date: str, kis) -> list[dict]:
    """
    당일 추천 종목 종가 수집 → 수익률 계산 → DB 업데이트.
    kis: KISClient 인스턴스
    반환: 업데이트된 종목 리스트
    """
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
            result = _classify(ret)
            with _conn() as c:
                c.execute(
                    "UPDATE stock_recommendations "
                    "SET close_price=?, return_pct=?, result=? "
                    "WHERE date=? AND code=?",
                    (close, round(ret, 2), result, date, rec["code"]),
                )
            updated.append({**rec, "close_price": close,
                             "return_pct": round(ret, 2), "result": result})
            logger.info("종가 업데이트: %s(%s) 진입 %s → 종가 %s (%.1f%% %s)",
                        rec["name"], rec["code"],
                        rec["entry_price"], close, ret, result)
        except Exception as e:
            logger.warning("종가 업데이트 실패 (%s): %s", rec["code"], e)

    return updated


def format_returns_for_report(results: list[dict]) -> str:
    """종가/수익률 결과를 텔레그램 메시지용 텍스트로 변환"""
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
