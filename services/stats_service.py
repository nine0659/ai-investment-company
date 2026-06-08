"""
주간 적중률 통계 대시보드
매주 일요일 20:00 KST 실행
"""
import logging
import math
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from services.recommendation_service import get_recent_recommendations
from clients.openai_client import chat
from clients.telegram_client import send_message
from db.database import get_conn
from sqlalchemy import text

logger = logging.getLogger(__name__)
_KST = ZoneInfo("Asia/Seoul")


# ── 리스크 조정 수익률 지표 ──────────────────────────────────────

def _sharpe_ratio(returns: list[float], risk_free: float = 0.0) -> float:
    """연환산 샤프비율 = (평균수익률 - 무위험률) / 표준편차 × √252
    추천 단위 수익률(%)을 사용하므로 일일 단위로 간주해 연환산."""
    if len(returns) < 3:
        return 0.0
    n    = len(returns)
    mean = sum(returns) / n
    var  = sum((r - mean) ** 2 for r in returns) / (n - 1)
    std  = math.sqrt(var) if var > 0 else 0.0
    if std == 0:
        return 0.0
    return round((mean - risk_free) / std * math.sqrt(252), 2)


def _sortino_ratio(returns: list[float], risk_free: float = 0.0) -> float:
    """연환산 소르티노비율 = (평균수익률 - 무위험률) / 하방표준편차 × √252
    하방: 무위험률(0%) 미만 수익률만 사용."""
    if len(returns) < 3:
        return 0.0
    n        = len(returns)
    mean     = sum(returns) / n
    downside = [r for r in returns if r < risk_free]
    if not downside:
        return 0.0
    down_var = sum((r - risk_free) ** 2 for r in downside) / len(downside)
    down_std = math.sqrt(down_var) if down_var > 0 else 0.0
    if down_std == 0:
        return 0.0
    return round((mean - risk_free) / down_std * math.sqrt(252), 2)


def _max_drawdown(returns: list[float]) -> float:
    """최대 드로다운(MDD) = 누적 수익률 고점 대비 최대 낙폭(%)"""
    if not returns:
        return 0.0
    cumulative = 1.0
    peak       = 1.0
    mdd        = 0.0
    for r in returns:
        cumulative *= (1 + r / 100)
        if cumulative > peak:
            peak = cumulative
        dd = (peak - cumulative) / peak * 100
        if dd > mdd:
            mdd = dd
    return round(mdd, 2)


def _sector_stats(recs: list[dict]) -> dict[str, dict]:
    from collections import defaultdict
    sectors: dict[str, dict] = defaultdict(lambda: {"total": 0, "success": 0, "returns": []})
    for r in recs:
        code = r.get("code", "")
        # 종목코드로 간략 섹터 분류 (정교한 분류는 별도 테이블 필요)
        sector = _guess_sector(code)
        sectors[sector]["total"] += 1
        if r.get("result") == "성공":
            sectors[sector]["success"] += 1
        if r.get("return_pct") is not None:
            sectors[sector]["returns"].append(r["return_pct"])
    return dict(sectors)


def _guess_sector(code: str) -> str:
    semiconductor = {"005930", "000660", "042700", "000990", "240810"}
    battery       = {"373220", "006400", "003670", "247540", "051910"}
    biotech       = {"207940", "068270", "000100", "326030"}
    finance       = {"105560", "055550", "086790", "316140", "032830"}
    if code in semiconductor: return "반도체"
    if code in battery:       return "2차전지"
    if code in biotech:       return "바이오"
    if code in finance:       return "금융"
    return "기타"


