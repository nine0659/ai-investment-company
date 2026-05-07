"""
graph/investment_graph.py
LangGraph 기반 투자 분석 메인 플로우

흐름:
START → collect_raw_data
      → futures_market_team
      → us_market_team
      → korea_spot_market_team
      → global_market_team
      → news_analysis_team
      → sector_theme_team
      → money_flow_team
      → risk_management_team
      → investment_committee
      → ceo_agent
      → save_report
      → send_telegram
      → END
"""
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from langgraph.graph import StateGraph, END

from graph.state import InvestmentState
from config.settings import TZ, RUN_TYPE_CLOSE

import agents.futures_market_team as futures_market_team
import agents.us_market_team as us_market_team
import agents.korea_spot_market_team as korea_spot_market_team
import agents.global_market_team as global_market_team
import agents.news_analysis_team as news_analysis_team
import agents.sector_theme_team as sector_theme_team
import agents.money_flow_team as money_flow_team
import agents.risk_management_team as risk_management_team
import agents.review_feedback_team as review_feedback_team
import agents.investment_committee as investment_committee
import agents.ceo_agent as ceo_agent

from clients.kis_client import KISClient
from clients.market_data_client import fetch_global_market_data
from clients.news_client import fetch_all_news
from clients.telegram_client import send_message, send_error_alert
from services.report_service import save_report, format_report_for_db

logger = logging.getLogger(__name__)

kis = KISClient()


# ─────────────────────────────────────────────────────────
# 노드 함수 정의
# ─────────────────────────────────────────────────────────

def collect_raw_data(state: InvestmentState) -> InvestmentState:
    """데이터 수집 노드: 글로벌 시장, KIS, 뉴스 동시 수집"""
    logger.info("[데이터수집] 시작")

    # 글로벌 시장 데이터
    try:
        state["raw_market_data"] = fetch_global_market_data()
        logger.info("[데이터수집] 글로벌 시장 데이터 완료")
    except Exception as e:
        logger.error("[데이터수집] 글로벌 시장 실패: %s", e)
        state["raw_market_data"] = {}
        state["errors"].append(f"collect_global: {e}")

    # KIS 한국시장 데이터
    try:
        kis_data = {}
        for market, code in [("kospi", "J"), ("kosdaq", "Q")]:
            try:
                kis_data[f"{market}_volume_rank"] = kis.get_volume_rank(code)
                kis_data[f"{market}_amount_rank"] = kis.get_amount_rank(code)
                kis_data[f"{market}_rise_rank"] = kis.get_fluctuation_rank(code, rise=True)
            except Exception as e:
                logger.warning("[데이터수집] KIS %s 실패: %s", market, e)
        state["raw_kis_data"] = kis_data
        logger.info("[데이터수집] KIS 데이터 완료")
    except Exception as e:
        logger.error("[데이터수집] KIS 전체 실패: %s", e)
        state["raw_kis_data"] = {}
        state["errors"].append(f"collect_kis: {e}")

    # 뉴스 데이터
    try:
        state["raw_news_data"] = fetch_all_news(max_per_category=8)
        logger.info("[데이터수집] 뉴스 수집 완료")
    except Exception as e:
        logger.error("[데이터수집] 뉴스 수집 실패: %s", e)
        state["raw_news_data"] = {}
        state["errors"].append(f"collect_news: {e}")

    return state


def node_futures(state: InvestmentState) -> InvestmentState:
    return futures_market_team.run(state)

def node_us(state: InvestmentState) -> InvestmentState:
    return us_market_team.run(state)

def node_korea(state: InvestmentState) -> InvestmentState:
    return korea_spot_market_team.run(state)

def node_global(state: InvestmentState) -> InvestmentState:
    return global_market_team.run(state)

def node_news(state: InvestmentState) -> InvestmentState:
    return news_analysis_team.run(state)

def node_sector(state: InvestmentState) -> InvestmentState:
    return sector_theme_team.run(state)

def node_money_flow(state: InvestmentState) -> InvestmentState:
    return money_flow_team.run(state)

