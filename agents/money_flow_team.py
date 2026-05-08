import logging
import os
import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from graph.state import InvestmentState
from clients.openai_client import chat
from services.scoring_service import score_stock

logger = logging.getLogger(__name__)

_KST = ZoneInfo("Asia/Seoul")
_DB  = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "data", "database.sqlite3"))

_SYSTEM = """당신은 수급 분석 전문가입니다.
후보 종목들의 수급 데이터와 외국인/기관 순매수 현황을 분석하여 오늘 매매 집중 가능성이 높은 종목을 점수화하세요.

분석 항목:
1. 거래량 급증 + 주가 상승 종목 (수급 유입 신호)
2. 외국인 3일 연속 순매수 종목 ★ (강력 매수 신호)
3. 기관 순매수 종목 (안정적 수급)
4. 상승 모멘텀 지속 종목
5. 섹터 강도와 일치하는 종목

출력:
- 수급 집중 종목 TOP5 (이유 + 매수 강도)
- 외국인 3일 연속 순매수 종목 ★ 별도 강조
- 전체 수급 판단 (매수우위·매도우위·중립)"""


def _conn():
    os.makedirs(os.path.dirname(_DB), exist_ok=True)
    return sqlite3.connect(_DB)


def _save_foreign_buy(date: str, stocks: list[dict]):
    try:
        with _conn() as c:
            c.execute("""
                CREATE TABLE IF NOT EXISTS foreign_buy_history (
                    date TEXT NOT NULL, code TEXT NOT NULL, name TEXT, amount REAL,
                    PRIMARY KEY (date, code)
                )
            """)
            rows = [
                (date,
                 s.get("mksc_shrn_iscd") or s.get("code", ""),
                 s.get("hts_kor_isnm") or s.get("name", ""),
                 float(s.get("frgn_ntby_qty") or s.get("amount", 0) or 0))
                for s in stocks
                if s.get("mksc_shrn_iscd") or s.get("code")
            ]
            if rows:
                c.executemany(
                    "INSERT OR REPLACE INTO foreign_buy_history (date, code, name, amount) VALUES (?,?,?,?)",
                    rows,
                )
    except Exception as e:
        logger.debug("외국인 이력 저장 실패: %s", e)


def _get_consecutive_foreign_buyers(days: int = 3) -> list[str]:
    try:
        dates = [(datetime.now(_KST) - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days)]
        with _conn() as c:
            placeholders = ",".join("?" * len(dates))
            rows = c.execute(
                f"SELECT code, COUNT(DISTINCT date) AS cnt FROM foreign_buy_history "
                f"WHERE date IN ({placeholders}) AND amount > 0 GROUP BY code HAVING cnt >= ?",
                (*dates, days),
            ).fetchall()
        return [r[0] for r in rows]
    except Exception:
        return []


def _fmt_stocks(stocks: list[dict], top_n: int = 10) -> str:
    lines = []
    for s in stocks[:top_n]:
        name = s.get("hts_kor_isnm") or s.get("name", "")
        code = s.get("mksc_shrn_iscd") or s.get("code", "")
        chg  = float(s.get("prdy_ctrt") or s.get("change_pct", 0) or 0)
        lines.append(f"  {name}({code}): {chg:+.2f}%")
    return "\n".join(lines) or "없음"


def run(state: InvestmentState) -> InvestmentState:
    try:
        candidates    = state.get("candidates", [])
        sector_scores = state.get("sector_scores", [])
        date          = state.get("date", datetime.now(_KST).strftime("%Y-%m-%d"))

        for c in candidates:
            c["score"] = score_stock(c, sector_scores)
        candidates.sort(key=lambda x: x.get("score", 0), reverse=True)
        state["candidates"] = candidates

        top10_text = "\n".join(
            f"{c.get('name', c.get('code', ''))}: 등락률 {c.get('change_pct', 0)}%, 점수 {c.get('score', 0)}"
            for c in candidates[:10]
        ) or "후보 없음"

        raw_kis = state.get("raw_kis_data", {})
        foreign_stocks = raw_kis.get("kospi_foreign_rank", []) + raw_kis.get("kosdaq_foreign_rank", [])
        inst_stocks    = raw_kis.get("kospi_institution_rank", []) + raw_kis.get("kosdaq_institution_rank", [])

        if foreign_stocks:
            _save_foreign_buy(date, foreign_stocks)

        foreign_text     = _fmt_stocks(foreign_stocks) if foreign_stocks else "조회 불가"
        institution_text = _fmt_stocks(inst_stocks)    if inst_stocks    else "조회 불가"

        consec = _get_consecutive_foreign_buyers(3)
        if consec:
            consec_names = [
                f"★ {c.get('name', code)}({code})"
                for c in candidates if (code := c.get("mksc_shrn_iscd") or c.get("code", "")) in consec
            ]
            consecutive_text = "\n".join(consec_names) if consec_names else "데이터 부족"
        else:
            consecutive_text = "데이터 부족 (수집 중)"

        context = (
            f"후보 종목:\n{top10_text}\n\n"
            f"외국인 순매수 상위:\n{foreign_text}\n\n"
            f"기관 순매수 상위:\n{institution_text}\n\n"
            f"외국인 3일 연속 순매수 종목:\n{consecutive_text}\n\n"
            f"섹터 분석:\n{state.get('sector_report', '')}"
        )
        result = chat(_SYSTEM, context, max_tokens=2000)
        state["money_flow_report"] = result
        logger.info("[수급팀] 완료")
    except Exception as e:
        logger.error("[수급팀] 실패: %s", e)
        state["money_flow_report"] = "분석 실패"
        state["errors"].append(f"money_flow_team: {e}")
    return state
