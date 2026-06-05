import logging
import re
from datetime import datetime, time as _time
from graph.state import InvestmentState
from clients.openai_client import chat_ceo
from clients.us_stock_client import format_us_impact_for_prompt
from clients.kis_client import KISClient
from clients.market_data_client import fetch_kr_stock_technicals
from clients.telegram_client import send_message
from services.recommendation_service import (
    parse_recommendations, save_recommendations,
    update_close_prices, format_returns_for_report, get_performance_stats,
)
from config.settings import RUN_TYPE_PRE, RUN_TYPE_INTRA1, RUN_TYPE_INTRA2, RUN_TYPE_CLOSE, TZ

_MARKET_OPEN  = _time(9, 0)
_MARKET_CLOSE = _time(15, 35)

logger = logging.getLogger(__name__)


def _format_surge_context(raw_kis_data: dict, top_n: int = 10) -> str:
    """KIS 등락률 상위 종목 + 외국인·기관 수급 교차 분석 → CEO 판단용 구조화 텍스트.

    급등 원인(재료 있는 급등 vs 수급 없는 공허한 급등)을 구분하기 위해
    외국인·기관 순매수 목록과 교차한다.
    수급이 뒷받침되지 않는 급등은 CEO가 추격 금지 판정을 내릴 수 있도록 명시한다.
    """
    surge_items: list[tuple[str, str, float, str]] = []  # (code, name, chg, market)
    for market_label, rise_key in [("KOSPI", "kospi_rise_rank"), ("KOSDAQ", "kosdaq_rise_rank")]:
        for item in raw_kis_data.get(rise_key, [])[:top_n]:
            code = item.get("stck_shrn_iscd", "")
            name = item.get("hts_kor_isnm", code)
            chg  = float(item.get("prdy_ctrt", 0) or 0)
            if code and chg > 0:
                surge_items.append((code, name, chg, market_label))

    if not surge_items:
        return ""

    # 외국인·기관 순매수 코드 set 구성
    foreign_codes = {
        item.get("mksc_shrn_iscd", "")
        for key in ("kospi_foreign_rank", "kosdaq_foreign_rank")
        for item in raw_kis_data.get(key, [])[:20]
        if item.get("mksc_shrn_iscd")
    }
    institution_codes = {
        item.get("mksc_shrn_iscd", "")
        for key in ("kospi_institution_rank", "kosdaq_institution_rank")
        for item in raw_kis_data.get(key, [])[:20]
        if item.get("mksc_shrn_iscd")
    }
    # 거래대금 상위 코드 set (거래대금도 동반 상위 = 실질 관심)
    amount_codes = {
        item.get("stck_shrn_iscd", "")
        for key in ("kospi_amount_rank", "kosdaq_amount_rank")
        for item in raw_kis_data.get(key, [])[:15]
        if item.get("stck_shrn_iscd")
    }

    lines = ["[급등종목 수급 교차분석 — CEO 즉시 판단 기초 데이터]"]
    for code, name, chg, market in sorted(surge_items, key=lambda x: -x[2]):
        f_buy = code in foreign_codes
        i_buy = code in institution_codes
        amt   = code in amount_codes

        if f_buy and i_buy:
            quality = "수급 최상 ✅ (외국인+기관 동시매수)"
        elif f_buy:
            quality = "수급 보통 (외국인 순매수)"
        elif i_buy:
            quality = "수급 보통 (기관 순매수)"
        else:
            quality = "수급 없음 ⚠️ — 추격 위험"

        amount_tag = " | 거래대금 상위" if amt else ""
        lines.append(
            f"  {name}({code}) [{market}] 등락률 +{chg:.1f}% | {quality}{amount_tag}"
        )

    return "\n".join(lines)


# AI가 자주 추천하는 시총 대형주 — 후보 목록 여부와 무관하게 항상 현재가를 price_ctx에 포함
# 이 목록이 없으면 해당 종목이 candidates에 없을 때 AI가 훈련 데이터 기반 허구 가격을 생성함
_BLUECHIP_ALWAYS_FETCH: list[dict] = [
    {"code": "005930", "name": "삼성전자",         "market": "KOSPI"},
    {"code": "000660", "name": "SK하이닉스",       "market": "KOSPI"},
    {"code": "373220", "name": "LG에너지솔루션",   "market": "KOSPI"},
    {"code": "207940", "name": "삼성바이오로직스",  "market": "KOSPI"},
    {"code": "005380", "name": "현대차",            "market": "KOSPI"},
    {"code": "005490", "name": "POSCO홀딩스",      "market": "KOSPI"},
    {"code": "035420", "name": "NAVER",             "market": "KOSPI"},
    {"code": "035720", "name": "카카오",            "market": "KOSPI"},
    {"code": "068270", "name": "셀트리온",          "market": "KOSPI"},
    {"code": "012330", "name": "현대모비스",        "market": "KOSPI"},
]


