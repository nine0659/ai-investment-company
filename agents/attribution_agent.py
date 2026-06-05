"""
agents/attribution_agent.py
주간 포트폴리오 성과 귀인 분석 (Attribution Analysis)

매주 일요일 19:00 KST 자동 실행 (scheduler.py).
수동 실행: python main.py --type attribution

분석 항목:
  1. 매크로 판단 정확도 — 우리의 시장 방향 예측이 맞았는가?
  2. 섹터 선택 기여 — 어떤 섹터가 수익/손실을 만들었는가?
  3. 종목 선택 기여 — 같은 섹터 내에서 종목 선택이 옳았는가?
  4. 타이밍 기여 — 진입/청산 타이밍이 적절했는가?
  5. 투자관 부합도 — 이번 주 추천이 현재 투자관와 얼마나 일치했는가?
"""
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from clients.openai_client import chat
from clients.telegram_client import send_message, send_error_alert
from services.recommendation_service import get_recent_recommendations, get_performance_stats
from services.portfolio_service import get_portfolio_summary, calculate_pnl
from clients.kis_client import KISClient

logger = logging.getLogger(__name__)
_TZ = ZoneInfo("Asia/Seoul")

_SYSTEM = """당신은 전문 포트폴리오 성과 분석가다.
제공된 데이터를 바탕으로 이번 주 투자 성과를 4가지 원인으로 분해하고,
다음 주 전략 개선을 위한 명확한 인사이트를 도출하라.

━━ [4가지 귀인 분석 의무] ━━

① 매크로 판단 정확도
  - 우리가 예측한 시장 방향(KOSPI 상승/하락)이 실제와 일치했는가?
  - 맞은 경우: 어떤 신호가 유효했는가?
  - 틀린 경우: 어떤 신호를 과대/과소평가했는가?
  - 매크로 판단 정확도 점수: X/10 (근거 포함)

② 섹터 선택 기여
  - 어느 섹터를 비중 확대했고 그 결과는?
  - 비중 확대 섹터의 성과 vs 시장 대비 초과/부진
  - 가장 잘된 섹터 선택과 가장 나쁜 섹터 선택 각 1개

③ 종목 선택 기여 (Alpha)
  - 같은 섹터 내에서 우리의 종목이 섹터 평균보다 좋았는가 나빴는가?
  - Alpha(초과수익) = 종목 수익률 - 해당 섹터 평균 수익률
  - 양의 Alpha 종목 vs 음의 Alpha 종목 분류

④ 타이밍 기여
  - 진입 타이밍이 좋았는가? (진입 후 바로 상승/하락 여부)
  - 청산 타이밍이 좋았는가? (청산 후 추가 상승 놓쳤는가)
  - 손절 실행 여부 및 적절성

⑤ 투자관 부합도
  - 이번 주 추천 종목들이 현재 투자관(섹터 방향성)와 얼마나 일치했는가?
  - 투자관에 반한 추천이 있었는가? 결과는?

━━ [출력 형식] ━━
한국어 텔레그램 텍스트. 수치 필수. 막연한 평가 금지.
"좋았다/나빴다" 대신 구체적 수치와 원인을 제시하라.

🏆 이번 주 성과 종합 점수: X/10
  매크로X | 섹터X | 종목X | 타이밍X | 투자관부합X

[각 항목 상세 분석]

🔧 다음 주 반드시 개선할 것 (1~3개, 구체적 행동 지침으로)"""


def _get_sector_performance() -> dict:
    """이번 주 주요 섹터 ETF 성과 수집."""
    try:
        import yfinance as yf
        sectors = {
            "반도체": "^SOX", "AI/기술": "QQQ", "방산": "ITA",
            "바이오": "XLV", "2차전지": "LIT", "금융": "XLF",
            "에너지": "XLE", "소재": "XLB",
        }
        result = {}
        for name, ticker in sectors.items():
            try:
                h = yf.Ticker(ticker).history(period="5d", interval="1d")
                if len(h) >= 2:
                    week_chg = (float(h.iloc[-1]["Close"]) - float(h.iloc[0]["Close"])) / float(h.iloc[0]["Close"]) * 100
                    result[name] = round(week_chg, 2)
            except Exception:
                pass
        return result
    except Exception:
        return {}


