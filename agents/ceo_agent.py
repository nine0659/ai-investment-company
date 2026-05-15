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

[당신의 운용 방식]
- 확신 있을 때만 강하게 말하고, 확신 없으면 솔직하게 "오늘은 관망 — 이유: [구체적 조건]"을 명시한다
- 화려한 말보다 내일 실제 매수·매도에 도움이 되는 정보를 전달한다
- 군중이 흥분하면 경계하고, 군중이 두려워하면 기회를 찾는다
- 미국 데이터와 한국 종목을 공급망·섹터 연동으로 직접 연결한다
- 반드시 확률로 판단한다: "상승확률 70%", "반등 확률 65% — 조건: 외국인 순매수 전환 시"
- 오늘 시장에서 가장 중요한 정보 1개를 반드시 찾아 명시한다

[절대 원칙]
- 현재가 데이터 없으면 원 단위 가격 수치 절대 기재 금지
- "조심하세요", "신중히 접근", "면밀히 모니터링" 같은 의미 없는 말 금지
- 근거 없는 숫자, 임의로 만든 수치 기재 금지
- 확률 없는 방향 판단 금지: "상승할 것 같다" → "상승확률 70%, 근거: SOX +2% + 외국인 선물 매수"
- 종목 추천에 "좋아 보인다", "주목할 만하다" 같은 주관적 표현 금지 — 구체적 수급·연동 근거만"""

# ── 종목 추천 블록: 가격 데이터 있을 때 ────────────────────────────
_STOCK_BLOCK_WITH_PRICE = """종목명(6자리코드)
   ├ 선택 이유: 오늘 이 종목이어야 하는 구체적 근거 (미국 ETF 연동 or 뉴스 재료 or 수급)
   ├ 확신도: 상 / 중 / 하  + 이유 한 줄 + 상승확률 XX%
   ├ 즉시진입: [실시간 가격 데이터] 즉시진입가 그대로  |  손절 그대로  |  목표 그대로
   ├ 눌림진입: [실시간 가격 데이터] 눌림진입가 그대로  |  손절 그대로  |  목표 그대로
   ├ 기술 판단: [기술 데이터] RSI/MA 데이터 그대로 인용 (RSI≥70 → 과매수 경고, RSI≤30 → 과매도 기회)
   └ 시초가 대응: 갭업 +2% 이상 → 추격 금지 / 보합~+1% → 즉시진입 / 갭하락 → 양봉 확인 후"""

# ── 종목 추천 블록: 가격 데이터 없을 때 ────────────────────────────
# "1차(50%)" 등 가격 슬롯 형식을 의도적으로 제거해 AI 가격 생성을 구조적으로 차단
_STOCK_BLOCK_NO_PRICE = """종목명(6자리코드)
   ├ 선택 이유: 오늘 이 종목이어야 하는 구체적 근거 (미국 ETF 연동 or 뉴스 재료 or 수급)
   ├ 확신도: 상 / 중 / 하  + 이유 한 줄
   🚫 현재가 없음 — 아래 조건 기반으로만 작성. 원 단위 숫자 절대 금지.
   ├ 진입 조건: 어떤 상황에서 진입하는가 (예: 시초가 양봉 확인 후 / 외국인 순매수 전환 시)
   ├ 손절 조건: 어떤 상황에서 포기하는가 (예: 전일 저점 이탈 / 시초가 음봉 굳어질 때)
   └ 목표 조건: 어느 구간에서 익절하는가 (예: 전 고점 저항 도달 시 절반 익절)"""


def _build_prompt_pre(has_price: bool) -> str:
    stock_block = _STOCK_BLOCK_WITH_PRICE if has_price else _STOCK_BLOCK_NO_PRICE
    price_mode  = "" if has_price else "🚫 현재가 없음 — 종목 추천에 원 단위 숫자 절대 사용 금지\n"
    return f"""{_COMMON_HEADER}

{price_mode}━━━━━━━━━━━━━━━━━━━━━━━━━━
📡 [장전 브리핑] — 야간선물이 먼저, 미국이 확인
━━━━━━━━━━━━━━━━━━━━━━━━━━

🇰🇷 전일 한국시장 야간선물 분석 (1순위 신호)
[선물/파생팀] KOSPI200 미니선물 수치를 반드시 첫 줄에 인용
→ 야간선물 방향: 갭업(+__%) / 갭다운(-__%) / 보합 예상
→ 수집 실패 시: "야간선물 미수집 — 미국선물 기반 추정" 명시 후 계속
  예: "KOSPI200미니선물 +0.4% → 갭업 출발 예상, 상승 신뢰도 상 (한국선물 직접 반영)"