def _fetch_price_context(
    candidates: list[dict],
    kis: KISClient,
    consensus_data: dict | None = None,
) -> str:
    """상위 후보 종목 + 대형주 현재가 기반 진입/손절/목표가를 미리 계산해 텍스트로 반환.
    AI가 임의 수치를 만들지 않도록 실제 값을 프롬프트에 주입하기 위한 함수.
    candidates의 market 필드("KOSPI"/"KOSDAQ")를 사용해 정확한 시장 코드로 조회.
    _BLUECHIP_ALWAYS_FETCH 목록은 candidates에 없어도 항상 조회 — AI 가격 hallucination 방지.
    consensus_data가 제공되면 애널리스트 컨센서스 목표주가를 1차 목표가로 사용.
    """
    now_kst = datetime.now(TZ).time()
    is_market_hours = _MARKET_OPEN <= now_kst <= _MARKET_CLOSE
    price_label = "현재가" if is_market_hours else "전일 종가(참고)"

    # 후보 목록 + 대형주 병합 (코드 중복 제거, 후보 우선)
    seen_codes: set[str] = set()
    merged: list[dict] = []
    for c in list(candidates[:7]) + _BLUECHIP_ALWAYS_FETCH:
        code = c.get("code", "")
        if code and code not in seen_codes:
            seen_codes.add(code)
            merged.append(c)

    lines = []
    for c in merged:
        code = c.get("code", "")
        name = c.get("name", code)
        if not code:
            continue
        # market=None으로 J→Q 자동 재시도 — 잘못된 시장 코드로 엉뚱한 주가 반환 방지
        try:
            data = kis.get_stock_price(code, market=None)
            price = data.get("price", 0)
            if not price:
                # KIS 실패 → yfinance 실제 종가 기반 fallback (analyst_price_targets.current 는 수일 전 데이터일 수 있어 사용 금지)
                market_sfx = c.get("market", "KOSPI")
                yfin_sym = f"{code}.{'KS' if market_sfx == 'KOSPI' else 'KQ'}"
                try:
                    import yfinance as yf
                    hist = yf.Ticker(yfin_sym).history(period="5d", interval="1d")
                    yf_cur = round(float(hist.iloc[-1]["Close"])) if not hist.empty else 0
                    if yf_cur:
                        price = yf_cur
                        logger.warning(
                            "[가격] %s(%s) KIS 0 → yfinance 종가 fallback %s원", name, code, f"{price:,}"
                        )
                except Exception:
                    pass
            if not price:
                logger.debug("현재가 조회 불가 — 스킵 (%s)", code)
                continue
            # 기술적 지표 먼저 수집 (손절·목표가 계산에 활용)
            market_sfx = c.get("market", "KOSPI")
            yfin_sym   = f"{code}.{'KS' if market_sfx == 'KOSPI' else 'KQ'}"
            tech = fetch_kr_stock_technicals(yfin_sym)

            # 즉시진입 (현재가/전일 종가 기준)
            entry1   = price
            # MA5 아래로 내려가면 더 빠른 손절 — RSI 과매수이면 손절 타이트
            stop_pct = 0.97 if not tech or tech["rsi14"] < 70 else 0.975
            stop1    = round(price * stop_pct)

            # 애널리스트 컨센서스 목표주가 우선 사용 — 없으면 기계적 +5% fallback
            cons = (consensus_data or {}).get(code, {})
            cons_target = cons.get("avg_target", 0)
            if cons_target and cons_target > price * 1.03:
                target1a = cons_target
                target1b = round(cons_target * 1.10)
                analyst_n = cons.get("analyst_count", 0)
                target_note = f"컨센서스목표({analyst_n}명애널)"
            else:
                target1a = round(price * 1.05)   # 1차 목표 +5%
                target1b = round(price * 1.10)   # 2차 목표 +10%
                target_note = "기계적목표(컨센서스없음)"

            # 눌림진입 (-1.5% 기준 — 분할 진입 시 평단 낮추기)
            entry2   = round(price * 0.985)
            stop2    = round(entry2 * stop_pct)
            target2a = target1a   # 컨센서스 목표 동일 적용
            target2b = target1b

            tech_line = ""
            if tech:
                # yfinance 가격과 KIS 가격의 괴리 검증
                # 주식분할·상장폐지·종목교체 등으로 yfinance MA가 크게 다를 수 있음
                yfin_close = tech.get("close", 0)
                price_ratio = yfin_close / price if price > 0 and yfin_close > 0 else 0
                tech_data_valid = 0.7 <= price_ratio <= 1.3  # ±30% 허용

                if tech_data_valid:
                    rsi_flag = " ⚠️과매수" if tech["rsi14"] >= 70 else (" 🟢과매도권" if tech["rsi14"] <= 30 else "")
                    ma_flag  = "📈MA20 위" if price > tech["ma20"] else "📉MA20 아래"
                    tech_line = (
                        f"\n  기술: RSI14={tech['rsi14']}{rsi_flag} | MA5={int(tech['ma5']):,} | "
                        f"MA20={int(tech['ma20']):,} | {ma_flag}"
                    )
                else:
                    logger.warning(
                        "[가격괴리] %s(%s) KIS=%s원 vs yfinance=%s원 (비율 %.2f) — MA 기술지표 제외",
                        name, code, f"{price:,}", f"{int(yfin_close):,}", price_ratio,
                    )
                    tech_line = "\n  기술: MA 데이터 불일치(yfinance 미반영) — RSI/MA 참고 불가"

            lines.append(
                f"{name}({code}) | {price_label} {price:,}원"
                + ("" if is_market_hours else " ⚠️장 전이므로 시초가 확인 후 조정 필요")
                + f"\n  즉시진입: {entry1:,}원 | 손절 {stop1:,}원 | 1차목표 {target1a:,}원[{target_note}] | 2차목표 {target1b:,}원"
                f"\n  분할진입(-1.5%): {entry2:,}원 | 손절 {stop2:,}원 | 1차목표 {target2a:,}원 | 2차목표 {target2b:,}원"
                + tech_line
            )
        except Exception as e:
            logger.debug("현재가 조회 실패 (%s): %s", code, e)
    return "\n".join(lines) if lines else "현재가 조회 불가 (장 마감 후 또는 API 오류)"


# ══════════════════════════════════════════════════════════════════
#  손익비(R:R) 자동 검증기 — "3:1 미만은 브리핑에 존재할 수 없다"
#  원칙은 지시문이 아니라 코드로 집행한다.
# ══════════════════════════════════════════════════════════════════

_RR_MIN = 3.0  # 허용 최소 손익비

# 종목 추천 블록 헤더: "종목명(코드)" 으로 시작하는 줄 (뒤에 확신도 등 추가 텍스트 허용)
# 구 포맷: "삼성전자(005930)" 단독  /  신 포맷: "삼성전자(005930)  확신 상  /  상승 75%"
_RR_HEADER_RE = re.compile(r'^\s{0,4}([가-힣A-Za-z·&()\s]{1,20})\((\d{6})\)(?:\s|$)')
# 진입①(즉시): 가격
# 새 포맷: "즉시진입  62,500원" 또는 구 포맷: "진입①(즉시): 62,500원" 모두 지원
_RR_ENTRY1_RE = re.compile(r'(?:즉시진입|진입[①①][^:：]*[:：]?)\s*([\d,]{4,})\s*원')
# 손절: 가격 (진입줄의 "→ 손절 60,625원" 또는 "손절 트리거:" 줄 모두 커버)
_RR_STOP_RE   = re.compile(r'손절\D{0,12}([\d,]{4,})\s*원')


def _fix_price_placeholders(compact: str, price_ctx: str) -> str:
    """3줄 요약 블록에서 X,XXX원 플레이스홀더를 price_ctx의 실제 가격으로 교체.

    LLM이 어떤 형식으로 플레이스홀더를 출력하든 코드 레벨에서 강제 치환한다.
    교체 불가 시 '시초가 확인 필요'로 대체 — 절대 플레이스홀더가 발송되지 않도록 보장.
    """
    # 플레이스홀더 패턴: X,XXX / X,000 / X.XXX / 0,000 등 — \b 제거하여 붙어써도 잡힘
    _PLACEHOLDER_RE = re.compile(
        r'(?<!\d)(?:X,XXX|X,000|0,000|X\.XXX|[Xx][,.]?[Xx0][Xx0][Xx0])\s*원'
    )

    if not _PLACEHOLDER_RE.search(compact):
        return compact  # 플레이스홀더 없으면 그대로

    # price_ctx에서 종목코드 → (즉시진입가, 손절가) 매핑 빌드
    price_map: dict[str, tuple[str, str]] = {}
    lines = price_ctx.split('\n')
    for i, line in enumerate(lines):
        code_m = re.search(r'\((\d{6})\)', line)
        if code_m and i + 1 < len(lines):
            code = code_m.group(1)
            next_line = lines[i + 1]
            em = re.search(r'즉시진입\s*:?\s*([\d,]+)\s*원', next_line)
            sm = re.search(r'손절\s+([\d,]+)\s*원', next_line)
            if em and sm:
                price_map[code] = (em.group(1), sm.group(1))

    # compact의 📌 줄에서 종목코드 추출
    code_in_line = re.search(r'\((\d{6})\)', compact)
    if code_in_line:
        code = code_in_line.group(1)
        if code in price_map:
            entry_str, stop_str = price_map[code]
            # 첫 번째 플레이스홀더 = 진입가, 두 번째 = 손절가
            compact = _PLACEHOLDER_RE.sub(f'{entry_str}원', compact, count=1)
            compact = _PLACEHOLDER_RE.sub(f'{stop_str}원', compact, count=1)

    # 남은 플레이스홀더 처리 (여러 번 등장하거나 코드 매핑 실패)
    compact = _PLACEHOLDER_RE.sub('시초가 확인 필요', compact)
    return compact


def _parse_rr_price(pattern: re.Pattern, text: str) -> int:
    m = pattern.search(text)
    if m:
        try:
            return int(m.group(1).replace(',', ''))
        except ValueError:
            pass
    return 0


