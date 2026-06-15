import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from langgraph.graph import StateGraph, END

from graph.state import InvestmentState
from config.settings import TZ, RUN_TYPE_GLOBAL, RUN_TYPE_PRE, RUN_TYPE_CLOSE, RUN_TYPE_INTRA1, RUN_TYPE_INTRA2

import agents.futures_market_team       as futures_market_team
import agents.us_global_team            as us_global_team        # 통합: us_market + us_impact + global
import agents.korea_flow_team           as korea_flow_team        # 통합: korea_spot + sector + money_flow
import agents.news_analysis_team        as news_analysis_team
import agents.bigfigure_agent           as bigfigure_agent
import agents.macro_team                as macro_team
import agents.event_risk_team           as event_risk_team
import agents.market_intelligence_team  as market_intelligence_team
import agents.risk_management_team      as risk_management_team
import agents.issue_stock_agent         as issue_stock_agent
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
                (kis.get_volume_rank,         "volume_rank"),
                (kis.get_amount_rank,         "amount_rank"),
                (kis.get_foreign_buy_rank,    "foreign_rank"),
                (kis.get_institution_buy_rank,"institution_rank"),
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

    try:
        from services.thesis_service import get_thesis_ceo_summary
        state["investment_thesis"] = get_thesis_ceo_summary()
        if state["investment_thesis"]:
            logger.info("[데이터수집] 투자관 로드 완료")
    except Exception as e:
        logger.debug("[데이터수집] 투자관 로드 실패 (무시): %s", e)
        state["investment_thesis"] = ""

    try:
        from services.strategy_service import get_latest_strategy_summary
        state["weekly_strategy_summary"] = get_latest_strategy_summary(max_days=7)
        if state["weekly_strategy_summary"]:
            logger.info("[데이터수집] 주간 전략 요약 로드 완료")
    except Exception as e:
        logger.debug("[데이터수집] 주간 전략 로드 실패 (무시): %s", e)
        state["weekly_strategy_summary"] = ""

    try:
        from clients.consensus_client import fetch_consensus_batch
        from agents.ceo_agent import _BLUECHIP_ALWAYS_FETCH

        bluechip_codes = [s["code"] for s in _BLUECHIP_ALWAYS_FETCH]
        name_map  = {s["code"]: s["name"]   for s in _BLUECHIP_ALWAYS_FETCH}
        market_map= {s["code"]: s["market"] for s in _BLUECHIP_ALWAYS_FETCH}
        consensus_raw = fetch_consensus_batch(bluechip_codes, delay=0.3, market_map=market_map)
        state["consensus_data"] = {"_raw": consensus_raw, "_name_map": name_map}
        logger.info("[데이터수집] 컨센서스 목표주가 완료: %d종목", len(consensus_raw))
    except Exception as e:
        logger.warning("[데이터수집] 컨센서스 수집 실패 (무시): %s", e)
        state["consensus_data"] = {}

    return state


# ── 에이전트 노드 ───────────────────────────────────────────────

def node_futures(state):      return futures_market_team.run(state)
def node_us_global(state):    return us_global_team.run(state)
def node_korea_flow(state):   return korea_flow_team.run(state)
def node_news(state):         return news_analysis_team.run(state)
def node_bigfigure(state):    return bigfigure_agent.run(state)
def node_macro(state):        return macro_team.run(state)
def node_event_risk(state):   return event_risk_team.run(state)
def node_intelligence(state): return market_intelligence_team.run(state)
def node_risk(state):         return risk_management_team.run(state)
def node_issue_stocks(state): return issue_stock_agent.run(state)

def node_midterm_stocks(state):
    if state.get("run_type") in (RUN_TYPE_INTRA1, RUN_TYPE_INTRA2):
        return {}
    return midterm_stock_agent.run(state)

def node_ceo(state):               return ceo_agent.run(state)
def node_portfolio_manager(state): return portfolio_manager_agent.run(state)
def node_committee(state):         return investment_committee.run(state)

def node_review(state):
    if state.get("run_type") == RUN_TYPE_CLOSE:
        return review_feedback_team.run(state)
    return {}

