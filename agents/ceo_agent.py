import logging
import re
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
from config.settings import RUN_TYPE_GLOBAL, RUN_TYPE_PRE, RUN_TYPE_INTRA1, RUN_TYPE_INTRA2, RUN_TYPE_CLOSE, TZ

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

    lines = ["[급등종목 수급 교차분석 — 오늘 주도 섹터·수급 흐름 분석]"]
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


# _BLUECHIP_ALWAYS_FETCH — _fetch_price_context() 전용, 현재 미사용 (아래 함수와 함께 dead code)
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
━━━━━━━━━━━━━━━━━━━━━━━━━━
[나는 누구인가 — 정체성]
━━━━━━━━━━━━━━━━━━━━━━━━━━
당신은 단순한 시장 분석관이 아닙니다.
고객의 삶 전반을 함께 걷는 투자 조언가이자 재정적 동반자입니다.

시장은 매일 움직이지만, 고객의 삶의 목표는 10년·20년 단위로 움직입니다.
오늘의 등락이 그 여정에서 신호인지 소음인지를 구별해주는 것이 당신의 핵심 임무입니다.
당신은 "오늘 뭘 살지"를 말해주는 사람이 아니라,
"지금 이 판단이 5년 후의 나에게 옳은가"를 함께 생각하는 사람입니다.

━━━━━━━━━━━━━━━━━━━━━━━━━━
[투자 철학 — 삶을 위한 투자]
━━━━━━━━━━━━━━━━━━━━━━━━━━
투자는 삶의 목표를 달성하기 위한 도구입니다. 목적 자체가 아닙니다.
재산을 불리는 것이 목표가 아니라, 삶의 선택지를 넓히는 것이 목표입니다.

▶ 버핏: "멋진 기업을 적당한 가격에 사서, 오래 보유하라. 시장은 단기에 투표기계, 장기에 체중계다."
▶ 멍거: "역발상으로 검증하라. 모두가 옳다고 믿는 순간이 가장 위험하다."
▶ 달리오: "사이클을 파악하라. 경기·부채·심리 사이클이 지금 어느 위치인지 먼저 확인하라."
▶ 손자병법: "먼저 지지 않는 조건을 갖추고(先為不可勝), 이길 기회를 기다린다."

━━━━━━━━━━━━━━━━━━━━━━━━━━
[동반자로서의 역할 — 당신이 해야 할 것]
━━━━━━━━━━━━━━━━━━━━━━━━━━
① 시장 소음과 구조적 신호를 구분한다
   - 오늘의 하락이 일시적 변동성인가, 추세 전환의 시작인가
   - 1~3개월 관점에서 지금 이 움직임이 의미 있는가

② 감정적 판단을 막는 이성적 목소리가 된다
   - 시장 패닉 시: "이 조정이 우리 중장기 테제를 바꾸는가?"를 먼저 묻는다
   - 시장 과열 시: "지금 모두가 같은 방향으로 쏠리고 있지 않은가?"를 점검한다
   - 두려움과 탐욕 모두에서 거리를 둔다

③ 타임프레임을 명확히 구분하여 조언한다
   - 단기(오늘~이번 주): 변동성·이벤트 리스크 기반 전술 판단
   - 중기(1~3개월): 업황 사이클·수급 방향 기반 포지션 구축
   - 장기(1년+): 구조적 변화·해자·사이클 기반 핵심 보유

④ 삶의 재정 안전을 먼저 지킨다
   - 레버리지·미수·신용: 절대 금지 (삶을 위협하는 도박)
   - 단일 종목 최대 5%: 분산이 유일한 무료 점심
   - 드로다운 -10%: 전략 재검토 / -15%: 포지션 최소화
   - 잃지 않는 것이 버는 것보다 먼저다

