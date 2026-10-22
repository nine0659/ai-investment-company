"""대형 배치 작업의 메모리 안전장치 — 한도 근접 시 OS가 죽이기 전에 스스로 중단(잡 fail), 측정 불가 환경에선 무동작."""
import pytest

from services import memory_watch as mw


def test_guard_noop_when_memory_unknown(monkeypatch):
    monkeypatch.setattr(mw, "container_rss_mb", lambda: None)
    mw.guard("테스트")                                           # Windows 등 측정 불가 → 예외 없음


def test_guard_noop_below_threshold(monkeypatch):
    monkeypatch.setattr(mw, "container_rss_mb", lambda: 300.0)
    mw.guard("테스트")


def test_guard_trims_and_continues_if_memory_drops(monkeypatch):
    seq = iter([470.0, 380.0])
    trimmed = []
    monkeypatch.setattr(mw, "container_rss_mb", lambda: next(seq))
    monkeypatch.setattr(mw, "trim", lambda: trimmed.append(1))
    mw.guard("테스트")                                           # 정리 후 내려갔으면 계속 진행
    assert trimmed == [1]


def test_guard_aborts_if_memory_stays_high(monkeypatch):
    monkeypatch.setattr(mw, "container_rss_mb", lambda: 480.0)
    monkeypatch.setattr(mw, "trim", lambda: None)
    with pytest.raises(MemoryError, match="한국 스크리너"):
        mw.guard("한국 스크리너 이력 수집")


def test_screeners_call_the_guard_between_chunks(monkeypatch):
    """두 스크리너의 다운로드 루프가 실제로 guard를 부른다 — 호출이 빠지면 안전장치가 조용히 사라진다."""
    from services import universe_screener as kr
    from services import us_universe_screener as us
    calls = []
    monkeypatch.setattr(mw, "guard", lambda what="", threshold_mb=0: calls.append(what))
    monkeypatch.setattr(kr, "_download_part", lambda part, tmap, out: 1)
    kr.fetch_history([{"code": f"{i:06d}", "market": "KOSPI"} for i in range(250)])
    assert len(calls) == 3 and all("한국" in c for c in calls)          # 250개 → 100개씩 3묶음
    calls.clear()
    import sys, types
    fake = types.ModuleType("yfinance")
    fake.download = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("net"))
    monkeypatch.setitem(sys.modules, "yfinance", fake)
    us.fetch_prices([f"T{i}" for i in range(150)])
    assert len(calls) == 2 and all("미국" in c for c in calls)


def test_memory_error_in_job_is_recorded_as_fail(monkeypatch):
    """MemoryError도 기존 잡 규약대로 fail로 기록되고 스케줄러 스레드는 살아남는다."""
    import scheduler
    rec = []
    monkeypatch.setattr("services.job_ledger.record_job", lambda n, s, d="": rec.append((n, s)))
    monkeypatch.setattr(scheduler, "is_krx_trading_day", lambda: True)
    from services import universe_screener as kr
    monkeypatch.setattr(kr, "run_daily_job", lambda: (_ for _ in ()).throw(MemoryError("메모리 480MB")))
    scheduler.job_universe_screen()
    assert rec == [("universe_screen", "fail")]
