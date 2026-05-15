import logging
from graph.state import InvestmentState
from clients.openai_client import chat

logger = logging.getLogger(__name__)

_SYSTEM = """당신은 한국 주식시장 현장 분석 전문가입니다.
KIS API 거래량·거래대금·등락률·외국인·기관 순위 데이터를 분석하세요.

분석 항목:
1. 오늘 실제 주도 섹터·테마 — 거래대금 기준으로 자금이 어디에 몰렸는가
2. 외국인·기관 동시 매수 종목 (수급 퀄리티 최상)
3. 급등 상위 종목의 재료와 단기 지속성 판단
4. 미국 방향과 한국 실제 흐름이 다른가? (디커플링 여부)
5. 오늘 실질 주도 종목 TOP5 — 거래대금·수급·등락률 교차 기준

출력: 시장 분위기 한줄 / 오늘 실질 주도 테마 / 주목 종목 TOP5 (근거 포함) / 미국-한국 디커플링 여부"""

_RANK_KEYS = [
    ("kospi_amount_rank",       "KOSPI 거래대금"),
    ("kosdaq_amount_rank",      "KOSDAQ 거래대금"),
    ("kospi_rise_rank",         "KOSPI 급등"),
    ("kosdaq_rise_rank",        "KOSDAQ 급등"),
    ("kospi_foreign_rank",      "KOSPI 외국인 순매수"),
    ("kosdaq_foreign_rank",     "KOSDAQ 외국인 순매수"),
    ("kospi_institution_rank",  "KOSPI 기관 순매수"),
    ("kosdaq_institution_rank", "KOSDAQ 기관 순매수"),
    ("kospi_volume_rank",       "KOSPI 거래량"),
    ("kosdaq_volume_rank",      "KOSDAQ 거래량"),
]


def run(state: InvestmentState) -> InvestmentState:
    try:
        kis = state.get("raw_kis_data", {})
        parts = []
        for key, label in _RANK_KEYS:
            items = kis.get(key, [])[:10]
            if items:
                names = ", ".join(
                    f"{x.get('hts_kor_isnm', x.get('stck_shrn_iscd', '?'))}({x.get('prdy_ctrt', '')}%)"
                    for x in items
                )
                parts.append(f"[{label}] {names}")

        text = "\n".join(parts) if parts else "KIS 데이터 없음 (장 마감 또는 API 오류)"
        result = chat(_SYSTEM, f"한국 시장 실시간 데이터:\n{text}", max_tokens=2500)
        state["korea_spot_report"] = result
        state["candidates"] = _extract_candidates(kis, state.get("us_sector_data", {}))
        logger.info("[한국현물팀] 완료")
    except Exception as e:
        logger.error("[한국현물팀] 실패: %s", e)
        state["korea_spot_report"] = "데이터 수집 실패"
        state["errors"].append(f"korea_spot_team: {e}")
    return state


def _extract_candidates(data: dict, us_sector_data: dict | None = None) -> list[dict]:
    """KIS 수급 데이터에서 후보 종목 추출. 신호 강도 순 가중 점수로 정렬.

    점수 가중치:
      외국인 순매수 3점 > 기관 순매수 2점 > 거래대금 2점 > 급등 1.5점 > 거래량 1점
    외국인·기관 동시 매수 종목은 최우선 후보.
    """
    seen:   dict[str, dict] = {}
    scores: dict[str, float] = {}

    # (KIS 키, 시장, 점수 가중치, 코드 필드)
    rank_specs = [
        ("kospi_foreign_rank",      "KOSPI",  3.0, "mksc_shrn_iscd"),
        ("kosdaq_foreign_rank",     "KOSDAQ", 3.0, "mksc_shrn_iscd"),
        ("kospi_institution_rank",  "KOSPI",  2.0, "mksc_shrn_iscd"),
        ("kosdaq_institution_rank", "KOSDAQ", 2.0, "mksc_shrn_iscd"),
        ("kospi_amount_rank",       "KOSPI",  2.0, "stck_shrn_iscd"),
        ("kosdaq_amount_rank",      "KOSDAQ", 2.0, "stck_shrn_iscd"),
        ("kospi_rise_rank",         "KOSPI",  1.5, "stck_shrn_iscd"),
        ("kosdaq_rise_rank",        "KOSDAQ", 1.5, "stck_shrn_iscd"),
        ("kospi_volume_rank",       "KOSPI",  1.0, "stck_shrn_iscd"),
        ("kosdaq_volume_rank",      "KOSDAQ", 1.0, "stck_shrn_iscd"),
    ]

    for key, market, weight, code_field in rank_specs:
        items = data.get(key, [])
        for rank_i, item in enumerate(items[:15]):
            code = item.get(code_field) or item.get("stck_shrn_iscd", "")
            if not code:
                continue
            name  = item.get("hts_kor_isnm", "")
            chg   = float(item.get("prdy_ctrt", 0) or 0)
            # 순위 높을수록 높은 점수 (1위=15점, 15위=1점) × 가중치
            pts   = (15 - rank_i) * weight
            scores[code] = scores.get(code, 0) + pts
            if code not in seen:
                seen[code] = {
                    "code":       code,
                    "name":       name,
                    "change_pct": chg,
                    "market":     market,
                    "score":      0,
                    "source":     "KIS",
                }
            elif name:
                seen[code]["name"] = name  # 더 정확한 이름으로 갱신

    # 점수 반영
    for code, s in seen.items():
        s["score"] = round(scores.get(code, 0))

    result = sorted(seen.values(), key=lambda x: x["score"], reverse=True)

    # 장전·장외 시간 등 KIS 데이터가 부족할 때 미국 강세 섹터 기반으로 보완
    if len(result) < 5 and us_sector_data:
        try:
            from agents.us_impact_agent import US_SECTOR_TO_KR
            existing_codes = {s["code"] for s in result}
            sorted_sectors = sorted(
                us_sector_data.items(), key=lambda x: x[1].get("change_pct", 0), reverse=True
            )
            for sector, sector_info in sorted_sectors[:4]:
                if sector_info.get("change_pct", 0) <= 0.3:
                    continue
                for s in US_SECTOR_TO_KR.get(sector, []):
                    if s["strength"] == "높음" and s["code"] not in existing_codes:
                        result.append({
                            "code":       s["code"],
                            "name":       s["name"],
                            "change_pct": 0.0,
                            "market":     s.get("market", "KOSPI"),
                            "score":      20,
                            "source":     "US_fallback",
                        })
                        existing_codes.add(s["code"])
            logger.info("[한국현물팀] KIS 데이터 부족 — 미국 강세 섹터 기반 후보 보완: %d개", len(result))
        except Exception as e:
            logger.warning("[한국현물팀] 미국 섹터 보완 실패: %s", e)

    return result[:20]
