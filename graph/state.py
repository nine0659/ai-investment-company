from typing import TypedDict, Any


class InvestmentState(TypedDict):
    run_type: str
    timestamp: str
    date: str

    raw_market_data: dict[str, Any]
    raw_kis_data: dict[str, Any]
    raw_news_data: dict[str, Any]

    futures_report: str
    us_market_report: str
    korea_spot_report: str
    global_market_report: str
    news_report: str
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