# 병렬 팬-인 배리어 (변경 없음 — state는 LangGraph가 자동 유지)
def node_l2_barrier(state): return {}
def node_l3_barrier(state): return {}


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

    ceo_report = state.get("ceo_report", "")

    # CIO 의사결정 아카이브 저장 (신규 구조화 출력)
    decisions = state.get("ceo_decisions", {})
    if decisions and (decisions.get("new_positions") or decisions.get("position_changes")):
        try:
            import json
            from db.database import get_conn
            from sqlalchemy import text as _text
            with get_conn() as conn:
                conn.execute(_text("""
                    CREATE TABLE IF NOT EXISTS cio_decisions_log (
                        id           SERIAL PRIMARY KEY,
                        date         TEXT NOT NULL,
                        run_type     TEXT NOT NULL,
                        macro_stance TEXT,
                        cash_target_pct INTEGER,
                        thesis_status   TEXT,
                        committee_alignment TEXT,
                        decisions_json TEXT,
                        created_at   TIMESTAMP DEFAULT NOW(),
                        UNIQUE (date, run_type)
                    )
                """))
                conn.execute(_text("""
                    INSERT INTO cio_decisions_log
                    (date, run_type, macro_stance, cash_target_pct, thesis_status,
                     committee_alignment, decisions_json)
                    VALUES (:date, :run_type, :stance, :cash, :thesis, :align, :json)
                    ON CONFLICT (date, run_type) DO UPDATE
                    SET macro_stance=EXCLUDED.macro_stance,
                        cash_target_pct=EXCLUDED.cash_target_pct,
                        thesis_status=EXCLUDED.thesis_status,
                        committee_alignment=EXCLUDED.committee_alignment,
                        decisions_json=EXCLUDED.decisions_json
                """), {
                    "date":     state["date"],
                    "run_type": state["run_type"],
                    "stance":   decisions.get("macro_stance", "neutral"),
                    "cash":     decisions.get("cash_target_pct", 30),
                    "thesis":   decisions.get("thesis_status", "intact"),
                    "align":    decisions.get("committee_alignment", "agree"),
                    "json":     json.dumps(decisions, ensure_ascii=False),
                })
            logger.info("[CIO결정저장] %s %s — 신규:%d건 조정:%d건",
                        state["date"], state["run_type"],
                        len(decisions.get("new_positions", [])),
                        len(decisions.get("position_changes", [])))
        except Exception as e:
            logger.debug("[CIO결정저장] 실패 (테이블 없을 수 있음): %s", e)

    if ceo_report and state.get("run_type") == RUN_TYPE_CLOSE:
        try:
            from services.recommendation_service import parse_recommendations, save_recommendations
            recs = parse_recommendations(ceo_report)
            if recs:
                save_recommendations(state["date"], recs)
                logger.info("[추천저장] 장마감 %d건 저장", len(recs))
        except Exception as e:
            logger.debug("[추천저장] 실패: %s", e)

    if ceo_report:
        try:
            from services.market_prediction_service import save_prediction
            save_prediction(state["date"], state["run_type"], ceo_report)
        except Exception as e:
            logger.debug("[예측저장] 실패: %s", e)

    try:
        from services.market_archive_service import save_market_snapshot, save_intelligence_summary
        save_market_snapshot(
            date=state["date"],
            run_type=state["run_type"],
            market_data=state.get("raw_market_data", {}),
        )
        intel_report = state.get("market_intelligence_report", "")
        if intel_report:
            sentiment = "강세" if "강세" in intel_report else ("약세" if "약세" in intel_report else "중립")
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
            _SKIP_PATTERNS = ("psycopg2", "OperationalError", "supabase", "connection to server")
            filtered = [e for e in errors if not any(p in e for p in _SKIP_PATTERNS)]
            if filtered:
                logger.warning("[텔레그램] 데이터 수집 오류 %d건 (로그 확인): %s", len(filtered), filtered[:3])
            db_errs = len(errors) - len(filtered)
            if db_errs:
                logger.warning("[텔레그램] DB 연결 오류 %d건 — 로그 확인 요망", db_errs)
    except Exception as e:
        logger.error("[텔레그램] 발송 실패: %s", e)
    return state


# ── 글로벌 시황 전용 데이터 수집 (KIS·DART 제외) ─────────────────