def node_risk(state: InvestmentState) -> InvestmentState:
    return risk_management_team.run(state)

def node_review(state: InvestmentState) -> InvestmentState:
    """장마감 시에만 복기 실행"""
    if state.get("run_type") == RUN_TYPE_CLOSE:
        return review_feedback_team.run(state)
    return state

def node_committee(state: InvestmentState) -> InvestmentState:
    return investment_committee.run(state)

def node_ceo(state: InvestmentState) -> InvestmentState:
    return ceo_agent.run(state)


def node_save_report(state: InvestmentState) -> InvestmentState:
    """DB에 리포트 저장"""
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
    """텔레그램으로 CEO 리포트 발송"""
    report = state.get("ceo_report", "")
    if not report:
        logger.warning("[텔레그램] 발송할 리포트 없음")
        return state

    try:
        send_message(report)
        logger.info("[텔레그램] 발송 완료")

        # 에러가 있으면 에러 요약도 발송
        errors = state.get("errors", [])
        if errors:
            error_summary = "⚠️ 일부 데이터 수집 실패:\n" + "\n".join(f"- {e}" for e in errors[:5])
            send_message(error_summary)

    except Exception as e:
        logger.error("[텔레그램] 발송 실패: %s", e)

    return state


# ─────────────────────────────────────────────────────────
# 그래프 빌드
# ─────────────────────────────────────────────────────────

def build_graph() -> StateGraph:
    graph = StateGraph(InvestmentState)

    # 노드 등록
    graph.add_node("collect_raw_data", collect_raw_data)
    graph.add_node("futures_market_team", node_futures)
    graph.add_node("us_market_team", node_us)
    graph.add_node("korea_spot_market_team", node_korea)
    graph.add_node("global_market_team", node_global)
    graph.add_node("news_analysis_team", node_news)
    graph.add_node("sector_theme_team", node_sector)
    graph.add_node("money_flow_team", node_money_flow)
    graph.add_node("risk_management_team", node_risk)
    graph.add_node("review_feedback_team", node_review)
    graph.add_node("investment_committee", node_committee)
    graph.add_node("ceo_agent", node_ceo)
    graph.add_node("save_report", node_save_report)
    graph.add_node("send_telegram", node_send_telegram)

    # 엣지 연결 (순차 실행)
    graph.set_entry_point("collect_raw_data")
    graph.add_edge("collect_raw_data", "futures_market_team")
    graph.add_edge("futures_market_team", "us_market_team")
    graph.add_edge("us_market_team", "korea_spot_market_team")
    graph.add_edge("korea_spot_market_team", "global_market_team")
    graph.add_edge("global_market_team", "news_analysis_team")
    graph.add_edge("news_analysis_team", "sector_theme_team")
    graph.add_edge("sector_theme_team", "money_flow_team")
    graph.add_edge("money_flow_team", "risk_management_team")
    graph.add_edge("risk_management_team", "review_feedback_team")
    graph.add_edge("review_feedback_team", "investment_committee")
    graph.add_edge("investment_committee", "ceo_agent")
    graph.add_edge("ceo_agent", "save_report")
    graph.add_edge("save_report", "send_telegram")
    graph.add_edge("send_telegram", END)

    return graph.compile()


# ─────────────────────────────────────────────────────────
# 실행 진입점
# ─────────────────────────────────────────────────────────

def run_pipeline(run_type: str) -> InvestmentState:
    """투자 분석 파이프라인 실행"""
    now = datetime.now(TZ)
    initial_state: InvestmentState = {
        "run_type": run_type,
        "timestamp": now.isoformat(),
        "date": now.strftime("%Y-%m-%d"),
        "raw_market_data": {},
        "raw_kis_data": {},
        "raw_news_data": {},
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
        final_state = graph.invoke(initial_state)
        logger.info("파이프라인 완료: %s", run_type)
        return final_state
    except Exception as e:
        logger.error("파이프라인 실패: %s", e)
        send_error_alert(f"파이프라인 실패 ({run_type}): {e}")
        raise
