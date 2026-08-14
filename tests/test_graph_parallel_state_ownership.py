"""L2/L3 병렬 브랜치 state 키 소유권 감사.

test_graph_parallel_state_merge.py는 3bdbb8d에서 고친 특정 버그(전체 state를
반환해 형제 브랜치를 덮어쓰는 것)의 재발만 막는다. 이 파일은 한 단계 더
일반화된 불변식을 검증한다: 같은 병렬 레이어의 두 브랜치가 애초에 같은
state 키를 쓰지 않아야 한다는 것. _parallel() 델타 래퍼가 있어도, 같은
super-step에서 두 브랜치가 같은 채널에 값을 반환하면 LangGraph는 명시적
리듀서 없는 채널에 대해 정의되지 않은 동작(보통 에러)을 낸다 — 새 에이전트를
추가하다 실수로 기존 report 키를 재사용하면 이 테스트가 즉시 잡아낸다.

또한 각 에이전트가 최소 하나의 state 키는 반드시 쓰는지도 확인한다 —
아무 키도 안 쓰면 계산 결과가 어디에도 반영되지 않는, 이 프로젝트에서
세 번이나 반복된 "계산은 되는데 안 보이는" 패턴의 가장 단순한 형태다.
"""
import ast
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent

# graph/investment_graph.py의 _L2_NODES / _L3_NODES와 대응 — 새 노드를
# 그래프에 추가하면 여기도 같이 추가해야 이 감사가 그 노드를 커버한다.
_L2_MODULES = {
    "futures_market_team":      "agents/futures_market_team.py",
    "us_global_team":           "agents/us_global_team.py",
    "news_analysis_team":       "agents/news_analysis_team.py",
    "bigfigure_agent":          "agents/bigfigure_agent.py",
    "macro_team":                "agents/macro_team.py",
    "event_risk_team":          "agents/event_risk_team.py",
    "market_intelligence_team": "agents/market_intelligence_team.py",
}
_L3_MODULES = {
    "korea_flow_team":  "agents/korea_flow_team.py",
    "issue_stock_agent": "agents/issue_stock_agent.py",
}


def _state_keys_written(rel_path: str) -> set[str]:
    """소스를 파싱해 `state["key"] = ...` 형태의 대입 키를 전부 추출."""
    tree = ast.parse((REPO_ROOT / rel_path).read_text(encoding="utf-8"))
    keys: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if not (isinstance(target, ast.Subscript) and isinstance(target.value, ast.Name)
                    and target.value.id == "state"):
                continue
            sl = target.slice
            key_node = sl.value if isinstance(sl, ast.Index) else sl  # py3.8 호환
            if isinstance(key_node, ast.Constant) and isinstance(key_node.value, str):
                keys.add(key_node.value)
    return keys


def _assert_disjoint_ownership(layer_name: str, modules: dict[str, str]):
    owned = {name: _state_keys_written(path) - {"errors"}  # errors는 _parallel()이 별도 처리
             for name, path in modules.items()}

    owner_of: dict[str, str] = {}
    conflicts = []
    for name, keys in owned.items():
        for k in keys:
            if k in owner_of and owner_of[k] != name:
                conflicts.append((k, owner_of[k], name))
            else:
                owner_of[k] = name

    assert not conflicts, (
        f"{layer_name} 레이어에서 두 병렬 브랜치가 같은 state 키를 씀 — "
        f"같은 super-step에 동일 채널 중복 갱신은 리듀서 없이는 미정의 동작: {conflicts}"
    )


def test_l2_parallel_nodes_own_disjoint_state_keys():
    _assert_disjoint_ownership("L2", _L2_MODULES)


def test_l3_parallel_nodes_own_disjoint_state_keys():
    _assert_disjoint_ownership("L3", _L3_MODULES)


def test_every_l2_module_writes_at_least_one_state_key():
    for name, path in _L2_MODULES.items():
        keys = _state_keys_written(path) - {"errors"}
        assert keys, f"{name}가 state 키를 하나도 안 씀 — 결과가 브리핑에 반영될 경로가 없다"


def test_every_l3_module_writes_at_least_one_state_key():
    for name, path in _L3_MODULES.items():
        keys = _state_keys_written(path) - {"errors"}
        assert keys, f"{name}가 state 키를 하나도 안 씀 — 결과가 브리핑에 반영될 경로가 없다"


def test_l2_l3_module_list_matches_graph_definition():
    # investment_graph.py의 _L2_NODES/_L3_NODES가 바뀌었는데 이 감사 목록을
    # 안 갱신하면 새/삭제된 노드가 조용히 감사에서 빠진다.
    import graph.investment_graph as g
    assert set(_L2_MODULES) == set(g._L2_NODES), (
        "이 테스트의 _L2_MODULES를 investment_graph._L2_NODES와 맞춰 갱신하세요"
    )
    assert set(_L3_MODULES) == set(g._L3_NODES), (
        "이 테스트의 _L3_MODULES를 investment_graph._L3_NODES와 맞춰 갱신하세요"
    )
