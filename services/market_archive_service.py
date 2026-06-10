"""
services/market_archive_service.py
시장 데이터 + 인텔리전스 아카이브 저장/조회

목적: 매 브리핑마다 수집되는 시장 지표와 인텔리전스 요약을 DB에 축적하여
     과거 추세 분석·컨텍스트 제공에 활용한다.
"""
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from db.database import get_conn
from sqlalchemy import text

logger = logging.getLogger(__name__)
_KST = ZoneInfo("Asia/Seoul")


# ── 시장 스냅샷 ───────────────────────────────────────────────────

def save_market_snapshot(date: str, run_type: str, market_data: dict) -> None:
    """raw_market_data에서 핵심 지표를 추출해 DB에 저장."""
    def _v(key: str, field: str = "close") -> float | None:
        d = market_data.get(key)
        if not d:
            return None
        v = d.get(field) or d.get("close") or d.get("price")
        return float(v) if v else None

    def _chg(key: str) -> float | None:
        d = market_data.get(key)
        if not d:
            return None
        v = d.get("change_pct") or d.get("realtime_pct")
        return float(v) if v is not None else None

    try:
        with get_conn() as conn:
            conn.execute(
                text("""
                    INSERT INTO market_snapshots
                    (date, run_type, kospi, kospi_chg, kosdaq, kosdaq_chg,
                     usd_krw, vix, oil_wti, gold, us10y,
                     sp500_fut, sp500_chg, nasdaq_fut, nasdaq_chg)
                    VALUES
                    (:date, :run_type, :kospi, :kospi_chg, :kosdaq, :kosdaq_chg,
                     :usd_krw, :vix, :oil_wti, :gold, :us10y,
                     :sp500_fut, :sp500_chg, :nasdaq_fut, :nasdaq_chg)
                """),
                {
                    "date":        date,
                    "run_type":    run_type,
                    "kospi":       _v("kospi"),
                    "kospi_chg":   _chg("kospi"),
                    "kosdaq":      _v("kosdaq"),
                    "kosdaq_chg":  _chg("kosdaq"),
                    "usd_krw":     _v("usd_krw"),
                    "vix":         _v("vix"),
                    "oil_wti":     _v("oil_wti"),
                    "gold":        _v("gold"),
                    "us10y":       _v("us10y"),
                    "sp500_fut":   _v("sp500_futures"),
                    "sp500_chg":   _chg("sp500_futures"),
                    "nasdaq_fut":  _v("nasdaq_futures"),
                    "nasdaq_chg":  _chg("nasdaq_futures"),
                },
            )
        logger.info("[아카이브] 시장 스냅샷 저장: %s %s", date, run_type)
    except Exception as e:
        logger.warning("[아카이브] 시장 스냅샷 저장 실패: %s", e)


def save_intelligence_summary(
    date: str,
    run_type: str,
    source_type: str,
    summary: str,
    sentiment: str = "중립",
    key_themes: str = "",
) -> None:
    """인텔리전스 요약을 DB에 저장."""
    try:
        with get_conn() as conn:
            conn.execute(
                text("""
                    INSERT INTO intelligence_archive
                    (date, run_type, source_type, summary, sentiment, key_themes)
                    VALUES (:date, :run_type, :source_type, :summary, :sentiment, :key_themes)
                """),
                {
                    "date":        date,
                    "run_type":    run_type,
                    "source_type": source_type,
                    "summary":     summary[:2000],
                    "sentiment":   sentiment,
                    "key_themes":  key_themes[:500],
                },
            )
    except Exception as e:
        logger.warning("[아카이브] 인텔리전스 저장 실패: %s", e)


# ── 조회 함수 (CEO 브리핑 컨텍스트 주입용) ──────────────────────────