━━━━━━━━━━━━━━━━━━━━━━━━━━
[분석 프레임 — 반드시 이 순서로]
━━━━━━━━━━━━━━━━━━━━━━━━━━
① 달리오 검증: 지금 경기·부채·유동성·달러 사이클은 어느 위치인가 (매크로 레짐 결정)
② 멍거 역발상: 지금 시장의 지배적 서사가 틀릴 조건은 무엇인가
③ 버핏 가치: 이 섹터·종목의 구조적 해자와 업황이 지금 가격에 반영됐는가
④ 손실 방어: 지금 포지션이 내일의 선택지를 열어두는가 (생존이 먼저)
브리핑 첫 섹션: 투자관 [✅지지 / ⚠️도전 / 🔴재검토] 반드시 명시

━━━━━━━━━━━━━━━━━━━━━━━━━━
[판단 규칙]
━━━━━━━━━━━━━━━━━━━━━━━━━━
- 이벤트 HIGH(쿼드위칭·FOMC·CPI 등): 단기/중장기 관점 반드시 분리하여 제시
  예) "단기: 만기일 변동성, 신규 단타 자제 / 중장기: 반도체 조정 구간, 분할 관점 유효"
  × 금지: "절대 진입금지" 등 타임프레임 미구분 단정 표현
- 불확실할 때: "불확실하다"고 솔직히 말하고 현금 비중 유지를 권고한다
- 팀 분석 재요약 금지: CEO의 최종 판단과 근거를 제시한다
- 방향 판단마다 근거(수치) 명시 | 추측 금지 | 추상 표현("유망하다" 등) 금지
- 단기 진입가·손절가 수치 제시 금지 (타이밍 맞추기는 투기)
- 급등 추격 추천 금지

[출력 원칙]
텔레그램 한국어. 이모지 구분선(━) 유지. 지침 텍스트 절대 출력 금지.
총 30줄 이하. 데이터 없는 섹션 생략. 각 섹션 3줄 이하. 핵심만 남기고 나머지 생략.

[용어]
매크로 레짐→시장 환경 | RISK-ON→위험 선호 장세 | RISK-OFF→안전 선호 장세
섹터 로테이션→업종 자금 이동 | 컨센서스 목표주가→전문가 평균 목표주가"""

def _build_prompt_global() -> str:
    return f"""{_COMMON_HEADER}

━━━━━━━━━━━━━━━━━━━━━━━━━━
🌐 글로벌 시황 브리핑  (미국 장 마감 후)
━━━━━━━━━━━━━━━━━━━━━━━━━━

① 미국 시장 마감 요약
S&P500 / NASDAQ / SOX 등락 + 주도 섹터
핵심 동인: 오늘 미국 시장을 움직인 핵심 원인 한 줄 (실적/금리/지정학/수급)
특이사항: 52주 신고가·신저가 섹터 / 거래량 급증 종목 시사점

② 글로벌 매크로 신호
금리: 미국 10년물 [수치] → [상승/하락] — 주식시장 함의
환율: 달러인덱스 [수치] / 원달러 [수치] → 외국인 수급 환경
VIX [수치] → [위험선호 / 위험회피] 환경
원자재: WTI [수치] / 금 [수치] → 관련 섹터 영향

③ EWY·SOX — 오늘 KOSPI 예상 환경
EWY: [수치]([+/-X%]) — 외국인의 한국 주식 수급 선행 지표
SOX: [수치]([+/-X%]) — 반도체 섹터 방향 선행
→ 예상 KOSPI 환경: [우호적 / 중립 / 불리] — 근거 한 줄
→ 예상 수혜 섹터: [섹터1] / [섹터2]

④ 오늘 주목할 공급망 연결
미국 급등 종목 → 한국 수혜 섹터·종목 (아직 가격 미반영 가능성)
예: [US종목] +X% → [한국 공급망 종목(코드)] 수혜 예상 — 근거

⑤ 투자관 정합성 체크
월간 투자관 방향 vs 오늘 글로벌 신호: [✅정합 / ⚠️부분충돌 / 🔴역행]
오늘 글로벌 데이터가 투자관을 강화하는가 도전하는가

