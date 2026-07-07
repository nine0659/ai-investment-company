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
