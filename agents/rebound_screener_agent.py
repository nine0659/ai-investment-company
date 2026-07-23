"""
agents/rebound_screener_agent.py
기술적·추세적 반등 스크리너 — 전체 종목군에서 눌림목 반등/추세전환 패턴을 탐지한다.

기존 discovery_agent(탑다운, 밸류에이션 중심)와 다른 관점: 이건 순수 차트 기반이다.
LLM을 쓰지 않는다 — services.chart_service의 실측 지표(이동평균·거래량·52주위치·
볼린저밴드)만으로 판정한다. "숫자는 코드가 계산, LLM은 서술만"(CLAUDE.md 절대원칙 2)을
가장 엄격하게 적용한 기능: 여기엔 서술할 LLM조차 없다 — 오판 여지를 원천 차단한다.

후보 풀은 오늘 급등한 종목이 아니라 KOSPI·KOSDAQ 하락률 상위(눌린 종목)에서 찾는다 —
반등을 볼 대상은 이미 오른 종목이 아니라 눌려있던 종목이어야 의미가 있다.

실행: 텔레그램 /rebound 온디맨드. 자동 스케줄 없음(discovery_agent와 동일하게
비용·빈도 관리 — 2026-07-23 결정).
"""
import logging
import re

from clients.kis_client import KISClient
from services.chart_service import analyze_chart, format_chart_summary

logger = logging.getLogger(__name__)

_MIN_MARKET_CAP_억 = 3000   # discovery_agent와 동일 기준 — 투기성 소형주 배제
_MAX_PER_MARKET = 30        # 하락률 순위에서 시장별로 가져올 후보 수
_TOP_N = 5                  # 카테고리별 최종 표시 개수

_EXCLUDE_NAME = re.compile(
    r"KODEX|TIGER|KBSTAR|ARIRANG|HANARO|SOL |PLUS |ACE |ETN|인버스|레버리지|선물|채권|배당주|"
    r"코스닥150|코스피200|2X|3X"
)


def classify_signal(ch: dict) -> str | None:
    """차트 지표 dict → "기술적 반등" / "추세적 반등" / None(해당 없음).

    기술적 반등: 골든크로스(ma5>ma20)가 막 발생 + 거래량 확인 + 아직 눌린 자리
                (아직 정배열 완성 전 — 초기 신호, 확인 필요).
    추세적 반등: 정배열(ma5>ma20>ma60) 완성 + 아직 52주 고점 근접 전(초입) + 거래량 뒷받침
                (이미 추세로 굳어진 신호 — 초기 반등보다 확신도 높음).

    둘 다 볼린저밴드 하단 이탈 중(bb_break_down=True, 아직 낙폭 진행 중)인 종목은
    제외한다 — "떨어지는 칼날"과 "반등"을 구분하는 최소 조건.
    """
    if ch.get("bb_break_down"):
        return None
    vol_ok = ch.get("vol_ratio", 1.0) >= 1.5
    still_low = ch.get("pos_52w", 50.0) < 70.0
    if not (vol_ok and still_low):
        return None

    if ch.get("ma_aligned"):
        return "추세적 반등"
    if ch.get("golden_cross"):
        return "기술적 반등"
    return None


def _decliner_pool(kis: KISClient) -> list[dict]:
    """KOSPI+KOSDAQ 하락률 상위에서 후보 풀 구성 — 반등을 찾을 대상은 눌린 종목."""
    pool: list[dict] = []
    for market, market_name in (("J", "KOSPI"), ("Q", "KOSDAQ")):
        try:
            items = kis.get_fluctuation_rank(market=market, rise=False, top_n=_MAX_PER_MARKET)
        except Exception as e:
            logger.debug("[반등스크리너] %s 하락률 순위 조회 실패: %s", market_name, e)
            items = []
        for item in items or []:
            code = item.get("stck_shrn_iscd") or item.get("mksc_shrn_iscd", "")
            name = item.get("hts_kor_isnm", "")
            if not code or not name or _EXCLUDE_NAME.search(name):
                continue
            pool.append({"code": code, "name": name, "market": market_name})
    return pool


def _filter_by_market_cap(kis: KISClient, pool: list[dict]) -> list[dict]:
    """시가총액 하한 필터 — 급락일 하락률 순위를 채우는 투기성 소형주 배제."""
    kept = []
    for c in pool:
        try:
            pd = kis.get_stock_price(c["code"], market="J" if c["market"] == "KOSPI" else "Q")
            if pd and pd.get("market_cap_억", 0) >= _MIN_MARKET_CAP_억:
                kept.append(c)
        except Exception:
            continue
    return kept


def screen(pool: list[dict]) -> dict[str, list[dict]]:
    """후보 풀 각각에 차트 분석을 돌려 신호별로 분류. chart_score 내림차순 정렬."""
    results: dict[str, list[dict]] = {"추세적 반등": [], "기술적 반등": []}
    for c in pool:
        ch = analyze_chart(c["code"], c["name"])
        signal = classify_signal(ch)
        if signal:
            results[signal].append({**c, **ch})
    for key in results:
        results[key].sort(key=lambda x: -x.get("chart_score", 0))
        results[key] = results[key][:_TOP_N]
    return results


def _format_report(results: dict[str, list[dict]]) -> str:
    lines = ["📈 *기술적·추세적 반등 스크리너*\n"]
    lines.append(
        "실측 차트 지표(이동평균·거래량·52주위치·볼린저밴드)만으로 판정 — LLM 서술 없음.\n"
        "펀더멘털·컨센서스는 별도 확인 필요 (이 리포트는 차트 신호만 봄).\n"
    )

    for label, emoji in (("추세적 반등", "📊"), ("기술적 반등", "⚡")):
        items = results.get(label, [])
        lines.append(f"\n{emoji} *{label}* ({len(items)}종목)")
        if not items:
            lines.append("  해당 없음")
            continue
        for c in items:
            lines.append("  " + format_chart_summary(c["code"], c["name"], c))

    return "\n".join(lines)


def run_rebound_screen(send: bool = True) -> str:
    from clients.telegram_client import send_message

    kis = KISClient()
    pool = _decliner_pool(kis)
    logger.info("[반등스크리너] 하락률 상위 후보 %d개", len(pool))
    pool = _filter_by_market_cap(kis, pool)
    logger.info("[반등스크리너] 시총 필터 통과 %d개", len(pool))

    results = screen(pool)
    report = _format_report(results)

    if send:
        send_message(report)
    logger.info(
        "[반등스크리너] 완료 — 추세적 %d / 기술적 %d",
        len(results.get("추세적 반등", [])), len(results.get("기술적 반등", [])),
    )
    return report


def run(state: dict = None) -> dict:
    run_rebound_screen()
    return state or {}
