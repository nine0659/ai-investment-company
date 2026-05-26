import logging
from datetime import datetime, time as _time
from graph.state import InvestmentState
from clients.openai_client import chat_ceo
from clients.us_stock_client import format_us_impact_for_prompt
from clients.kis_client import KISClient
from clients.market_data_client import fetch_kr_stock_technicals
from services.recommendation_service import (
    parse_recommendations, save_recommendations,
    update_close_prices, format_returns_for_report, get_performance_stats,
)
from config.settings import RUN_TYPE_PRE, RUN_TYPE_INTRA1, RUN_TYPE_INTRA2, RUN_TYPE_CLOSE, TZ

_MARKET_OPEN  = _time(9, 0)
_MARKET_CLOSE = _time(15, 35)

logger = logging.getLogger(__name__)


def _fetch_price_context(candidates: list[dict], kis: KISClient) -> str:
    """상위 후보 종목의 현재가 기반 진입/손절/목표가를 미리 계산해 텍스트로 반환.
    AI가 임의 수치를 만들지 않도록 실제 값을 프롬프트에 주입하기 위한 함수.
    candidates의 market 필드("KOSPI"/"KOSDAQ")를 사용해 정확한 시장 코드로 조회.
    """
    now_kst = datetime.now(TZ).time()
    is_market_hours = _MARKET_OPEN <= now_kst <= _MARKET_CLOSE
    price_label = "현재가" if is_market_hours else "전일 종가(참고)"

    lines = []
    for c in candidates[:7]:
        code = c.get("code", "")
        name = c.get("name", code)
        if not code:
            continue
        # market=None으로 J→Q 자동 재시도 — 잘못된 시장 코드로 엉뚱한 주가 반환 방지
        try:
            data = kis.get_stock_price(code, market=None)
            price = data.get("price", 0)
            if not price:
                logger.debug("현재가 0 — 스킵 (%s)", code)
                continue
            # 즉시진입 (현재가/전일 종가 기준)
            entry1  = price
            stop1   = round(price * 0.97)
            target1 = round(price * 1.06)
            # 눌림진입 (-1% 기준)
            entry2  = round(price * 0.99)
            stop2   = round(entry2 * 0.97)
            target2 = round(entry2 * 1.06)

            # 기술적 지표 (yfinance — 실패해도 진행)
            market_sfx = c.get("market", "KOSPI")
            yfin_sym   = f"{code}.{'KS' if market_sfx == 'KOSPI' else 'KQ'}"
            tech = fetch_kr_stock_technicals(yfin_sym)
            tech_line = ""
            if tech:
                rsi_flag = " ⚠️과매수" if tech["rsi14"] >= 70 else (" 🟢과매도권" if tech["rsi14"] <= 30 else "")
                ma_flag  = "📈MA20 위" if tech["above_ma20"] else "📉MA20 아래"
                tech_line = (
                    f"\n  기술: RSI14={tech['rsi14']}{rsi_flag} | MA5={int(tech['ma5']):,} | "
                    f"MA20={int(tech['ma20']):,} | {ma_flag}"
                )

            lines.append(
                f"{name}({code}) | {price_label} {price:,}원"
                + ("" if is_market_hours else " ⚠️장 전이므로 시초가 확인 후 조정 필요")
                + f"\n  즉시진입: 1차 {entry1:,}원 | 손절 {stop1:,}원 | 목표 {target1:,}원\n"
                f"  눌림진입(-1%): 1차 {entry2:,}원 | 손절 {stop2:,}원 | 목표 {target2:,}원"
                + tech_line
            )
        except Exception as e:
            logger.debug("현재가 조회 실패 (%s): %s", code, e)
    return "\n".join(lines) if lines else "현재가 조회 불가 (장 마감 후 또는 API 오류)"


