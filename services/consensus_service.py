"""
services/consensus_service.py

애널리스트 컨센서스 목표주가 데이터를 처리하고
CEO 컨텍스트용 텍스트를 생성한다.
"""
import logging

logger = logging.getLogger(__name__)


def build_consensus_context(
    codes: list[str],
    name_map: dict[str, str],
    kis_data: dict[str, dict],
    consensus_raw: dict[str, dict] | None = None,
) -> dict[str, dict]:
    """
    컨센서스 raw 데이터 + KIS 현재가를 조합하여
    괴리율·손익비를 계산한 최종 컨텍스트 dict를 반환한다.

    Parameters
    ----------
    codes : list[str]
        처리할 종목코드 목록
    name_map : dict[str, str]
        {code: name} 종목명 매핑
    kis_data : dict[str, dict]
        {code: {"price": int}} 형태의 현재가 데이터
        (KIS API를 직접 호출하지 않고 이미 수집된 데이터 활용)
    consensus_raw : dict[str, dict] | None
        fetch_consensus_batch 결과.
        None이면 빈 dict로 처리.

    Returns
    -------
    dict[str, dict]
        {
            code: {
                "code": str,
                "name": str,
                "current_price": int,
                "avg_target": int,
                "median_target": int,
                "gap_pct": float,          # (목표-현재)/현재 × 100
                "analyst_count": int,
                "consensus_opinion": str,
                "low_confidence": bool,
                "rr_at_3pct_stop": float,  # 손절 -3% 기준 R:R
                "rr_at_5pct_stop": float,  # 손절 -5% 기준 R:R
                "max_target": int,
                "min_target": int,
            }
        }
    """
    raw = consensus_raw or {}
    result: dict[str, dict] = {}

    for code in codes:
        cons = raw.get(code)
        if not cons:
            continue

        avg_target = cons.get("avg_target", 0)
        if not avg_target:
            continue

        # 현재가
        price_info = kis_data.get(code, {})
        current_price = price_info.get("price", 0)
        if not current_price or current_price <= 0:
            continue

        # 괴리율: (목표가 - 현재가) / 현재가 × 100
        gap_pct = (avg_target - current_price) / current_price * 100

        # R:R 계산 (손절 기준별)
        # R:R = 기대수익 / 최대손실 = gap_pct / stop_pct
        rr_3 = round(gap_pct / 3.0, 1) if gap_pct > 0 else 0.0
        rr_5 = round(gap_pct / 5.0, 1) if gap_pct > 0 else 0.0

        result[code] = {
            "code": code,
            "name": name_map.get(code, code),
            "current_price": current_price,
            "avg_target": avg_target,
            "median_target": cons.get("median_target", avg_target),
            "gap_pct": round(gap_pct, 1),
            "analyst_count": cons.get("analyst_count", 0),
            "consensus_opinion": cons.get("consensus_opinion", ""),
            "low_confidence": cons.get("low_confidence", True),
            "rr_at_3pct_stop": rr_3,
            "rr_at_5pct_stop": rr_5,
            "max_target": cons.get("max_target", 0),
            "min_target": cons.get("min_target", 0),
        }

    logger.info("[컨센서스서비스] 컨텍스트 구성 완료: %d종목", len(result))
    return result


def format_consensus_for_ceo(
    consensus_data: dict[str, dict],
    top_n: int = 12,
) -> str:
    """
    CEO 컨텍스트용 텍스트를 생성한다.
    괴리율 높은 순으로 정렬하여 top_n개만 포함.

    Parameters
    ----------
    consensus_data : dict[str, dict]
        build_consensus_context 반환값
    top_n : int
        출력할 최대 종목 수

    Returns
    -------
    str
        CEO 프롬프트에 주입할 텍스트. 데이터 없으면 빈 문자열.
    """
    if not consensus_data:
        return ""

    # 괴리율 내림차순 정렬
    sorted_items = sorted(
        consensus_data.values(),
        key=lambda x: x.get("gap_pct", -999),
        reverse=True,
    )[:top_n]

    lines = [
        "[애널리스트 컨센서스 목표주가 — 괴리율 상위 순위]",
        "(손익비 계산 시 이 목표주가를 1차목표로 사용하라. 기계적 +5%/+10% 사용 금지)",
        "",
    ]

    for item in sorted_items:
        name = item["name"]
        code = item["code"]
        cur = item["current_price"]
        tgt = item["avg_target"]
        gap = item["gap_pct"]
        cnt = item["analyst_count"]
        opinion = item["consensus_opinion"]
        rr3 = item["rr_at_3pct_stop"]
        low_conf = item.get("low_confidence", False)

        conf_tag = " ⚠️신뢰도낮음" if low_conf else ""
        sign = "+" if gap >= 0 else ""

        lines.append(
            f"  {name}({code}): 현재 {cur:,}원 → 컨센서스 {tgt:,}원 "
            f"({sign}{gap:.1f}%) | R:R {rr3}:1(손절-3%) | "
            f"애널리스트 {cnt}명{conf_tag} | {opinion}"
        )

    return "\n".join(lines)
