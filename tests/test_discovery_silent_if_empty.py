"""discovery_agent.run_discovery(silent_if_empty=True)의 발송 억제 로직 검증.

주간 자동 실행(job_discovery_weekly)이 "이번 주 발굴 없음"류 공허한
리포트를 반복 발송하지 않는다는 것 자체를 직접 검증 — 스케줄러 레벨
테스트(test_discovery_scheduler.py)와 상호보완.
"""
import agents.discovery_agent as discovery_agent
from db.database import init_db


def _stub_heavy_deps(monkeypatch):
    monkeypatch.setattr(discovery_agent, "KISClient", lambda: object())
    monkeypatch.setattr(discovery_agent, "fetch_global_market_data", lambda: {})
    monkeypatch.setattr(discovery_agent, "fetch_us_sectors", lambda: {})
    monkeypatch.setattr(discovery_agent, "_get_holding_codes", lambda: set())
    monkeypatch.setattr(discovery_agent, "_build_candidate_pool", lambda kis, exclude: [])
    monkeypatch.setattr(discovery_agent, "_enrich_candidates", lambda kis, candidates: [])


def test_silent_if_empty_suppresses_send_when_no_candidates(monkeypatch):
    init_db()
    _stub_heavy_deps(monkeypatch)
    monkeypatch.setattr(discovery_agent, "chat", lambda *a, **kw: "이번 주 발굴 없음 — 반증할 후보 부족")

    sent = []
    monkeypatch.setattr(discovery_agent, "send_message", lambda text: sent.append(text))

    report = discovery_agent.run_discovery(send=True, silent_if_empty=True)

    assert sent == []
    assert "발굴 없음" in report  # 리포트 텍스트 자체는 항상 반환됨


def test_silent_if_empty_still_sends_when_candidate_found(monkeypatch):
    init_db()
    _stub_heavy_deps(monkeypatch)
    canned = (
        "③ 발굴 종목\n삼성전자(005930) | 현재가 70000원 | 컨센서스 85000원 (업사이드 +21%)\n\n"
        "=WATCH_START=\n"
        "watch|005930|삼성전자|70000|외국인 순매수+파운드리 반등\n"
        "=WATCH_END="
    )
    monkeypatch.setattr(discovery_agent, "chat", lambda *a, **kw: canned)

    sent = []
    monkeypatch.setattr(discovery_agent, "send_message", lambda text: sent.append(text))

    report = discovery_agent.run_discovery(send=True, silent_if_empty=True)

    assert len(sent) == 1
    assert "WATCH_START" not in report  # 파싱 블록은 사용자 노출 전 제거됨
