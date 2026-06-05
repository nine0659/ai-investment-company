"""
agents/thesis_agent.py
월간 투자관(Investment Thesis) 생성 에이전트

매월 첫째 월요일 19:00 KST 자동 실행 (scheduler.py).
수동 실행: python main.py --type thesis

역할:
  - 현재 경기 사이클 위치 규명
  - 6~12개월 전망과 핵심 드라이버 식별
  - 섹터 비중 확대/축소 가이드라인 수립
  - 핵심 확신 아이디어 3~5개 선정
  - 시나리오(강세/기본/약세) + 투자관 무효 조건 정의
  - 일일 CEO 브리핑에 주입될 압축 요약 자동 생성
"""
import json
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from clients.openai_client import chat
from clients.market_data_client import fetch_global_market_data
from clients.telegram_client import send_message, send_error_alert
from clients.us_market_client import fetch_us_sectors
from services.thesis_service import save_thesis
from services.recommendation_service import get_performance_stats

logger = logging.getLogger(__name__)
_TZ = ZoneInfo("Asia/Seoul")


_THESIS_SYSTEM = """당신은 전 세계 최고 수준의 CIO(최고투자책임자)다.
레이 달리오의 매크로 사이클 분석, 워런 버핏의 가치 판단,
하워드 막스의 사이클 포지셔닝을 통합한 시각으로
향후 6~12개월 한국 주식 시장을 바라보는 종합 투자관를 수립하라.

━━ [투자관의 역할] ━━
이 문서는 향후 모든 일일 브리핑과 개별 종목 추천의 "헌법"이다.
개별 매매 신호는 반드시 이 투자관의 방향성과 정합해야 한다.
투자관에 반하는 단기 추천은 명시적 이유 없이 허용되지 않는다.

━━ [필수 분석 항목] ━━

① 경기 사이클 위치 규명
  - 현재 어느 단계인가: [초기확장 / 중기확장 / 후기확장 / 수축 초입 / 수축 심화 / 바닥]
  - 판단 근거: 수익률 곡선, PMI 방향, 기업 이익 모멘텀, 고용 상황
  - 한국 vs 미국 사이클 동조/디커플링 여부

② 핵심 매크로 드라이버 (5개 이내)
  - 지금 시장을 움직이는 가장 중요한 힘 5개
  - 각 드라이버가 6~12개월 내 어떻게 전개될지 방향 예측

③ 6개월 전망 (구체적 수치 근거)
  KOSPI 방향: [상승/횡보/하락] 확률 XX%
  핵심 촉매: [무엇이 위 방향을 만들 것인가]
  주의 이벤트: [방향을 뒤집을 수 있는 이벤트]

④ 12개월 전망 (구조적 관점)
  경제 환경 변화 방향과 그로 인한 한국 주식 시장의 구조적 포지션

⑤ 자산 배분 가이드라인
  현금 비중 권고: XX% (이유)
  섹터 비중 확대: [섹터명] — 근거
  섹터 비중 축소: [섹터명] — 근거
  국내/해외 비중: XX% / YY%

⑥ 핵심 확신 아이디어 (3~5개)
  종목명(코드) | 투자 기간: X개월 | 기대 수익: +XX%
  ┌ 매크로 연결고리: [어떤 투자관 방향성과 연결되는가]
  ├ 기업 특유 강점: [해자/실적 모멘텀/수급]
  ├ 매수 조건: [즉시 or 조건부]
  ├ 목표가 근거: [PER/PBR/컨센서스 등]
  └ 틀릴 조건: [이 아이디어가 실패하는 시나리오]

⑦ 시나리오 분석
  강세(확률 XX%): [조건 + KOSPI 예상 레인지]
  기본(확률 XX%): [조건 + KOSPI 예상 레인지]
  약세(확률 XX%): [조건 + KOSPI 예상 레인지]

⑧ 투자관 무효 조건 (이 중 하나라도 발생하면 투자관 전면 재검토)
  - [조건 1]
  - [조건 2]
  - [조건 3]

━━ [출력 언어 및 형식] ━━
한국어 텔레그램 텍스트. 이모지·구분선 유지.
모든 판단에 확률 수치 명시. "좋아 보인다" 같은 모호한 표현 금지."""

