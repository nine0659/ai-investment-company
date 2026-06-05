import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from langgraph.graph import StateGraph, END

from graph.state import InvestmentState
from config.settings import TZ, RUN_TYPE_PRE, RUN_TYPE_CLOSE

import agents.futures_market_team    as futures_market_team
import agents.us_market_team         as us_market_team
import agents.us_impact_agent        as us_impact_agent
import agents.korea_spot_market_team as korea_spot_market_team
import agents.global_market_team     as global_market_team
import agents.news_analysis_team     as news_analysis_team
import agents.bigfigure_agent        as bigfigure_agent
import agents.sector_theme_team      as sector_theme_team
import agents.money_flow_team        as money_flow_team
import agents.macro_team                  as macro_team
import agents.event_risk_team             as event_risk_team
import agents.market_intelligence_team    as market_intelligence_team
import agents.risk_management_team        as risk_management_team
import agents.issue_stock_agent          as issue_stock_agent
import agents.review_feedback_team      as review_feedback_team
import agents.investment_committee      as investment_committee
import agents.portfolio_manager_agent   as portfolio_manager_agent
import agents.midterm_stock_agent       as midterm_stock_agent
import agents.ceo_agent                 as ceo_agent

from clients.kis_client          import KISClient
from clients.market_data_client  import fetch_global_market_data, fetch_kr_index_realtime, check_data_freshness
from clients.news_client         import fetch_all_news
from clients.telegram_client     import send_message, send_error_alert
from clients.us_stock_client     import fetch_us_top_movers
from clients.us_market_client    import fetch_us_sectors, fetch_us_52w_highs
from clients.bigfigure_client    import fetch_bigfigure_news
from services.report_service     import save_report, format_report_for_db

logger = logging.getLogger(__name__)

kis = KISClient()


# ── 데이터 수집 노드 ────────────────────────────────────────────

