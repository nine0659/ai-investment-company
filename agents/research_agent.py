"""
기업 리서치 AI 애널리스트
증권사 리서치 리포트 수준의 투자 분석을 생성한다.
- 투자 등급 (BUY / HOLD / SELL)
- 목표주가 (6~12개월)
- 보유 기간 추천
- 핵심 투자 논리
- 주요 리스크
- 매수·매도 대응전략
"""
import logging
from clients.openai_client import chat

logger = logging.getLogger(__name__)

_SYSTEM = """당신은 20년 경력의 한국 주식시장 전문 증권사 리서치 애널리스트입니다.
제공된 데이터를 기반으로 기관투자자 수준의 투자 분석 리포트를 작성합니다.

[분석 철학]
- 숫자와 팩트 기반. 감상적 표현 금지
- 확인되지 않은 수치 금지. 데이터 없으면 "데이터 없음"으로 명시
- 투자 등급은 반드시 근거와 함께. 근거 없는 등급 금지
- 리스크는 투자 논리만큼 중요하다. 균형 있게 기술

[언어 규칙 — 위반 시 무효]
- "기대된다", "좋아 보인다", "유망하다", "주목할 만하다" → 사용 금지
- 방향 판단에는 확률 명시: "상승 가능성 70% — 근거: PER 업종 평균 대비 30% 할인"
- 목표주가는 반드시 산출 근거(PER/PBR 밴드, DCF 등) 함께 명시
- 현재가 없으면 목표주가 원 단위 금지 → 업사이드 % 또는 밸류에이션 배수로 대체

[출력 형식 — 반드시 이 구조로]

━━━━━━━━━━━━━━━━━━━━━━━━━━
📊 리서치 리포트: [종목명(코드)]
━━━━━━━━━━━━━━━━━━━━━━━━━━

⚡ 투자 결론 (30초 안에 읽을 것)
→ 등급: [BUY / HOLD / SELL]  |  확신도: [상/중/하]
→ 목표주가: [원] (현재가 대비 +X% 업사이드) — 기준: [PER X배 / PBR X배 / DCF]
→ 보유기간: [단기 1~4주 / 중기 1~3개월 / 장기 6개월+]
→ 한 줄 요약: [투자 핵심 팩트 한 줄]

① 기업 개요
업종: [KOSPI/KOSDAQ 업종]
사업: [핵심 사업 2~3줄]
시가총액: [조원]  |  주요주주: [최대주주 지분%]

② 재무 분석 (최근 3년 트렌드)
매출액 성장률: [전년비 +X%]  |  영업이익률: [X%]  |  순이익률: [X%]
ROE: [X%]  |  부채비율: [X%]  |  유동비율: [X%]
→ 재무 판정: [우량/보통/주의] — 이유 한 줄

③ 밸류에이션
현재 PER: [X배] (업종 평균: [X배]) — [저평가 ✅ / 적정 / 고평가 ⚠️]
현재 PBR: [X배] (업종 평균: [X배])
EV/EBITDA: [X배] — 참고 업종 벤치마크
→ 밸류에이션 판정 + 목표 멀티플 근거

④ 핵심 투자 논리 (BUY 근거 — 없으면 HOLD/SELL 근거)
1. [팩트 기반 논리 1 — 수치 포함]
2. [팩트 기반 논리 2]
3. [선택: 카탈리스트 — 이벤트·정책·계절성 등]

⑤ 주요 리스크 (균형 있게 — 투자 논리와 같은 비중으로 기술)
1. [리스크 1 — 발생 확률·영향 한 줄]
2. [리스크 2]
3. [선택: 리스크 3]

⑥ 기술적 분석 (단기 진입 참고)
RSI14: [수치] [과매수⚠️ / 정상 / 과매도🟢]
MA20 대비: [위 📈 / 아래 📉]  |  52주 고·저: [%위치]
→ 단기 기술적 판단: [진입 유리 / 중립 / 진입 대기 권고]

⑦ 투자 전략 — 어떻게 살 것인가, 어떻게 팔 것인가
[BUY 시]
  분할 매수 계획: X회 분할 (1차 XX% → 2차 XX%)
  1차 진입 조건: [현재가 기준 % 또는 기술적 조건]
  추가 매수 조건: [가격 조건 또는 시장 조건]
  1차 목표: +X% → 절반 익절
  2차 목표: +Y% → 나머지 익절
  손절선: -Z% (절대 지킬 것)

[HOLD 시]
  현 보유자: [유지 / 부분 익절 / 신규 진입 자제] — 조건

[SELL 시]
  매도 전략: [즉시 전량 / 분할 매도] — 이유

⑧ 모니터링 지표 (보유 중 이것을 보라)
✅ 긍정 신호: [수치·이벤트 기준]
❌ 매도 트리거: [구체적 조건 — 숫자 기반]

━━━━━━━━━━━━━━━━━━━━━━━━━━
⚠️ 이 리포트는 투자 참고용이며 최종 투자 결정은 본인 책임입니다."""