_CEO_INJECT_SYSTEM = """아래 월간 투자관 전문에서
일일 CEO 브리핑에 주입할 600자 이내 핵심 요약을 작성하라.

[반드시 포함할 내용]
• 경기 사이클 현재 위치 (한 줄)
• 매크로 레짐과 6개월 핵심 방향 (한 줄)
• 비중 확대 섹터 2~3개 / 축소 섹터 1~2개
• 핵심 확신 아이디어 2~3개 (종목명·코드·한 줄 근거)
• 투자관 무효 조건 1~2개 (경계해야 할 신호)

[출력 형식]
📋 투자관 핵심 (YYYY-MM-DD)
📍 사이클: [위치 + 근거 한 줄]
🌐 방향: [레짐] — [6개월 핵심 근거]
📈 확대: [섹터1·섹터2] | 📉 축소: [섹터]
🎯 확신: 종목(코드) — 근거 / 종목(코드) — 근거
⛔ 무효조건: [조건 1] / [조건 2]"""


def _parse_structured_fields(report: str) -> dict:
    """전체 리포트에서 구조화된 필드 간략 파싱 (DB 컬럼용)."""
    fields = {
        "cycle_stage": "", "macro_regime": "", "outlook_6m": "", "outlook_12m": "",
        "sector_overweight": [], "sector_underweight": [],
        "conviction_ideas": [], "bull_scenario": "", "base_scenario": "",
        "bear_scenario": "", "invalidation": "",
    }
    import re
    # 사이클 단계
    m = re.search(r"초기확장|중기확장|후기확장|수축\s*초입|수축\s*심화|바닥", report)
    if m:
        fields["cycle_stage"] = m.group(0)
    # 매크로 레짐
    m = re.search(r"RISK-ON|RISK-OFF|NEUTRAL", report)
    if m:
        fields["macro_regime"] = m.group(0)
    # 6개월 전망 (간략 파싱)
    m = re.search(r"6개월[^:]*:?\s*(.{20,150})", report)
    if m:
        fields["outlook_6m"] = m.group(1)[:200].strip()
    # 12개월 전망
    m = re.search(r"12개월[^:]*:?\s*(.{20,150})", report)
    if m:
        fields["outlook_12m"] = m.group(1)[:200].strip()
    # 시나리오
    m = re.search(r"강세[^:]*:?\s*(.{10,120})", report)
    if m:
        fields["bull_scenario"] = m.group(1)[:200].strip()
    m = re.search(r"기본[^:]*:?\s*(.{10,120})", report)
    if m:
        fields["base_scenario"] = m.group(1)[:200].strip()
    m = re.search(r"약세[^:]*:?\s*(.{10,120})", report)
    if m:
        fields["bear_scenario"] = m.group(1)[:200].strip()
    return fields