def collect_raw_data(state: InvestmentState) -> InvestmentState:
    logger.info("[데이터수집] 시작")

    try:
        state["raw_market_data"] = fetch_global_market_data()
        freshness = check_data_freshness(state["raw_market_data"])
        state["data_freshness"] = freshness
        if freshness["warning"]:
            logger.warning("[데이터수집] %s", freshness["warning"])
            state["errors"].append(f"data_stale: {freshness['warning']}")
        else:
            logger.info("[데이터수집] 글로벌 시장 완료 — 데이터 기준: %s", freshness["label"])
    except Exception as e:
        logger.error("[데이터수집] 글로벌 실패: %s", e)
        state["raw_market_data"] = {}
        state["data_freshness"] = {}
        state["errors"].append(f"collect_global: {e}")

    try:
        state["us_hot_stocks"] = fetch_us_top_movers(n=5)
        logger.info("[데이터수집] 미국 상위 종목 완료: %d개", len(state["us_hot_stocks"]))
    except Exception as e:
        logger.error("[데이터수집] 미국 상위 종목 실패: %s", e)
        state["us_hot_stocks"] = []
        state["errors"].append(f"collect_us_hot: {e}")

    try:
        state["us_sector_data"] = fetch_us_sectors()
        logger.info("[데이터수집] 미국 섹터 ETF 완료: %d개", len(state["us_sector_data"]))
    except Exception as e:
        logger.error("[데이터수집] 미국 섹터 실패: %s", e)
        state["us_sector_data"] = {}
        state["errors"].append(f"collect_us_sectors: {e}")

    try:
        state["us_52w_highs"] = fetch_us_52w_highs()
        logger.info("[데이터수집] 미국 52주 신고가 완료: %d개", len(state["us_52w_highs"]))
    except Exception as e:
        logger.error("[데이터수집] 미국 52w 실패: %s", e)
        state["us_52w_highs"] = []
        state["errors"].append(f"collect_us_52w: {e}")

    try:
        state["bigfigure_news"] = fetch_bigfigure_news(max_per_figure=3)
        logger.info("[데이터수집] 빅피겨 뉴스 완료: %d명", len(state["bigfigure_news"]))
    except Exception as e:
        logger.error("[데이터수집] 빅피겨 뉴스 실패: %s", e)
        state["bigfigure_news"] = []
        state["errors"].append(f"collect_bigfigure: {e}")

    try:
        kis_data: dict = {}
        for market, code in [("kospi", "J"), ("kosdaq", "Q")]:
            for fn, suffix in [
                (kis.get_volume_rank,       "volume_rank"),
                (kis.get_amount_rank,       "amount_rank"),
                (kis.get_foreign_buy_rank,  "foreign_rank"),
                (kis.get_institution_buy_rank, "institution_rank"),
            ]:
                try:
                    kis_data[f"{market}_{suffix}"] = fn(code)
                except Exception as e:
                    logger.warning("[데이터수집] KIS %s_%s 실패: %s", market, suffix, e)
            try:
                kis_data[f"{market}_rise_rank"] = kis.get_fluctuation_rank(code, rise=True)
            except Exception as e:
                logger.warning("[데이터수집] KIS %s_rise_rank 실패: %s", market, e)
        state["raw_kis_data"] = kis_data
        logger.info("[데이터수집] KIS 완료")
    except Exception as e:
        logger.error("[데이터수집] KIS 전체 실패: %s", e)
        state["raw_kis_data"] = {}
        state["errors"].append(f"collect_kis: {e}")

    try:
        # 시황 데이터를 함께 전달 → LLM이 동적 검색어 생성에 활용
        state["raw_news_data"] = fetch_all_news(
            max_per_category=8,
            market_data=state.get("raw_market_data", {}),
        )
        logger.info("[데이터수집] 뉴스 완료 (동적 검색어 포함)")
    except Exception as e:
        logger.error("[데이터수집] 뉴스 실패: %s", e)
        state["raw_news_data"] = {}
        state["errors"].append(f"collect_news: {e}")

    try:
        from agents.dart_alert_agent import fetch_for_briefing
        state["dart_disclosures"] = fetch_for_briefing()
        logger.info("[데이터수집] DART 공시 완료: %d건", len(state["dart_disclosures"]))
    except Exception as e:
        logger.warning("[데이터수집] DART 공시 실패: %s", e)
        state["dart_disclosures"] = []

    try:
        state["kr_index_realtime"] = fetch_kr_index_realtime()
        logger.info("[데이터수집] 한국 지수 실시간 완료: %s", list(state["kr_index_realtime"].keys()))
    except Exception as e:
        logger.warning("[데이터수집] 한국 지수 실시간 실패: %s", e)
        state["kr_index_realtime"] = {}

    # ── 월간 투자관 로드 (CEO 최우선 컨텍스트 — 모든 판단의 헌법) ──────
    try:
        from services.thesis_service import get_thesis_ceo_summary
        state["investment_thesis"] = get_thesis_ceo_summary()
        if state["investment_thesis"]:
            logger.info("[데이터수집] 투자관 로드 완료")
        else:
            logger.debug("[데이터수집] 투자관 없음 (아직 생성 전)")
    except Exception as e:
        logger.debug("[데이터수집] 투자관 로드 실패 (무시): %s", e)
        state["investment_thesis"] = ""

    # ── 최신 주간 전략 요약 로드 (CEO 컨텍스트 주입용) ──────────────────
    try:
        from services.strategy_service import get_latest_strategy_summary
        state["weekly_strategy_summary"] = get_latest_strategy_summary(max_days=7)
        if state["weekly_strategy_summary"]:
            logger.info("[데이터수집] 주간 전략 요약 로드 완료")
        else:
            logger.debug("[데이터수집] 주간 전략 없음 (7일 내 실행 기록 없음)")
    except Exception as e:
        logger.debug("[데이터수집] 주간 전략 로드 실패 (무시): %s", e)
        state["weekly_strategy_summary"] = ""

    # ── 애널리스트 컨센서스 목표주가 수집 ─────────────────────────────
    try:
        from clients.consensus_client import fetch_consensus_batch
        from agents.ceo_agent import _BLUECHIP_ALWAYS_FETCH

        bluechip_codes = [s["code"] for s in _BLUECHIP_ALWAYS_FETCH]
        name_map = {s["code"]: s["name"] for s in _BLUECHIP_ALWAYS_FETCH}
        market_map = {s["code"]: s["market"] for s in _BLUECHIP_ALWAYS_FETCH}

        # 배치 수집 (rate limit 준수)
        consensus_raw = fetch_consensus_batch(bluechip_codes, delay=0.3, market_map=market_map)

        # raw 컨센서스 + name_map 저장 (현재가와 조합은 ceo_agent에서 수행)
        state["consensus_data"] = {
            "_raw": consensus_raw,
            "_name_map": name_map,
        }
        logger.info("[데이터수집] 컨센서스 목표주가 완료: %d종목", len(consensus_raw))
    except Exception as e:
        logger.warning("[데이터수집] 컨센서스 수집 실패 (무시): %s", e)
        state["consensus_data"] = {}

    return state