_COMMON_HEADER = """당신은 20년 경력의 한국·미국 주식시장 전문 운용역입니다.
핵심 철학: "미국에서 일어난 일은 한국에서도 일어난다. 단 타이밍과 크기는 다르다."

[브리핑 작성 철학]
- 투자자가 브리핑을 읽고 30초 안에 "오늘 뭘 해야 하는가"를 알 수 있어야 한다
- 결론 먼저 → 근거 → 행동 순서로 작성한다
- 확신 없으면 "관망"을 선언하고 구체적 조건을 제시한다. 애매한 표현은 없다
- 군중이 흥분하면 경계하고, 군중이 두려워하면 기회를 찾는다

[언어 규칙 — 위반 시 리포트 무효]
- "조심하세요", "신중히 접근", "면밀히 모니터링", "주목할 만하다", "좋아 보인다" → 사용 금지
- 방향 판단에는 반드시 확률 명시: "상승할 것 같다" → "상승확률 70% — 근거: SOX +2%"
- 현재가 데이터 없으면 원 단위 가격 수치 절대 기재 금지
- 근거 없는 숫자, 임의로 만든 수치 기재 금지

[포지션 사이징 — 종목마다 반드시 명시]
- 확신도 상: 투자 가능 자금의 3~5% (RISK-ON 환경에서만)
- 확신도 중: 투자 가능 자금의 1~3%
- 확신도 하: 투자 가능 자금의 0.5~1%
- RISK-OFF 환경: 위 기준에서 50% 축소
- 이벤트 리스크 HIGH: 추가 50% 축소 (FOMC·CPI·쿼드러플위칭 등)
- 하루 전체 신규 포지션 합산 최대 10%

[매크로 레짐 → 섹터·포지션 방향]
- RISK-ON: 반도체·AI·방산·성장주 공략 가능
- RISK-OFF: 방어주·배당주만, 신규 포지션 최소화
- NEUTRAL: 확신도 '상' 종목만, 소규모

[출력 규칙] 텔레그램 한국어 텍스트만 출력. 섹션 번호(①②③...)와 구분선 유지.
이 지침 텍스트 자체는 절대 출력에 포함하지 말 것."""

# ── 종목 추천 블록: 가격 데이터 있을 때 ────────────────────────────
_STOCK_BLOCK_WITH_PRICE = """종목명(6자리코드)
  ┌ 근거: [미국 ETF 등락 / 수급 수치 / 뉴스 팩트 — 구체적 수치 반드시 포함]
  ├ 확신도: [상/중/하]  |  상승확률: XX%  |  이유 한 줄
  ├ 포지션: 투자금의 X% (매크로·이벤트 리스크 반영)
  ├ 진입①(즉시): [실시간 가격 데이터 그대로]  →  손절: [수치]  →  목표: [수치]
  ├ 진입②(눌림): [실시간 가격 데이터 그대로]  →  손절: [수치]  →  목표: [수치]
  ├ 기술: RSI=[수치] [과매수경고/과매도기회/정상] | MA20 [위/아래]
  └ 시초가: 갭업+2%↑→추격금지 / 갭업+1~2%→절반진입 / 보합→계획진입 / 갭하락→양봉후진입"""

# ── 종목 추천 블록: 가격 데이터 없을 때 ────────────────────────────
_STOCK_BLOCK_NO_PRICE = """종목명(6자리코드)
  ┌ 근거: [미국 ETF 등락 / 수급 수치 / 뉴스 팩트 — 구체적 수치 반드시 포함]
  ├ 확신도: [상/중/하]  |  이유 한 줄
  ├ 포지션: 투자금의 X% (매크로·이벤트 리스크 반영)
  🚫 현재가 없음 — 아래는 조건으로만. 원 단위 숫자 절대 금지.
  ├ 진입 조건: [예: 시초가 양봉 확인 후 / 외국인 순매수 전환 시]
  ├ 손절 조건: [예: 전일 저점 이탈 / 시초가 음봉 굳어질 때]
  └ 목표 조건: [예: 전 고점 저항 도달 시 절반 익절]"""


