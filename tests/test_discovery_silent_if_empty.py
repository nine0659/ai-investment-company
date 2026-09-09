"""discovery_agent.run_discovery(silent_if_empty=True)의 발송 억제 로직 검증.

주간 자동 실행(job_discovery_weekly)이 "이번 주 발굴 없음"류 공허한
리포트를 반복 발송하지 않는다는 것 자체를 직접 검증 — 스케줄러 레벨
테스트(test_discovery_scheduler.py)와 상호보완.
"""
import agents.discovery_agent as discovery_agent
from db.database import init_db, get_conn
from sqlalchemy import text


def setup_function(_):
    init_db()
    with get_conn() as conn:
        # stage_watchlist_candidate는 이미 존재하는 code를 스킵하므로(2026-09-09
        # 승인 큐), 이전 테스트가 남긴 005930 candidate 행이 있으면 이번 테스트의
        # "신규 스테이징" 카운트가 0이 돼 assert가 깨진다 — 매 테스트 전 정리.
        conn.execute(text("DELETE FROM watchlist_items"))


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

    sent, cards = [], []
    monkeypatch.setattr(discovery_agent, "send_message", lambda text: sent.append(text))
    monkeypatch.setattr(discovery_agent, "send_message_with_buttons",
                         lambda text, buttons: cards.append((text, buttons)))

    report = discovery_agent.run_discovery(send=True, silent_if_empty=True)

    assert sent == []
    assert cards == []  # 후보 0개면 승인 카드도 발송되지 않아야 함 (2026-09-09 승인 큐)
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

    sent, cards = [], []
    monkeypatch.setattr(discovery_agent, "send_message", lambda text: sent.append(text))
    monkeypatch.setattr(discovery_agent, "send_message_with_buttons",
                         lambda text, buttons: cards.append((text, buttons)))

    report = discovery_agent.run_discovery(send=True, silent_if_empty=True)

    assert len(sent) == 1
    assert len(cards) == 1  # 후보 1개 → 승인 카드 1장 (2026-09-09 승인 큐)
    assert "WATCH_START" not in report  # 파싱 블록은 사용자 노출 전 제거됨