# ── 에이전트 노드 ───────────────────────────────────────────────

def node_futures(state):       return futures_market_team.run(state)
def node_us(state):            return us_market_team.run(state)
def node_us_impact(state):     return us_impact_agent.run(state)
def node_korea(state):         return korea_spot_market_team.run(state)
def node_global(state):        return global_market_team.run(state)
def node_news(state):          return news_analysis_team.run(state)
def node_bigfigure(state):     return bigfigure_agent.run(state)
def node_sector(state):        return sector_theme_team.run(state)
def node_issue_stocks(state):  return issue_stock_agent.run(state)
def node_money_flow(state):    return money_flow_team.run(state)
def node_macro(state):         return macro_team.run(state)
def node_event_risk(state):    return event_risk_team.run(state)
def node_intelligence(state):  return market_intelligence_team.run(state)
def node_risk(state):          return risk_management_team.run(state)
def node_committee(state):          return investment_committee.run(state)
def node_portfolio_manager(state):  return portfolio_manager_agent.run(state)
def node_midterm_stocks(state):     return midterm_stock_agent.run(state)
def node_ceo(state):                return ceo_agent.run(state)

def node_review(state):
    if state.get("run_type") == RUN_TYPE_CLOSE:
        return review_feedback_team.run(state)
    return state


# ── 저장 / 발송 노드 ────────────────────────────────────────────

def node_save_report(state: InvestmentState) -> InvestmentState:
    try:
        save_report(
            date=state["date"],
            run_type=state["run_type"],
            ceo_report=state.get("ceo_report", ""),
            candidates=state.get("candidates", []),
            sector_scores=state.get("sector_scores", []),
            market_direction=state.get("market_direction", ""),
        )
        logger.info("[리포트저장] 완료")
    except Exception as e:
        logger.error("[리포트저장] 실패: %s", e)
        state["errors"].append(f"save_report: {e}")

    # ── CEO 추천 종목 저장 (장전/장마감 브리핑에서 파싱) ──────────
    ceo_report = state.get("ceo_report", "")
    if ceo_report and state.get("run_type") in (RUN_TYPE_PRE, RUN_TYPE_CLOSE):
        try:
            from services.recommendation_service import parse_recommendations, save_recommendations
            recs = parse_recommendations(ceo_report)
            if recs:
                save_recommendations(state["date"], recs)
                logger.info("[추천저장] %d건 저장", len(recs))
        except Exception as e:
            logger.debug("[추천저장] 실패: %s", e)

    # ── 시장 방향 예측 저장 ────────────────────────────────────
    if ceo_report:
        try:
            from services.market_prediction_service import save_prediction
            save_prediction(state["date"], state["run_type"], ceo_report)
        except Exception as e:
            logger.debug("[예측저장] 실패: %s", e)

    # ── 시장 스냅샷 아카이브 저장 ───────────────────────────────
    try:
        from services.market_archive_service import save_market_snapshot, save_intelligence_summary
        save_market_snapshot(
            date=state["date"],
            run_type=state["run_type"],
            market_data=state.get("raw_market_data", {}),
        )
        # 인텔리전스 요약 저장 (market_intelligence_report에서 추출)
        intel_report = state.get("market_intelligence_report", "")
        if intel_report:
            # 감성 키워드 간단 추출
            sentiment = "강세" if "강세" in intel_report else ("약세" if "약세" in intel_report else "중립")
            # 테마 키워드 (AI/반도체/금리/환율 등)
            themes = ",".join(
                kw for kw in ["AI", "반도체", "금리", "환율", "중국", "미국", "수급", "실적"]
                if kw in intel_report
            )
            save_intelligence_summary(
                date=state["date"],
                run_type=state["run_type"],
                source_type="market_intelligence",
                summary=intel_report[:800],
                sentiment=sentiment,
                key_themes=themes,
            )
        logger.info("[아카이브] 저장 완료")
    except Exception as e:
        logger.warning("[아카이브] 저장 실패: %s", e)

    return state