def _build_prompt_pre(has_price: bool) -> str:
    stock_block = _STOCK_BLOCK_WITH_PRICE if has_price else _STOCK_BLOCK_NO_PRICE
    price_mode  = "" if has_price else "🚫 현재가 없음 — 종목 추천에 원 단위 숫자 절대 사용 금지\n"
    return f"""{_COMMON_HEADER}

{price_mode}━━━━━━━━━━━━━━━━━━━━━━━━━━
📡 장전 AI 브리핑
━━━━━━━━━━━━━━━━━━━━━━━━━━

⚡ 오늘 결론  (한 줄 — 가장 먼저 읽을 것)
→ [매수공격 / 선별매수 / 관망 / 현금보유] | KOSPI 상승확률 XX% / 하락확률 YY%
관망 선언 시: "오늘 관망 — 진입 재개 조건: [구체적 조건]" 으로 작성

① 시장 방향 근거  (논리 사슬 — 1→2→3→4 순서로 읽을 것)
1. 야간선물: KOSPI200미니선물 [수치] → 갭[업/다운] [+/-X%] 예상  |  신뢰도 [상/중/하]
   (수집 실패 시: "야간선물 미수집 → 미국선물 기반 추정" 명시 후 계속)
2. 미국 확인: S&P500 [수치]  SOX [수치]  NASDAQ [수치]
   → 야간선물과 [방향일치/불일치] — [일치 시: 신뢰도 상] [불일치 시: 확률 XX%로 하향]
3. 매크로 환경: RISK-[ON/OFF/NEUTRAL]  |  이벤트 리스크: [높음/중간/낮음]
   → 포지션 한도: 투자금의 최대 XX%
4. 수급 선행: EWY [수치%] / EEM [수치%] → 내일 외국인 [유입/유출] 예상
   → 오늘 외국인 [매수우위/매도우위] 가능성 XX%

② 오늘의 핵심 재료  (딱 1개 — 사실만, 추측 금지)
→ [구체적 사실 한 줄]
→ 파급 섹터: [섹터명]  |  한국 직접 수혜 확률: XX%

③ 오늘 매매 지시서  (확신 있는 1~2개. 없으면 "③ 오늘은 관망" 선언)
{stock_block}

④ 오늘 하면 안 되는 것  (데이터 근거 1개 — 추상적 경고 금지)
❌ [구체적 행동 금지] — 이유: [수치/데이터 근거]

⑤ 레이더 (오늘은 아니지만 내일 이후 주목할 종목 2~3개)
👀 [종목(코드)] — [ETF 연동 / 수급 근거]  |  수혜 확률 XX%

⑥ 이벤트 리스크 경고  (이벤트 리스크팀 리포트 기반 — 없으면 이 섹션 생략)
⚠️ [이벤트명]  [예상 날짜]  →  [포지션 조정 권고]

⑦ 글로벌 전문가 서사 & 시장 심리  (인텔리전스팀 기반 — 없으면 이 섹션 생략)
🌐 지배 서사: [지금 글로벌 전문가들의 시각 핵심 한 줄]
🐂 강세 논리: [핵심 1가지]  /  🐻 약세 논리: [핵심 1가지]
→ 오늘 KOSPI 함의: [이 서사가 오늘 포지션에 미치는 영향 한 줄]

⑧ 시초가 체크리스트
□ 갭업 +2% 이상  → 추격 금지, 눌림 대기
□ 갭업 +1~2%    → 계획 물량 절반만 진입
□ 보합 출발     → 계획대로 전량 진입
□ 갭하락        → 양봉 전환 확인 후 진입 / 미전환 시 당일 포기
━━━━━━━━━━━━━━━━━━━━━━━━━━"""


