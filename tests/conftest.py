"""pytest 공통 설정 — 프로젝트 루트를 import 경로에 추가.

DB_FORCE_SQLITE=1: db.database가 .env를 자동 로드하므로, 플래그 없이는
로컬 pytest가 운영 Neon에 연결된다. 테스트는 항상 로컬 SQLite로 격리한다.
(db.database를 import하기 전에 설정돼야 하므로 conftest 최상단에 둔다)
"""
import os
import sys
from pathlib import Path

os.environ["DB_FORCE_SQLITE"] = "1"

sys.path.insert(0, str(Path(__file__).parent.parent))


import pytest


@pytest.fixture(autouse=True)
def _no_naver_network(monkeypatch):
    """네이버 지수 보강(market_data_client._naver_get)이 테스트에서 실망을 타지 않게 차단."""
    try:
        import clients.market_data_client as m
        monkeypatch.setattr(m, "_naver_get", lambda url: None)
    except Exception:
        pass
    yield


@pytest.fixture(autouse=True)
def _no_naver_client_network(monkeypatch):
    try:
        import clients.naver_client as n
        monkeypatch.setattr(n, "_get", lambda url: None)
    except Exception:
        pass
    yield


@pytest.fixture(autouse=True)
def _no_research_pipeline_network(monkeypatch, request):
    """리서치 파이프라인(증권사·중앙은행 수집 + LLM 읽기)이 다른 테스트에서 실망·LLM을 타지 않게 차단.
    전용 테스트(test_research_pipeline)는 실제 함수를 그대로 검증한다."""
    if request.module.__name__.endswith("test_research_pipeline"):
        yield
        return
    try:
        import services.research_pipeline as rp
        monkeypatch.setattr(rp, "run", lambda *a, **k: "")
    except Exception:
        pass
    yield