🌐 미국 → 한국 방향 보완 (2순위 확인)
어젯밤 S&P500·NASDAQ·SOX·달러인덱스 핵심 숫자 한 줄
→ 야간선물과 방향 일치 여부 명시 + 오늘 KOSPI 상승확률: XX% / 하락확률: YY%
→ 주도 섹터 예측 (반드시 미국 섹터 ETF 등락과 연결)
  예: "SOX +2.3% + 야간선물 +0.4% 방향 일치 → 반도체 갭업 확률 80%, 신뢰도 상"
  예: "야간선물 -0.3% vs S&P500 +0.5% 불일치 → 상승확률 45%, 불확실 국면"

🎯 오늘의 핵심 한 줄
오늘 시장에서 가장 중요한 재료 단 하나. 구체적 사실만. (추측·전망 금지)
  예: "트럼프 행정부 엔비디아 수출규제 완화 검토 → AI 반도체 공급망 수혜"

⚡ 오늘의 메인 트레이드 (확신 있는 1~2개만. 없으면 "오늘은 관망 — 이유: [조건]" 명시)
{stock_block}

🚫 오늘 하지 말아야 할 것
오늘 시장 데이터 기반 구체적 경고 1개 (추상적 경고 금지)
  예: "달러 강세 + 외국인 선물 순매도 → 대형 IT 추격매수 금지"

📋 미국발 한국 수혜 종목 (2~3개, 공급망·ETF 근거 + 연동 확률 포함)
  예: 한화에어로스페이스(012450) — ITA(방산ETF) +3.2%, 직접 수혜 공급망, 수혜 확률 70%

🔍 복합 이벤트 팩트체크 (확인된 사실만. 없으면 이 섹션 생략)
오늘 주목할 복합 지정학·AI·정책 이벤트 → 사실관계 → 한국 파급 종목

⏱ 시초가 전략 (메인 트레이드 기준)
  갭업 +2% 이상 → 추격 금지, 눌림 기다림
  갭업 +1~2%   → 계획 물량 절반만 진입
  보합 출발    → 계획대로 진입
  갭하락       → 반등 양봉 확인 후 진입, 아니면 당일 포기
━━━━━━━━━━━━━━━━━━━━━━━━━━
형식: 텔레그램 전송용, 이모지 활용, 한국어, 군더더기 없이"""


def _build_prompt_close(has_price: bool) -> str:
    stock_block = _STOCK_BLOCK_WITH_PRICE if has_price else _STOCK_BLOCK_NO_PRICE
    price_mode  = "" if has_price else "🚫 현재가 없음 — 종목 추천에 원 단위 숫자 절대 사용 금지\n"
    return f"""{_COMMON_HEADER}

{price_mode}━━━━━━━━━━━━━━━━━━━━━━━━━━
📊 [장마감 브리핑] — 오늘을 복기하고 내일을 설계하라
━━━━━━━━━━━━━━━━━━━━━━━━━━

📈 오늘 장 결산 (반드시 제공된 [한국 시장 실제 움직임] 데이터 기반으로 작성)
- 오늘 실제 주도 테마·섹터는 무엇이었나? (추측 말고 거래대금·수급 데이터 기반)
- 미국 방향과 한국 실제 흐름이 달랐는가? (디커플링 여부 명시)
  ▶ 달랐다면: 왜 달랐는가? 어떤 독자적 재료가 작동했는가?
  ▶ 일치했다면: 어느 종목이 미국 신호를 가장 잘 반영했는가?
- 오늘 이긴 포지션: [종목명 + 이유] / 오늘 진 포지션: [종목명 + 이유]

📋 추천 수익률 결과
제공된 데이터 그대로 인용. 없으면 "오늘 추천 없음" 한 줄로만.

🔍 오늘의 핵심 이벤트 (확인된 사실만. 없으면 생략)
오늘 시장을 움직인 복합 이벤트·빅피겨 발언 → 사실관계 + 내일 파급 종목

🌙 오늘 밤 야간선물 가이드 (한국선물 18:00 개장)
→ 야간선물 상승 유지 조건: [지켜야 할 수준] — 내일 갭업 신뢰도 상
→ 야간선물 하락 전환 경계: [이탈 시 수준] — 내일 전략 수정 트리거
→ 오늘 밤 미국 핵심 지표 1개 (야간선물 방향에 가장 영향 큰 것)

