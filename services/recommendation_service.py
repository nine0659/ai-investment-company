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

_HEADER_RE = re.compile(r"([가-힣A-Za-z()·&·\s]{1,25}?)\s*\((\d{6})\)")
# 1차 진입가 (분할 매수 형식: "1차(50%): 240,000원")
_ENTRY1_RE = re.compile(r"1차[^\n]{0,30}?([\d,]{4,})\s*원")
# 구형 단일 진입가 ("진입가 240,000원" / "진입: 240,000원")
_ENTRY_OLD_RE = re.compile(r"진입[가격：: ]{0,5}([\d,]{4,})\s*원")
_STOP_RE   = re.compile(r"손절[가격：: ·]{0,5}([\d,]{4,})\s*원")
_TARGET_RE = re.compile(r"목표[가격：: ·]{0,5}([\d,]{4,})\s*원")


def _extract_price(pattern: re.Pattern, text: str) -> int | None:
    m = pattern.search(text)
    if m:
        try:
            return int(m.group(1).replace(",", ""))
        except ValueError:
            pass
    return None


def parse_recommendations(report_text: str) -> list[dict]:
    """CEO 브리핑 텍스트에서 추천 종목 파싱.
    분할 매수 형식(1차/2차/손절/목표)과 구형 단일 라인 형식을 모두 지원.
    """
    results: list[dict] = []
    seen_codes: set[str] = set()
    lines = report_text.split("\n")

    for i, line in enumerate(lines):
        m = _HEADER_RE.search(line)
        if not m:
            continue
        name = m.group(1).strip()
        code = m.group(2)
        if code in seen_codes:
            continue

        # 종목 헤더 이후 최대 10줄을 블록으로 추출
        block = "\n".join(lines[i: i + 10])

        # 1차 진입가 우선, 없으면 구형 진입가
        entry = _extract_price(_ENTRY1_RE, block) or _extract_price(_ENTRY_OLD_RE, block)
        stop  = _extract_price(_STOP_RE, block)
        target = _extract_price(_TARGET_RE, block)

        if entry and stop and target and entry > 0 and stop > 0 and target > 0:
            seen_codes.add(code)
            results.append({
                "name": name, "code": code,
                "entry_price": entry, "stop_price": stop,
                "target_price": target, "rationale": "",
            })

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
