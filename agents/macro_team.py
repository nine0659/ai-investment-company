"""
agents/macro_team.py
글로벌 매크로 경제 분석 — 금리 사이클·신용 스프레드·달러 추세·레짐 분류

투자위원회와 CEO 에이전트에 "매크로 레짐(Risk-On/Off/Neutral)"을 제공해
섹터 로테이션 방향과 포지션 크기 기준을 잡아준다.
"""
import logging
from graph.state import InvestmentState
from clients.openai_client import chat

logger = logging.getLogger(__name__)

_SYSTEM = """당신은 글로벌 매크로 경제 분석 전문가입니다.
제공된 지표를 분석해 오늘 한국 주식 시장의 투자 환경을 평가하세요.

[분석 기준]
1. 수익률 곡선(10Y-3M 금리차)
   - +0.5 이상: 정상 — 경기 확장 기대
   - 0 ~ +0.5: 평탄화 — 경계 구간
   - 0 미만: 역전 ⚠️ — 과거 역전 후 12~18개월 내 침체 발생

2. 신용 스프레드(HYG vs LQD 상대 성과)
   - HYG > LQD: 위험선호 — 고수익채 수요 강함
   - HYG < LQD: 위험회피 — 안전자산 선호, 주식 압박

3. 달러(DXY)
   - 상승: 외국인 KOSPI 매도 압력 → 외국인 수급 악화
   - 하락: 외국인 유입 기대 → 대형주·성장주 유리

4. 공포지수(VIX)
   - <15: 과도한 낙관 (경계)
   - 15~25: 정상
   - 25~35: 경계 구간 — 변동성 확대 가능
   - >35: 패닉 — 역발상 매수 기회 점검

5. 금(Gold)
   - 급등: 지정학 리스크 or 달러 약세
   - 약세: 위험자산 선호 재확인

6. 구리(Copper) — "Dr. Copper": 글로벌 실물 경기 선행 지표
   - 상승: 글로벌 제조업 수요 확대 → 한국 소재·조선·산업재 수혜
   - 하락: 경기 둔화 우려 → 경기민감주 회피 신호
   - 기준: 전일대비 +1% 이상 = 강한 경기 기대 / -1% 이하 = 경기 우려

7. LIT ETF(리튬·배터리) — 2차전지 섹터 선행 지표
   - 상승: 전기차·배터리 투자 심리 개선 → 에코프로·LG에너지솔루션 수혜 방향
   - 하락: 배터리 업황 불확실 → 2차전지 종목 압박

[매크로 레짐 분류]
RISK-ON: VIX<20, 크레딧 스프레드 안정, DXY 하락/보합
  → 반도체·AI·방산·성장주·소재 선호
RISK-OFF: VIX>25, 크레딧 스프레드 확대, DXY 급등
  → 방어주·배당주·현금 비중 확대
NEUTRAL: 신호 혼재
  → 분산, 확신도 낮은 종목 추천 자제

[금리 환경별 한국 섹터 힌트]
금리 상승 국면: 은행/보험 유리, 고밸류 성장주 불리, 리츠 불리
금리 하락 국면: 기술/성장주·리츠·배당주 유리
달러 강세: 수출기업(조선·방산·자동차) 유리, 내수·원자재 수입기업 불리
달러 약세: 외국인 수급 기대, 대형 성장주 유리

[출력 형식 — 이 형식을 그대로 따를 것]
🌐 매크로 레짐: [RISK-ON / RISK-OFF / NEUTRAL]
⚡ 핵심 신호 3가지 (수치 포함, 한 줄씩)
✅ 오늘 유리한 섹터: [섹터1, 섹터2, 섹터3]
🚫 오늘 불리한 섹터: [섹터1, 섹터2]
🔩 구리 신호: [경기확장/경기둔화/중립] + 한 줄 근거
🔋 배터리(LIT) 신호: [긍정/부정/중립] + 한국 2차전지 섹터 영향
⚠️ 매크로 주의사항: [한 줄]"""


