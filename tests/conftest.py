"""pytest 공통 설정 — 프로젝트 루트를 import 경로에 추가."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