def collect_raw_data_global(state: InvestmentState) -> InvestmentState:
    logger.info("[글로벌수집] 시작 (KIS 제외)")

    for key, fn, label, default in [
        ("raw_market_data", fetch_global_market_data,          "글로벌 시장",      {}),
        ("us_sector_data",  fetch_us_sectors,                  "미국 섹터 ETF",    {}),
        ("us_52w_highs",    fetch_us_52w_highs,                "미국 52주 신고가", []),
    ]:
        try:
            state[key] = fn()
            logger.info("[글로벌수집] %s 완료", label)
        except Exception as e:
            logger.error("[글로벌수집] %s 실패: %s", label, e)
            state[key] = default
            state["errors"].append(f"global_{key}: {e}")

    try:
        freshness = check_data_freshness(state["raw_market_data"])
        state["data_freshness"] = freshness
        if freshness.get("warning"):
            logger.warning("[글로벌수집] %s", freshness["warning"])
    except Exception:
        state["data_freshness"] = {}

    try:
        state["us_hot_stocks"] = fetch_us_top_movers(n=10)
        logger.info("[글로벌수집] 미국 상위 종목 %d개", len(state["us_hot_stocks"]))
    except Exception as e:
        logger.error("[글로벌수집] 미국 상위 종목 실패: %s", e)
        state["us_hot_stocks"] = []
        state["errors"].append(f"global_us_hot: {e}")

    try:
        state["bigfigure_news"] = fetch_bigfigure_news(max_per_figure=3)
        logger.info("[글로벌수집] 빅피겨 뉴스 %d명", len(state["bigfigure_news"]))
    except Exception as e:
        logger.error("[글로벌수집] 빅피겨 뉴스 실패: %s", e)
        state["bigfigure_news"] = []

    try:
        state["raw_news_data"] = fetch_all_news(
            max_per_category=6,
            market_data=state.get("raw_market_data", {}),
        )
        logger.info("[글로벌수집] 뉴스 완료")
    except Exception as e:
        logger.error("[글로벌수집] 뉴스 실패: %s", e)
        state["raw_news_data"] = {}

    state["raw_kis_data"]     = {}
    state["dart_disclosures"] = []
    state["consensus_data"]   = {}
    state["kr_index_realtime"]= {}

    try:
        from services.thesis_service import get_thesis_ceo_summary
        state["investment_thesis"] = get_thesis_ceo_summary()
    except Exception:
        state["investment_thesis"] = ""

    try:
        from services.strategy_service import get_latest_strategy_summary
        state["weekly_strategy_summary"] = get_latest_strategy_summary(max_days=7)
    except Exception:
        state["weekly_strategy_summary"] = ""

    return state


# ── 그래프 빌드 ────────────────────────────────────────────────
#
# 병렬 실행 구조:
#   Layer 1: collect_raw_data (sequential)
#   Layer 2: futures | us_global | news | bigfigure | macro | event_risk | intelligence (parallel)
#   [l2_barrier: fan-in]
#   Layer 3: korea_flow | issue_stocks (parallel)
#   [l3_barrier: fan-in]
#   Layer 4: risk → review → committee → portfolio → midterm → ceo (sequential)
#   Layer 5: save → nav → telegram → END (sequential)

_L2_NODES = [
    "futures_market_team",
    "us_global_team",
    "news_analysis_team",
    "bigfigure_agent",
    "macro_team",
    "event_risk_team",
    "market_intelligence_team",
]

_L3_NODES = [
    "korea_flow_team",
    "issue_stock_agent",
]


def build_graph() -> StateGraph:
    g = StateGraph(InvestmentState)

    # ── 노드 등록
    g.add_node("collect_raw_data",         collect_raw_data)
    g.add_node("futures_market_team",      node_futures)
    g.add_node("us_global_team",           node_us_global)
    g.add_node("news_analysis_team",       node_news)
    g.add_node("bigfigure_agent",          node_bigfigure)
    g.add_node("macro_team",               node_macro)
    g.add_node("event_risk_team",          node_event_risk)
    g.add_node("market_intelligence_team", node_intelligence)
    g.add_node("l2_barrier",               node_l2_barrier)
    g.add_node("korea_flow_team",          node_korea_flow)
    g.add_node("issue_stock_agent",        node_issue_stocks)
    g.add_node("l3_barrier",               node_l3_barrier)
    g.add_node("risk_management_team",     node_risk)
    g.add_node("review_feedback_team",     node_review)
    g.add_node("investment_committee",     node_committee)
    g.add_node("portfolio_manager_agent",  node_portfolio_manager)
    g.add_node("midterm_stock_agent",      node_midterm_stocks)
    g.add_node("ceo_agent",               node_ceo)
    g.add_node("save_report",             node_save_report)
    g.add_node("record_nav",              node_record_nav)
    g.add_node("send_telegram",           node_send_telegram)

    # ── 엣지
    g.set_entry_point("collect_raw_data")

    # Layer 1 → Layer 2 (fan-out)
    for n in _L2_NODES:
        g.add_edge("collect_raw_data", n)

    # Layer 2 → l2_barrier (fan-in)
    for n in _L2_NODES:
        g.add_edge(n, "l2_barrier")

    # l2_barrier → Layer 3 (fan-out)
    for n in _L3_NODES:
        g.add_edge("l2_barrier", n)

    # Layer 3 → l3_barrier (fan-in)
    for n in _L3_NODES:
        g.add_edge(n, "l3_barrier")

    # Sequential tail
    for src, dst in [
        ("l3_barrier",           "risk_management_team"),
        ("risk_management_team", "review_feedback_team"),
        ("review_feedback_team", "investment_committee"),
        ("investment_committee", "portfolio_manager_agent"),
        ("portfolio_manager_agent", "midterm_stock_agent"),
        ("midterm_stock_agent",  "ceo_agent"),
        ("ceo_agent",            "save_report"),
        ("save_report",          "record_nav"),
        ("record_nav",           "send_telegram"),
    ]:
        g.add_edge(src, dst)
    g.add_edge("send_telegram", END)

    return g.compile()


