import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from graph.state import InvestmentState
from clients.openai_client import chat
from services.scoring_service import score_stock
from db.database import get_conn
from sqlalchemy import text

logger = logging.getLogger(__name__)

_KST = ZoneInfo("Asia/Seoul")

_SYSTEM = """당신은 수급 분석 전문가입니다.
후보 종목들의 수급 데이터와 외국인/기관 순매수 현황을 분석하여 오늘 매매 집중 가능성이 높은 종목을 점수화하세요.

분석 항목:
1. 거래량 급증 + 주가 상승 종목 (수급 유입 신호)
2. 외국인 3거래일 연속 순매수 종목 ★ (강력 매수 신호) — 반드시 별도 강조
3. 기관 순매수 종목 (안정적 수급)
4. 상승 모멘텀 지속 종목
5. 섹터 강도와 일치하는 종목
6. [글로벌 자금 선행 지표] EWY·EEM 등락 분석
   - EWY(한국ETF) 상승: 미국 시간 기준 외국인이 한국을 매수 중 → 내일 외국인 순매수 기대
   - EWY 하락: 미국 시간에 한국 매도 → 내일 외국인 순매도 압력
   - EEM(신흥국ETF) 방향과 EWY 비교: EWY > EEM → 한국 단독 강세 (알파 발생)

출력:
- [글로벌 자금 선행 신호] EWY·EEM 분석 → 내일 외국인 수급 방향 예측 (먼저 작성)
- 수급 집중 종목 TOP5 (이유 + 매수 강도 상/중/하)
- 외국인 3거래일 연속 순매수 종목 ★ 별도 강조 (없으면 "해당 없음" 명시)
- 전체 수급 판단 (매수우위·매도우위·중립)"""





def _last_n_trading_days(n: int) -> list[str]:
    """최근 n 거래일(주말 제외) 날짜 반환 — 오늘 포함."""
    result: list[str] = []
    d = datetime.now(_KST)
    while len(result) < n:
        if d.weekday() < 5:  # 0=월 … 4=금
            result.append(d.strftime("%Y-%m-%d"))
        d -= timedelta(days=1)
    return result


def _save_foreign_buy(date: str, stocks: list[dict]) -> int:
    """외국인 순매수 상위 종목을 DB에 저장. 저장 건수를 반환."""
    try:
        rows = [
            (date,
             s.get("mksc_shrn_iscd") or s.get("stck_shrn_iscd") or s.get("code", ""),
             s.get("hts_kor_isnm") or s.get("name", ""),
             float(s.get("frgn_ntby_qty") or s.get("amount", 0) or 0))
            for s in stocks
            if s.get("mksc_shrn_iscd") or s.get("stck_shrn_iscd") or s.get("code")
        ]
        valid = [(d, c_, n, a) for d, c_, n, a in rows if a > 0]
        if valid:
            with get_conn() as conn:
                for d, c_, n, a in valid:
                    conn.execute(
                        text(
                            "INSERT INTO foreign_buy_history (date, code, name, amount) "
                            "VALUES (:date, :code, :name, :amount) "
                            "ON CONFLICT (date, code) DO UPDATE SET name=EXCLUDED.name, amount=EXCLUDED.amount"
                        ),
                        {"date": d, "code": c_, "name": n, "amount": a},
                    )
        return len(valid)
    except Exception as e:
        logger.debug("외국인 이력 저장 실패: %s", e)
        return 0


def _get_consecutive_foreign_buyers(days: int = 3) -> dict[str, str]:
    """최근 days 거래일 연속 외국인 순매수 종목 반환."""
    try:
        trading_dates = _last_n_trading_days(days)
        placeholders = ", ".join(f":d{i}" for i in range(len(trading_dates)))
        params = {f"d{i}": d for i, d in enumerate(trading_dates)}
        params["days"] = days
        with get_conn() as conn:
            rows = conn.execute(
                text(
                    f"SELECT code, name, COUNT(DISTINCT date) AS cnt "
                    f"FROM foreign_buy_history "
                    f"WHERE date IN ({placeholders}) AND amount > 0 "
                    f"GROUP BY code HAVING cnt >= :days"
                ),
                params,
            ).fetchall()
        return {r[0]: r[1] for r in rows}
    except Exception:
        return {}


