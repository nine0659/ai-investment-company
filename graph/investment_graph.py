import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from langgraph.graph import StateGraph, END

from graph.state import InvestmentState
from config.settings import TZ, RUN_TYPE_CLOSE

import agents.futures_market_team   as futures_market_team
import agents.us_market_team        as us_market_team
import agents.korea_spot_market_team as korea_spot_market_team
import agents.global_market_team    as global_market_team
import agents.news_analysis_team    as news_analysis_team
import agents.sector_theme_team     as sector_theme_team
import agents.money_flow_team       as money_flow_team
import agents.risk_management_team  as risk_management_team
import agents.review_feedback_team  as review_feedback_team
import agents.investment_committee  as investment_committee
import agents.ceo_agent             as ceo_agent

from clients.kis_client          import KISClient
from clients.market_data_client  import fetch_global_market_data
from clients.news_client         import fetch_all_news
from clients.telegram_client     import send_message, send_error_alert
from clients.us_stock_client     import fetch_us_top_movers
from services.report_service     import save_report, format_report_for_db

logger = logging.getLogger(__name__)

kis = KISClient()


# ── 데이터 수집 노드 ────────────────────────────────────────────

def collect_raw_data(state: InvestmentState) -> InvestmentState:
    logger.info("[데이터수집] 시작")

    try:
        state["raw_market_data"] = fetch_global_market_data()
        logger.info("[데이터수집] 글로벌 시장 완료")
    except Exception as e:
        logger.error("[데이터수집] 글로벌 실패: %s", e)
        state["raw_market_data"] = {}
        state["errors"].append(f"collect_global: {e}")

    try:
        state["us_hot_stocks"] = fetch_us_top_movers(n=5)
        logger.info("[데이터수집] 미국 상위 종목 완료: %d개", len(state["us_hot_stocks"]))
    except Exception as e:
        logger.error("[데이터수집] 미국 상위 종목 실패: %s", e)
        state["us_hot_stocks"] = []
        state["errors"].append(f"collect_us_hot: {e}")

    try:
        kis_data: dict = {}
        for market, code in [("kospi", "J"), ("kosdaq", "Q")]:
            for fn, suffix in [(kis.get_volume_rank, "volume_rank"),
                               (kis.get_amount_rank, "amount_rank")]:
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

    return state


# ── 에이전트 노드 ───────────────────────────────────────────────

def node_futures(state):      return futures_market_team.run(state)
def node_us(state):           return us_market_team.run(state)
def node_korea(state):        return korea_spot_market_team.run(state)
def node_global(state):       return global_market_team.run(state)
def node_news(state):         return news_analysis_team.run(state)
def node_sector(state):       return sector_theme_team.run(state)
def node_money_flow(state):   return money_flow_team.run(state)
def node_risk(state):         return risk_management_team.run(state)
def node_committee(state):    return investment_committee.run(state)
def node_ceo(state):          return ceo_agent.run(state)

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
        ("korea_spot_market_team", node_korea),
        ("global_market_team",     node_global),
        ("news_analysis_team",     node_news),
        ("sector_theme_team",      node_sector),
        ("money_flow_team",        node_money_flow),
        ("risk_management_team",   node_risk),
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
    initial: InvestmentState = {
        "run_type": run_type,
        "timestamp": now.isoformat(),
        "date": now.strftime("%Y-%m-%d"),
        "raw_market_data": {},
        "raw_kis_data": {},
        "raw_news_data": {},
        "us_hot_stocks": [],
        "futures_report": "",
        "us_market_report": "",
        "korea_spot_report": "",
        "global_market_report": "",
        "news_report": "",
        "sector_report": "",
        "money_flow_report": "",
        "risk_report": "",
        "committee_report": "",
        "ceo_report": "",
        "candidates": [],
        "sector_scores": [],
        "risks": [],
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
