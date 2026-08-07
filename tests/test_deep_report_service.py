"""심층 리포트 생성 회귀 테스트.

2026-07-23: 매크로 분석(macro_report)이 메인 브리핑(9줄 압축)에도, 심층 리포트에도
안 담기고 있었다는 게 발견됨 — build_deep_report의 섹션 목록에 빠져 있었다.
2026-08-07: 같은 패턴으로 선물시장 분석(futures_report — 오버나이트 신호 기반
오늘 수혜 섹터/종목)도 어디에도 노출 안 되고 있었던 것 발견, 섹션 추가.
"""
from services.deep_report_service import build_deep_report


def test_macro_report_included_in_deep_report():
    state = {"macro_report": "미국 10년물 금리 급등, 위험자산 회피 국면 진입 신호"}
    content = build_deep_report(state)
    assert "미국 10년물 금리" in content
    assert "매크로" in content


def test_futures_report_included_in_deep_report():
    state = {"futures_report": "SOX +2%↑ → 반도체 섹터 수혜 (삼성전자·SK하이닉스)"}
    content = build_deep_report(state)
    assert "SOX" in content
    assert "선물시장" in content


def test_empty_state_returns_empty_string():
    assert build_deep_report({}) == ""


def test_sections_joined_in_order_macro_first():
    state = {
        "macro_report": "매크로 내용",
        "market_intelligence_report": "글로벌 서사 내용",
        "issue_stocks_report": "이슈종목 내용",
    }
    content = build_deep_report(state)
    assert content.index("매크로 내용") < content.index("글로벌 서사 내용")
    assert content.index("글로벌 서사 내용") < content.index("이슈종목 내용")
