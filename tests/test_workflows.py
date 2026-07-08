"""GH Actions 워크플로 회귀 테스트.

2026-07-08 사고: nav-tracker.yml(+realtime/emergency-monitor.yml)이 YAML 문법
오류(run 블록 들여쓰기)로 생성일(2026-06-08)부터 한 번도 실행되지 못했고,
Render 재시작으로 증발한 daily_tracker 16:20 실행을 아무도 대신하지 않았다.
깨진 워크플로는 push마다 failure로 뜨지만 아무도 눈치채지 못했다 —
여기서 잡아서 push 자체(원칙 1)를 막는다.
"""
import os
from glob import glob

import pytest
import yaml

_WF_DIR = os.path.join(os.path.dirname(__file__), "..", ".github", "workflows")
_ROOT = os.path.join(os.path.dirname(__file__), "..")


def _workflow_files() -> list[str]:
    return sorted(glob(os.path.join(_WF_DIR, "*.yml")) + glob(os.path.join(_WF_DIR, "*.yaml")))


def test_workflow_dir_not_empty():
    assert _workflow_files(), ".github/workflows 에 워크플로가 없음"


@pytest.mark.parametrize("path", _workflow_files(), ids=os.path.basename)
def test_workflow_yaml_parses(path):
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    assert isinstance(data, dict), f"{os.path.basename(path)}: 최상위가 매핑이 아님"
    # YAML 1.1에서 'on'은 불리언 True로 파싱될 수 있다 — 어느 쪽이든 존재해야 함
    assert ("on" in data) or (True in data), f"{os.path.basename(path)}: 트리거(on) 없음"
    assert "jobs" in data and data["jobs"], f"{os.path.basename(path)}: jobs 없음"


def test_nav_tracker_backup_script_exists():
    """백업 워크플로가 가리키는 실행 스크립트가 실제로 존재해야 한다."""
    with open(os.path.join(_WF_DIR, "nav-tracker.yml"), encoding="utf-8") as f:
        content = f.read()
    assert "scripts/backup_daily_jobs.py" in content
    assert os.path.exists(os.path.join(_ROOT, "scripts", "backup_daily_jobs.py"))