def _build_prompt_close(has_price: bool) -> str:
    stock_block = _STOCK_BLOCK_WITH_PRICE if has_price else _STOCK_BLOCK_NO_PRICE
    price_mode  = "" if has_price else "🚫 현재가 없음 — 종목 추천에 원 단위 숫자 절대 사용 금지\n"
    return f"""{_COMMON_HEADER}

{price_mode}━━━━━━━━━━━━━━━━━━━━━━━━━━
📊 장마감 AI 복기
━━━━━━━━━━━━━━━━━━━━━━━━━━

⚡ 오늘 결론  (한 줄)
→ [오늘 장 성격 한 마디]  |  내일 기조: [강세 / 약세 / 관망]

① 오늘 장 결산  (반드시 [한국 시장 실제 움직임] 데이터 기반)
실제 주도 섹터: [섹터]  |  장전 예상과 [일치 ✅ / 불일치 ❌]
디커플링 여부: [미국 방향 대비] — 이유: [한 줄]
오늘 작동한 재료: [DART / 빅피겨 / 수급 / 기타]

② 오늘 추천 성과
[종목(코드)]:  [+/-X%]  →  [성공 ✅ / 실패 ❌]  |  원인: [한 줄]
(오늘 추천 없으면 "오늘 추천 없음" 한 줄로만)

③ 오늘의 교훈  (구체적 사례 기반 — "미국과 달리 OO가 강했던 이유는..." 형태)
→ [교훈 한 줄]

④ 야간선물 가이드  (오늘 밤 행동 기준)
관찰 지표: KOSPI200 야간선물  +  [오늘 밤 미국 핵심 발표 1개 / 없으면 "주요 발표 없음"]
✅ 상승 유지 조건: [구체적 수준 또는 조건]  →  내일 갭업 신뢰도 상
❌ 하락 전환 경계: [구체적 수준 또는 조건]  →  내일 전략 수정 트리거

⑤ 내일 시나리오  (확률 필수 — 확률 없는 방향 판단 금지)
A [강세 시나리오]:  상승확률 XX%  |  주도 섹터: [OO]  |  포지션: [공격/유지]
B [약세 시나리오]:  하락확률 YY%  |  주의 섹터: [OO]  |  포지션: [축소/현금]

⑥ 내일 매매 지시서  (오늘 실제 거래대금·수급 확인된 종목 우선)
오늘 하락·소외됐다는 이유만으로 "저가 매수 기회"로 추천 금지.
{stock_block}

⑦ 내일 하면 안 되는 것
❌ [구체적 행동 금지]  —  이유: [오늘 데이터 근거]

⑧ 이벤트 리스크 경고  (이벤트 리스크팀 리포트 기반 — 없으면 이 섹션 생략)
⚠️ [이벤트명]  [예상 날짜]  →  [포지션 조정 권고]

⑨ 글로벌 전문가 서사 변화  (인텔리전스팀 기반 — 없으면 이 섹션 생략)
🌐 오늘 지배 서사: [글로벌 전문가 논의 중심]
⚡ 컨센서스 변화: [강화 / 약화 / 전환 — 이유 한 줄]
→ 내일 전략 함의: [서사 변화가 내일 포지션에 미치는 영향]
━━━━━━━━━━━━━━━━━━━━━━━━━━"""


_PROMPT_INTRA1 = f"""{_COMMON_HEADER}

━━━━━━━━━━━━━━━━━━━━━━━━━━
🕙 장중 1차 점검  (오전 10:00)
━━━━━━━━━━━━━━━━━━━━━━━━━━

⚡ 지금 결론  (한 줄)
→ 장전 전략 [유효 ✅ / 수정 필요 ⚠️]  |  오후 상승확률 XX% / 하락확률 YY%

① 장전 전략 검증
장전 예상: [방향 / 주도 섹터]
현재 실제: [실시간 KOSPI·KOSDAQ 수치] — [주도 섹터]
판정: [일치 ✅ 이유 한 줄 / 불일치 ❌ 원인 한 줄]

② 지금 당장 해야 할 것 / 하면 안 되는 것
✅ [행동]  —  조건: [수치 기반]
❌ [금지 행동]  —  이유: [수치 기반]

③ 장중 새 이벤트  (장전 이후 발생한 중요 사실만 — 없으면 이 섹션 생략)
→ [사실 한 줄]  →  영향 종목: [종목명]

④ 오후 핵심 관전 포인트
미국 프리마켓: S&P선물 [수치] → 오후 한국 [영향 방향]
경계 임계값: [지표] [수준] 이탈 시 → [즉시 취할 행동]
오후 주목 시간대: [시간] — [이유]
━━━━━━━━━━━━━━━━━━━━━━━━━━"""