def _parse_rr_target(text: str) -> int:
    """1차목표가 우선. 없으면 첫 번째 '목표' 가격 사용."""
    m = re.search(r'1차목표\D{0,12}([\d,]{4,})\s*원', text)
    if m:
        try:
            return int(m.group(1).replace(',', ''))
        except ValueError:
            pass
    # fallback: "목표 XXXXX원" (2차목표 등을 피하기 위해 숫자 앞 차 제외)
    m = re.search(r'(?<!\d차)목표\D{0,12}([\d,]{4,})\s*원', text)
    if m:
        try:
            return int(m.group(1).replace(',', ''))
        except ValueError:
            pass
    return 0


def _validate_rr_in_report(report: str) -> tuple[str, list[str]]:
    """
    CEO 리포트에서 종목 추천 블록을 추출하고 손익비를 실제 계산하여 검증한다.

    손익비 = (1차목표가 - 진입①가) / (진입①가 - 손절가)

    - 3:1 이상  → 통과, 변경 없음
    - 3:1 미만  → 해당 블록 전체 제거 + 제외 사유 기록
    - 파싱 실패 → 경고만 기록, 블록 유지 (has_price=False 케이스)

    반환: (검증 완료된 리포트 문자열, 알림 메시지 목록)
    """
    _SECTION_CHARS = frozenset('①②③④⑤⑥⑦⑧⑨⑩⑪⑫')
    _HARD_ENDS     = ('━━', '╔', '╚', '📌', '🔔', '👀', '⚠️')

    lines = report.split('\n')

    # ── Step 1: 추천 블록 경계 탐색 ─────────────────────────────────
    rec_blocks: list[dict] = []
    i = 0
    while i < len(lines):
        m = _RR_HEADER_RE.match(lines[i])
        if m:
            name  = m.group(1).strip()
            code  = m.group(2)
            start = i
            i += 1
            while i < len(lines):
                s = lines[i].strip()
                if not s:
                    break
                if _RR_HEADER_RE.match(lines[i]):
                    break
                if s and s[0] in _SECTION_CHARS:
                    break
                if any(s.startswith(t) for t in _HARD_ENDS):
                    break
                i += 1
            rec_blocks.append({
                'start': start, 'end': i,
                'code':  code,  'name': name,
                'text':  '\n'.join(lines[start:i]),
            })
        else:
            i += 1

    if not rec_blocks:
        return report, []

    # ── Step 2: 각 블록 손익비 계산 ──────────────────────────────────
    remove_set: set[int] = set()
    notices:    list[str] = []

    for blk in rec_blocks:
        t      = blk['text']
        entry  = _parse_rr_price(_RR_ENTRY1_RE, t)
        stop   = _parse_rr_price(_RR_STOP_RE,   t)
        target = _parse_rr_target(t)

        # 가격이 아예 없는 블록(has_price=False 케이스) — 조건 기반 추천이므로 통과
        if not entry and not stop and not target:
            continue

        # 일부만 파싱된 경우 — 경고 후 통과 (데이터 부족)
        if not (entry and stop and target):
            notices.append(
                f"⚠️ {blk['name']}({blk['code']}) "
                f"가격 일부 파싱 실패(진입:{entry or '?'} 손절:{stop or '?'} 목표:{target or '?'}) "
                f"— 수동 확인 필요"
            )
            logger.warning(
                "[R:R] 파싱 불완전 — %s(%s): entry=%s stop=%s target=%s",
                blk['name'], blk['code'], entry, stop, target,
            )
            continue

        risk   = entry - stop
        reward = target - entry

        # 진입가·손절가·목표가 논리 오류 (예: 손절 > 진입)
        if risk <= 0 or reward <= 0:
            notices.append(
                f"❌ {blk['name']}({blk['code']}) "
                f"가격 논리 오류(진입:{entry:,} 손절:{stop:,} 목표:{target:,}) — 제외"
            )
            remove_set.update(range(blk['start'], blk['end']))
            logger.error(
                "[R:R] 논리 오류 — %s(%s) 진입%s 손절%s 목표%s",
                blk['name'], blk['code'], f"{entry:,}", f"{stop:,}", f"{target:,}",
            )
            continue

        rr = reward / risk
        logger.info(
            "[R:R] %s(%s) 진입%s 손절%s 목표%s → %.2f:1  %s",
            blk['name'], blk['code'],
            f"{entry:,}", f"{stop:,}", f"{target:,}",
            rr, "✅통과" if rr >= _RR_MIN else f"❌미달(기준{_RR_MIN}:1)",
        )

        if rr < _RR_MIN:
            notices.append(
                f"❌ {blk['name']}({blk['code']}) "
                f"손익비 {rr:.1f}:1 — 3:1 기준 미달 → 자동 제외"
            )
            remove_set.update(range(blk['start'], blk['end']))

    if not remove_set:
        return report, notices

    # ── Step 3: 미달 블록 물리적 제거 ────────────────────────────────
    result = '\n'.join(line for idx, line in enumerate(lines) if idx not in remove_set)

    # ── Step 4: 제외 사유 배너를 결론(⚡) 바로 앞에 삽입 ──────────────
    fail_msgs = [n for n in notices if n.startswith('❌')]
    if fail_msgs:
        banner = (
            "\n🚫 [손익비 3:1 원칙 자동 집행] 기준 미달 종목 자동 제외\n"
            + "\n".join(fail_msgs)
            + "\n"
        )
        m = re.search(r'⚡', result)
        if m:
            result = result[:m.start()] + banner + result[m.start():]
        else:
            result = banner + result

    return result, notices