def run_thesis():
    """월간 투자관 생성, DB 저장, 텔레그램 발송."""
    now = datetime.now(_TZ)
    logger.info("[투자관에이전트] 월간 투자관 수립 시작: %s", now.strftime("%Y-%m-%d"))

    # ── 데이터 수집 ────────────────────────────────────────────────
    try:
        market = fetch_global_market_data()
    except Exception as e:
        market = {}
        logger.warning("[투자관에이전트] 시장 데이터 실패: %s", e)

    try:
        us_sectors = fetch_us_sectors()
    except Exception:
        us_sectors = {}

    try:
        perf = get_performance_stats(days=30)
    except Exception:
        perf = {}

    # 크레딧 스프레드 (HYG/LQD)
    credit_text = ""
    try:
        import yfinance as yf
        credit_lines = []
        for sym, label in [("HYG", "하이일드(HYG)"), ("LQD", "투자등급(LQD)"),
                           ("^TNX", "미국10Y금리"), ("^IRX", "미국3M금리"),
                           ("HG=F", "구리선물"), ("DX-Y.NYB", "달러인덱스")]:
            try:
                h = yf.Ticker(sym).history(period="1mo", interval="1d")
                if len(h) >= 2:
                    cur = float(h.iloc[-1]["Close"])
                    mo_start = float(h.iloc[0]["Close"])
                    chg = (cur - mo_start) / mo_start * 100
                    credit_lines.append(f"  {label}: {cur:.2f} (월간 {chg:+.2f}%)")
            except Exception:
                pass
        credit_text = "\n".join(credit_lines)
    except Exception:
        pass

    def _mkt(k, f="close"):
        d = market.get(k, {})
        return d.get(f, "N/A")

    # 최근 귀인 분석 교훈 로드 (학습 루프)
    recent_learnings = ""
    try:
        from agents.attribution_agent import get_recent_learnings
        recent_learnings = get_recent_learnings(weeks=2)
    except Exception:
        pass

    context = f"""분석 기준일: {now.strftime('%Y년 %m월 %d일')}

[최근 2주 귀인 분석 교훈 — 이번 투자관에 반드시 반영할 것]
{recent_learnings if recent_learnings else '귀인 분석 이력 없음 (첫 번째 투자관)'}

[글로벌 매크로 현황]
S&P500: {_mkt('sp500')} ({_mkt('sp500','change_pct'):+.2f}% 전일) | SOX 반도체: {_mkt('sox')}
KOSPI: {_mkt('kospi')} | KOSDAQ: {_mkt('kosdaq')}
VIX(공포지수): {_mkt('vix')} | 원/달러: {_mkt('usd_krw')}
미국10Y금리: {_mkt('us10y')}% | 금: {_mkt('gold')} | WTI: {_mkt('oil_wti')}
나스닥: {_mkt('nasdaq')} | 다우: {_mkt('dow')}

[금리 및 신용 시장 — 경기 사이클 판단 핵심 지표]
{credit_text or '데이터 없음'}

[미국 섹터 ETF 최근 한 달 흐름]
{chr(10).join(f'  {k}: {v.get("change_pct",0):+.2f}%' for k,v in sorted(us_sectors.items(), key=lambda x: x[1].get('change_pct',0), reverse=True)[:10]) or '없음'}

[최근 30일 추천 성과 — 현재 전략의 유효성 검증]
총 {perf.get('total',0)}건 | 승률 {perf.get('win_rate',0)}% | 평균수익률 {perf.get('avg_return',0):+.2f}%
손익비 {perf.get('profit_factor',0):.2f} | 최대손실 {perf.get('max_loss',0):.2f}%

위 데이터를 바탕으로 향후 6~12개월 종합 투자관를 수립하라.
수치 근거 없는 판단 금지. 모든 방향 예측에 확률 명시."""

    # ── 투자관 생성 ───────────────────────────────────────────────
    try:
        full_report = chat(_THESIS_SYSTEM, context, max_tokens=4000)
    except Exception as e:
        logger.error("[투자관에이전트] LLM 실패: %s", e)
        send_error_alert(f"월간 투자관 생성 실패: {e}")
        return

    # ── CEO 압축 요약 생성 ────────────────────────────────────────
    try:
        ceo_summary = chat(_CEO_INJECT_SYSTEM, full_report[:3000], max_tokens=500)
    except Exception as e:
        logger.warning("[투자관에이전트] CEO 요약 생성 실패: %s", e)
        ceo_summary = full_report[:500]

    # ── 구조화 필드 파싱 ─────────────────────────────────────────
    parsed = _parse_structured_fields(full_report)

    # ── DB 저장 ─────────────────────────────────────────────────
    try:
        save_thesis(
            date=now.strftime("%Y-%m-%d"),
            cycle_stage=parsed["cycle_stage"],
            macro_regime=parsed["macro_regime"],
            outlook_6m=parsed["outlook_6m"],
            outlook_12m=parsed["outlook_12m"],
            sector_overweight=parsed["sector_overweight"],
            sector_underweight=parsed["sector_underweight"],
            conviction_ideas=parsed["conviction_ideas"],
            bull_scenario=parsed["bull_scenario"],
            base_scenario=parsed["base_scenario"],
            bear_scenario=parsed["bear_scenario"],
            invalidation=parsed["invalidation"],
            full_report=full_report,
            ceo_summary=ceo_summary,
        )
    except Exception as e:
        logger.warning("[투자관에이전트] DB 저장 실패: %s", e)

    # ── 텔레그램 발송 ────────────────────────────────────────────
    header = (
        f"📜 *월간 투자관* ({now.strftime('%Y년 %m월')})\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"경기 사이클·6~12개월 전망·자산배분·확신 아이디어\n\n"
    )
    send_message(header + full_report)
    logger.info("[투자관에이전트] 완료")


def run(state: dict = None) -> dict:
    run_thesis()
    return state or {}