def _fetch_credit_data() -> dict:
    """HYG(정크본드)/LQD(투자등급) 크레딧 스프레드 프록시 데이터 수집"""
    try:
        import yfinance as yf
        result = {}
        for sym, key in [("HYG", "hyg"), ("LQD", "lqd")]:
            try:
                hist = yf.Ticker(sym).history(period="5d", interval="1d")
                if len(hist) >= 2:
                    close = float(hist.iloc[-1]["Close"])
                    prev  = float(hist.iloc[-2]["Close"])
                    pct   = (close - prev) / prev * 100 if prev else 0
                    result[key] = {
                        "close":      round(close, 2),
                        "change_pct": round(pct, 2),
                        "data_date":  hist.index[-1].strftime("%Y-%m-%d"),
                    }
            except Exception as e:
                logger.debug("크레딧 데이터 조회 실패 (%s): %s", sym, e)
        return result
    except Exception as e:
        logger.debug("크레딧 모듈 오류: %s", e)
        return {}


def run(state: InvestmentState) -> InvestmentState:
    try:
        raw = state.get("raw_market_data", {})

        def val(key: str, field: str = "close"):
            return raw.get(key, {}).get(field)

        vix       = val("vix")
        us10y     = val("us10y")
        us3m      = val("us3m")   # 13주(3개월) T-Bill — 단기금리 기준
        dxy       = val("dxy")
        dxy_chg   = val("dxy", "change_pct")
        gold_chg  = val("gold", "change_pct")
        sp500_chg = val("sp500", "change_pct")

        # HYG / LQD 크레딧 데이터
        credit  = _fetch_credit_data()
        hyg_chg = credit.get("hyg", {}).get("change_pct")
        lqd_chg = credit.get("lqd", {}).get("change_pct")

        # 수익률 곡선 (10Y - 3M 스프레드)
        yield_curve = None
        if us10y is not None and us3m is not None:
            yield_curve = round(us10y - us3m, 2)
        yc_label = (
            f"{yield_curve:+.2f}% (역전 ⚠️)" if yield_curve is not None and yield_curve < 0
            else f"{yield_curve:+.2f}% (평탄화)" if yield_curve is not None and yield_curve < 0.5
            else f"{yield_curve:+.2f}% (정상)" if yield_curve is not None
            else "N/A"
        )

        # 신용 스프레드 방향
        credit_signal = "N/A"
        if hyg_chg is not None and lqd_chg is not None:
            diff = round(hyg_chg - lqd_chg, 2)
            if diff > 0.1:
                credit_signal = f"위험선호 (HYG-LQD: +{diff:.2f}%)"
            elif diff < -0.1:
                credit_signal = f"위험회피 ⚠️ (HYG-LQD: {diff:.2f}%)"
            else:
                credit_signal = f"중립 (HYG-LQD: {diff:+.2f}%)"

        copper_chg = val("copper", "change_pct")
        lit_chg    = val("lit",    "change_pct")

        context_lines = [
            "=== 글로벌 매크로 지표 ===",
            f"VIX(공포지수)    : {vix or 'N/A'}",
            f"미국 10년물 금리  : {us10y or 'N/A'}%",
            f"미국 3개월물 금리  : {us3m or 'N/A'}%",
            f"수익률 곡선(10Y-3M): {yc_label}",
            f"신용 스프레드(HYG-LQD): {credit_signal}",
            f"달러인덱스(DXY)  : {dxy or 'N/A'} (전일비 {f'{dxy_chg:+.2f}%' if dxy_chg is not None else 'N/A'})",
            f"금(Gold) 등락    : {f'{gold_chg:+.2f}%' if gold_chg is not None else 'N/A'}",
            f"구리(Copper) 등락: {f'{copper_chg:+.2f}%' if copper_chg is not None else 'N/A'}  ← 글로벌 경기 선행",
            f"LIT(리튬배터리ETF): {f'{lit_chg:+.2f}%' if lit_chg is not None else 'N/A'}  ← 2차전지 섹터 선행",
            f"S&P500 전일 등락 : {f'{sp500_chg:+.2f}%' if sp500_chg is not None else 'N/A'}",
        ]

        context = "\n".join(context_lines)
        result  = chat(_SYSTEM, context, max_tokens=500)
        state["macro_report"] = result
        logger.info("[매크로팀] 완료")
    except Exception as e:
        logger.error("[매크로팀] 실패: %s", e)
        state["macro_report"] = "매크로 분석 실패"
        state["errors"].append(f"macro_team: {e}")
    return state
