from agents.ceo_agent import _build_summary_card

_HOLDINGS = [("005380", "현대차"), ("009150", "삼성전기"), ("000660", "SK하이닉스"), ("0117V0", "TIGER ETF")]


def _d(holds, changes=()):
    return {"macro_stance": "neutral", "thesis_status": "intact",
            "position_holds": [{"code": c} for c in holds],
            "position_changes": [{"code": c, "action": "reduce", "name": c} for c in changes]}


def test_missing_holding_is_flagged():
    card = _build_summary_card(_d(["005380", "009150", "000660"]), _HOLDINGS)
    assert "CEO 판단 누락 보유종목: TIGER ETF" in card


def test_change_counts_as_covered():
    card = _build_summary_card(_d(["005380", "009150"], ["000660", "0117V0"]), _HOLDINGS)
    assert "누락" not in card


def test_no_holdings_info_is_silent():
    assert "누락" not in _build_summary_card(_d(["005380"]), [])
    assert "누락" not in _build_summary_card(_d(["005380"]))