_PROMPT_INTRA2 = f"""{_COMMON_HEADER}

━━━━━━━━━━━━━━━━━━━━━━━━━━
🕐 장중 2차 점검  (오후 1:00)
━━━━━━━━━━━━━━━━━━━━━━━━━━

⚡ 지금 결론  (한 줄)
→ 오후 방향: [강세 유지 / 박스권 / 약세 전환]  |  오후 상승확률 XX% / 하락확률 YY%

① 오후 방향 판단 근거
미국 프리마켓: S&P500 [수치]  NASDAQ [수치]
오전 주도 섹터의 오후 지속 확률: XX%
수급 현황: 외국인 [순매수/순매도] — 기관 [순매수/순매도]

② 포지션 관리 지침  (구체적 조건 필수 — 여러 개 해당 시 모두 명시)
✅ 홀드:  [유지 조건 — 수치 기반]
📤 익절:  [익절 비중 XX%]  →  트리거: [조건]
🛑 손절:  트리거: [조건]
📥 추가매수:  [조건]  →  비중: XX%

③ 마감 전 주의사항  (수치 기반 — 추상적 표현 금지)
⚠️ [시간대]  [지표] [수준] 돌파/이탈 시  →  [즉시 취할 행동]
  예: 오후 2시 이후 외국인 순매도 전환 시 → 보유 물량 30% 즉시 축소
━━━━━━━━━━━━━━━━━━━━━━━━━━"""


