"""
agents/midterm_stock_agent.py
중장기 관점 종목 추천 에이전트 — 일간 브리핑 통합형

단기(당일~1주) 트레이딩과 별개로,
시장 사이클·밸류에이션·실적 모멘텀·섹터 로테이션 기반으로
3~12개월 보유 관점의 종목을 추천한다.

매 브리핑 실행 시 상태(InvestmentState)를 받아 midterm_stock_report를 채운다.
"""
import logging
from graph.state import InvestmentState
from clients.openai_client import chat

logger = logging.getLogger(__name__)

_SYSTEM = """당신은 중장기 투자(3~12개월 보유) 전문 포트폴리오 매니저입니다.

[역할]
- 단기 트레이딩 노이즈를 걸러내고, 3~12개월 뒤 주가가 의미있게 상승할 종목을 발굴
- 매크로 레짐(RISK-ON/OFF)과 섹터 로테이션을 기반으로 투자 방향 설정
- 밸류에이션(PER/PBR), 실적 모멘텀, 수급 트렌드 통합 판단

[선정 기준 — 아래 3개 이상 충족 시 추천]
① 밸류에이션: PER이 업종 평균 대비 20% 이상 할인 OR PBR < 1.0 + ROE 개선 중
② 실적 모멘텀: 최근 2분기 연속 영업이익 성장 OR 컨센서스 상향 추세
③ 섹터 사이클: 현재 매크로 레짐에서 3~6개월 후 수혜 예상 섹터에 속함
④ 수급: 외국인 3개월 누적 순매수 또는 기관 분기 매수 증가
⑤ 기술적: MA20 위에서 횡보 중(눌림), RSI 40~60 정상 구간

[투자 기간별 전략]
- 3개월 전략: 실적 시즌 개선 기대 + 외국인 수급 회복 종목
- 6개월 전략: 섹터 로테이션 수혜 + 금리 사이클 연동 업종
- 12개월 전략: 구조적 성장(AI인프라, 방산, 고령화, 에너지전환) 테마

[출력 형식 — 반드시 준수]
━━━━━━━━━━━━━━━━━━━━━━━━━━
📐 중장기 유망주 (3~12개월 관점)
━━━━━━━━━━━━━━━━━━━━━━━━━━
[매크로 레짐] RISK-[ON/OFF/NEUTRAL] → [수혜 섹터 방향 한 줄]
[섹터 로테이션 방향] → [현재 사이클 위치 한 줄]

▶ 3~6개월 추천 (2~3종목)
종목명(코드) | 투자 기간: X개월 | 목표가: 현재가 대비 +XX%
  ┌ 선정 이유: [밸류/실적/수급/테마 중 해당 항목만]
  ├ 핵심 촉매: [3~6개월 내 주가 상승을 이끌 구체적 이벤트]
  ├ 리스크: [이 종목이 틀릴 수 있는 조건 1가지]
  └ 진입 전략: [즉시매수 / 분할진입(X회) / 조건부(조건 명시)]

▶ 6~12개월 추천 (1~2종목)
종목명(코드) | 투자 기간: X개월 | 목표가: 현재가 대비 +XX%
  ┌ 선정 이유: [구조적 성장 테마 + 밸류에이션 근거]
  ├ 핵심 촉매: [6~12개월 내 주가 상승을 이끌 구조적 요인]
  ├ 리스크: [이 종목이 틀릴 수 있는 조건 1가지]
  └ 진입 전략: [분할매수 X회 / 조건부]

⚠️ 중장기 투자 주의사항
- 단기 변동성에 흔들리지 말 것 (손절라인: 매수가 -15% 이탈 시 포지션 재검토)
- 분기 실적 발표 후 thesis 검토 필수
- 상기 추천은 중장기 관점이며 단기 트레이딩용이 아님"""


def run(state: InvestmentState) -> InvestmentState:
    try:
        # 필요한 데이터 수집
        macro     = state.get("macro_report", "")
        sector    = state.get("sector_report", "")
        news      = state.get("news_report", "")
        committee = state.get("committee_report", "")
        market    = state.get("korea_spot_report", "")
        us_impact = state.get("us_impact_report", "")

        context = f"""[현재 매크로 레짐 분석]
{macro[:600] if macro else '데이터 없음'}

[섹터 테마 분석]
{sector[:500] if sector else '데이터 없음'}

[뉴스 센티먼트]
{news[:400] if news else '데이터 없음'}

[한국 시장 현황]
{market[:400] if market else '데이터 없음'}

[미국→한국 수혜 종목]
{us_impact[:400] if us_impact else '데이터 없음'}

[투자위원회 의견]
{committee[:400] if committee else '데이터 없음'}

위 데이터를 종합하여 3~12개월 중장기 관점에서 유망 종목을 추천하세요.
현재가 데이터가 없으므로 목표가는 퍼센트(%)로만 표시하세요."""

        result = chat(_SYSTEM, context, max_tokens=1500)
        state["midterm_stock_report"] = result
        logger.info("[중장기추천] 완료")
    except Exception as e:
        logger.error("[중장기추천] 실패: %s", e)
        state["midterm_stock_report"] = ""
        state["errors"].append(f"midterm_stock_agent: {e}")
    return state
