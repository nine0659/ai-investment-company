"""
agents/discovery_agent.py
종목 발굴 에이전트 — 탑다운 (시장 흐름 → 주도 산업 → 종목 발굴 → 워치리스트 등록)

역할:
  1. 글로벌·국내 시장 흐름에서 주도 산업을 읽는다
  2. 외국인·기관 수급 + 거래대금 상위에서 후보 풀을 만든다 (보유 종목 제외)
  3. 후보마다 실제 가격·PER·EPS·애널리스트 컨센서스로 검증한다
  4. 고객 프로필(집중도·감내선) 기준으로 2~3개를 발굴한다
  5. 발굴 종목을 워치리스트에 자동 등록 → 15분 모니터가 진입 조건 추적

실행: 매주 화요일 19:00 (scheduler) + 텔레그램 /discover 온디맨드
"""
import logging
import re
import time as _time
from datetime import datetime
from zoneinfo import ZoneInfo

from clients.openai_client import chat
from clients.kis_client import KISClient
from clients.market_data_client import fetch_global_market_data
from clients.us_market_client import fetch_us_sectors
from clients.telegram_client import send_message

logger = logging.getLogger(__name__)
_TZ = ZoneInfo("Asia/Seoul")

_MAX_CANDIDATES = 15   # 컨센서스 API 속도 제한 고려 상한
_MIN_MARKET_CAP_억 = 3000   # 시가총액 하한 — 중장기 가치투자 대상 최소 규모
_MIN_ANALYSTS = 3           # 컨센서스 신뢰 최소 애널리스트 수

# ETF/ETN/인버스/레버리지 등 파생상품 제외 (기업이 아님)
_EXCLUDE_NAME = re.compile(
    r"KODEX|TIGER|KBSTAR|ARIRANG|HANARO|SOL |PLUS |ACE |ETN|인버스|레버리지|선물|채권|배당주|"
    r"코스닥150|코스피200|2X|3X"
)

_SYSTEM = """당신은 한 명의 고객을 전담하는 투자 자문가의 종목 발굴 책임자입니다.
탑다운으로 발굴합니다: 글로벌 시장 흐름 → 주도 산업 → 그 산업에서 수급이 확인된 종목.

[발굴 철학]
- Howard Marks: "좋은 기업"이 아니라 "좋은 기업인데 왜 지금 저평가인가"가 답이어야 한다
- 수급 확인: 외국인·기관이 실제로 사고 있는 종목 우선 (컨텍스트의 수급 데이터 근거)
- 컨센서스 업사이드: 애널리스트 평균 목표가 대비 상승 여력이 실제 숫자로 존재해야 한다
- 고객 우선: [고객 프로필]의 집중도·감내선을 반영하라.
  고객이 이미 과집중한 섹터의 종목은 "이미 충분히 보유한 산업"이라고 명시하고
  다른 산업에서의 발굴을 우선하라. 분산 가치가 있는 종목에 가점.

[절대 규칙]
- 컨텍스트에 없는 가격·PER·EPS·목표가·수치를 지어내지 말 것.
  검증 데이터가 없는 종목은 추천 불가. 예외 없음.
- 컨센서스 업사이드 +15% 미만 종목은 추천하지 말 것 (안전마진 부족).
- 후보가 전부 부적합하면 "이번 주 발굴 없음"이 옳은 답이다. 억지 추천 금지.
  발굴 없음도 유효한 결론이며, 그 이유(예: 급락장이라 수급 왜곡)를 한 줄로 설명하라.
- 모든 추천에 반증 조건(이것이 발생하면 논리 폐기) 필수.

[출력 형식 — 텔레그램용, 한국어, 전문용어 풀어서]
🔭 종목 발굴 리포트

① 시장 흐름 (3줄 이내)
[글로벌→국내 자금 흐름의 방향, 실수치 인용]

② 주도 산업 (2~3개)
[산업명] — [왜 지금인지, 수치 근거]

③ 발굴 종목 (2~3개, 없으면 "이번 주 발굴 없음")
종목명(코드) | 현재가 X원 | 컨센서스 목표 Y원 (업사이드 +Z%)
  왜 지금: [저평가·수급 근거, 실수치]
  진입 접근: [현 수준 ±X% 분할 등]
  반증 조건: [이것이 발생하면 폐기]
  포트폴리오 적합성: [고객 집중도 관점 — 분산 기여 여부]

④ 고객 포트폴리오 함의 (2줄 이내)
[월 신규 자금 배분 제안 포함]

마지막에 반드시 아래 블록 출력 (파싱용, 메시지에서 제거됨):
=WATCH_START=
watch|코드|종목명|진입희망가(숫자만)|발굴논리 한 줄
=WATCH_END=
발굴 종목이 없으면 블록 안을 비워둘 것."""