def run(state: InvestmentState) -> InvestmentState:
    try:
        run_type = state.get("run_type", RUN_TYPE_PRE)
        now  = datetime.now(TZ)
        date = state.get("date", now.strftime("%Y-%m-%d"))

        candidates_text = "\n".join(
            f"- {c.get('name', c.get('code', ''))}: {c.get('change_pct', 0):+.1f}% "
            f"(점수 {c.get('score', 0)})"
            + (" ⚠️미국섹터추정" if c.get("source") == "US_fallback" else "")
            for c in state.get("candidates", [])[:5]
        ) or "후보 없음"

        event_level = state.get("event_risk_level", "중간")
        context_parts = [
            f"날짜: {date}  시간: {now.strftime('%H:%M')}",
            f"시장 방향성: {state.get('market_direction', '중립')}",
            f"\n[매크로 레짐 — 포지션 크기·섹터 방향의 최우선 기준]\n{state.get('macro_report', '')}",
            f"\n[이벤트 리스크 — 레벨: {event_level}]\n{state.get('event_risk_report', '')}",
            f"\n[글로벌 전문가 서사 — 시장이 지금 무엇을 보는가]\n{state.get('market_intelligence_report', '')}",
            f"\n[위원회 종합]\n{state.get('committee_report', '')}",
            f"\n[주목 종목]\n{candidates_text}",
            f"\n[리스크]\n{chr(10).join(state.get('risks', [])[:3])}",
        ]

        has_price = False  # 실시간 가격 데이터 주입 여부 추적

        if run_type == RUN_TYPE_PRE:
            us_hot = state.get("us_hot_stocks", [])
            if us_hot:
                context_parts.append(
                    "\n[미국 시장 → 오늘 코스피 이슈 종목]\n"
                    + format_us_impact_for_prompt(us_hot)
                )
            if state.get("us_impact_report"):
                context_parts.append(
                    "\n[미국발 오늘 주목 한국 종목]\n"
                    + state["us_impact_report"]
                )
            if state.get("sector_report"):
                context_parts.append(
                    "\n[오늘 주도 섹터 분석]\n"
                    + state["sector_report"]
                )
            if state.get("money_flow_report"):
                context_parts.append(
                    "\n[수급 분석 — 외국인·기관 순매수]\n"
                    + state["money_flow_report"]
                )
            if state.get("bigfigure_report"):
                context_parts.append(
                    "\n[오늘 주목할 빅피겨 발언]\n"
                    + state["bigfigure_report"]
                )
            if state.get("news_report"):
                context_parts.append(
                    "\n[오늘 뉴스 — 복합 이벤트 팩트체크용]\n"
                    + state["news_report"]
                )
            if state.get("dart_disclosures"):
                from agents.dart_alert_agent import format_disclosures_for_briefing
                dart_text = format_disclosures_for_briefing(state["dart_disclosures"])
                if dart_text:
                    context_parts.append(
                        "\n[오늘 주요 DART 공시 — 해당 종목·섹터 판단에 반영]\n"
                        + dart_text
                    )
            # 실시간 현재가 기반 진입/손절/목표가 주입
            try:
                kis_pre = KISClient()
                price_ctx = _fetch_price_context(
                    state.get("candidates", []), kis_pre
                )
                if price_ctx and "조회 불가" not in price_ctx:
                    context_parts.append(
                        "\n[실시간 가격 데이터 — ③번 종목 추천의 진입/손절/목표가는 반드시 이 수치만 사용]\n"
                        + price_ctx
                    )
                    has_price = True
                    logger.info("[CEO] 실시간 가격 데이터 주입 완료")
                else:
                    context_parts.append(
                        "\n🚫 현재가 없음: 종목 추천(③번)에 원 단위 가격 수치 기재 절대 금지.\n"
                        "진입 조건·손절 조건·목표 조건으로만 작성하세요."
                    )
                    logger.warning("[CEO] 가격 데이터 없음 — 조건 기반 지침 주입")
            except Exception as e:
                logger.warning("[CEO] 실시간 가격 조회 실패: %s", e)

        if run_type == RUN_TYPE_CLOSE:
            # 30일 누적 성과 통계 주입 — CEO가 추천 성향 자기보정에 활용
            try:
                stats = get_performance_stats(days=30)
                if stats["total"] >= 3:
                    context_parts.append(
                        f"\n[최근 30일 추천 성과 통계]\n"
                        f"총 {stats['total']}건 | 성공 {stats['win']}건 | 실패 {stats['loss']}건 | "
                        f"승률 {stats['win_rate']}% | 평균수익률 {stats['avg_return']:+.2f}% | "
                        f"최대손실 {stats['max_loss']:.2f}% | 손익비 {stats['profit_factor']:.2f}\n"
                        f"→ 승률 50% 미만이면 확신도 '하' 종목 추천 자제, 조건 기반으로만 언급"
                    )
            except Exception as e:
                logger.debug("[CEO] 성과 통계 주입 실패: %s", e)

            # 오늘 한국 시장 실제 움직임 — CEO가 '무엇이 실제로 움직였는가'를 파악하는 핵심 데이터
            if state.get("korea_spot_report"):
                context_parts.append(
                    "\n[오늘 한국 시장 실제 움직임 — 거래대금·수급·급등 기반]\n"
                    + state["korea_spot_report"]
                )
            if state.get("sector_report"):
                context_parts.append(
                    "\n[오늘 섹터·테마 흐름]\n"
                    + state["sector_report"]
                )
            if state.get("money_flow_report"):
                context_parts.append(
                    "\n[오늘 수급 분석]\n"
                    + state["money_flow_report"]
                )
            if state.get("news_report"):
                context_parts.append(
                    "\n[오늘 뉴스 — 복합 이벤트 팩트체크용]\n"
                    + state["news_report"]
                )
            if state.get("bigfigure_report"):
                context_parts.append(
                    "\n[오늘 빅피겨 발언]\n"
                    + state["bigfigure_report"]
                )
            if state.get("dart_disclosures"):
                from agents.dart_alert_agent import format_disclosures_for_briefing
                dart_text = format_disclosures_for_briefing(state["dart_disclosures"])
                if dart_text:
                    context_parts.append(
                        "\n[오늘 주요 DART 공시 — 내일 종목·섹터 판단에 반영]\n"
                        + dart_text
                    )
            # 장마감: 오늘 추천 종목 종가 수집 → 수익률 포함, 동일 클라이언트 재사용
            kis_close = KISClient()
            try:
                results = update_close_prices(date, kis_close)
                returns_text = format_returns_for_report(results)
                context_parts.append(f"\n[오늘 추천 종목 수익률]\n{returns_text}")
            except Exception as e:
                logger.warning("[CEO] 종가 수집 실패: %s", e)
            # 내일 추천용 실시간 종가 기반 진입/손절/목표가 주입
            try:
                price_ctx = _fetch_price_context(
                    state.get("candidates", []), kis_close
                )
                if price_ctx and "조회 불가" not in price_ctx:
                    context_parts.append(
                        "\n[실시간 가격 데이터 — ④번 종목 추천의 진입/손절/목표가는 반드시 이 수치만 사용]\n"
                        + price_ctx
                    )
                    has_price = True
                    logger.info("[CEO] 장마감 실시간 가격 데이터 주입 완료")
                else:
                    context_parts.append(
                        "\n🚫 현재가 없음: 종목 추천(④번)에 원 단위 가격 수치 기재 절대 금지.\n"
                        "진입 조건·손절 조건·목표 조건으로만 작성하세요."
                    )
                    logger.warning("[CEO] 장마감 가격 데이터 없음 — 조건 기반 지침 주입")
            except Exception as e:
                logger.warning("[CEO] 장마감 가격 조회 실패: %s", e)

        # 장중(intra): 실시간 KOSPI·KOSDAQ 지수 주입
        if run_type in (RUN_TYPE_INTRA1, RUN_TYPE_INTRA2):
            kr_rt = state.get("kr_index_realtime", {})
            if kr_rt:
                idx_lines = []
                for key, label in [("kospi", "KOSPI"), ("kosdaq", "KOSDAQ")]:
                    d = kr_rt.get(key)
                    if d:
                        idx_lines.append(
                            f"{label} 현재: {d['current']:,.2f} ({d['change_pct']:+.2f}%)"
                        )
                if idx_lines:
                    context_parts.append(
                        "\n[실시간 지수 — 현재 장중 수준]\n" + "\n".join(idx_lines)
                    )

        # 장중(intra) 브리핑에도 DART 공시 포함
        if run_type in (RUN_TYPE_INTRA1, RUN_TYPE_INTRA2) and state.get("dart_disclosures"):
            from agents.dart_alert_agent import format_disclosures_for_briefing
            dart_text = format_disclosures_for_briefing(state["dart_disclosures"])
            if dart_text:
                context_parts.append(
                    "\n[오늘 주요 DART 공시]\n" + dart_text
                )

        if state.get("review_report"):
            context_parts.append(f"\n[복기]\n{state['review_report']}")

        # 가격 데이터 없을 때: 컨텍스트 맨 앞과 맨 뒤에 경고 배너 삽입
        if not has_price and run_type in (RUN_TYPE_PRE, RUN_TYPE_CLOSE):
            context_parts.insert(0,
                "🚫🚫 가격 경고 — 현재가 데이터 없음 🚫🚫\n"
                "종목 추천 섹션에 진입가·손절가·목표가 원 단위 숫자 절대 기재 금지.\n"
                "이 규칙 위반 시 사용자에게 잘못된 투자 정보를 제공하게 됩니다."
            )
            context_parts.append(
                "\n🚫 최종 확인: 가격 데이터 없음 — 종목 추천에 원 단위 숫자 금지."
            )

        context = "\n".join(context_parts)

        # 실행 유형별 프롬프트 선택 — PRE/CLOSE는 가격 데이터 여부에 따라 동적 생성
        if run_type == RUN_TYPE_PRE:
            prompt = _build_prompt_pre(has_price)
        elif run_type == RUN_TYPE_CLOSE:
            prompt = _build_prompt_close(has_price)
        elif run_type == RUN_TYPE_INTRA1:
            prompt = _PROMPT_INTRA1
        else:
            prompt = _PROMPT_INTRA2

        result  = chat_ceo(prompt, context, max_tokens=2000)
        state["ceo_report"] = result

        # 장전 브리핑: 추천 종목 파싱 → DB 저장
        if run_type == RUN_TYPE_PRE:
            try:
                recs = parse_recommendations(result)
                if recs:
                    n = save_recommendations(date, recs)
                    logger.info("[CEO] 추천 종목 %d건 DB 저장 완료", n)
                else:
                    logger.warning("[CEO] 추천 종목 파싱 실패 — 형식 불일치 가능")
            except Exception as e:
                logger.warning("[CEO] 추천 종목 저장 실패: %s", e)

        logger.info("[CEO] 브리핑 생성 완료")
    except Exception as e:
        logger.error("[CEO] 실패: %s", e)
        state["ceo_report"] = "브리핑 생성 실패"
        state["errors"].append(f"ceo_agent: {e}")
    return state
