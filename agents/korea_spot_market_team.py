import logging
from graph.state import InvestmentState
from clients.openai_client import chat

logger = logging.getLogger(__name__)

_SYSTEM = """당신은 한국 주식시장 현장 분석 전문가입니다.
KIS API 거래량·거래대금·등락률 순위 데이터를 분석하세요.

분석 항목:
1. 거래량 상위 종목들의 공통 테마/섹터
2. 거래대금 상위 종목 (기관·외국인 수급 추정)
3. 급등 상위 종목의 재료 및 지속성 판단
4. 코스피/코스닥 시장별 특이사항
5. 오늘 주목 종목 TOP5 (이유 포함)

출력: 시장 분위기 한줄 / 주목 종목 TOP5 / 주도 섹터·테마 / 특이사항"""

_RANK_KEYS = [
    ("kospi_volume_rank",  "KOSPI 거래량"),
    ("kosdaq_volume_rank", "KOSDAQ 거래량"),
    ("kospi_amount_rank",  "KOSPI 거래대금"),
    ("kospi_rise_rank",    "KOSPI 급등"),
    ("kosdaq_rise_rank",   "KOSDAQ 급등"),
]


def run(state: InvestmentState) -> InvestmentState:
    try:
        kis = state.get("raw_kis_data", {})
        parts = []
        for key, label in _RANK_KEYS:
            items = kis.get(key, [])[:5]
            if items:
                names = ", ".join(
                    f"{x.get('hts_kor_isnm', x.get('stck_shrn_iscd', '?'))}({x.get('prdy_ctrt', '')}%)"
                    for x in items
                )
                parts.append(f"[{label}] {names}")

        text = "\n".join(parts) if parts else "KIS 데이터 없음 (장 마감 또는 API 오류)"
        result = chat(_SYSTEM, f"한국 시장 실시간 데이터:\n{text}", max_tokens=2500)
        state["korea_spot_report"] = result
        state["candidates"] = _extract_candidates(kis)
        logger.info("[한국현물팀] 완료")
    except Exception as e:
        logger.error("[한국현물팀] 실패: %s", e)
        state["korea_spot_report"] = "데이터 수집 실패"
        state["errors"].append(f"korea_spot_team: {e}")
    return state


def _extract_candidates(data: dict) -> list[dict]:
    seen: dict[str, dict] = {}
    for key in ["kospi_rise_rank", "kosdaq_rise_rank", "kospi_volume_rank", "kosdaq_volume_rank"]:
        for item in data.get(key, [])[:10]:
            code = item.get("stck_shrn_iscd", "")
            if code and code not in seen:
                seen[code] = {
                    "code":       code,
                    "name":       item.get("hts_kor_isnm", ""),
                    "change_pct": item.get("prdy_ctrt", "0"),
                    "market":     "KOSPI" if "kospi" in key else "KOSDAQ",
                    "score":      0,
                }
    return list(seen.values())[:20]