_WATCH_RE = re.compile(r"=WATCH_START=[ \t]*\n(.*?)\n?[ \t]*=WATCH_END=", re.DOTALL)


def _get_holding_codes() -> set[str]:
    try:
        from services.portfolio_service import get_portfolio
        return {p["code"] for p in get_portfolio()}
    except Exception:
        return set()


def _build_candidate_pool(kis: KISClient, exclude: set[str]) -> list[dict]:
    """외국인·기관·거래대금 순위에서 후보 풀 구성 (보유 종목 제외)."""
    pool: dict[str, dict] = {}
    sources = [
        ("외국인순매수", "J", "KOSPI",  kis.get_foreign_buy_rank),
        ("외국인순매수", "Q", "KOSDAQ", kis.get_foreign_buy_rank),
        ("기관순매수",   "J", "KOSPI",  kis.get_institution_buy_rank),
        ("거래대금상위", "J", "KOSPI",  kis.get_amount_rank),
        ("거래대금상위", "Q", "KOSDAQ", kis.get_amount_rank),
    ]
    for label, mkt_code, mkt_name, fn in sources:
        try:
            for item in fn(mkt_code)[:8]:
                code = item.get("mksc_shrn_iscd") or item.get("stck_shrn_iscd", "")
                name = item.get("hts_kor_isnm", "")
                if not code or code in exclude:
                    continue
                if _EXCLUDE_NAME.search(name):
                    continue  # ETF/ETN/인버스 등 파생상품 제외
                if code in pool:
                    pool[code]["signals"].append(label)
                else:
                    pool[code] = {
                        "code": code, "name": name, "market": mkt_name,
                        "signals": [label],
                    }
        except Exception as e:
            logger.debug("[발굴] %s(%s) 순위 조회 실패: %s", label, mkt_code, e)

    # 신호 수 많은 순 (수급이 여러 경로로 확인될수록 우선)
    ranked = sorted(pool.values(), key=lambda x: -len(set(x["signals"])))
    return ranked[:_MAX_CANDIDATES]


def _enrich_candidates(kis: KISClient, candidates: list[dict]) -> list[dict]:
    """후보마다 실제 가격·밸류·컨센서스 수집.

    품질 필터 (중장기 가치투자 대상 요건):
      - 시가총액 3,000억 이상 (급락일 거래대금 순위를 채우는 투기성 소형주 배제)
      - 애널리스트 3명 이상 컨센서스 존재 (검증 불가 종목 추천 금지)
    """
    from clients.consensus_client import fetch_analyst_targets
    enriched = []
    for c in candidates:
        try:
            pd = kis.get_stock_price(c["code"], market="J" if c["market"] == "KOSPI" else "Q")
            if not pd or not pd.get("price"):
                continue
            if pd.get("market_cap_억", 0) < _MIN_MARKET_CAP_억:
                continue
            c["price"]    = pd["price"]
            c["per"]      = pd.get("per", 0)
            c["eps"]      = pd.get("eps", 0)
            c["chg_pct"]  = pd.get("change_pct", 0)
            c["w52_high"] = pd.get("52w_high", 0)
            c["w52_low"]  = pd.get("52w_low", 0)
            c["mcap_억"]  = pd.get("market_cap_억", 0)
        except Exception:
            continue

        try:
            cons = fetch_analyst_targets(c["code"], market=c["market"])
            _time.sleep(0.3)
            if not cons or not cons.get("avg_target") or cons.get("analyst_count", 0) < _MIN_ANALYSTS:
                continue  # 컨센서스 없는 종목은 추천 자격 없음
            c["target"]   = cons["avg_target"]
            c["analysts"] = cons["analyst_count"]
            c["opinion"]  = cons.get("consensus_opinion", "")
            c["upside"]   = round((cons["avg_target"] - c["price"]) / c["price"] * 100, 1)
        except Exception:
            continue
        enriched.append(c)
    return enriched


def _format_candidates(cands: list[dict]) -> str:
    lines = []
    for c in cands:
        lines.append(
            f"{c['name']}({c['code']}) [{c['market']}] 시총 {c['mcap_억']:,}억 "
            f"수급신호: {'+'.join(sorted(set(c['signals'])))}\n"
            f"  현재가 {c['price']:,}원 ({c['chg_pct']:+.2f}%) | PER {c['per']:.1f} EPS {c['eps']:,}원 "
            f"| 52주 {c['w52_low']:,}~{c['w52_high']:,}원\n"
            f"  컨센서스 {c['target']:,}원 (업사이드 {c['upside']:+.1f}%, 애널 {c['analysts']}명, {c['opinion']})"
        )
    return "\n".join(lines) if lines else "후보 없음 (품질 필터 통과 종목 없음)"


