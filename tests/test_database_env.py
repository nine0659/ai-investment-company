"""DB 레이어 환경 로드 — SQLite 조용한 폴백으로 인한 데이터 유실 방지.

2026-07-03·2026-07-06 두 차례, 진입점이 load_dotenv()를 거치지 않은 실행에서
DATABASE_URL을 못 읽고 조용히 SQLite로 폴백해 포트폴리오 기록이 유실됐다.
db.database가 .env를 자체 로드하고, 폴백은 항상 시끄럽게 알리는지 검증한다.
"""
import os

import db.database as database


def test_engine_is_sqlite_under_pytest():
    # conftest가 DB_FORCE_SQLITE=1을 설정한다 — 테스트가 운영 Neon에 붙으면 안 됨
    assert database.engine.dialect.name == "sqlite"


def test_env_file_loaded_without_override(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("PROBE_UNSET=from_file\nPROBE_SET=from_file\n", encoding="utf-8")
    monkeypatch.delenv("PROBE_UNSET", raising=False)
    monkeypatch.setenv("PROBE_SET", "from_env")

    database._load_project_env(env_file)

    assert os.getenv("PROBE_UNSET") == "from_file"   # .env에서 채워짐
    assert os.getenv("PROBE_SET") == "from_env"      # 기존 환경변수가 우선
    monkeypatch.delenv("PROBE_UNSET", raising=False)


def test_missing_env_file_is_noop(tmp_path):
    database._load_project_env(tmp_path / "no_such.env")  # 예외 없이 통과


def test_force_sqlite_flag_wins_over_url(monkeypatch):
    # 테스트·오프라인 개발이 운영 DB에 붙는 것을 막는 명시적 스위치
    monkeypatch.setenv("DB_FORCE_SQLITE", "1")
    eng = database._make_engine("postgresql://user:pw@db.invalid/prod")
    assert eng.dialect.name == "sqlite"


def test_missing_url_falls_back_loudly(monkeypatch):
    # 과거엔 무경고 폴백 → 유실을 아무도 몰랐다. 반드시 경보가 나가야 한다.
    monkeypatch.delenv("DB_FORCE_SQLITE", raising=False)
    alerts = []
    monkeypatch.setattr(database, "_alert_db_fallback", lambda detail: alerts.append(detail))

    eng = database._make_engine("")

    assert eng.dialect.name == "sqlite"
    assert alerts and "DATABASE_URL" in alerts[0]