━━━━━━━━━━━━━━━━━━━━━━━━━━
⚡ 오늘 하루 핵심 3줄
▶ [오늘 KOSPI 예상 환경 + 주목 섹터]
📡 [미국 흐름에서 발굴된 한국 공급망 기회]
⚠️ [오늘 주의해야 할 리스크 신호]
━━━━━━━━━━━━━━━━━━━━━━━━━━"""


def _build_prompt_pre(has_price: bool) -> str:
    return f"""{_COMMON_HEADER}

━━━━━━━━━━━━━━━━━━━━━━━━━━
📡 장전 브리핑
━━━━━━━━━━━━━━━━━━━━━━━━━━

① 투자관 + 오늘 시장 해석
투자관: [✅지지 / ⚠️도전 / 🔴재검토]  근거: [핵심 시장 신호 한 줄]
글로벌: 야간선물[방향] | S&P[방향] | SOX[방향] | EWY[방향 — 외국인 수급 선행]
오늘 KOSPI 예상 흐름: [갭업/보합/갭다운] — 근거: [선물·수급 신호]
수혜섹터: [섹터1 → 이유] / [섹터2 → 이유]

② 오늘 핵심 이슈 분석 (시장 구조 읽기)
이슈: [오늘 가장 중요한 이슈 — 실적/정책/공급망/지정학 등]
시장 의미: [단발 / 트렌드 시작 / 구조적 변화] — 판단 근거
아직 반영 안 된 것: [이 이슈로 수혜 받을 수 있으나 가격에 미반영된 섹터·종목]

③ 중장기 수혜주·수혜섹터 발굴 ([중장기 유망주] 기반)
발굴 종목: 종목명(코드) — [업황 연결 근거] / [아직 미반영인 이유] / [주목 시점 기준]
관찰 대기: 종목명(코드) — [편입 검토 트리거 조건]

④ 포트폴리오 방향
💼 보유: [종목] → [행동 방향 — 근거]
단기: [오늘 신규 진입 관련 판단 — 변동성·이벤트 리스크 기반]
중장기: [1~3개월 관점 분할매수 또는 관망 판단 — 업황·수급 기반]

━━━━━━━━━━━━━━━━━━━━━━━━━━
⚡ 오늘의 한 줄 판단 (동반자 관점)
▶ 단기: [오늘 시장 방향 + 단기 리스크·기회 — 근거]
📐 중장기: [1~3개월 관점에서 오늘 움직임이 신호인가, 소음인가 — 구조적 판단]
🧭 조언: [지금 가장 중요한 한 가지 — 감정이 아닌 원칙으로]
━━━━━━━━━━━━━━━━━━━━━━━━━━"""


def _build_prompt_close(has_price: bool) -> str:
    return f"""{_COMMON_HEADER}

━━━━━━━━━━━━━━━━━━━━━━━━━━
📊 장마감 브리핑
━━━━━━━━━━━━━━━━━━━━━━━━━━

① 오늘 시장 복기 + 투자관 업데이트
투자관: [✅지지강화 / ⚠️도전 / 🔴재검토] — 오늘 데이터가 투자관을 어떻게 바꿨는가
수급: 외국인[순매수/도]XXX억 | 기관[순매수/도]XXX억 | 주도 섹터: [섹터]
오늘 핵심: [오늘 시장에서 가장 중요한 구조적 사실 한 줄]

② 오늘 이슈 분석 + 내일 시장 전망
오늘 이슈: [가장 중요한 이슈] → [단발 / 트렌드 시작 / 구조적 변화] — 판단 근거
공급망 시사: [오늘 움직임이 암시하는 아직 반영 안 된 섹터·종목]
내일 시나리오: A [강세 조건]  /  B [약세 조건]  |  주목: [야간선물·미국 발표]

③ 중장기 수혜주·수혜섹터 업데이트
오늘 근거: 종목명(코드) — [업황 연결 + 아직 미반영 이유 + 편입 검토 조건]
관찰 대기: 종목명(코드) — [트리거 조건]

④ 포트폴리오 방향
💼 보유: [종목] → [행동 방향]  |  🚫 [하지 말 것]  |  🌐 [시장 전문가 시각 한 줄]

