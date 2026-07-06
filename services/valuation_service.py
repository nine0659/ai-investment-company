"""
밸류에이션 종합 분석 서비스
KIS(현재가·PER·PBR) + DART(재무제표) → 핵심 지표 산출
"""
import logging
from clients.kis_client import KISClient
from clients import dart_client

logger = logging.getLogger(__name__)


def get_stock_valuation(kis: KISClient, stock_code: str, stock_name: str,
                        years: int = 3) -> dict:
    """한 종목의 종합 밸류에이션 데이터 수집·계산"""
    result: dict = {"code": stock_code, "name": stock_name}

    # ── KIS: 현재가·PER·PBR ───────────────────────────────────
    try:
        price_data = kis.get_stock_price(stock_code)
        result.update(price_data)
    except Exception as e:
        logger.warning("KIS 가격 조회 실패 (%s): %s", stock_code, e)

    # ── KIS: 배당 ─────────────────────────────────────────────
    try:
        div_data = kis.get_dividend_info(stock_code)
        result.update(div_data)
    except Exception as e:
        logger.debug("KIS 배당 조회 실패 (%s): %s", stock_code, e)

    # ── DART: 최근 N년 재무제표 ───────────────────────────────
    history = dart_client.get_multi_year_financials(stock_code, years)
    result["financials"] = history

    # ── 지표 계산 ─────────────────────────────────────────────
    # history[0]는 당해 최신 분기 보고서일 수 있다. 분기 손익(3~9개월 누적)을
    # 직전 연간(12개월)과 그대로 비교하면 매출성장률이 전 종목 -60~-75%로
    # 찍히는 왜곡이 생기므로(2026-07-05 브리핑 오류), 손익 지표는 반드시
    # 연간 보고서끼리만 비교하고 분기 실적은 별도 필드로 분리한다.
    if history:
        annuals   = [h for h in history if h.get("period") == "연간"]
        quarterly = history[0] if history[0].get("period") != "연간" else None

        if annuals:
            latest = annuals[0]
            equity      = latest.get("자본총계", 0)
            net_income  = latest.get("당기순이익", 0)
            total_debt  = latest.get("부채총계", 0)
            revenue     = latest.get("매출액", 0)
            op_income   = latest.get("영업이익", 0)

            if equity > 0:
                result["roe"] = round(net_income / equity * 100, 2)
                result["debt_ratio"] = round(total_debt / equity * 100, 2)
            if revenue > 0:
                result["op_margin"] = round(op_income / revenue * 100, 2)
            if len(annuals) >= 2 and annuals[1].get("매출액", 0) > 0:
                prev_rev = annuals[1]["매출액"]
                result["revenue_growth"] = round((revenue - prev_rev) / prev_rev * 100, 2)

            result["revenue_억"]    = revenue    // 100_000_000
            result["op_income_억"]  = op_income  // 100_000_000
            result["net_income_억"] = net_income // 100_000_000
            result["equity_억"]     = equity     // 100_000_000
            result["latest_period"] = f"{latest.get('year', '')} {latest.get('period', '')}"

        if quarterly:
            q_rev = quarterly.get("매출액", 0)
            q_op  = quarterly.get("영업이익", 0)
            result["q_label"]        = f"{quarterly.get('year', '')} {quarterly.get('period', '')}"
            result["q_revenue_억"]   = q_rev // 100_000_000
            result["q_op_income_억"] = q_op  // 100_000_000
            if q_rev > 0:
                result["q_op_margin"] = round(q_op / q_rev * 100, 2)

    # ── 이상치 차단 — LLM 주입 전 마지막 관문 ─────────────────
    from services.data_guard import sanitize_stock_data
    result, guard_warnings = sanitize_stock_data(result)
    result["_data_warnings"] = guard_warnings

    return result


def _fmt(val, suffix="", default="N/A") -> str:
    if val is None or val == "" or val == 0 and suffix != "원":
        return default
    if isinstance(val, float):
        return f"{val:,.1f}{suffix}"
    if isinstance(val, int):
        return f"{val:,}{suffix}"
    return f"{val}{suffix}"


def format_for_prompt(stock_data: dict) -> str:
    """AI 프롬프트용 종목 데이터 포맷 (price 없어도 포함)"""
    f = stock_data
    price = f.get("price")
    cap   = f.get("market_cap_억")

    lines = [
        f"【{f.get('name', '')} ({f.get('code', '')})】",
        f"  현재가: {_fmt(price, '원')}  시가총액: {_fmt(cap, '억원')}",
        f"  PER: {_fmt(f.get('per'))}  PBR: {_fmt(f.get('pbr'))}  ROE(연간): {_fmt(f.get('roe'), '%')}",
        f"  부채비율: {_fmt(f.get('debt_ratio'), '%')}  영업이익률(연간): {_fmt(f.get('op_margin'), '%')}",
        f"  연간실적({f.get('latest_period', 'N/A')}): 매출 {_fmt(f.get('revenue_억'), '억')}"
        f"  영업이익 {_fmt(f.get('op_income_억'), '억')}  순이익 {_fmt(f.get('net_income_억'), '억')}",
        f"  매출성장률(연간, 전년 대비): {_fmt(f.get('revenue_growth'), '%')}"
        f"  배당수익률: {_fmt(f.get('dividend_yield'), '%')}",
    ]
    if f.get("q_label"):
        lines.append(
            f"  최근분기({f['q_label']}): 매출 {_fmt(f.get('q_revenue_억'), '억')}"
            f"  영업이익 {_fmt(f.get('q_op_income_억'), '억')}"
            f"  영업이익률 {_fmt(f.get('q_op_margin'), '%')}"
        )
    if price:
        lines.append(
            f"  52주 고/저: {_fmt(f.get('52w_high'), '원')} / {_fmt(f.get('52w_low'), '원')}"
        )
    return "\n".join(lines)
