"""심층 리포트 생성 회귀 테스트.

2026-07-23: 매크로 분석(macro_report)이 메인 브리핑(9줄 압축)에도, 심층 리포트에도
안 담기고 있었다는 게 발견됨 — build_deep_report의 섹션 목록에 빠져 있었다.
"""
from services.deep_report_service import build_deep_report


def test_macro_report_included_in_deep_report():
    state = {"macro_report": "미국 10년물 금리 급등, 위험자산 회피 국면 진입 신호"}
    content = build_deep_report(state)
    assert "미국 10년물 금리" in content
    assert "매크로" in content


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
