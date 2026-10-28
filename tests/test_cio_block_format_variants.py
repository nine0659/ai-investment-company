"""CIO 결정 블록의 줄 서식 변형 — 2026-10-28 점검: 블록 마커는 인식돼도 줄 앞의 불릿·번호·볼드·백틱,
전각 파이프(｜), 비중 칸의 %가 있으면 그 줄이 조용히 버려져 구조화 결정 전체가 사라졌다."""
import pytest

from agents.ceo_agent import _find_untracked_action_line, _parse_cio_decisions as parse

BASE = [
    "stance|defensive|30",
    "thesis|challenged",
    "new|009150|삼성전기|3|high|mid|AI 수요|developing|3:1|종가 8만원 하회",
    "reduce|000660|SK하이닉스|34.3|비중 집중",
    "exit|0117V0|TIGER ETF||전량",
]


def _wrap(lines, start="=CIO_DECISION_START=", end="=CIO_DECISION_END="):
    return "본문\n" + start + "\n" + "\n".join(lines) + "\n" + end + "\n"


VARIANTS = {
    "plain": BASE,
    "bullet": ["- " + l for l in BASE],
    "bold": ["**" + l + "**" for l in BASE],
    "numbered": [f"{i + 1}. {l}" for i, l in enumerate(BASE)],
    "backtick": ["`" + l + "`" for l in BASE],
    "fullwidth_pipe": [l.replace("|", "｜") for l in BASE],
    "percent": ["stance|defensive|30%", "thesis|challenged",
                "new|009150|삼성전기|3%|high|mid|x|developing|3:1|y",
                "reduce|000660|SK하이닉스|34.3%|z", "exit|0117V0|TIGER||전량"],
    "fence_inside": ["```"] + BASE + ["```"],
}


@pytest.mark.parametrize("name", VARIANTS)
def test_line_format_variants_are_all_parsed(name):
    _, d = parse(_wrap(VARIANTS[name]), "2026-10-28", "pre_market")
    assert d["stance_parsed"] is True and d["cash_target_pct"] == 30
    assert len(d["new_positions"]) == 1 and d["new_positions"][0]["size_pct"] == 3
    assert [c["action"] for c in d["position_changes"]] == ["reduce", "exit"]


def test_lowercase_markers_recognized():
    _, d = parse(_wrap(BASE, "=cio_decision_start=", "=cio_decision_end="), "d", "r")
    assert d["stance_parsed"] is True
    assert _find_untracked_action_line("=cio_decision_start=\n살 것: 삼성전기 3%") is None


def test_unparseable_block_leaves_defaults_marked_unparsed():
    _, d = parse(_wrap(["아무 말이나 적힌 줄"]), "d", "r")
    assert d["stance_parsed"] is False and d["new_positions"] == []
