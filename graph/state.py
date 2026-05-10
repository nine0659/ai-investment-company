from typing import TypedDict, Any


class InvestmentState(TypedDict):
    run_type: str
    timestamp: str
    date: str

    raw_market_data: dict[str, Any]
    raw_kis_data: dict[str, Any]
    raw_news_data: dict[str, Any]
    us_hot_stocks: list[dict]        # 미국 거래량 급증·등락 상위 종목 + 한국 연관 매핑
    us_sector_data: dict[str, Any]   # 미국 섹터 ETF 등락률 데이터
    us_52w_highs: list[dict]         # 미국 52주 신고가 근접 종목
    bigfigure_news: list[dict]       # 글로벌 빅피겨 최신 뉴스

    futures_report: str
    us_market_report: str
    us_impact_report: str            # 미국 섹터 → 한국 수혜 종목 분석
    korea_spot_report: str
    global_market_report: str
    news_report: str
    bigfigure_report: str            # 빅피겨 발언 분석
    sector_report: str
    money_flow_report: str
    risk_report: str
    committee_report: str
    review_report: str
    ceo_report: str

    candidates: list[dict]
    sector_scores: list[dict]
    risks: list[str]
    market_direction: str

    errors: list[str]