def _get_latest_tracking(days: int) -> list[dict]:
    """각 rec_id의 최신 추적 스냅샷 반환 (최근 N일 추천 기준)."""
    cutoff = (datetime.now(_KST) - timedelta(days=days)).strftime("%Y-%m-%d")
    try:
        with get_conn() as conn:
            rows = conn.execute(text("""
                SELECT rt.rec_id, rt.code, rt.name, rt.rec_date,
                       rt.entry_price, rt.current_price, rt.return_pct,
                       rt.max_return, rt.min_return, rt.days_held, rt.status
                FROM recommendation_tracking rt
                INNER JOIN (
                    SELECT rec_id, MAX(date) AS max_date
                    FROM recommendation_tracking
                    GROUP BY rec_id
                ) latest ON rt.rec_id = latest.rec_id AND rt.date = latest.max_date
                WHERE rt.rec_date >= :cutoff
                ORDER BY rt.rec_date DESC
            """), {"cutoff": cutoff}).fetchall()
        return [
            {"rec_id": r[0], "code": r[1], "name": r[2], "rec_date": r[3],
             "entry_price": r[4], "current_price": r[5], "return_pct": r[6],
             "max_return": r[7], "min_return": r[8], "days_held": r[9], "status": r[10]}
            for r in rows
        ]
    except Exception as e:
        logger.warning("[통계] tracking 데이터 조회 실패: %s", e)
        return []