def _fmt_rank(stocks: list[dict], qty_field: str = "frgn_ntby_qty", top_n: int = 10) -> str:
    """외국인/기관 순매수 상위 종목 포맷 (종목명·등락률·순매수량)."""
    lines = []
    for s in stocks[:top_n]:
        name = s.get("hts_kor_isnm") or s.get("name", "")
        code = (s.get("mksc_shrn_iscd") or s.get("stck_shrn_iscd") or s.get("code", ""))
        chg  = float(s.get("prdy_ctrt") or s.get("change_pct", 0) or 0)
        qty  = int(float(s.get(qty_field) or 0))
        qty_str = f"  순매수 {qty:,}주" if qty else ""
        lines.append(f"  {name}({code}): {chg:+.2f}%{qty_str}")
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

        # 외국인 수급 DB 저장 (순매수량 > 0인 건만)
        if foreign_stocks:
            saved = _save_foreign_buy(date, foreign_stocks)
            logger.info("[수급팀] 외국인 순매수 %d건 DB 저장", saved)

        # 포맷: 외국인 순매수량 표시
        foreign_text     = _fmt_rank(foreign_stocks, "frgn_ntby_qty") if foreign_stocks else "조회 불가 (장전/장후)"
        institution_text = _fmt_rank(inst_stocks,    "inst_ntby_qty") if inst_stocks    else "조회 불가 (장전/장후)"

        # 3거래일 연속 외국인 순매수 탐지 (주말 제외)
        consec_map = _get_consecutive_foreign_buyers(3)
        if consec_map:
            # 이름은 DB에 저장된 것 우선, 없으면 당일 foreign_stocks에서 보완
            foreign_name_map = {
                (s.get("mksc_shrn_iscd") or s.get("stck_shrn_iscd") or s.get("code", "")): s.get("hts_kor_isnm", "")
                for s in foreign_stocks
            }
            lines = []
            for code, db_name in consec_map.items():
                name = foreign_name_map.get(code) or db_name or code
                lines.append(f"★ {name}({code})")
            consecutive_text = "\n".join(lines)
        else:
            trading_days_str = ", ".join(_last_n_trading_days(3))
            consecutive_text = f"해당 없음 (조회 기준: {trading_days_str})"

        # EWY·EEM 글로벌 자금 흐름 (외국인 수급 선행 지표)
        raw_mkt = state.get("raw_market_data", {})
        ewy = raw_mkt.get("ewy", {})
        eem = raw_mkt.get("eem", {})
        ewy_line = (
            f"EWY(한국ETF): {ewy['close']} ({ewy['change_pct']:+.2f}%)"
            if ewy else "EWY: 데이터 없음"
        )
        eem_line = (
            f"EEM(신흥국ETF): {eem['close']} ({eem['change_pct']:+.2f}%)"
            if eem else "EEM: 데이터 없음"
        )
        ewy_vs_eem = ""
        if ewy and eem:
            diff = round(ewy.get("change_pct", 0) - eem.get("change_pct", 0), 2)
            ewy_vs_eem = f"EWY vs EEM 차이: {diff:+.2f}% ({'한국 단독 강세 ★' if diff > 0.3 else '한국 단독 약세 ⚠️' if diff < -0.3 else '신흥국 동반'})"

        context = (
            f"[글로벌 자금 선행 지표 — EWY·EEM]\n{ewy_line}\n{eem_line}\n{ewy_vs_eem}\n\n"
            f"후보 종목:\n{top10_text}\n\n"
            f"외국인 순매수 상위 (KOSPI+KOSDAQ):\n{foreign_text}\n\n"
            f"기관 순매수 상위 (KOSPI+KOSDAQ):\n{institution_text}\n\n"
            f"외국인 3거래일 연속 순매수 종목:\n{consecutive_text}\n\n"
            f"섹터 분석:\n{state.get('sector_report', '')}"
        )
        result = chat(_SYSTEM, context, max_tokens=2000)
        state["money_flow_report"] = result
        logger.info("[수급팀] 완료 — 외국인 %d종목, 기관 %d종목, 연속매수 %d종목",
                    len(foreign_stocks), len(inst_stocks), len(consec_map))
    except Exception as e:
        logger.error("[수급팀] 실패: %s", e)
        state["money_flow_report"] = "분석 실패"
        state["errors"].append(f"money_flow_team: {e}")
    return state
