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

# "X:Y" 손익비 문자열(예: "3.5:1") 파싱 — ceo_decisions.new_positions의 risk_reward 필드
_RR_RE = re.compile(r"([\d.]+)\s*[:：]\s*([\d.]+)")
_CIO_DEFAULT_STOP_PCT = 15.0   # CEO 헌장(agents/ceo_agent.py)의 "재검토 의무 발동" 기준
_CIO_DEFAULT_RR_RATIO = 2.5    # risk_reward 파싱 실패 시 보수적 기본값(헌장 "3:1 미만 보류"보다 낮게)
# 아래 recs_from_weekly_picks 전용 _DEFAULT_STOP_PCT(10.0)와 이름이 겹치면 모듈 로드 시
# 나중 정의가 앞 정의를 덮어써 조용히 잘못된 값이 쓰인다 — 반드시 접두사로 구분할 것.


def recs_from_cio_decisions(
    decisions: dict,
    price_fn,  # Callable[[str], int] — 종목코드 → 현재가(원), 실패 시 0
) -> list[dict]:
    """ceo_decisions.new_positions(장전/마감 CIO 판단) → stock_recommendations 레코드.

    2026-09-03 재설계: 과거 버전은 브리핑 텍스트의 "목표 +X% / 손절 -Y%" 문구를
    정규식으로 긁어왔으나, 2026-06-19 브리핑 포맷 개편(9줄 압축형) 이후 그 문구 자체가
    리포트에 나타나지 않아 사실상 죽은 코드였다. 텍스트 파싱 대신 코드로 직접 계산한다
    (헌장 원칙 2 "계산은 코드가, LLM은 서술만"에 더 충실한 방식):

    - 진입가: 항상 price_fn(code)의 실데이터. 실패(0/None)하면 그 포지션은 폐기
      (recs_from_weekly_picks의 "환각 의심 시 폐기" 원칙과 동일 — 텍스트 가격을 쓰지 않는다)
    - 손절가: CEO 헌장에 이미 명문화된 "-15% 이상 손실 시 보유 근거 재검토 의무" 규칙을
      기본 손절 기준으로 사용 → entry * 0.85
    - 목표가: new_positions의 risk_reward 문자열("X:Y")을 손절폭에 곱해 산출.
      파싱 실패 시 보수적 기본 비율(_DEFAULT_RR_RATIO) 사용.
    """
    results: list[dict] = []
    seen: set[str] = set()

    for pos in (decisions or {}).get("new_positions", []) or []:
        code = pos.get("code", "")
        name = pos.get("name", "")
        if not code or code in seen:
            continue

        entry = price_fn(code) if price_fn else 0
        if not entry:
            logger.warning("[CIO추천파싱] %s(%s) 실데이터 가격 조회 실패 — 폐기", name, code)
            continue

        stop = entry * (1 - _CIO_DEFAULT_STOP_PCT / 100)

        ratio = _CIO_DEFAULT_RR_RATIO
        m = _RR_RE.search(pos.get("risk_reward", "") or "")
        if m:
            try:
                up, down = float(m.group(1)), float(m.group(2))
                if down > 0:
                    ratio = up / down
            except (ValueError, ZeroDivisionError):
                pass

        target = entry + (entry - stop) * ratio

        seen.add(code)
        results.append({
            "name":         name,
            "code":         code,
            "entry_price":  int(entry),
            "stop_price":   int(stop),
            "target_price": int(target),
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

# 같은 줄/근처 블록에서 손절 기준을 찾는 패턴 — "손절 -10%" 또는 "손절가 280,000원"
_STOP_PCT_RE   = re.compile(r"손절\s*[:：]?\s*-?([\d.]+)\s*%")
_STOP_PRICE_RE = re.compile(r"손절가\s*[:：]?\s*([\d,]+)\s*원")
_DEFAULT_STOP_PCT = 10.0  # 브리핑에 손절 기준이 없을 때 적용하는 기본 하락폭


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

    손절가: 브리핑 텍스트에 "손절 -N%" 또는 "손절가 N원"이 있으면 그 기준을
    실데이터 진입가에 적용해 계산한다. 텍스트에 손절 기준이 전혀 없으면
    기본값(_DEFAULT_STOP_PCT)을 적용한다 — 0으로 저장하지 않는다. stop_price가
    0이면 daily_tracker의 손절 판정(`if stop_price and price <= stop_price`)이
    0을 falsy로 취급해 손절 알림이 영구히 발동하지 않는 버그가 있었다(2026-08).
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

        # 손절가: 텍스트에 명시된 기준 우선, 없으면 기본 하락폭 적용 (절대 0 아님)
        stop_price = None
        pct_m = _STOP_PCT_RE.search(line_block)
        if pct_m:
            try:
                stop_price = actual_price * (1 - float(pct_m.group(1)) / 100)
            except ValueError:
                stop_price = None
        if stop_price is None:
            price_m = _STOP_PRICE_RE.search(line_block)
            if price_m:
                try:
                    stop_price = float(price_m.group(1).replace(",", ""))
                except ValueError:
                    stop_price = None
        if stop_price is None or not (0 < stop_price < actual_price):
            stop_price = actual_price * (1 - _DEFAULT_STOP_PCT / 100)

        seen.add(code)
        results.append({
            "name":         name,
            "code":         code,
            "entry_price":  int(actual_price),
            "stop_price":   int(stop_price),
            "target_price": int(target),
            "rationale":    rationale,
        })

    return results


def has_open_recommendation(code: str, days: int = 45) -> bool:
    """최근 N일 내 같은 종목의 미결(추적 가능) 추천이 있는지 — 중복 추적 방지.

    days 기본값은 recommendation_tracker_service._get_active_recommendations()의
    45일 활성 풀 창과 맞춘 것 — 추적기의 만료 기준(30 영업일 ≈ 캘린더 42일)보다
    이 창이 짧으면, 아직 만료되지 않은 추천인데도 dedup을 통과해 같은 종목이
    중복 추적 행으로 쌓일 수 있다(2026-08 발견).
    """
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