def generate_weekly_report(days: int = 7) -> str:
    now  = datetime.now(_KST)
    recs = get_recent_recommendations(days)

    if not recs:
        return f"📊 주간 적중률 리포트 ({now.strftime('%Y-%m-%d')})\n\n이번 주 추천 종목 데이터 없음"

    total    = len(recs)
    success  = sum(1 for r in recs if r.get("result") == "성공")
    fail     = sum(1 for r in recs if r.get("result") == "실패")
    normal   = total - success - fail
    returns  = [r["return_pct"] for r in recs if r.get("return_pct") is not None]
    avg_ret  = sum(returns) / len(returns) if returns else 0
    win_rate = success / total * 100 if total else 0

    # 리스크 조정 수익률 지표
    sharpe   = _sharpe_ratio(returns)
    sortino  = _sortino_ratio(returns)
    mdd      = _max_drawdown(returns)

    sector_data = _sector_stats(recs)
    sector_lines = []
    for sec, data in sorted(sector_data.items(), key=lambda x: -x[1]["success"]):
        sr  = data["success"] / data["total"] * 100 if data["total"] else 0
        avg = sum(data["returns"]) / len(data["returns"]) if data["returns"] else 0
        sector_lines.append(f"  {sec}: 적중률 {sr:.0f}% / 평균 {avg:+.1f}%")

    best = max(recs, key=lambda x: x.get("return_pct") or -99)
    worst = min(recs, key=lambda x: x.get("return_pct") or 99)

    rec_text = "\n".join(
        f"  {r['date']} {r['name']}({r['code']}): {r.get('return_pct', 0):+.1f}% [{r.get('result', '?')}]"
        for r in recs
    )

    # ── recommendation_tracking 데이터 병합 ──────────────────────────
    tracking = _get_latest_tracking(days)
    t_total   = len(tracking)
    t_target  = sum(1 for t in tracking if t["status"] == "target_hit")
    t_stop    = sum(1 for t in tracking if t["status"] == "stop_hit")
    t_active  = sum(1 for t in tracking if t["status"] == "tracking")
    t_expired = sum(1 for t in tracking if t["status"] == "expired")

    # max_return 기준 최고/최저 구간 (tracking 데이터)
    track_best  = max(tracking, key=lambda x: x.get("max_return") or -99) if tracking else None
    track_worst = min(tracking, key=lambda x: x.get("min_return") or 99)  if tracking else None

    tracking_section = ""
    if t_total:
        lines_t = [f"  목표달성: {t_target}건 | 손절: {t_stop}건 | 추적중: {t_active}건 | 만료: {t_expired}건"]
        if track_best and (track_best.get("max_return") or 0) > 0:
            lines_t.append(f"  최고구간: {track_best['name']} 최대 +{track_best['max_return']:.1f}%")
        if track_worst and (track_worst.get("min_return") or 0) < 0:
            lines_t.append(f"  최저구간: {track_worst['name']} 최저 {track_worst['min_return']:.1f}%")
        tracking_section = "\n━━ AI 추적 현황 ━━\n총 추적: {t_total}건\n".format(t_total=t_total) + "\n".join(lines_t) + "\n\n"

    # ── AI 추천 실행률 집계 ──────────────────────────────────────────
    execution_section = ""
    try:
        cutoff_exec = (datetime.now(_KST) - timedelta(days=days)).strftime("%Y-%m-%d")
        with get_conn() as conn:
            row_exec = conn.execute(text("""
                SELECT
                    COUNT(*) AS total_orders,
                    SUM(CASE WHEN rec_id IS NOT NULL THEN 1 ELSE 0 END) AS ai_orders,
                    SUM(CASE WHEN rec_id IS NOT NULL AND success=1 THEN 1 ELSE 0 END) AS ai_success
                FROM order_history
                WHERE created_at >= :cutoff
            """), {"cutoff": cutoff_exec}).fetchone()
        if row_exec:
            total_orders = row_exec[0] or 0
            ai_orders    = row_exec[1] or 0
            ai_success   = row_exec[2] or 0
            # AI 추천 대비 실행률 = 실제 실행된 AI 추천 / 총 AI 추천
            rec_count = total if total else 1
            exec_rate = ai_orders / rec_count * 100
            exec_section_lines = [
                f"\n━━ AI 추천 실행률 ━━",
                f"이번 주 AI 추천: {total}건  |  실제 실행: {ai_orders}건",
                f"🤖 AI 추천 대비 실행률: {exec_rate:.0f}%",
            ]
            if ai_success and ai_orders:
                exec_section_lines.append(f"  실행 성공률: {ai_success/ai_orders*100:.0f}% ({ai_success}/{ai_orders}건)")
            execution_section = "\n".join(exec_section_lines) + "\n"
    except Exception as _ee:
        logger.debug("[통계] 실행률 집계 실패: %s", _ee)

    gpt_prompt = f"""지난 {days}일간 투자 추천 성과를 분석하고 개선점을 제시하세요.

성과 데이터:
{rec_text}

총 {total}건 | 성공 {success}건 | 실패 {fail}건 | 보통 {normal}건
평균 수익률: {avg_ret:+.2f}%  적중률: {win_rate:.0f}%
샤프비율(연환산): {sharpe}  소르티노비율: {sortino}  MDD: -{mdd:.1f}%

AI 추적 현황 (recommendation_tracking):
총 {t_total}건 — 목표달성 {t_target} / 손절 {t_stop} / 추적중 {t_active} / 만료 {t_expired}

샤프비율 해석 기준: >1.0 우수 / 0.5~1.0 보통 / <0.5 개선 필요
MDD 해석: 낮을수록 손실 관리 우수

1. 이번 주 잘 맞은 조건은?
2. 틀린 조건은?
3. 샤프비율·MDD 관점에서 리스크 관리 평가
4. 목표달성/손절 비율로 본 리스크리워드 평가
5. 다음 주 반영할 개선점 (구체적으로)
"""
    analysis = chat("당신은 투자 성과 분석 전문가입니다.", gpt_prompt, max_tokens=900)

    report = (
        f"📊 *주간 적중률 리포트* ({now.strftime('%Y.%m.%d')} 기준)\n\n"
        f"━━ 이번 주 성과 ━━\n"
        f"총 추천: {total}건 | 성공: {success} / 실패: {fail} / 보통: {normal}\n"
        f"✅ 적중률: {win_rate:.0f}%  |  📈 평균 수익률: {avg_ret:+.2f}%\n\n"
        f"━━ 리스크 조정 지표 ━━\n"
        f"📐 샤프비율(연환산): {sharpe:+.2f}"
        + (" ✅우수" if sharpe >= 1.0 else " ⚠️개선필요" if sharpe < 0.5 else "") + "\n"
        f"📐 소르티노비율    : {sortino:+.2f}\n"
        f"📉 최대 드로다운   : -{mdd:.1f}%\n\n"
        f"🏆 최고: {best['name']} {best.get('return_pct', 0):+.1f}%\n"
        f"💔 최저: {worst['name']} {worst.get('return_pct', 0):+.1f}%\n\n"
        + tracking_section
        + execution_section
        + f"━━ 섹터별 적중률 ━━\n"
        + "\n".join(sector_lines) + "\n\n"
        + f"━━ AI 분석 & 개선점 ━━\n{analysis}"
    )
    return report


def send_weekly_report():
    logger.info("[통계] 주간 리포트 생성 시작")
    try:
        report = generate_weekly_report(7)
        send_message(report)
        logger.info("[통계] 주간 리포트 발송 완료")
    except Exception as e:
        logger.error("[통계] 주간 리포트 실패: %s", e)
