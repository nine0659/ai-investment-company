import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from langgraph.graph import StateGraph, END

from graph.state import InvestmentState
from config.settings import TZ, RUN_TYPE_CLOSE

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
import agents.review_feedback_team   as review_feedback_team
import agents.investment_committee   as investment_committee
import agents.ceo_agent              as ceo_agent

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
        state["raw_news_data"] = fetch_all_news(max_per_category=8)
        logger.info("[데이터수집] 뉴스 완료")
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
def node_money_flow(state):    return money_flow_team.run(state)
def node_macro(state):         return macro_team.run(state)
def node_event_risk(state):    return event_risk_team.run(state)
def node_intelligence(state):  return market_intelligence_team.run(state)
def node_risk(state):          return risk_management_team.run(state)
def node_committee(state):     return investment_committee.run(state)
def node_ceo(state):           return ceo_agent.run(state)

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
            send_message("⚠️ 일부 데이터 수집 실패:\n" + "\n".join(f"- {e}" for e in errors[:5]))
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
        ("money_flow_team",        node_money_flow),
        ("macro_team",                node_macro),            # 매크로 레짐 분석 (금리/크레딧/달러/구리)
        ("event_risk_team",         node_event_risk),       # 경제 이벤트 캘린더 리스크
        ("market_intelligence_team",node_intelligence),     # 글로벌 전문가 서사·컨센서스 (해석 레이어)
        ("risk_management_team",    node_risk),
        ("review_feedback_team",   node_review),
        ("investment_committee",   node_committee),
        ("ceo_agent",              node_ceo),
        ("save_report",            node_save_report),
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
        "money_flow_report": "",
        "risk_report": "",
        "committee_report": "",
        "ceo_report": "",
        "candidates": [],
        "sector_scores": [],
        "risks": [],
        "risk_level": "중간",
        "market_direction": "",
        "review_report": "",
        "errors": [],
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