def node_record_nav(state: InvestmentState) -> InvestmentState:
    """장마감 브리핑 후 포트폴리오 NAV 기록 (자산 성장 추적)."""
    if state.get("run_type") != RUN_TYPE_CLOSE:
        return state
    try:
        from services.nav_service import record_nav
        nav = record_nav()
        if nav:
            state["nav_recorded"] = nav
            logger.info("[NAV기록] 완료: 총손익 %+.2f%% / Alpha %+.2f%%",
                        nav.get("total_pnl_pct", 0), nav.get("alpha_ytd", 0))
    except Exception as e:
        logger.debug("[NAV기록] 실패 (무시): %s", e)
    return state


def node_send_telegram(state: InvestmentState) -> InvestmentState:
    report = state.get("ceo_report", "")
    if not report:
        logger.warning("[텔레그램] 발송할 리포트 없음")
        return state
    try:
        send_message(report)
        logger.info("[텔레그램] 발송 완료")
        errors = state.get("errors", [])
        if errors:
            # DB 연결 오류(Supabase/psycopg2)는 텔레그램 발송 제외 — 로그에만 기록
            _SKIP_PATTERNS = ("psycopg2", "OperationalError", "supabase", "connection to server")
            filtered = [e for e in errors if not any(p in e for p in _SKIP_PATTERNS)]
            if filtered:
                send_message("⚠️ 일부 데이터 수집 실패:\n" + "\n".join(f"- {e}" for e in filtered[:5]))
            if len(errors) != len(filtered):
                logger.warning("[텔레그램] DB 연결 오류 %d건 — 텔레그램 발송 생략 (로그 확인)", len(errors) - len(filtered))
    except Exception as e:
        logger.error("[텔레그램] 발송 실패: %s", e)
    return state


# ── 그래프 빌드 ────────────────────────────────────────────────

def build_graph() -> StateGraph:
    g = StateGraph(InvestmentState)

    nodes = [
        ("collect_raw_data",       collect_raw_data),
        ("futures_market_team",    node_futures),
        ("us_market_team",         node_us),
        ("us_impact_agent",        node_us_impact),       # 미국 섹터 → 한국 종목 매핑
        ("korea_spot_market_team", node_korea),
        ("global_market_team",     node_global),
        ("news_analysis_team",     node_news),
        ("bigfigure_agent",        node_bigfigure),       # 빅피겨 발언 분석
        ("sector_theme_team",      node_sector),
        ("issue_stock_agent",      node_issue_stocks),    # 거래량·수급·미국연동 이슈종목 발굴
        ("money_flow_team",        node_money_flow),
        ("macro_team",                node_macro),            # 매크로 레짐 분석 (금리/크레딧/달러/구리)
        ("event_risk_team",         node_event_risk),       # 경제 이벤트 캘린더 리스크
        ("market_intelligence_team",node_intelligence),     # 글로벌 전문가 서사·컨센서스 (해석 레이어)
        ("risk_management_team",    node_risk),
        ("review_feedback_team",   node_review),
        ("investment_committee",   node_committee),
        ("portfolio_manager_agent", node_portfolio_manager),  # 실제 포트폴리오 + 워치리스트 분석
        ("midterm_stock_agent",    node_midterm_stocks),     # 중장기(3~12개월) 종목 추천
        ("ceo_agent",              node_ceo),
        ("save_report",            node_save_report),
        ("record_nav",             node_record_nav),
        ("send_telegram",          node_send_telegram),
    ]
    for name, fn in nodes:
        g.add_node(name, fn)

    g.set_entry_point("collect_raw_data")
    for i in range(len(nodes) - 1):
        g.add_edge(nodes[i][0], nodes[i + 1][0])
    g.add_edge("send_telegram", END)

    return g.compile()


