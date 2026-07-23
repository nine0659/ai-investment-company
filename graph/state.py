from typing import TypedDict, Any, Annotated
import operator


def _last(a, b):
    """병렬 브랜치에서 동일 키에 여러 값이 들어올 때 마지막 값 사용 (last-writer-wins)."""
    return b


class InvestmentState(TypedDict):
    run_type:   Annotated[str, _last]
    timestamp:  Annotated[str, _last]
    date:       Annotated[str, _last]

    raw_market_data:        Annotated[dict[str, Any], _last]
    data_freshness:         Annotated[dict[str, Any], _last]
    raw_kis_data:           Annotated[dict[str, Any], _last]
    raw_news_data:          Annotated[dict[str, Any], _last]
    us_hot_stocks:          Annotated[list[dict],     _last]
    us_sector_data:         Annotated[dict[str, Any], _last]
    us_52w_highs:           Annotated[list[dict],     _last]
    bigfigure_news:         Annotated[list[dict],     _last]
    dart_disclosures:       Annotated[list[dict],     _last]
    kr_index_realtime:      Annotated[dict[str, Any], _last]
    consensus_data:         Annotated[dict[str, Any], _last]
    weekly_strategy_summary:Annotated[str, _last]
    investment_thesis:      Annotated[str, _last]

    futures_report:             Annotated[str, _last]
    us_market_report:           Annotated[str, _last]
    us_impact_report:           Annotated[str, _last]
    korea_spot_report:          Annotated[str, _last]
    global_market_report:       Annotated[str, _last]
    news_report:                Annotated[str, _last]
    bigfigure_report:           Annotated[str, _last]
    dart_report:                Annotated[str, _last]
    macro_report:               Annotated[str, _last]
    event_risk_report:          Annotated[str, _last]
    event_risk_level:           Annotated[str, _last]
    market_intelligence_report: Annotated[str, _last]
    sector_report:              Annotated[str, _last]
    issue_stocks_report:        Annotated[str, _last]
    midterm_stock_report:       Annotated[str, _last]
    money_flow_report:          Annotated[str, _last]
    risk_report:                Annotated[str, _last]
    committee_report:           Annotated[str, _last]
    review_report:              Annotated[str, _last]
    portfolio_report:           Annotated[str, _last]
    ceo_report:                 Annotated[str, _last]

    candidates:     Annotated[list[dict], _last]
    sector_scores:  Annotated[list[dict], _last]
    risks:          Annotated[list[str],  _last]
    risk_level:     Annotated[str, _last]
    market_direction: Annotated[str, _last]

    errors:         Annotated[list[str], operator.add]  # 병렬 브랜치 오류 자동 병합
    nav_recorded:   Annotated[dict,      _last]
    ceo_decisions:  Annotated[dict,      _last]
    deep_report_content: Annotated[str,  _last]
