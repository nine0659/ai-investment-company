"""
주간 적중률 통계 대시보드
매주 일요일 20:00 KST 실행
"""
import logging
import math
from datetime import datetime
from zoneinfo import ZoneInfo

from services.recommendation_service import get_recent_recommendations
from clients.openai_client import chat
from clients.telegram_client import send_message

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

    gpt_prompt = f"""지난 {days}일간 투자 추천 성과를 분석하고 개선점을 제시하세요.

성과 데이터:
{rec_text}

총 {total}건 | 성공 {success}건 | 실패 {fail}건 | 보통 {normal}건
평균 수익률: {avg_ret:+.2f}%  적중률: {win_rate:.0f}%
샤프비율(연환산): {sharpe}  소르티노비율: {sortino}  MDD: -{mdd:.1f}%

샤프비율 해석 기준: >1.0 우수 / 0.5~1.0 보통 / <0.5 개선 필요
MDD 해석: 낮을수록 손실 관리 우수

1. 이번 주 잘 맞은 조건은?
2. 틀린 조건은?
3. 샤프비율·MDD 관점에서 리스크 관리 평가
4. 다음 주 반영할 개선점 (구체적으로)
"""
    analysis = chat("당신은 투자 성과 분석 전문가입니다.", gpt_prompt, max_tokens=800)

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
        f"━━ 섹터별 적중률 ━━\n"
        + "\n".join(sector_lines) + "\n\n"
        f"━━ AI 분석 & 개선점 ━━\n{analysis}"
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
