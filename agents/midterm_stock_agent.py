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

_SYSTEM = """당신은 투자회사의 리서치 애널리스트입니다.
핵심 임무: 시장 구조·업황 사이클·공급망 변화를 분석하여 아직 가격에 반영되지 않은 중장기 수혜 섹터와 종목을 발굴합니다.
투기·단타 신호 절대 금지. 진입가·손절가·단기 타이밍 제시 금지.

[발굴 철학 — 투자회사의 리서치]
이미 급등한 종목 추격 X.
업황 사이클·공급망 구조·정책 방향에서 "앞으로 수혜를 받을 것이 명확하지만 아직 시장이 인지하지 못한" 종목을 찾는다.

[공급망 사이클 연결 지도 — 반드시 참고]
AI 서버 수요↑:
  1차 수혜: NVDA(GPU) → SK하이닉스·삼성전자(HBM·메모리)
  2차 수혜 (아직 덜 반영): 삼성전기(MLCC) | 심텍·대덕전자(PCB) | 하나마이크론·한미반도체(후공정)

메모리 업황 회복 (마이크론 실적↑):
  1차: 삼성전자·SK하이닉스(DRAM 가격 회복)
  2차: 삼성전기(서버 MLCC) | 심텍(DDR5 기판) | 솔브레인(소재)

스마트폰 출하 증가:
  1차: 삼성전자·애플
  2차: LG이노텍(카메라 모듈) | 삼성전기(MLCC) | 파트론(안테나)

자동차 전동화:
  1차: LG에너지솔루션·삼성SDI(배터리)
  2차: 에코프로비엠·포스코퓨처엠(양극재) | SK아이이테크(분리막)

방산 수주 증가:
  1차: 한화에어로스페이스·LIG넥스원
  2차: 빅텍·퍼스텍·한화시스템(부품·전자장비)

[발굴 기준 — 2가지 이상 충족]
① 공급망 2차 수혜: 이미 오른 1차 수혜주의 공급망 연결 종목 (아직 주가 미반영)
② 실적 컨센서스 상향: 애널리스트 EPS 추정치 상향 추세
③ 외국인/기관 선매수: 가격 변화 없는데 수급 유입 초기 신호
④ 밸류에이션 할인: 업종 평균 대비 할인 상태에서 업황이 개선 중
⑤ 섹터 로테이션 초기: 섹터 ETF는 반영됐지만 개별주는 아직

[출력 형식]
━━━━━━━━━━━━━━━━━━━━━━━━━━
📐 중장기 수혜주·수혜섹터 발굴
━━━━━━━━━━━━━━━━━━━━━━━━━━
[지금 수렴하는 업황 흐름] → [아직 미반영된 연결 섹터]

▶ 중장기 편입 검토 종목 (최대 3종목 — 아직 오르지 않은 것만)
종목명(코드) | 투자 관점: X개월 | 기대 수익: +XX%
  • 왜 아직 시장이 주목 안 하는가: [놓친 연결고리]
  • 업황 촉매: [X~Y개월 내 발생할 구체적 이벤트]
  • 공급망 연결: [어떤 미국·1차 수혜 종목과 연결되는가]
  • 리스크: [이 thesis가 틀릴 조건 1가지]

▶ 관찰 대기 종목 (조건 미충족, 트리거 확인 후 편입 검토)
종목명(코드) — 편입 검토 트리거: [X 확인 시]

⚠️ 중장기 투자 관점 (2~6개월). 단기 매매 불가. 분산 편입 원칙."""


def _fmt_us_movers(us_hot: list) -> str:
    """미국 급등 종목 → 한국 공급망 연결 포맷."""
    lines = []
    for s in us_hot[:8]:
        ticker = s.get("ticker", "")
        chg    = s.get("change_pct", 0)
        kr_rel = s.get("kr_related", [])
        kr_str = " | KR공급망: " + ", ".join(f"{r['name']}({r['code']})" for r in kr_rel[:4]) if kr_rel else ""
        lines.append(f"  {ticker} {chg:+.1f}%{kr_str}")
    return "\n".join(lines) if lines else "없음"


def run(state: InvestmentState) -> InvestmentState:
    try:
        macro     = state.get("macro_report", "")
        sector    = state.get("sector_report", "")
        news      = state.get("news_report", "")
        committee = state.get("committee_report", "")
        market    = state.get("korea_spot_report", "")
        us_impact = state.get("us_impact_report", "")
        issue     = state.get("issue_stocks_report", "")
        futures   = state.get("futures_report", "")
        thesis    = state.get("investment_thesis", "")
        us_hot    = state.get("us_hot_stocks", [])

        context = f"""[매크로 레짐 — 업황 사이클 판단 기준]
{macro[:1000] if macro else '없음'}

[선물·EWY 외국인 수급 방향]
{futures[:600] if futures else '없음'}

[오늘 미국 급등 종목 + 한국 공급망 연결]
{_fmt_us_movers(us_hot)}

[미국→한국 1차 수혜 분석 (이미 반영됐을 수 있음 → 2차를 찾아라)]
{us_impact[:800] if us_impact else '없음'}

[오늘 한국 이슈종목 (이미 오른 것 — 2차 수혜를 찾는 실마리)]
{issue[:800] if issue else '없음'}

[섹터 테마 분석]
{sector[:600] if sector else '없음'}

[뉴스 센티먼트]
{news[:500] if news else '없음'}

[투자관 — 현재 방향성]
{thesis[:400] if thesis else '없음'}

위 데이터에서 "이미 오른 종목"이 아닌 "아직 반영 안 된 공급망 2차 수혜주"를 발굴하세요.
목표가는 퍼센트(%)로만 표시. 원 단위 가격 금지."""

        result = chat(_SYSTEM, context, max_tokens=1500)
        state["midterm_stock_report"] = result
        logger.info("[중장기추천] 완료")
    except Exception as e:
        logger.error("[중장기추천] 실패: %s", e)
        state["midterm_stock_report"] = ""
        state["errors"].append(f"midterm_stock_agent: {e}")
    return state