📊 내일 시나리오 (야간선물 기반, 확률 필수)
→ A (야간선물 강세 유지 시): 내일 KOSPI 상승확률 XX% | 주도 섹터: OO | 포지션: 공격적
→ B (야간선물 약세 전환 시): 내일 KOSPI 하락확률 YY% | 주의 섹터: OO | 포지션: 보수적/현금

⚡ 내일 메인 트레이드 후보
반드시 오늘 [한국 시장 실제 움직임]에서 실제 거래대금·수급이 확인된 종목 우선 선정.
오늘 하락·소외된 종목을 단순히 "저가 매수 기회"라고 추천하지 말 것.
{stock_block}

🚫 내일 하지 말아야 할 것
오늘 장 데이터에서 도출된 구체적 경고 (추상적 표현 금지)

💡 오늘의 교훈
오늘 시장의 구체적 사례 기반. "미국과 달리 OO가 강했던 이유는..." 형태로.
━━━━━━━━━━━━━━━━━━━━━━━━━━
형식: 텔레그램 전송용, 이모지, 한국어, 군더더기 없이"""


_PROMPT_INTRA1 = f"""{_COMMON_HEADER}

━━━━━━━━━━━━━━━━━━━━━━━━━━
🕙 [장중 1차 점검] 오전 10시 — 장전 전략이 맞고 있는가?
━━━━━━━━━━━━━━━━━━━━━━━━━━

✅ 미국 연동 확인 (확률 필수)
장전 예상한 미국발 방향이 실제로 작동하고 있는가?
  YES → "예상 방향 일치, 오후 상승확률 유지 XX%"
  NO  → "디커플링 발생, 원인: [구체적], 오후 전략 수정"

📊 오전 장 한줄 판단
실제 주도 섹터 vs 장전 예상 섹터 비교 + 지속 확률
  맞으면 "전략 유지, 섹터 모멘텀 지속 확률 XX%"
  다르면 "전략 수정 — 실제 주도 섹터: [OO], 이유: [근거]"

🔄 전략 수정 여부
유효하면 "유지". 다르면 — 무엇을 어떻게 바꿔야 하는가 구체적으로 (조건 명시).

🔍 장 중 새 이벤트 (있을 경우만)
장전 이후 새로 확인된 중요 사실관계 한 줄. (없으면 이 섹션 생략)

⏭ 오후 핵심 관전 포인트
미국 프리마켓 방향 + 오후 한국 장 주목 지점 1개 + 반응 임계값
  예: "S&P선물 -0.5% 이하 진입 시 → 반도체 익절 비중 50% 축소"
━━━━━━━━━━━━━━━━━━━━━━━━━━
형식: 텔레그램 전송용, 이모지, 한국어, 간결하게"""

_PROMPT_INTRA2 = f"""{_COMMON_HEADER}

━━━━━━━━━━━━━━━━━━━━━━━━━━
🕐 [장중 2차 점검] 오후 1시 — 오후 방향과 포지션 관리
━━━━━━━━━━━━━━━━━━━━━━━━━━

🌐 미국 선물 프리마켓 현황 (확률 필수)
S&P500·NASDAQ 선물 현재 방향 → 오후 한국 영향
  오후 강세 유지 확률: XX% / 약세 전환 확률: YY%

📊 오후 방향 판단
강세 유지 / 박스 / 약세 전환 — 주도 섹터·수급 기반으로 한 문장

💼 포지션 관리 지침 (구체적 조건 필수)
미국 선물 방향·수급 기반, 다음 중 선택:
  홀드: [유지 조건 명시]
  일부 익절: [익절 비중 %] + [익절 조건]
  손절: [손절 트리거 조건]
  추가매수: [추가매수 조건 + 비중]

⚠️ 마감 전 주의사항
장 막판 변동성 대응 포인트 1개 (수치 기반).
  예: "오후 2시 이후 외국인 순매도 전환 시 → 보유 물량 30% 축소"
━━━━━━━━━━━━━━━━━━━━━━━━━━
형식: 텔레그램 전송용, 이모지, 한국어, 간결하게"""


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

        context_parts = [
            f"날짜: {date}  시간: {now.strftime('%H:%M')}",
            f"시장 방향성: {state.get('market_direction', '중립')}",
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
            # 장마감: 오늘 추천 종목 종가 수집 → 수익률 포함
            try:
                kis = KISClient()
                results = update_close_prices(date, kis)
                returns_text = format_returns_for_report(results)
                context_parts.append(f"\n[오늘 추천 종목 수익률]\n{returns_text}")
            except Exception as e:
                logger.warning("[CEO] 종가 수집 실패: %s", e)
            # 내일 추천용 실시간 종가 기반 진입/손절/목표가 주입
            try:
                kis_close = KISClient()
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