def build_global_graph() -> StateGraph:
    """글로벌 시황 브리핑 전용 경량 그래프 (KIS 제외, 미국·글로벌 데이터만).

    Layer 1: collect_raw_data_global
    Layer 2: futures | us_global | news | bigfigure | macro | intelligence (parallel)
    [gl2_barrier: fan-in]
    Layer 3: midterm → ceo → save → telegram
    """
    _GL2 = [
        "futures_market_team",
        "us_global_team",
        "news_analysis_team",
        "bigfigure_agent",
        "macro_team",
        "market_intelligence_team",
    ]

    g = StateGraph(InvestmentState)

    g.add_node("collect_raw_data_global",  collect_raw_data_global)
    g.add_node("futures_market_team",      node_futures)
    g.add_node("us_global_team",           node_us_global)
    g.add_node("news_analysis_team",       node_news)
    g.add_node("bigfigure_agent",          node_bigfigure)
    g.add_node("macro_team",               node_macro)
    g.add_node("market_intelligence_team", node_intelligence)
    g.add_node("gl2_barrier",              node_l2_barrier)
    g.add_node("midterm_stock_agent",      node_midterm_stocks)
    g.add_node("ceo_agent",               node_ceo)
    g.add_node("save_report",             node_save_report)
    g.add_node("send_telegram",           node_send_telegram)

    g.set_entry_point("collect_raw_data_global")
    for n in _GL2:
        g.add_edge("collect_raw_data_global", n)
    for n in _GL2:
        g.add_edge(n, "gl2_barrier")

    for src, dst in [
        ("gl2_barrier",     "midterm_stock_agent"),
        ("midterm_stock_agent", "ceo_agent"),
        ("ceo_agent",       "save_report"),
        ("save_report",     "send_telegram"),
    ]:
        g.add_edge(src, dst)
    g.add_edge("send_telegram", END)

    return g.compile()


# ── 실행 진입점 ────────────────────────────────────────────────

def run_pipeline(run_type: str) -> InvestmentState:
    if run_type == RUN_TYPE_GLOBAL:
        return _run_global(run_type)

    now = datetime.now(TZ)

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
        "ceo_decisions": {},
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


def _run_global(run_type: str) -> InvestmentState:
    now = datetime.now(TZ)

    try:
        from services.report_service import already_ran_today
        if already_ran_today(now.strftime("%Y-%m-%d"), run_type):
            logger.info("[글로벌] 오늘 이미 발송 — 스킵")
            return {}
    except Exception as e:
        logger.debug("[글로벌] 중복 체크 실패 (무시): %s", e)

    initial: InvestmentState = {
        "run_type": run_type,
        "timestamp": now.isoformat(),
        "date": now.strftime("%Y-%m-%d"),
        "raw_market_data": {}, "data_freshness": {},
        "raw_kis_data": {}, "raw_news_data": {},
        "us_hot_stocks": [], "us_sector_data": {}, "us_52w_highs": [],
        "bigfigure_news": [], "dart_disclosures": [],
        "kr_index_realtime": {}, "consensus_data": {},
        "weekly_strategy_summary": "", "investment_thesis": "",
        "futures_report": "", "us_market_report": "", "us_impact_report": "",
        "korea_spot_report": "", "global_market_report": "", "news_report": "",
        "bigfigure_report": "", "dart_report": "", "macro_report": "",
        "event_risk_report": "", "event_risk_level": "중간",
        "market_intelligence_report": "", "sector_report": "",
        "issue_stocks_report": "", "midterm_stock_report": "",
        "money_flow_report": "", "risk_report": "", "committee_report": "",
        "portfolio_report": "", "ceo_report": "",
        "candidates": [], "sector_scores": [], "risks": [],
        "risk_level": "중간", "market_direction": "",
        "review_report": "", "errors": [], "nav_recorded": {},
        "ceo_decisions": {},
    }

    graph = build_global_graph()
    logger.info("[글로벌] 파이프라인 시작 (%s)", now.strftime("%Y-%m-%d %H:%M"))
    try:
        final = graph.invoke(initial)
        logger.info("[글로벌] 파이프라인 완료")
        return final
    except Exception as e:
        logger.error("[글로벌] 파이프라인 실패: %s", e)
        send_error_alert(f"글로벌 시황 파이프라인 실패: {e}")
        raise