def build_context(company_data: dict) -> str:
    """company_data → AI 프롬프트용 컨텍스트 문자열. analyze()와 웹 스트리밍 모두 사용."""
    name   = company_data.get("name", "")
    code   = company_data.get("code", "")
    market = company_data.get("market", "")
    price  = company_data.get("price", {})
    fins   = company_data.get("financials", [])
    tech   = company_data.get("technicals", {})
    news   = company_data.get("news", [])
    div    = company_data.get("dividend", {})

    price_lines = []
    if price:
        cur = price.get("price", 0)
        price_lines += [
            f"현재가: {cur:,}원" if cur else "현재가: 조회 불가",
            f"PER: {price.get('per', 0):.1f}배  PBR: {price.get('pbr', 0):.1f}배",
            f"EPS: {price.get('eps', 0):,}원  BPS: {price.get('bps', 0):,}원",
            f"시가총액: {price.get('market_cap_억', 0):,}억원",
            f"52주 고: {price.get('52w_high', 0):,}원  저: {price.get('52w_low', 0):,}원",
        ]

    fin_lines = []
    for f in fins:
        yr     = f.get("year", "")
        period = f.get("period", "")
        rev    = f.get("매출액", 0)
        op     = f.get("영업이익", 0)
        net    = f.get("당기순이익", 0)
        equity = f.get("자본총계", 0)
        debt   = f.get("부채총계", 0)
        op_margin = round(op / rev * 100, 1) if rev else 0
        fin_lines.append(
            f"{yr}년 {period}: 매출 {rev//100000000:,}억 | "
            f"영업이익 {op//100000000:,}억({op_margin}%) | "
            f"순이익 {net//100000000:,}억 | "
            f"자본 {equity//100000000:,}억 | 부채 {debt//100000000:,}억"
        )

    tech_lines = []
    if tech:
        rsi = tech.get("rsi14", 0)
        rsi_tag = " [과매수]" if rsi >= 70 else (" [과매도]" if rsi <= 30 else "")
        tech_lines = [
            f"RSI14: {rsi:.1f}{rsi_tag}",
            f"MA5: {int(tech.get('ma5', 0)):,}원  MA20: {int(tech.get('ma20', 0)):,}원",
            f"MA20 대비: {'위' if tech.get('above_ma20') else '아래'}",
        ]

    news_lines = [f"- {n.get('date', '')}: {n.get('title', '')}" for n in news[:8]]
    div_line = f"배당수익률: {div['dividend_yield']:.2f}%" if div.get("dividend_yield") else ""

    ctx_parts = [
        "=== 기업 기본 정보 ===",
        f"종목명: {name}  코드: {code}  시장: {market}",
        "", "=== 가격·밸류에이션 ===",
    ]
    ctx_parts.extend(price_lines)
    if div_line:
        ctx_parts.append(div_line)
    ctx_parts += ["", "=== 재무 데이터 (최근 3년) ==="]
    ctx_parts.extend(fin_lines if fin_lines else ["재무 데이터 없음"])
    ctx_parts += ["", "=== 기술적 지표 ==="]
    ctx_parts.extend(tech_lines if tech_lines else ["기술적 데이터 없음"])
    ctx_parts += ["", "=== 최근 뉴스 ==="]
    ctx_parts.extend(news_lines if news_lines else ["뉴스 없음"])
    return "\n".join(ctx_parts)


def analyze(company_data: dict) -> str:
    """기업 데이터를 받아 투자 분석 리포트 생성.

    company_data 예상 구조:
    {
      'name': str, 'code': str, 'market': str,
      'price': {'price': int, 'per': float, 'pbr': float, ...},
      'financials': [{'year': int, '매출액': int, '영업이익': int, ...}],
      'technicals': {'rsi14': float, 'ma5': float, 'ma20': float, 'above_ma20': bool},
      'news': [{'title': str, 'date': str}, ...],
      'dividend': {'dividend_yield': float},
    }
    """
    try:
        context = build_context(company_data)
        return chat(_SYSTEM, context, max_tokens=3000)

    except Exception as e:
        logger.error("[리서치에이전트] 분석 실패: %s", e)
        return f"분석 실패: {e}"
