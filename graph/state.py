from typing import TypedDict, Any


class InvestmentState(TypedDict):
    run_type: str
    timestamp: str
    date: str

    raw_market_data: dict[str, Any]
    data_freshness: dict[str, Any]    # check_data_freshness() 결과
    raw_kis_data: dict[str, Any]
    raw_news_data: dict[str, Any]
    us_hot_stocks: list[dict]        # 미국 거래량 급증·등락 상위 종목 + 한국 연관 매핑
    us_sector_data: dict[str, Any]   # 미국 섹터 ETF 등락률 데이터
    us_52w_highs: list[dict]         # 미국 52주 신고가 근접 종목
    bigfigure_news: list[dict]       # 글로벌 빅피겨 최신 뉴스
    dart_disclosures: list[dict]     # 오늘 주요 DART 공시 (브리핑 통합용)
    kr_index_realtime: dict[str, Any]  # 장중 KOSPI·KOSDAQ 실시간 현재 지수
    consensus_data: dict[str, Any]   # 종목별 컨센서스 목표주가 데이터
    weekly_strategy_summary: str     # 최신 주간 전략 요약 (CEO 컨텍스트 주입용)
    investment_thesis: str           # 현재 월간 투자관 요약 (CEO 최우선 컨텍스트)

    futures_report: str
    us_market_report: str
    us_impact_report: str            # 미국 섹터 → 한국 수혜 종목 분석
    korea_spot_report: str
    global_market_report: str
    news_report: str
    bigfigure_report: str            # 빅피겨 발언 분석
    dart_report: str                 # 오늘 DART 공시 요약
    macro_report: str                # 매크로 레짐 분석 (금리/크레딧/달러/VIX/구리/LIT)
    event_risk_report: str           # 경제 이벤트 캘린더 리스크 (FOMC/CPI/옵션만기 등)
    event_risk_level: str            # 높음 / 중간 / 낮음
    market_intelligence_report: str  # 글로벌 전문가 서사·강세론/약세론·컨센서스 변화
    sector_report: str
    issue_stocks_report: str   # 이슈종목 발굴 — 거래량/수급/미국연동 기반, 1~3주 대응전략
    midterm_stock_report: str  # 중장기 종목 추천 — 3~12개월 밸류에이션·섹터사이클 기반
    money_flow_report: str
    risk_report: str
    committee_report: str
    review_report: str
    portfolio_report: str  # 포트폴리오 매니저 분석 (보유 종목 행동 지시 + 워치리스트 트리거)
    ceo_report: str

    candidates: list[dict]
    sector_scores: list[dict]
    risks: list[str]
    risk_level: str           # 높음 / 중간 / 낮음
    market_direction: str

    errors: list[str]
    nav_recorded: dict   # 장마감 후 기록된 NAV 스냅샷 (optional — 없으면 {})