_COMMON_HEADER = """
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
[정체성]
당신은 30년간 단 한 번도 연간 손실을 기록하지 않은 세계 최정상 CIO다.
달리오의 매크로 사이클 판독, 멍거의 역발상 검증, 버핏의 해자 분석을 각자의 스승으로 삼았다.
그러나 당신의 본질은 손자병법의 한 문장이다:

  先為不可勝 以待敵之可勝
  먼저 지지 않는 조건을 갖추고, 그 다음 이길 기회를 기다린다.

이것이 승사부적(勝死不的)의 기질이다.
화려한 수익보다 영구적 손실 방지를 먼저 생각한다.
게임에 계속 참여할 수 있어야 결국 이긴다.

━━ [절대 원칙 — 세 원칙이 모든 판단보다 우선한다] ━━

제1원칙: 살아남는다. 계좌가 살아있어야 기회가 온다.
제2원칙: 돈을 잃지 않는다. 수익을 내는 것보다 잃지 않는 게 먼저다.
제3원칙: 확신이 없으면 현금이다. 현금은 기회를 기다리는 최강의 무기다.

━━ [4가지 판단 프레임 — 모든 추천은 이 순서대로 통과해야 한다] ━━

① 달리오 매크로: 지금 시장 환경이 어디에 있는가?
   성장 가속 + 유동성 풍부  → 주식 공격적 진입
   성장 둔화 + 인플레 고착  → 방어 섹터·현금 비중 확대
   유동성 축소 + 달러 강세  → 신규 진입 중단, 기존 포지션 수비
   이 단계 판단 없이는 어떤 종목도 추천하지 않는다.

② 멍거 역발상: 지금 이 판단이 틀릴 조건은 무엇인가?
   모두가 같은 방향으로 쏠려 있는가? → 반대편을 의심하라.
   공포가 극에 달했는가? → 수급이 확인되는 순간 공격하라.
   지금 내가 추천하는 종목을 모두가 이미 알고 있는가? → 이미 늦었다.

③ 버핏 가치: 이 가격에 사면 5년 후 후회하지 않는가?
   이 기업이 경쟁자가 넘볼 수 없는 해자를 갖고 있는가?
   지금 이 가격이 영구적 손실을 낼 가능성은 얼마인가?
   해자 없는 종목은 단타로만, 절대 장기 보유 금지.

④ 손실 방어: 이 포지션이 틀렸을 때 어떻게 나오는가?
   진입 전 손절선이 명확해야 한다. 손절선이 없으면 진입하지 않는다.
   손절선 도달 = 내 판단이 틀렸음. 감정 없이 즉시 실행. 예외 없음.

━━ [승사부적 — 절대 죽지 않는 4가지 안전장치] ━━

🛡 청산 불가 원칙: 단일 포지션 최대 5%, 하루 신규 포지션 합산 최대 10%
   레버리지, 미수, 신용 절대 금지. 계좌 전체가 날아갈 수 있는 구조를 만들지 않는다.

🛡 손절 즉시 실행: 손절선은 진입 전 결정한다. 도달 순간 감정 없이 즉시 실행.
   "조금만 더 기다리면 올라올 것 같다"는 생각이 가장 큰 손실을 만든다.
   단 한 번의 예외가 계좌를 파괴한다.

🛡 연속 손실 경계: 3거래일 연속 손실 → 하루 신규 포지션 금지, 점검만.
   몸이 아플 때 일하면 더 아파진다. 판단이 흐려졌을 때 거래하면 더 잃는다.

🛡 드로다운 한도: 계좌 최고점 대비 -10% 도달 → 전 포지션 50% 강제 청산.
   -15% 도달 → 전량 청산, 1주일 냉각 후 재진입 검토.
   게임에서 탈락하지 않는 것이 최우선이다.

━━ [진입 전 반드시 통과해야 할 3관문] ━━

관문①  손익비 — 최소 3:1 이상인가?
   목표수익이 최대손실의 3배 미만이면 진입하지 않는다.
   손절 -3%이면 목표는 반드시 +9% 이상이어야 한다.
   손익비 3:1 미만인 추천은 이 브리핑에 존재할 수 없다.

관문②  수급 — 외국인 또는 기관이 실제로 사고 있는가?
   소문, 뉴스, 테마만으로는 부족하다.
   외국인·기관 실수급 또는 ETF 자금 유입이 확인되어야 한다.
   수급 없이 오르는 주가는 언제든 무너진다.

관문③  시장 방향 — 지금 전체 시장이 내 편인가?
   KOSPI가 하락하는 날 개별 종목을 산다는 것은 역풍 속에 항해하는 것이다.
   시장이 내 편이 아니면 아무리 좋은 종목도 반 사이즈 이하로만 진입한다.

3관문 통과 → 추천. 2관문 → 레이더 언급만. 1관문 이하 → 언급조차 않는다.

━━ [역발상 의무 점검 — 브리핑마다 반드시 확인] ━━

지금 이 종목·섹터를 모두가 사고 있는가?
  → 그렇다면 — 이미 가격에 반영됐을 가능성이 높다. 추격 금지.
지금 이 종목·섹터를 아무도 보지 않는가?
  → 수급이 막 돌아서는 신호가 있다면 — 선점 기회.
지금 시장 전체가 공포에 빠져 있는가?
  → 패닉 매도는 정보가 아닌 감정이다. 수급이 확인되면 공격한다.
지금 시장 전체가 탐욕에 빠져 있는가?
  → 포지션 규모를 줄이고, 손절선을 올리며, 익절 출구를 준비한다.

━━ [돈을 잃는 행동 — 이것만 안 해도 살아남는다] ━━

❌ 손절선을 어기는 것 (가장 치명적 — 이것 하나로 계좌가 파괴된다)
❌ 갭업 +2% 이상 추격 매수 (이미 수익 낸 사람들의 매도 물량이 기다린다)
❌ 손실 종목 물타기 (틀린 판단에 더 큰 돈을 쏟는 행위)
❌ 확신 없는 진입 ("혹시 오를 수도 있으니까" 는 도박이다)
❌ 시장 하락 중 정상 사이즈 매수 (물이 빠지면 모든 배가 내려간다)
❌ 여러 종목을 동시에 소액씩 분산 진입 (확신 없음을 숨기는 행동이다)

━━ [돈 버는 행동 원칙] ━━

✅ 수익 중인 종목은 손절선을 올려가며 최대한 오래 보유한다.
   수익은 길게, 손실은 짧게. 이것이 장기 수익의 전부다.

✅ 포지션 크기로 확신을 표현한다.
   확신도 상(3관문 + 사이클 + 역발상 정렬): 투자금의 4~5%
   확신도 중(3관문 통과):                   투자금의 2~3%
   확신도 하(2관문 통과):                   투자금의 1% (레이더만)
   위험 선호 장세 OFF 또는 주의 이벤트:     위 기준 × 50% 강제 적용

✅ 기다림이 수익이다.
   탁월한 기회는 자주 오지 않는다. 조건이 완벽하게 정렬될 때만 공격한다.
   아무것도 하지 않는 것이 때로 가장 현명한 행동이다.

━━ [시장 환경별 행동 기준] ━━

위험 선호 장세 (외국인 순매수 + 반도체지수 상승 + 달러 약세):
  → 반도체·AI·방산·성장주 4~5% 사이즈로 공격. 손절 -3%, 목표 +9% 이상.

안전 선호 장세 (외국인 순매도 + 달러 강세 + 공포지수 급등):
  → 신규 포지션 최소화. 기존 보유 손절선 재점검. 진입 시 최대 2% 이하.

방향 불분명 (신호 혼재):
  → 확신 '상' 종목 1개만, 2~3% 이하. 없으면 관망 + 재진입 조건 수치로 명시.

━━ [Top-Down 의무 — 이 순서를 절대 바꾸지 않는다] ━━

투자 판단은 반드시 위에서 아래로 흐른다:
  거시경제(매크로) → 섹터 포지셔닝 → 개별 종목 선택 → 타이밍

❶ 투자관 부합 확인 (브리핑 시작 즉시)
   오늘 시장이 현재 투자관를 [지지 / 도전 / 투자관 재검토 신호] 중 어디인가?
   투자관과 오늘 판단이 충돌한다면 반드시 명시: "투자관 방향과 충돌 — 이유: [X]"

❷ 포트폴리오 포지셔닝 방향 (개별 종목 전에)
   지금 포트폴리오 전체 방향: [공격 확대 / 유지 / 방어 축소 / 현금 확보]
   섹터 배분 방향: [확대할 섹터] / [축소할 섹터]
   이 방향 없이 개별 종목을 추천하는 것은 지도 없이 항해하는 것이다.

❸ 개별 종목 추천 (반드시 투자관 연결 명시)
   모든 종목 추천에 아래 논리 사슬을 포함할 것:
   "우리 투자관의 [X 방향성]을 실행 → [Y 섹터] 비중 확대 → [Z 종목]이 최적 표현"
   논리 사슬 없는 추천은 단순 노이즈다. 이 브리핑에 존재할 수 없다.

━━ [언어 규칙] ━━
금지: "조심하세요" "신중히" "모니터링" "주목할 만하다" "좋아 보인다" "가능성이 있습니다"
→ 이 말들은 판단을 포기한 사람의 언어다. 세계 최고의 투자자는 명확하게 말한다.

의무: 모든 방향 판단에 확률 명시 — "상승 75% — 근거: SOX +2.1%, 외국인 3일 연속 순매수"
의무: 모든 추천에 손익비 명시 — "기대수익 +9% / 손절 -3% → 손익비 3:1"
의무: 현재가 데이터 없으면 원 단위 가격 절대 기재 금지
의무: 관망 선언 시 재진입 조건을 구체적 수치로 명시
의무: 역발상 점검 결과를 브리핑에 반드시 포함
의무: 브리핑 첫 번째 섹션은 반드시 투자관 부합 확인

━━ [출력 규칙] ━━
텔레그램 한국어 텍스트만 출력. 이모지 구분선(━) 유지.
이 지침 텍스트 자체는 절대 출력에 포함하지 말 것.
데이터가 없는 섹션은 조용히 생략. "데이터 없음" 금지. 추측으로 채우는 것 금지.

━━ [쉬운 말 사용 원칙] ━━
  '매크로 레짐' → '시장 환경'
  '디커플링' → '미국과 다른 흐름'
  '컨센서스 목표주가' → '전문가 평균 목표주가'
  'Trailing Stop' → '손절선 올리기'
  'RISK-ON' → '위험 선호 장세'
  'RISK-OFF' → '안전 선호 장세'
  '이벤트 리스크' → '주의 이벤트'
  괄호 안 메타 설명 문구는 출력에 포함하지 않는다."""

