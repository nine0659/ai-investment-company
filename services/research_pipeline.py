"""
services/research_pipeline.py

리서치 인텔리전스 한 바퀴: 수집(증권사 리포트·중앙은행 원문·보유종목 커버리지) → 본문 읽기·검증 → 집계 컨텍스트.
market_intelligence_team이 호출한다. 어떤 단계가 실패해도 브리핑 전체를 막지 않는다(빈 문자열 반환).
"""
import logging

logger = logging.getLogger(__name__)


def _record(ok: bool, detail: str) -> None:
    try:
        from services.job_ledger import record_job
        record_job("branch:report_analysis", "success" if ok else "fail", detail)
    except Exception as e:
        logger.warning("[리서치] 상태 기록 실패: %s", e)


def run(naver_reports: list[dict]) -> str:
    try:
        from clients.official_docs_client import list_official_documents
        from clients.securities_report_client import fetch_company_reports_for
        from services import report_analyzer, research_synthesis

        candidates = [r for r in (naver_reports or []) if r.get("type") != "종목분석" and r.get("id")]
        n_naver = len(candidates)

        official = list_official_documents()
        candidates += official

        held_codes = [c for c, _ in research_synthesis.holdings()]
        company = fetch_company_reports_for(held_codes) if held_codes else []
        if company:
            report_analyzer.store_company_snapshots(company)
            candidates += company

        stats = report_analyzer.analyze_new_documents(candidates, deadline_s=100)

        # 새로 읽을 게 있었는데 하나도 못 읽었다면 읽기 단계 장애 (이미 다 읽은 날은 selected=0이라 정상)
        broken = stats["selected"] > 0 and stats["analyzed"] == 0 and stats["no_text"] < stats["selected"]
        _record(not broken, f"후보 {len(candidates)}(네이버 {n_naver}·공식 {len(official)}·보유종목 {len(company)}) {stats}")

        return research_synthesis.build_research_context()
    except Exception as e:
        logger.error("[리서치] 파이프라인 실패: %s", e)
        _record(False, f"예외: {str(e)[:150]}")
        return ""
