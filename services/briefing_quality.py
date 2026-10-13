"""브리핑 한 번의 '판단 재료가 제대로 모였는가'를 코드로 점검한다.

job_runs는 잡이 돌았는지(성공/실패/빈 결과)만 알 뿐, 브리핑이 쓸 만한 재료로 만들어졌는지는
모른다 — 상류 분석 리포트 여러 개가 비거나 "실패" 문구로 채워져도 CEO는 남은 재료로 그럴듯한
브리핑을 쓰고 잡은 "success"로 끝난다(2026-06~08 병렬 state 소거 사고가 이 사각지대에서
2개월 지속됐다). 이 모듈은 실행 직후 상류 리포트 상태를 평가해 `branch:briefing_quality`로
기록한다. 규약은 `_parallel()`/`_track_sequential()`과 같은 `branch:` 접두사라 daily_health가
별도 코드 없이 그대로 집어서 문제 시에만 경보한다.

기준값은 초기 보수적 추정이다(상류 리포트 14개 중 4개 이상 비면 문제). 실제 기준선이
job_runs 상세에 쌓이면 그 분포를 보고 조정할 것.
"""
import logging

logger = logging.getLogger(__name__)

# run_type별 CEO가 기대하는 상류 리포트 필드
_EXPECTED = {
    "pre_market": [
        "futures_report", "us_market_report", "us_impact_report", "global_market_report",
        "news_report", "bigfigure_report", "macro_report", "event_risk_report",
        "market_intelligence_report", "sector_report", "money_flow_report",
        "issue_stocks_report", "risk_report", "committee_report", "portfolio_report",
    ],
    "close_market": [
        "korea_spot_report", "sector_report", "money_flow_report", "issue_stocks_report",
        "news_report", "bigfigure_report", "macro_report", "event_risk_report",
        "market_intelligence_report", "risk_report", "committee_report", "portfolio_report",
    ],
}
_FAIL_MARKERS = ("실패", "일시 불가", "생성 실패", "데이터 없음", "분석 불가")
_MIN_LEN = 30
# 이 개수 이상의 상류 리포트가 비면 '재료 부족 브리핑'으로 본다
EMPTY_PROBLEM_THRESHOLD = 4


def _is_empty(v) -> bool:
    if not v or not isinstance(v, str):
        return True
    s = v.strip()
    return len(s) < _MIN_LEN or any(m in s[:60] for m in _FAIL_MARKERS)


def assess(state: dict, run_type: str) -> dict:
    """반환: {"expected", "empty": [필드...], "problems": [문구...], "metrics": {...}}"""
    fields = _EXPECTED.get(run_type)
    if not fields:
        return {"expected": 0, "empty": [], "problems": [], "metrics": {}}
    empty = [f for f in fields if _is_empty(state.get(f))]
    problems: list[str] = []
    if len(empty) >= EMPTY_PROBLEM_THRESHOLD:
        problems.append(f"상류 리포트 {len(empty)}/{len(fields)}개 비었거나 실패: {', '.join(empty[:6])}")
    ceo = state.get("ceo_report", "") or ""
    if not ceo or "브리핑 생성 실패" in ceo:
        problems.append("CEO 브리핑 생성 실패")
    if not state.get("ceo_decisions"):
        problems.append("CEO 구조화 결정 로그 없음")
    if not state.get("debate_verdict"):
        problems.append("Bull/Bear 토론 판정 없음")
    fc = state.get("ceo_fact_check") or {}
    metrics = {
        "empty": len(empty), "expected": len(fields),
        "ungrounded": fc.get("count"), "claims": fc.get("total"),
        "ceo_len": len(ceo), "na_cnt": ceo.count("N/A"),
    }
    return {"expected": len(fields), "empty": empty, "problems": problems, "metrics": metrics}


def record(state: dict, run_type: str) -> dict:
    """assess 결과를 job_runs에 branch:briefing_quality로 기록 (문제 있으면 fail → daily_health 경보)."""
    result = assess(state, run_type)
    if not result["expected"]:
        return result
    try:
        from services.job_ledger import record_job
        m = result["metrics"]
        detail = (f"{run_type} 빈리포트={m['empty']}/{m['expected']} 수치주장={m['claims']} "
                  f"근거없음={m['ungrounded']} N/A={m['na_cnt']} 길이={m['ceo_len']}")
        if result["problems"]:
            detail = "; ".join(result["problems"])[:150] + " | " + detail
            record_job("branch:briefing_quality", "fail", detail)
        else:
            record_job("branch:briefing_quality", "success", detail)
    except Exception as e:
        logger.debug("[브리핑품질] 기록 실패 (무시): %s", e)
    return result
