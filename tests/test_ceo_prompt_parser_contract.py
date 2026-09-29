"""CEO 프롬프트의 =CIO_DECISION= 출력 스키마 ↔ _parse_cio_decisions 파서 계약 테스트.

반복 사고 클러스터 ①(2026-09 코드 감사): 프롬프트 문구·필드 순서를 고치면 정규식/파이프
파서가 조용히 깨지는데(예외를 삼켜 빈 dict 반환 → 승인 큐·추천추적·리스크게이트 전부
무음 스킵), 그 계약을 강제하는 장치가 없었다. 이 테스트는 프롬프트에서 스키마를 직접
추출해 파서와 대조한다 — 프롬프트나 파서를 한쪽만 고치면 여기서 깨진다.
"""
import re

import agents.ceo_agent as ceo_agent

# 프롬프트 스키마의 키별 '키 포함 필드 수' — 프롬프트를 바꾸면 이 표와 파서를 함께 갱신할 것.
_EXPECTED_FIELDS = {
    "stance": 3, "thesis": 2, "analyst": 3,
    "new": 10, "reduce": 5, "exit": 5, "hold": 7,
    "risk": 2, "note": 2,
}


def _prompt_schema_lines() -> dict[str, list[str]]:
    m = ceo_agent._LOG_RE.search(ceo_agent._CIO_CHARTER)
    assert m, "헌장 프롬프트에서 =CIO_DECISION_START/END= 블록을 찾지 못함"
    out = {}
    for line in m.group(1).split("\n"):
        line = line.strip()
        if line and not line.startswith("#"):
            parts = line.split("|")
            out[parts[0].strip().lower()] = parts
    return out


def test_prompt_schema_keys_and_field_counts_match_contract():
    schema = _prompt_schema_lines()
    assert set(schema) == set(_EXPECTED_FIELDS), (
        f"프롬프트 스키마 키가 계약과 다름: {set(schema) ^ set(_EXPECTED_FIELDS)} — "
        "파서(_parse_cio_decisions)와 이 테스트를 함께 갱신하세요"
    )
    for key, parts in schema.items():
        assert len(parts) == _EXPECTED_FIELDS[key], (
            f"'{key}' 필드 수 {len(parts)} != 계약 {_EXPECTED_FIELDS[key]} — "
            "필드 추가/삭제 시 파서 인덱스를 확인하세요"
        )


def test_prompt_placeholders_are_not_parsed_as_data():
    """플레이스홀더 그대로(예: [코드])가 들어와도 숫자 필드가 예외로 조용히 버려지지 않는지가 아니라,
    '프롬프트 스키마 모양 그대로 채운 유효한 값'이 모든 필드에 반영되는지 확인한다."""
    schema = _prompt_schema_lines()
    values = {
        "stance":  ["stance", "defensive", "35"],
        "thesis":  ["thesis", "challenged"],
        "analyst": ["analyst", "partial", "위원회 이견"],
        "new":     ["new", "009150", "삼성전기", "3", "high", "mid", "AI 서버 MLCC", "early", "3.5:1", "수요 둔화"],
        "reduce":  ["reduce", "005930", "삼성전자", "2", "비중 초과"],
        "exit":    ["exit", "000660", "SK하이닉스", "", "근거 훼손"],
        "hold":    ["hold", "005380", "현대차", "medium", "mature", "실적 발표", "점유율 하락"],
        "risk":    ["risk", "환율 급등"],
        "note":    ["note", "현금 확대"],
    }
    for key, parts in values.items():
        assert len(parts) == len(schema[key]), f"{key}: 테스트 샘플이 프롬프트 스키마와 필드 수가 다름"

    block = "\n".join("|".join(v) for v in values.values())
    text = f"본문\n=CIO_DECISION_START=\n{block}\n=CIO_DECISION_END="
    cleaned, d = ceo_agent._parse_cio_decisions(text, "2026-09-29", "pre_market")

    assert "CIO_DECISION" not in cleaned
    assert (d["macro_stance"], d["cash_target_pct"]) == ("defensive", 35)
    assert d["thesis_status"] == "challenged"
    assert (d["committee_alignment"], d["committee_dissent"]) == ("partial", "위원회 이견")

    new = d["new_positions"][0]
    assert (new["code"], new["size_pct"], new["conviction"], new["timeframe"]) == ("009150", 3.0, "high", "mid")
    assert (new["thesis_stage"], new["risk_reward"], new["falsification"]) == ("early", "3.5:1", "수요 둔화")

    actions = {c["action"]: c for c in d["position_changes"]}
    assert actions["reduce"]["size_change_pct"] == 2 and actions["reduce"]["reason"] == "비중 초과"
    assert actions["exit"]["code"] == "000660" and actions["exit"]["reason"] == "근거 훼손"

    hold = d["position_holds"][0]
    assert (hold["thesis_stage"], hold["review_trigger"], hold["falsification"]) == ("mature", "실적 발표", "점유율 하락")
    assert d["key_risks"] == ["환율 급등"] and d["strategic_note"] == "현금 확대"


def test_every_briefing_prompt_still_requires_the_decision_block():
    """PRE/CLOSE 프롬프트 어디에서도 결정 블록 출력 지시가 빠지면 안 된다(헌장 경유 포함)."""
    assert "=CIO_DECISION_START=" in ceo_agent._CIO_CHARTER
    assert re.search(r"반드시 .*블록을 출력", ceo_agent._CIO_CHARTER)