def _get_kospi_weekly() -> float:
    """이번 주 KOSPI 등락률."""
    try:
        import yfinance as yf
        h = yf.Ticker("^KS11").history(period="5d", interval="1d")
        if len(h) >= 2:
            return round((float(h.iloc[-1]["Close"]) - float(h.iloc[0]["Close"])) / float(h.iloc[0]["Close"]) * 100, 2)
    except Exception:
        pass
    return 0.0


def run_attribution():
    """주간 귀인 분석 실행 및 텔레그램 발송."""
    now = datetime.now(_TZ)
    week_str = now.strftime("%Y-%m-%d")
    logger.info("[귀인분석] 주간 성과 귀인 분석 시작: %s", week_str)

    # ── 데이터 수집 ────────────────────────────────────────────
    recs = get_recent_recommendations(days=7)
    perf = get_performance_stats(days=7)
    kospi_chg = _get_kospi_weekly()
    sector_perf = _get_sector_performance()

    # 포트폴리오 현황
    portfolio_text = "보유 포지션 없음"
    try:
        kis = KISClient()
        portfolio = calculate_pnl(kis)
        if portfolio:
            lines = [
                f"  {p['name']}({p['code']}): {p['pnl_pct']:+.2f}% "
                f"[{p.get('timeframe','단기')}] {p.get('status_flag','')}"
                for p in portfolio
            ]
            pnl_total = sum(p.get("pnl_pct", 0) for p in portfolio) / len(portfolio) if portfolio else 0
            portfolio_text = f"평균 손익: {pnl_total:+.2f}%\n" + "\n".join(lines)
    except Exception as e:
        logger.debug("[귀인분석] 포트폴리오 조회 실패: %s", e)

    # 투자관 (현재 활성 투자관의 방향과 이번 주 추천 비교용)
    thesis_summary = ""
    try:
        from services.thesis_service import get_thesis_ceo_summary
        thesis_summary = get_thesis_ceo_summary()
    except Exception:
        pass

    # 추천 종목 성과 상세
    rec_lines = []
    for r in recs:
        ret = r.get("return_pct")
        result = r.get("result", "?")
        name = r.get("name", "")
        code = r.get("code", "")
        rationale = (r.get("rationale") or "")[:80]
        ret_str = f"{ret:+.1f}%" if ret is not None else "집계중"
        rec_lines.append(f"  {name}({code}): {ret_str} [{result}] — {rationale}")

    context = f"""분석 기준: {now.strftime('%Y년 %m월 %d일')} (이번 주)

[이번 주 시장 성과]
KOSPI 주간 등락: {kospi_chg:+.2f}%

[섹터별 주간 성과]
{chr(10).join(f'  {k}: {v:+.2f}%' for k, v in sorted(sector_perf.items(), key=lambda x: -x[1])) or '없음'}

[이번 주 추천 종목 성과]
총 {perf.get('total', 0)}건 | 성공 {perf.get('win', 0)} | 실패 {perf.get('loss', 0)}
승률 {perf.get('win_rate', 0)}% | 평균수익률 {perf.get('avg_return', 0):+.2f}%
{chr(10).join(rec_lines) if rec_lines else '추천 데이터 없음'}

[현재 보유 포지션]
{portfolio_text}

[현재 투자관 (정합도 평가 기준)]
{thesis_summary or '투자관 미수립 (평가 불가)'}

위 데이터를 바탕으로 4가지 귀인 분석과 다음 주 개선 방향을 도출하라."""

    try:
        report = chat(_SYSTEM, context, max_tokens=2000)
    except Exception as e:
        logger.error("[귀인분석] LLM 실패: %s", e)
        send_error_alert(f"주간 귀인 분석 실패: {e}")
        return

    # DB 저장 (학습 루프용)
    try:
        _save_attribution(now.strftime("%Y-%m-%d"), report)
    except Exception as e:
        logger.warning("[귀인분석] DB 저장 실패: %s", e)

    # NAV 주간 현황 첨부
    nav_section = ""
    try:
        from services.nav_service import generate_nav_report
        nav_section = generate_nav_report(days=7)
    except Exception:
        pass

    header = (
        f"📊 *주간 성과 귀인 분석* ({now.strftime('%Y.%m.%d')} 기준)\n"
        f"매크로 판단·섹터 선택·종목 Alpha·타이밍·투자관 부합도\n\n"
    )
    full_msg = header + report
    if nav_section:
        full_msg += f"\n\n{nav_section}"
    send_message(full_msg)
    logger.info("[귀인분석] 완료")