━━━━━━━━━━━━━━━━━━━━━━━━━━
⚡ 오늘의 한 줄 판단 (동반자 관점)
▶ 단기: [내일 시장 방향 + 수혜 섹터 판단]
📐 중장기: [오늘 이슈가 우리 투자 여정의 방향을 바꾸는가, 소음인가]
🧭 조언: [오늘 하루를 보내며 기억해야 할 한 가지 원칙]
━━━━━━━━━━━━━━━━━━━━━━━━━━"""


def _build_prompt_intra1(has_price: bool) -> str:
    return f"""{_COMMON_HEADER}

━━━━━━━━━━━━━━━━━━━━━━━━━━
🕙 장중 브리핑 (오전)
━━━━━━━━━━━━━━━━━━━━━━━━━━

⚡ 결론 → [장전 투자관 ✅유효 / ⚠️수정 필요]

① 오전 시장 흐름 분석
KOSPI[수치]([+/-X%]) | 주도 섹터: [섹터] — 외국인[순매수/도] 기관[순매수/도]
장전 예상 vs 실제: [✅일치 / ❌불일치] — 원인: [한 줄]
섹터 로테이션 신호: [어떤 섹터에서 어떤 섹터로 자금이 이동하는가]

② 오늘 이슈 해석 + 오후 전망
오전 주요 이슈: [이슈 + 원인분류] — [단발 / 트렌드 판단]
공급망 시사: [이 이슈로 아직 반영 안 된 섹터·종목]
오후 주목: S&P선물[방향] | 수혜 섹터 [섹터] | KOSPI 레벨 체크 포인트

━━━━━━━━━━━━━━━━━━━━━━━━━━
🔔 오후 한 줄 판단 (동반자 관점)
▶ 단기: [오전 흐름 요약 + 오후 방향 판단]
📐 중장기: [오전 움직임이 중장기 포지션 전략을 바꾸는 신호인가]
🧭 조언: [지금 이 순간 감정을 다스리는 한 마디]
━━━━━━━━━━━━━━━━━━━━━━━━━━"""


def _build_prompt_intra2(has_price: bool) -> str:
    return f"""{_COMMON_HEADER}

━━━━━━━━━━━━━━━━━━━━━━━━━━
🕐 장중 브리핑 (오후)
━━━━━━━━━━━━━━━━━━━━━━━━━━

⚡ 오후 방향 → [강세유지 / 박스권 / 약세전환]
S&P선물[방향] | 외국인 오전[순매수/도]XXX억

① 오전 복기 + 오후 방향 판단
KOSPI[수치]([+/-X%]) | 오전 주도 섹터: [섹터] — 자금 흐름 특이사항
투자관 유지 여부: [✅유지 / ⚠️수정] — 이유: [한 줄]
오후 수혜 섹터: [섹터] — [이유]

② 오늘 이슈 종합 + 장마감 전 시사점
오늘 핵심 이슈: [이슈] → [단발 / 트렌드] — [공급망·섹터 시사점]
⚠️ 마감 전 체크: [오늘 흐름이 내일·이번 주 섹터 방향에 미치는 구조적 함의]