def _register_watchlist(report: str) -> tuple[str, int]:
    """WATCH 블록 파싱 → 워치리스트 자동 등록. (블록 제거된 리포트, 등록 수) 반환."""
    m = _WATCH_RE.search(report)
    if not m:
        return report, 0
    block   = m.group(1)
    cleaned = (report[: m.start()] + report[m.end():]).strip()

    count = 0
    try:
        from services.watchlist_service import add_to_watchlist
        for raw in block.split("\n"):
            parts = [p.strip() for p in raw.strip().split("|")]
            if len(parts) < 5 or parts[0].lower() != "watch":
                continue
            code, name = parts[1].zfill(6), parts[2]
            try:
                entry = float(parts[3].replace(",", ""))
            except ValueError:
                entry = None
            add_to_watchlist(
                code, name, target_entry=entry, timeframe="mid",
                reason=f"[발굴] {parts[4]}", priority="high",
            )
            count += 1
            logger.info("[발굴] 워치리스트 등록: %s(%s)", name, code)
    except Exception as e:
        logger.warning("[발굴] 워치리스트 등록 실패: %s", e)
    return cleaned, count


def run_discovery(send: bool = True) -> str:
    """탑다운 종목 발굴 실행. 리포트 텍스트 반환."""
    now = datetime.now(_TZ)
    logger.info("[발굴] 시작: %s", now.strftime("%Y-%m-%d %H:%M"))

    kis = KISClient()

    # ① 시장 흐름 데이터
    try:
        mkt = fetch_global_market_data()
    except Exception:
        mkt = {}
    def _m(k):
        d = mkt.get(k, {})
        return f"{d.get('close', 'N/A')} ({d.get('change_pct', 0):+.2f}%)" if d else "N/A"
    macro_text = (
        f"KOSPI {_m('kospi')} | KOSDAQ {_m('kosdaq')} | S&P500 {_m('sp500')} "
        f"| NASDAQ {_m('nasdaq')} | SOX {_m('sox')}\n"
        f"원/달러 {_m('usd_krw')} | 미10년금리 {_m('us10y')} | VIX {_m('vix')} | 구리 {_m('copper')}"
    )

    # ② 미국 섹터 흐름 (산업 방향 선행 지표)
    try:
        us_sectors = fetch_us_sectors()
        sector_text = "\n".join(
            f"  {k}: {v.get('change_pct', 0):+.2f}%"
            for k, v in sorted(us_sectors.items(), key=lambda x: x[1].get("change_pct", 0), reverse=True)
        ) or "없음"
    except Exception:
        sector_text = "없음"

    # ③ 후보 풀 구성 + 실데이터 검증
    holdings = _get_holding_codes()
    candidates = _build_candidate_pool(kis, exclude=holdings)
    candidates = _enrich_candidates(kis, candidates)
    logger.info("[발굴] 검증 완료 후보: %d개", len(candidates))

    # ④ 고객 프로필
    try:
        from services.profile_service import get_profile_context
        profile_ctx = get_profile_context(kis)
    except Exception:
        profile_ctx = ""

    context = f"""분석 기준일: {now.strftime('%Y년 %m월 %d일 (%A)')}

{profile_ctx}

[글로벌·국내 시장 흐름]
{macro_text}

[미국 섹터 등락 — 국내 산업 방향 선행 지표]
{sector_text}

[발굴 후보 풀 — 수급 확인 + 실데이터 검증 완료. 추천은 반드시 이 안에서만]
{_format_candidates(candidates)}"""

    try:
        report = chat(_SYSTEM, context, max_tokens=2000)
    except Exception as e:
        logger.error("[발굴] LLM 실패: %s", e)
        return f"발굴 분석 실패: {e}"

    # ⑤ 워치리스트 자동 등록
    report, n_watch = _register_watchlist(report)

    header = f"🔭 *종목 발굴 리포트* ({now.strftime('%Y.%m.%d')})\n"
    if n_watch:
        header += f"발굴 종목 {n_watch}개 워치리스트 자동 등록 — 진입 조건 도달 시 알림\n"
    header += "\n"

    if send:
        send_message(header + report)
    logger.info("[발굴] 완료 (워치리스트 등록 %d개)", n_watch)
    return header + report


def run(state: dict = None) -> dict:
    run_discovery()
    return state or {}