# ── 실행 진입점 ────────────────────────────────────────────────

def run_pipeline(run_type: str) -> InvestmentState:
    now = datetime.now(TZ)

    # KRX 거래일 체크 — 공휴일·선거일 등 비거래일에는 파이프라인 실행 자체를 차단
    try:
        from utils.market_calendar import is_krx_trading_day, get_holiday_name
        if not is_krx_trading_day(now.date()):
            holiday = get_holiday_name(now.date())
            label = f" ({holiday})" if holiday else ""
            logger.info(
                "[파이프라인] 오늘은 KRX 비거래일%s — %s 실행 차단 (텔레그램 발송 없음)",
                label, run_type,
            )
            return {}
    except Exception as e:
        logger.debug("[파이프라인] 공휴일 체크 실패 (무시): %s", e)

    # 중복 실행 방지: 같은 날 같은 run_type이 이미 DB에 저장됐으면 스킵
    try:
        from services.report_service import already_ran_today
        if already_ran_today(now.strftime("%Y-%m-%d"), run_type):
            logger.warning(
                "[중복방지] %s/%s 오늘 이미 실행 완료 — 중복 발송 방지를 위해 스킵",
                run_type, now.strftime("%Y-%m-%d"),
            )
            return {}
    except Exception as e:
        logger.debug("[중복방지] DB 체크 실패 (무시): %s", e)

    initial: InvestmentState = {
        "run_type": run_type,
        "timestamp": now.isoformat(),
        "date": now.strftime("%Y-%m-%d"),
        "raw_market_data": {},
        "data_freshness": {},
        "raw_kis_data": {},
        "raw_news_data": {},
        "us_hot_stocks": [],
        "us_sector_data": {},
        "us_52w_highs": [],
        "bigfigure_news": [],
        "dart_disclosures": [],
        "kr_index_realtime": {},
        "consensus_data": {},
        "weekly_strategy_summary": "",
        "investment_thesis": "",
        "futures_report": "",
        "us_market_report": "",
        "us_impact_report": "",
        "korea_spot_report": "",
        "global_market_report": "",
        "news_report": "",
        "bigfigure_report": "",
        "dart_report": "",
        "macro_report": "",
        "event_risk_report": "",
        "event_risk_level": "중간",
        "market_intelligence_report": "",
        "sector_report": "",
        "issue_stocks_report": "",
        "midterm_stock_report": "",
        "money_flow_report": "",
        "risk_report": "",
        "committee_report": "",
        "portfolio_report": "",
        "ceo_report": "",
        "candidates": [],
        "sector_scores": [],
        "risks": [],
        "risk_level": "중간",
        "market_direction": "",
        "review_report": "",
        "errors": [],
        "nav_recorded": {},
    }

    graph = build_graph()
    logger.info("파이프라인 시작: %s (%s)", run_type, now.strftime("%Y-%m-%d %H:%M"))
    try:
        final = graph.invoke(initial)
        logger.info("파이프라인 완료: %s", run_type)
        return final
    except Exception as e:
        logger.error("파이프라인 실패: %s", e)
        send_error_alert(f"파이프라인 실패 ({run_type}): {e}")
        raise