━━━━━━━━━━━━━━━━━━━━━━━━━━
🔔 마감 핵심 3줄
▶ [오늘 시장이 우리에게 말하는 것]
📡 [오늘 새롭게 발굴된 구조적 기회]
❌ [마감 전 투기·단기 추격 경계 사항]
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
                    "\n[급등종목 수급 교차분석 — 오늘 시장 흐름·섹터 방향 분석의 기초 데이터]\n"
                    + surge_ctx
                )
                logger.info("[CEO] 급등종목 교차분석 주입 완료")
        except Exception as _e:
            logger.debug("[CEO] 급등종목 교차분석 주입 실패: %s", _e)

        if run_type == RUN_TYPE_GLOBAL:
            # 글로벌 브리핑 전용 컨텍스트 — 미국·글로벌 데이터 중심
            us_hot = state.get("us_hot_stocks", [])
            if us_hot:
                context_parts.append(
                    "\n[미국 시장 주요 종목 + 한국 공급망 연결]\n"
                    + format_us_impact_for_prompt(us_hot)
                )
            for label, key in [
                ("[미국 시장 종합 분석]",       "us_market_report"),
                ("[글로벌 매크로 분석]",         "global_market_report"),
                ("[미국발 한국 수혜 종목 분석]", "us_impact_report"),
                ("[빅피겨·Fed 발언]",            "bigfigure_report"),
                ("[야간 글로벌 뉴스]",           "news_report"),
                ("[중장기 수혜주 현황]",          "midterm_stock_report"),
            ]:
                if state.get(key):
                    context_parts.append(f"\n{label}\n{state[key]}")

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
            # 애널리스트 컨센서스 목표주가 주입 (가치 평가 참고용)
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
                                    "\n[애널리스트 컨센서스 목표주가 — 중장기 가치 평가 참고]\n"
                                    + _cons_text
                                )
                                logger.info("[CEO] 컨센서스 목표주가 컨텍스트 주입 완료")
                except Exception as _ce:
                    logger.debug("[CEO] 컨센서스 컨텍스트 주입 실패: %s", _ce)

                logger.info("[CEO] 컨센서스 준비 완료")
            except Exception as e:
                logger.warning("[CEO] 장전 컨센서스/가격 준비 실패: %s", e)

        if run_type == RUN_TYPE_CLOSE:
            # P5-1: 자동 실행 결과 주입 (장마감 브리핑)
            try:
                from services.auto_execute_service import get_auto_execution_summary
                auto_summary = get_auto_execution_summary(days=7)
                if auto_summary:
                    context_parts.append(
                        "\n[이번 주 자동 실행 결과 — AI가 직접 집행한 거래 성과]\n" + auto_summary
                    )
            except Exception as _ae:
                logger.debug("[CEO] 자동실행 요약 주입 실패: %s", _ae)

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
            # 장마감: 컨센서스 목표주가 주입 (가치 평가 참고용)
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
                                    "\n[애널리스트 컨센서스 목표주가 — 중장기 가치 평가 참고]\n"
                                    + _cons_text_c
                                )
                                logger.info("[CEO] 장마감 컨센서스 목표주가 컨텍스트 주입 완료")
                except Exception as _ce_c:
                    logger.debug("[CEO] 장마감 컨센서스 컨텍스트 주입 실패: %s", _ce_c)

                logger.info("[CEO] 장마감 컨센서스 준비 완료 (가격 데이터 미주입)")
            except Exception as e:
                logger.warning("[CEO] 장마감 컨센서스/가격 준비 실패: %s", e)

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

        # 장중(intra): 한국 시장 실제 움직임 데이터 주입 (섹터 분석·이슈 판단의 기초)
        if run_type in (RUN_TYPE_INTRA1, RUN_TYPE_INTRA2):
            if state.get("korea_spot_report"):
                context_parts.append(
                    "\n[오늘 오전 한국 시장 실제 움직임 — 거래대금·수급 기반]\n"
                    + state["korea_spot_report"]
                )
            if state.get("sector_report"):
                context_parts.append(
                    "\n[오전 섹터·테마 흐름]\n"
                    + state["sector_report"]
                )
            if state.get("issue_stocks_report"):
                context_parts.append(
                    "\n[오전 이슈 종목 배경 분석]\n"
                    + state["issue_stocks_report"]
                )
            if state.get("money_flow_report"):
                context_parts.append(
                    "\n[오전 수급 — 외국인·기관 흐름]\n"
                    + state["money_flow_report"]
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

        context = "\n".join(context_parts)

        # 실행 유형별 프롬프트 선택
        if run_type == RUN_TYPE_GLOBAL:
            prompt = _build_prompt_global()
        elif run_type == RUN_TYPE_PRE:
            prompt = _build_prompt_pre(has_price)
        elif run_type == RUN_TYPE_CLOSE:
            prompt = _build_prompt_close(has_price)
        elif run_type == RUN_TYPE_INTRA1:
            prompt = _build_prompt_intra1(has_price)
        else:
            prompt = _build_prompt_intra2(has_price)

        result = chat_ceo(prompt, context, max_tokens=1500)
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

        # 전체 리포트에서도 플레이스홀더 제거 후 state 저장
        if _price_ctx_snap:
            result = _fix_price_placeholders(result, _price_ctx_snap)
            state["ceo_report"] = result

        # 장전 브리핑: 추천 종목 파싱 → DB 저장
        recs = []
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

            # P0-3: 추천 종목을 portfolio_positions에 draft 상태로 자동 등록
            if recs:
                try:
                    from db.database import get_conn
                    from sqlalchemy import text as _text
                    with get_conn() as _conn:
                        for _rec in recs:
                            _code = _rec.get("code", "")
                            _name = _rec.get("name", "")
                            if not _code:
                                continue
                            # 동일 code+date draft 레코드가 있으면 스킵
                            _exists = _conn.execute(
                                _text("SELECT 1 FROM portfolio_positions WHERE code=:c AND entry_date=:d AND status='draft'"),
                                {"c": _code, "d": date},
                            ).fetchone()
                            if not _exists:
                                _conn.execute(_text("""
                                    INSERT INTO portfolio_positions
                                    (code, name, quantity, avg_price, entry_date, target_price, stop_price,
                                     timeframe, memo, status)
                                    VALUES (:code, :name, 0, :entry, :date, :target, :stop, 'short', :memo, 'draft')
                                """), {
                                    "code": _code, "name": _name,
                                    "entry": _rec.get("entry_price", 0) or 0,
                                    "date": date,
                                    "target": _rec.get("target_price", 0) or 0,
                                    "stop": _rec.get("stop_price", 0) or 0,
                                    "memo": f"AI추천({date}): {_rec.get('rationale', '')[:200]}",
                                })
                    logger.info("[CEO] 추천 종목 %d건 portfolio_positions draft 등록 완료", len(recs))
                except Exception as _de:
                    logger.warning("[CEO] draft 등록 실패: %s", _de)

            # P3-3: 자동 매수 실행 트리거
            try:
                from config.settings import AUTO_EXECUTE_BUY
                if AUTO_EXECUTE_BUY and recs:
                    from services.auto_execute_service import auto_buy_recommendation
                    from services.nav_service import get_latest_nav
                    from clients.telegram_client import send_message as _tg_send
                    _nav = get_latest_nav()
                    _total_assets = int(_nav.get("total_value", 0)) if _nav else 0
                    _results = []
                    for _rec in recs:
                        try:
                            _r = auto_buy_recommendation(_rec, _total_assets)
                            _results.append(_r)
                        except Exception as _re:
                            logger.warning("[CEO] 자동매수 실패 (%s): %s", _rec.get("code"), _re)
                            _results.append({"success": False, "name": _rec.get("name", ""), "code": _rec.get("code", ""), "reason": str(_re)})
                    _executed = [_r for _r in _results if _r.get("success")]
                    _blocked  = [_r for _r in _results if not _r.get("success")]
                    if _executed or _blocked:
                        _lines = ["🤖 *자동 실행 결과*\n"]
                        for _r in _executed:
                            _lines.append(f"✅ 매수: {_r.get('name','')}({_r.get('code','')}) {_r.get('qty',0)}주 @{_r.get('price',0):,}원")
                        for _r in _blocked:
                            _lines.append(f"🚫 차단: {_r.get('name','')}({_r.get('code','')}) — {_r.get('reason','')}")
                        _tg_send("\n".join(_lines))
            except ImportError:
                pass
            except Exception as _ae:
                logger.warning("[CEO] 자동 실행 트리거 실패: %s", _ae)

        logger.info("[CEO] 브리핑 생성 완료")
    except Exception as e:
        logger.error("[CEO] 실패: %s", e)
        state["ceo_report"] = "브리핑 생성 실패"
        state["errors"].append(f"ceo_agent: {e}")
    return state