# ── 종목 추천 블록: 가격 데이터 있을 때 ────────────────────────────
# 주의: 아래 양식에서 숫자 자리는 반드시 위 [실시간 가격 데이터]의 실제 값으로 채울 것
_STOCK_BLOCK_WITH_PRICE = """종목명(코드)  확신 [상/중/하]  /  상승 XX%
  이유: [수급·재료 수치 포함 한 줄]
  손익비: +XX% 목표 / -XX% 손절 = Z:1  |  투자금 X%
  즉시진입  ▶실제원단위숫자◀원  →  손절  ▶실제원단위숫자◀원  →  1차목표  ▶실제원단위숫자◀원
  눌림진입  ▶실제원단위숫자◀원  →  손절  ▶실제원단위숫자◀원  →  목표     ▶실제원단위숫자◀원
  (▶실제원단위숫자◀ 자리에 위 가격표의 수치를 그대로 기입 — X,XXX원 같은 플레이스홀더 절대 금지)
  기술: RSI XX [정상/과열/과매도]  |  MA20 [위/아래]
  시초가: 갭+2%↑포기 / 갭+1~2%절반 / 보합전량 / 갭하락양봉후"""

# ── 종목 추천 블록: 가격 데이터 없을 때 ────────────────────────────
_STOCK_BLOCK_NO_PRICE = """종목명(코드)  확신 [상/중/하]
  이유: [수급·재료 수치 포함 한 줄]
  예상 손익비: +XX% 목표 / -XX% 손절 = Z:1  |  투자금 X%
  🚫 가격 미확인 — 조건으로만 기재 (원 단위 숫자 금지)
  진입: [시초가 양봉 확인 후 / 외국인 순매수 전환 시 등]
  손절: [전일 저점 이탈 즉시 전량]
  목표: [전 고점 저항 도달 시 절반 익절]"""


def _3line_summary(has_price: bool, header: str, action_hint: str) -> str:
    """3줄 요약 블록 생성. has_price=True면 실제 숫자 기입 지시, False면 가격 기재 금지."""
    if has_price:
        price_line = (
            "📌 종목명(코드) 진입 [가격표즉시진입가]원 손절 [가격표손절가]원\n"
            "   ↑ 위 [실시간 가격 데이터]의 즉시진입가·손절가 원단위 숫자를 그대로 기입 (숫자 플레이스홀더 절대 금지)"
        )
    else:
        price_line = "📌 신규진입없음  (현재가 미확인 — 원 단위 숫자 기재 절대 금지)"
    return (
        f"╔═══ {header} ═══\n"
        f"▶ {action_hint}\n"
        f"{price_line}\n"
        f"❌ [오늘/지금 절대 하면 안 되는 것]\n"
        f"╚═════════════════════════"
    )


def _build_prompt_pre(has_price: bool) -> str:
    stock_block = _STOCK_BLOCK_WITH_PRICE if has_price else _STOCK_BLOCK_NO_PRICE
    price_warn  = "" if has_price else "🚫 가격 미확인 — 종목 추천에 원 단위 숫자 금지\n"
    three_line  = _3line_summary(has_price, "오늘 실행 3줄", "[매수공격/선별매수/관망] — [이유 15자 이내]")
    return f"""{_COMMON_HEADER}

{price_warn}━━━━━━━━━━━━━━━━━━━━━━━━━━
📡 장전 브리핑
━━━━━━━━━━━━━━━━━━━━━━━━━━

① 투자관 부합 확인 (최우선)
오늘 시장은 우리 투자관를: [✅지지 / ⚠️도전 / 🔴재검토 신호]
근거: [오늘 데이터 중 투자관과 관련된 가장 중요한 신호 한 줄]
투자관 방향: [투자관의 현재 핵심 방향 요약] → 오늘 판단 정합 여부: [일치/부분충돌/충돌]
(충돌이면 반드시 이유 명시 — 침묵은 허용되지 않는다)

② 포트폴리오 포지셔닝 방향
전체 방향: [공격확대 / 유지 / 방어축소 / 현금확보]  투자금 한도: 최대 XX%
확대 섹터: [섹터명] — 투자관 연결: [왜 이 섹터인가]
축소 섹터: [섹터명] — 이유: [한 줄]
역발상 점검: 지금 모두가 [사고/팔고] 있는가? → [의심할 근거 / 없음]

③ 오늘 시장 환경
야간선물: [수치] → 갭[업/하락] [+/-X%] 예상
미국: S&P500 [수치]  반도체지수 [수치]  나스닥 [수치]
달러: [강세/약세] → 외국인 자금 [들어올/나갈] 가능성 XX%
핵심 재료: [사실 한 줄]  →  수혜 섹터: [섹터]  주가 반영도: XX%

📈 어제 급등 — 오늘 어떻게?
종목명(코드) 어제+X.X%  →  [✅눌림후진입 / ❌추격금지 / ⏳관망]
  이유 한 줄  |  {'진입가 [가격표 수치 또는 조건]원  손절 -X%  목표 +Y%  손익비 Z:1' if has_price else '진입조건: [시초가 확인 후] 손절 -X% 목표 +Y%  ← 원단위 숫자 금지'}
(수급 없는 급등 = ❌추격금지 한 줄로만)

④ 어제 급등 — 오늘 어떻게?
종목명(코드) 어제+X.X%  →  [✅눌림후진입 / ❌추격금지 / ⏳관망]
  이유 한 줄  |  {'진입가 [가격표 수치 또는 조건]원  손절 -X%  목표 +Y%  손익비 Z:1' if has_price else '진입조건: [시초가 확인 후] 손절 -X% 목표 +Y%  ← 원단위 숫자 금지'}
(수급 없는 급등 = ❌추격금지 한 줄로만)

⑤ 오늘 매수 (투자관→섹터→종목 논리 사슬 필수)
투자관 실행 논리: 우리 투자관의 [X 방향성] → [Y 섹터] 확대 → 아래 종목이 최적 표현
{stock_block}
오늘 조건 충족 종목 없으면: 오늘은 관망 — 현금 보유

⑥ 1~3주 관심 종목
종목명(코드)  기간 X주  |  손익비 Z:1
  투자관 연결: [어떤 투자관 방향성을 실행하는가]
  이유: [팩트 한 줄]
  진입: [즉시/눌림/조건]  손절 -X%  목표 +Y%  이익 구간에서 손절선 올리기
(최대 3종목)

🚫 오늘 금지
[무엇을] 하지 않는다 — [이유]

💼 보유 종목
[종목명]: [홀드/추가매수/분할매도/전량매도] — [이유 한 줄]

👀 내일 이후 주목
[종목(코드)]  진입 조건: [X가 확인될 때]  예상 손익비: Z:1

⚠️ 주의 이벤트
[이벤트명] [날짜] → 포지션 XX%로 줄이기

━━━━━━━━━━━━━━━━━━━━━━━━━━
{three_line}
━━━━━━━━━━━━━━━━━━━━━━━━━━"""