def _save_attribution(week_end: str, report: str) -> None:
    """귀인 분석 결과를 DB에 저장 (투자관 학습 루프용)."""
    import re
    from db.database import get_conn
    from sqlalchemy import text

    # 종합 점수 파싱 (예: "종합 점수: 7/10" 또는 "7/10")
    def _parse_score(pattern: str) -> float:
        m = re.search(pattern, report)
        if m:
            try:
                return float(m.group(1))
            except Exception:
                pass
        return 0.0

    total   = _parse_score(r"종합\s*점수[^\d]*(\d+(?:\.\d+)?)")
    macro   = _parse_score(r"매크로[^\d]*(\d+(?:\.\d+)?)")
    sector  = _parse_score(r"섹터[^\d]*(\d+(?:\.\d+)?)")
    stock   = _parse_score(r"종목[^\d]*(\d+(?:\.\d+)?)")
    timing  = _parse_score(r"타이밍[^\d]*(\d+(?:\.\d+)?)")
    thesis_s= _parse_score(r"테제\s*정합[^\d]*(\d+(?:\.\d+)?)")

    # 핵심 교훈 파싱
    m = re.search(r"다음\s*주[^\n]*반드시[^\n]*개선[^\n]*(.{20,300})", report, re.DOTALL)
    key_learnings = m.group(1)[:400].strip() if m else report[-300:]

    with get_conn() as conn:
        conn.execute(
            text("""
                INSERT INTO attribution_log
                (week_end, macro_score, sector_score, stock_score, timing_score,
                 thesis_score, total_score, key_learnings, full_report)
                VALUES (:we, :ms, :ss, :sts, :ts, :ths, :tot, :kl, :fr)
            """),
            {"we": week_end, "ms": macro, "ss": sector, "sts": stock, "ts": timing,
             "ths": thesis_s, "tot": total, "kl": key_learnings, "fr": report},
        )
    logger.info("[귀인분석] DB 저장 완료: %s (종합 %.1f점)", week_end, total)


def get_recent_learnings(weeks: int = 2) -> str:
    """최근 N주 핵심 교훈 반환 (다음 투자관 수립 시 참조용)."""
    try:
        from datetime import timedelta
        from db.database import get_conn
        from sqlalchemy import text
        cutoff = (datetime.now(_TZ) - timedelta(weeks=weeks)).strftime("%Y-%m-%d")
        with get_conn() as conn:
            rows = conn.execute(
                text("""
                    SELECT week_end, total_score, key_learnings
                    FROM attribution_log WHERE week_end >= :cutoff
                    ORDER BY week_end DESC LIMIT :lim
                """),
                {"cutoff": cutoff, "lim": weeks},
            ).fetchall()
        if not rows:
            return ""
        parts = []
        for week_end, score, learnings in rows:
            parts.append(f"[{week_end} 주간 귀인 — 종합 {score:.1f}점]\n{learnings}")
        return "\n\n".join(parts)
    except Exception as e:
        logger.debug("[귀인분석] 교훈 조회 실패: %s", e)
        return ""


def run(state: dict = None) -> dict:
    run_attribution()
    return state or {}
