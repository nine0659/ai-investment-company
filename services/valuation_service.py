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
    price_data = kis.get_stock_price(stock_code)
    result.update(price_data)

    # ── KIS: 배당 ─────────────────────────────────────────────
    div_data = kis.get_dividend_info(stock_code)
    result.update(div_data)

    # ── DART: 최근 N년 재무제표 ───────────────────────────────
    history = dart_client.get_multi_year_financials(stock_code, years)
    result["financials"] = history

    # ── 최근 연도 기준 지표 계산 ──────────────────────────────
    if history:
        latest = history[0]
        equity     = latest.get("자본총계", 0)
        net_income = latest.get("당기순이익", 0)
        total_debt = latest.get("부채총계", 0)
        revenue    = latest.get("매출액", 0)
        op_income  = latest.get("영업이익", 0)

        # ROE (%)
        if equity > 0:
            result["roe"] = round(net_income / equity * 100, 2)

        # 부채비율 (%)
        if equity > 0:
            result["debt_ratio"] = round(total_debt / equity * 100, 2)

        # 영업이익률 (%)
        if revenue > 0:
            result["op_margin"] = round(op_income / revenue * 100, 2)

        # 매출 성장률 (전년 대비 %)
        if len(history) >= 2 and history[1].get("매출액", 0) > 0:
            prev_rev = history[1]["매출액"]
            result["revenue_growth"] = round((revenue - prev_rev) / prev_rev * 100, 2)

        # 억원 단위 변환
        result["revenue_억"] = revenue // 100_000_000
        result["op_income_억"] = op_income // 100_000_000
        result["net_income_억"] = net_income // 100_000_000
        result["equity_억"] = equity // 100_000_000

    return result


def format_for_prompt(stock_data: dict) -> str:
    """AI 프롬프트용 종목 데이터 포맷"""
    f = stock_data
    lines = [
        f"【{f.get('name', '')} ({f.get('code', '')})】",
        f"  현재가: {f.get('price', 'N/A'):,}원  시가총액: {f.get('market_cap_억', 'N/A'):,}억원",
        f"  PER: {f.get('per', 'N/A')}  PBR: {f.get('pbr', 'N/A')}  ROE: {f.get('roe', 'N/A')}%",
        f"  부채비율: {f.get('debt_ratio', 'N/A')}%  영업이익률: {f.get('op_margin', 'N/A')}%",
        f"  매출: {f.get('revenue_억', 'N/A')}억  영업이익: {f.get('op_income_억', 'N/A')}억  순이익: {f.get('net_income_억', 'N/A')}억",
        f"  매출성장률: {f.get('revenue_growth', 'N/A')}%  배당수익률: {f.get('dividend_yield', 'N/A')}%",
        f"  52주 고/저: {f.get('52w_high', 'N/A'):,} / {f.get('52w_low', 'N/A'):,}",
    ]
    return "\n".join(lines)