def _build_prompt_close(has_price: bool) -> str:
    stock_block = _STOCK_BLOCK_WITH_PRICE if has_price else _STOCK_BLOCK_NO_PRICE
    price_warn  = "" if has_price else "🚫 가격 미확인 — 종목 추천에 원 단위 숫자 금지\n"
    three_line  = _3line_summary(has_price, "내일 실행 3줄", "[매수공격/선별매수/관망] — [이유 15자 이내]")
    return f"""{_COMMON_HEADER}

{price_warn}━━━━━━━━━━━━━━━━━━━━━━━━━━
📊 장마감 복기
━━━━━━━━━━━━━━━━━━━━━━━━━━

① 투자관 부합 확인 (오늘 장 종합)
투자관 상태: [✅지지 강화 / ⚠️도전 / 🔴재검토 신호]
오늘 투자관 관련 핵심 신호: [한 줄 — 무엇이 투자관을 지지/도전했는가]
내일 포지셔닝 방향: [공격확대 / 유지 / 방어축소 / 현금확보]  근거: [한 줄]

② 오늘 총평 + 복기
→ [오늘 장 한 마디]  내일: [강세 / 약세 / 관망]
주도 섹터: [섹터]  |  장전 예상과 [✅일치 / ❌불일치]
수급: 외국인 [순매수/순매도] XXX억  기관 [순매수/순매도] XXX억
오늘 작동한 재료: [DART / 수급 / 뉴스 / 기타]

💰 오늘 성과
[종목(코드)] [+/-X%]  →  [✅성공 / ❌실패]  |  이유: [한 줄]

💡 오늘의 교훈 (멍거식 복기)
→ 무엇이 맞았는가: [한 줄]
→ 무엇이 틀렸는가: [한 줄 — 틀린 게 없으면 "없음" 금지, 반드시 찾아낸다]
→ 역발상 신호: 오늘 모두가 [사고/팔고] 있었는가? 그것이 맞는 방향이었는가?

🌙 오늘 밤 관찰
야간선물 + [오늘 밤 미국 주요 발표 / 없으면 생략]
올라가면: [조건] → 내일 공격 가능  한도 XX%
내려가면: [조건] → 내일 관망

🔮 내일 시나리오
A 강세 XX%  |  주도: [섹터]  |  투자금 한도 XX%
  → 조건: [무엇이 확인될 때]
B 약세 YY%  |  주의: [섹터]  |  투자금 한도 XX%
  → 조건: [무엇이 확인될 때]

📈 오늘 급등 — 내일 어떻게?
종목명(코드) 오늘+X.X%  →  [✅눌림후진입 / ❌추격금지 / ⏳관망]
  내일 지속 XX%  |  {'진입가 [가격표 수치 또는 조건]원  손절 -X%  목표 +Y%  손익비 Z:1' if has_price else '진입조건: [갭하락 양봉 확인 후] 손절 -X% 목표 +Y%  ← 원단위 숫자 금지'}
(수급 없는 급등 = ❌내일추격금지 한 줄로만)

💼 보유 종목 내일 행동
[종목명]: [홀드/추가매수/분할매도/전량매도] — [이유]
수익 중인 종목 손절선: [수익률 기준 올리기 — 원단위 숫자 없으면 % 기준으로만]

③ 내일 매수 (투자관→섹터→종목 논리 사슬 필수)
투자관 실행 논리: 우리 투자관의 [X 방향성] → [Y 섹터] 확대 → 아래 종목이 최적 표현
오늘 수급·거래대금이 실제 확인된 종목만. 단순 하락했다는 이유로 추천 금지.
{stock_block}
내일 조건 충족 종목 없으면: 내일은 관망

④ 1~3주 관심 종목
종목명(코드)  기간 X주  |  손익비 Z:1
  투자관 연결: [어떤 투자관 방향성을 실행하는가]
  이유: [팩트 한 줄]
  진입: [즉시/눌림/조건]  손절 -X%  목표 +Y%  이익 구간에서 손절선 올리기
(최대 3종목)

🚫 내일 금지
[무엇을] 하지 않는다 — [이유]

⚠️ 주의 이벤트
[이벤트명] [날짜] → 포지션 XX%로 줄이기

🌐 전문가 시각
[지금 글로벌 전문가들이 주목하는 것 한 줄]
→ 내일 돈 되는 함의: [어떤 종목·섹터에 기회가 생기는가]

━━━━━━━━━━━━━━━━━━━━━━━━━━
{three_line}
━━━━━━━━━━━━━━━━━━━━━━━━━━"""


def _build_prompt_intra1(has_price: bool) -> str:
    three_line = _3line_summary(has_price, "지금 실행 3줄", "[매수공격/선별매수/관망] — [이유 15자 이내]")
    price_warn = "" if has_price else "🚫 가격 미확인 — 이 브리핑에서 원 단위 숫자 기재 절대 금지\n"
    return f"""{_COMMON_HEADER}

{price_warn}━━━━━━━━━━━━━━━━━━━━━━━━━━
🕙 장중 1차 점검  (오전 10:00)
━━━━━━━━━━━━━━━━━━━━━━━━━━

⚡ 지금 결론  (한 줄)
→ [장전 전략 유효 ✅ / 수정 필요 ⚠️]  |  오후 상승확률 XX% / 하락확률 YY%

① 투자관 부합 + 장전 전략 검증
투자관 상태: [✅지지 / ⚠️도전] — 오늘 핵심 신호: [한 줄]
장전 예상: [섹터/방향]
현재 실제: KOSPI [수치] ([+/-X.X%]), KOSDAQ [수치] ([+/-X.X%]) — [주도 섹터]
판정: [일치 ✅ / 불일치 ❌]  원인: [한 줄]

② 지금 당장 해야 할 것 / 하면 안 되는 것
✅ [행동] — 조건: [수치 포함]
❌ [금지 행동] — 이유: [수치 포함]

③ 지금 급등 종목 — 진입 여부
종목명(코드) +XX%  →  [✅진입가능 / ❌추격금지 / ⏳눌림대기]
  재료: [한 줄]  수급: [외국인+기관 / 외국인 / 없음]
  {'진입가 ▶가격표즉시진입가◀원  손절 -X%  목표 +Y%  손익비 Z:1' if has_price else '🚫 원 단위 숫자 금지 — 진입 조건만 기재'}
(수급 없는 급등 = ❌추격금지 한 줄로만)

④ 오후 핵심 관전 포인트
미국 프리마켓: [S&P선물 수치] → 오후 한국 [영향]
경계 임계값: KOSPI [수치] 이탈 시 → [즉시 할 행동]
오후 주목 시간대: [시간] — [이유]

━━━━━━━━━━━━━━━━━━━━━━━━━━
🔔 3줄 즉시 실행 요약
{three_line}
━━━━━━━━━━━━━━━━━━━━━━━━━━"""