def get_market_trend_context(days: int = 7) -> str:
    """최근 N일 시장 추세를 텍스트로 반환 — CEO 프롬프트에 주입."""
    cutoff = (datetime.now(_KST) - timedelta(days=days)).strftime("%Y-%m-%d")
    try:
        with get_conn() as conn:
            rows = conn.execute(
                text("""
                    SELECT date, run_type, kospi, kospi_chg, kosdaq, kosdaq_chg,
                           usd_krw, vix, oil_wti, gold, us10y, sp500_chg, nasdaq_chg
                    FROM market_snapshots
                    WHERE date >= :cutoff AND run_type = 'close_market'
                    ORDER BY date DESC
                    LIMIT :n
                """),
                {"cutoff": cutoff, "n": days},
            ).fetchall()
    except Exception as e:
        logger.warning("[아카이브] 시장추세 조회 실패: %s", e)
        return ""

    if not rows:
        return ""

    lines = ["[최근 시장 추세 (장마감 기준)]"]
    for r in reversed(rows):
        date, _, kospi, kospi_chg, kosdaq, kosdaq_chg, usd_krw, vix, oil, gold, us10y, sp500_chg, nq_chg = r
        parts = [f"{date}"]
        if kospi:
            parts.append(f"KOSPI {kospi:,.0f}({kospi_chg:+.1f}%)")
        if kosdaq:
            parts.append(f"KOSDAQ {kosdaq:,.0f}({kosdaq_chg:+.1f}%)")
        if usd_krw:
            parts.append(f"USD/KRW {usd_krw:,.0f}")
        if vix:
            parts.append(f"VIX {vix:.1f}")
        if sp500_chg is not None:
            parts.append(f"S&P선물 {sp500_chg:+.1f}%")
        lines.append("  " + " | ".join(parts))
    return "\n".join(lines)


def get_intelligence_context(days: int = 5) -> str:
    """최근 N일 인텔리전스 요약을 텍스트로 반환 — CEO 프롬프트에 주입."""
    cutoff = (datetime.now(_KST) - timedelta(days=days)).strftime("%Y-%m-%d")
    try:
        with get_conn() as conn:
            rows = conn.execute(
                text("""
                    SELECT date, source_type, sentiment, key_themes, summary
                    FROM intelligence_archive
                    WHERE date >= :cutoff
                    ORDER BY date DESC, id DESC
                    LIMIT 15
                """),
                {"cutoff": cutoff},
            ).fetchall()
    except Exception as e:
        logger.warning("[아카이브] 인텔리전스 조회 실패: %s", e)
        return ""

    if not rows:
        return ""

    lines = ["[최근 인텔리전스 아카이브]"]
    for r in rows:
        date, src, sentiment, themes, summary = r
        lines.append(f"  [{date}][{src}][{sentiment}] {themes}")
        if summary:
            lines.append(f"    → {summary[:200]}")
    return "\n".join(lines)


def get_sector_rotation_history(days: int = 3) -> str:
    """최근 N일 섹터 순환매 이력 — sector_theme_team 컨텍스트 주입용."""
    cutoff = (datetime.now(_KST) - timedelta(days=days)).strftime("%Y-%m-%d")
    try:
        import json as _json
        with get_conn() as conn:
            rows = conn.execute(
                text("""
                    SELECT date, run_type, sector_scores
                    FROM reports
                    WHERE date >= :cutoff AND sector_scores IS NOT NULL
                      AND run_type IN ('pre_market', 'close_market')
                    ORDER BY date DESC, run_type DESC
                    LIMIT :n
                """),
                {"cutoff": cutoff, "n": days * 2},
            ).fetchall()
    except Exception as e:
        logger.warning("[아카이브] 섹터 이력 조회 실패: %s", e)
        return ""

    if not rows:
        return ""

    lines = [f"[최근 {days}일 섹터 순환매 이력 — 오늘 방향 비교용]"]
    for date, run_type, scores_json in rows:
        try:
            scores = _json.loads(scores_json) if isinstance(scores_json, str) else (scores_json or [])
            top3 = [f"{s['sector']}({s['score']})" for s in scores[:3] if s.get("sector")]
            label = "장전" if "pre" in run_type else "장마감"
            lines.append(f"  {date} {label}: {' > '.join(top3) or '데이터없음'}")
        except Exception:
            continue
    return "\n".join(lines)


def get_recommendation_performance_context() -> str:
    """최근 추천 종목 성과 요약 — CEO 프롬프트 자기학습용."""
    try:
        from services.recommendation_service import get_performance_stats
        stats = get_performance_stats()
        if not stats:
            return ""
        lines = ["[추천 종목 누적 성과]"]
        for run_type, s in stats.items():
            lines.append(
                f"  {run_type}: 총 {s['total']}건 | "
                f"수익 {s['win_rate']:.0f}% | 평균 {s['avg_return']:+.1f}%"
            )
        return "\n".join(lines)
    except Exception as e:
        logger.debug("[아카이브] 성과 조회 실패: %s", e)
        return ""