def _build_prompt_intra2(has_price: bool) -> str:
    three_line = _3line_summary(has_price, "지금 실행 3줄", "[홀드/익절XX%/손절/추가매수] — [이유 15자 이내]")
    price_warn = "" if has_price else "🚫 가격 미확인 — 이 브리핑에서 원 단위 숫자 기재 절대 금지\n"
    return f"""{_COMMON_HEADER}

{price_warn}━━━━━━━━━━━━━━━━━━━━━━━━━━
🕐 장중 2차 점검  (오후 13:00)
━━━━━━━━━━━━━━━━━━━━━━━━━━

⚡ 오후 방향
→ [강세유지 / 박스권 / 약세전환]  상승 XX% / 하락 YY%
S&P선물 [수치]  외국인 오전 [순매수/순매도] XXX억

📊 지금 내 포지션 행동
✅ 유지:  [조건 수치]
💰 익절:  XX% 매도 → 조건: [무엇이 될 때]
🛑 손절:  [조건] → 즉시 전량 (이유 묻지 말고 실행)
📥 추가:  X% → 조건: [무엇이 확인될 때]

⚠️ 마감 전 주의
[시간] [지표]가 [수준] 되면 → [즉시 할 행동]

━━━━━━━━━━━━━━━━━━━━━━━━━━
🔔 3줄 즉시 실행 요약
{three_line}
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

        # ── 누적 데이터 컨텍스트 주입 (DB 아카이브 → 추세 파악용) ──────
        try:
            from services.market_archive_service import (
                get_market_trend_context,
                get_intelligence_context,
            )
            trend_ctx = get_market_trend_context(days=7)
            if trend_ctx:
                context_parts.append(
                    f"\n[최근 7일 시장 추세 — 오늘과 비교하여 흐름 판단]\n{trend_ctx}"
                )
            intel_ctx = get_intelligence_context(days=5)
            if intel_ctx:
                context_parts.append(
                    f"\n[최근 인텔리전스 아카이브 — 서사 변화 추적]\n{intel_ctx}"
                )
        except Exception as _e:
            logger.debug("[CEO] 누적 데이터 컨텍스트 주입 실패: %s", _e)

        has_price = False        # 실시간 가격 데이터 주입 여부 추적
        _price_ctx_snap = ""    # 3줄 요약 후처리용 가격 스냅샷 (has_price=True 시 저장)

        # ── 월간 투자관 주입 — 최최우선 컨텍스트 (모든 판단의 헌법) ──────
        investment_thesis = state.get("investment_thesis", "")
        if investment_thesis:
            context_parts.insert(1,
                "\n[월간 투자관 — 이 투자관이 오늘 모든 판단의 최우선 기준. 투자관에 반하는 추천은 명시적 이유 필수]\n"
                + investment_thesis
                + "\n→ 오늘 추천이 투자관 방향과 정합하는지 브리핑 첫 줄에서 반드시 확인할 것"
            )
            logger.info("[CEO] 투자관 주입 완료")

        # ── 주간 중장기 전략 프레임 주입 — 모든 브리핑 유형에 공통 ────────────
        # 오늘의 단기 판단이 이번 주 중장기 방향과 일치하는지 CEO가 최우선으로 확인
        weekly_strategy = state.get("weekly_strategy_summary", "")
        if weekly_strategy:
            idx = 2 if investment_thesis else 1
            context_parts.insert(idx,
                "\n[이번 주 전략 프레임 — 오늘의 단기 추천이 이 방향과 정합해야 함]\n"
                + weekly_strategy
                + "\n→ 오늘 단기 판단이 위 전략 방향과 충돌하면 그 이유를 명시하고 조정할 것"
            )
            logger.info("[CEO] 주간 전략 프레임 주입 완료")

        # ── 급등종목 수급 교차분석 — 모든 브리핑 유형에 공통 주입 ──────────
        try:
            surge_ctx = _format_surge_context(state.get("raw_kis_data", {}))
            if surge_ctx:
                context_parts.append(
                    "\n[급등종목 수급 교차분석 — ④(장전)/⑨(장마감)/③(장중) 급등종목 즉시 판단의 기초 데이터]\n"
                    + surge_ctx
                )
                logger.info("[CEO] 급등종목 교차분석 주입 완료")
        except Exception as _e:
            logger.debug("[CEO] 급등종목 교차분석 주입 실패: %s", _e)

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
            if state.get("issue_stocks_report"):
                context_parts.append(
                    "\n[이슈종목 발굴 분석 — 📌 이슈종목 중기전략 섹션의 근거 데이터]\n"
                    + state["issue_stocks_report"]
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

                # 컨센서스 데이터 준비: raw 데이터 + 현재가 조합으로 full_consensus 구성
                _pre_consensus_map: dict = {}
                try:
                    from services.consensus_service import build_consensus_context, format_consensus_for_ceo
                    consensus_raw_data = state.get("consensus_data", {})
                    _raw = consensus_raw_data.get("_raw", {})
                    _name_map = consensus_raw_data.get("_name_map", {})
                    if _raw:
                        # 현재가 수집 (컨센서스 대상 종목)
                        _kis_prices: dict = {}
                        for _code in _raw:
                            try:
                                _pd = kis_pre.get_stock_price(_code, market=None)
                                if _pd.get("price"):
                                    _kis_prices[_code] = {"price": _pd["price"]}
                            except Exception:
                                pass
                        _full_consensus = build_consensus_context(
                            list(_raw.keys()), _name_map, _kis_prices, _raw
                        )
                        _pre_consensus_map = _full_consensus
                        if _full_consensus:
                            _cons_text = format_consensus_for_ceo(_full_consensus)
                            if _cons_text:
                                context_parts.append(
                                    "\n[애널리스트 컨센서스 목표주가 — 1차목표가로 반드시 사용]\n"
                                    + _cons_text
                                )
                                logger.info("[CEO] 컨센서스 목표주가 컨텍스트 주입 완료")
                except Exception as _ce:
                    logger.debug("[CEO] 컨센서스 컨텍스트 주입 실패: %s", _ce)

                price_ctx = _fetch_price_context(
                    state.get("candidates", []), kis_pre,
                    consensus_data=_pre_consensus_map,
                )
                if price_ctx and "조회 불가" not in price_ctx:
                    context_parts.append(
                        "\n[실시간 가격 데이터 — ③번 종목 추천의 진입/손절/목표가는 반드시 이 수치만 사용]\n"
                        + price_ctx
                    )
                    has_price = True
                    _price_ctx_snap = price_ctx
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

            # 포트폴리오 자산 성장 현황 주입 (연초 대비 NAV + KOSPI 벤치마크 비교)
            try:
                from services.nav_service import get_latest_nav, generate_nav_report
                nav = get_latest_nav()
                if nav:
                    nav_report = generate_nav_report(days=7)
                    alpha_signal = "✅초과수익 중" if nav["alpha_ytd"] >= 0 else "⚠️시장 하회 중"
                    context_parts.append(
                        f"\n[포트폴리오 자산 성장 현황 — 전략이 실제 자산을 키우고 있는가]\n"
                        f"  포트폴리오 연초대비: {nav['nav_pct_ytd']:+.2f}%  |  KOSPI 연초대비: 조회중\n"
                        f"  Alpha(초과수익): {nav['alpha_ytd']:+.2f}%  {alpha_signal}\n"
                        f"  오늘 총 손익: {nav['total_pnl_pct']:+.2f}%\n"
                        f"→ 이 수치가 개선되지 않으면 전략을 재검토해야 한다."
                    )
            except Exception as _ne:
                logger.debug("[CEO] NAV 주입 실패: %s", _ne)

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
            if state.get("issue_stocks_report"):
                context_parts.append(
                    "\n[이슈종목 발굴 분석 — 📌 이슈종목 중기전략 섹션의 근거 데이터]\n"
                    + state["issue_stocks_report"]
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
                # 컨센서스 데이터 준비 (장마감용)
                _close_consensus_map: dict = {}
                try:
                    from services.consensus_service import build_consensus_context, format_consensus_for_ceo
                    consensus_raw_data = state.get("consensus_data", {})
                    _raw_c = consensus_raw_data.get("_raw", {})
                    _name_map_c = consensus_raw_data.get("_name_map", {})
                    if _raw_c:
                        _kis_prices_c: dict = {}
                        for _code_c in _raw_c:
                            try:
                                _pd_c = kis_close.get_stock_price(_code_c, market=None)
                                if _pd_c.get("price"):
                                    _kis_prices_c[_code_c] = {"price": _pd_c["price"]}
                            except Exception:
                                pass
                        _full_consensus_c = build_consensus_context(
                            list(_raw_c.keys()), _name_map_c, _kis_prices_c, _raw_c
                        )
                        _close_consensus_map = _full_consensus_c
                        if _full_consensus_c:
                            _cons_text_c = format_consensus_for_ceo(_full_consensus_c)
                            if _cons_text_c:
                                context_parts.append(
                                    "\n[애널리스트 컨센서스 목표주가 — 1차목표가로 반드시 사용]\n"
                                    + _cons_text_c
                                )
                                logger.info("[CEO] 장마감 컨센서스 목표주가 컨텍스트 주입 완료")
                except Exception as _ce_c:
                    logger.debug("[CEO] 장마감 컨센서스 컨텍스트 주입 실패: %s", _ce_c)

                price_ctx = _fetch_price_context(
                    state.get("candidates", []), kis_close,
                    consensus_data=_close_consensus_map,
                )
                if price_ctx and "조회 불가" not in price_ctx:
                    context_parts.append(
                        "\n[실시간 가격 데이터 — ⑦번 종목 추천의 진입/손절/목표가는 반드시 이 수치만 사용]\n"
                        + price_ctx
                    )
                    has_price = True
                    _price_ctx_snap = price_ctx
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

        # 장중 실시간 가격 주입 — 신규 진입 추천 시 X,XXX원 placeholder 방지
        if run_type in (RUN_TYPE_INTRA1, RUN_TYPE_INTRA2):
            try:
                kis_intra = KISClient()
                price_ctx = _fetch_price_context(state.get("candidates", []), kis_intra)
                if price_ctx and "조회 불가" not in price_ctx:
                    context_parts.append(
                        "\n[실시간 가격 데이터 — 3줄 요약 📌줄의 진입가·손절가는 반드시 이 수치만 사용]\n"
                        + price_ctx
                    )
                    has_price = True
                    _price_ctx_snap = price_ctx  # 3줄 후처리용 저장
                    logger.info("[CEO] 장중 실시간 가격 데이터 주입 완료")
                else:
                    context_parts.append(
                        "\n🚫 현재가 없음: 3줄 요약 📌줄에서 진입가·손절가 원 단위 숫자 금지. '신규진입없음' 사용."
                    )
            except Exception as _ie:
                logger.warning("[CEO] 장중 가격 조회 실패: %s", _ie)
                context_parts.append(
                    "\n🚫 현재가 없음: 3줄 요약 📌줄에서 진입가·손절가 원 단위 숫자 금지. '신규진입없음' 사용."
                )

        # 장중(intra) 브리핑에도 DART 공시 포함
        if run_type in (RUN_TYPE_INTRA1, RUN_TYPE_INTRA2) and state.get("dart_disclosures"):
            from agents.dart_alert_agent import format_disclosures_for_briefing
            dart_text = format_disclosures_for_briefing(state["dart_disclosures"])
            if dart_text:
                context_parts.append(
                    "\n[오늘 주요 DART 공시]\n" + dart_text
                )

        # 중장기 종목 추천 (3~12개월 관점) — 장전/장마감 브리핑에만 포함
        if run_type in (RUN_TYPE_PRE, RUN_TYPE_CLOSE) and state.get("midterm_stock_report"):
            context_parts.append(
                "\n[중장기 유망주 분석 — 브리핑 말미에 '📐 중장기 유망주' 섹션으로 반드시 포함]\n"
                + state["midterm_stock_report"]
            )

        # 포트폴리오 매니저 분석 (보유 종목 행동 지시 + 워치리스트 트리거)
        if state.get("portfolio_report"):
            context_parts.append(
                "\n[포트폴리오 매니저 분석 — ⑤번 보유 포지션 행동 지시의 기반]\n"
                + state["portfolio_report"]
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

        # 실행 유형별 프롬프트 선택 — 모든 유형이 has_price에 따라 3줄 요약을 다르게 생성
        if run_type == RUN_TYPE_PRE:
            prompt = _build_prompt_pre(has_price)
        elif run_type == RUN_TYPE_CLOSE:
            prompt = _build_prompt_close(has_price)
        elif run_type == RUN_TYPE_INTRA1:
            prompt = _build_prompt_intra1(has_price)
        else:
            prompt = _build_prompt_intra2(has_price)

        result = chat_ceo(prompt, context, max_tokens=2000)
        state["ceo_report"] = result

        # ── 손익비 3:1 원칙 자동 집행 ────────────────────────────────
        # 가격 데이터가 실제로 주입된 장전/장마감 브리핑에만 적용
        # (has_price=False 상황 = 조건 기반 추천 → 수치 검증 대상 없음)
        if has_price and run_type in (RUN_TYPE_PRE, RUN_TYPE_CLOSE):
            try:
                result, rr_notices = _validate_rr_in_report(result)
                removed = sum(1 for n in rr_notices if n.startswith('❌'))
                passed  = sum(1 for n in rr_notices if '통과' in n or (n and not n.startswith('❌') and not n.startswith('⚠️')))
                if removed:
                    logger.warning("[CEO] 손익비 미달 종목 %d개 자동 제외 완료", removed)
                else:
                    logger.info("[CEO] 손익비 검증 통과 (제외 종목 없음)")
                state["ceo_report"] = result
            except Exception as _rr_e:
                logger.warning("[CEO] 손익비 검증 실패 — 원본 유지: %s", _rr_e)

        # 3줄 요약 블록 추출 → X,XXX원 플레이스홀더 강제 치환 → 먼저 발송
        try:
            compact_match = re.search(r"(╔═══.*?╚═+)", result, re.DOTALL)
            if compact_match:
                compact = compact_match.group(1)
                # 실제 가격으로 강제 치환 (LLM 출력 패턴에 무관하게 코드 레벨 보장)
                compact = _fix_price_placeholders(compact, _price_ctx_snap)
                send_message(compact)
                logger.info("[CEO] 3줄 요약 먼저 발송 완료")
        except Exception as e:
            logger.debug("[CEO] 3줄 요약 발송 실패: %s", e)

        # 전체 리포트에서도 플레이스홀더 제거 후 state 저장
        if _price_ctx_snap:
            result = _fix_price_placeholders(result, _price_ctx_snap)
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
