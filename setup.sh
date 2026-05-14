#!/bin/bash
# ============================================================
# AI Investment Company — 새 PC 자동 셋업 스크립트 (Mac/Linux)
# 사용법:
#   cd <프로젝트 폴더>
#   chmod +x setup.sh && ./setup.sh
# ============================================================

set -e

echo ""
echo "=== AI Investment Company 셋업 시작 ==="
echo ""

# ── 1. Python 버전 확인 ──────────────────────────────────────
echo "[1/5] Python 버전 확인..."
if command -v python3 &>/dev/null; then
    PYVER=$(python3 --version)
    echo "  $PYVER"
    PY=python3
elif command -v python &>/dev/null; then
    PYVER=$(python --version)
    echo "  $PYVER"
    PY=python
else
    echo "  ERROR: Python이 설치되지 않았습니다."
    echo "  https://python.org 에서 Python 3.12 이상 설치 후 다시 실행하세요."
    exit 1
fi

# ── 2. 가상환경 생성 및 활성화 ──────────────────────────────
echo ""
echo "[2/5] 가상환경 설정..."
if [ ! -d ".venv" ]; then
    $PY -m venv .venv
    echo "  .venv 생성 완료"
else
    echo "  .venv 이미 존재 — 스킵"
fi
source .venv/bin/activate
echo "  가상환경 활성화 완료"

# ── 3. 패키지 설치 ──────────────────────────────────────────
echo ""
echo "[3/5] 패키지 설치 (requirements.txt)..."
pip install -r requirements.txt --quiet
echo "  패키지 설치 완료"

# ── 4. .env 파일 확인 ───────────────────────────────────────
echo ""
echo "[4/5] .env 파일 확인..."
if [ -f ".env" ]; then
    echo "  .env 파일 존재 — OK"
else
    echo "  .env 파일 없음 — .env.example 복사 후 API 키 입력 필요"
    cp .env.example .env
    echo ""
    echo "  ★ .env 파일이 생성되었습니다. 아래 항목을 직접 입력하세요:"
    echo "     - OPENAI_API_KEY"
    echo "     - TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID"
    echo "     - KIS_APP_KEY / KIS_APP_SECRET / KIS_ACCOUNT_NO"
    echo "     - DART_API_KEY"
    echo ""
    echo "  편집: nano .env  또는  code .env"
fi

# ── 5. data 폴더 생성 ───────────────────────────────────────
echo ""
echo "[5/5] data 폴더 확인..."
mkdir -p data
echo "  data/ 폴더 준비 완료"

# ── 완료 ────────────────────────────────────────────────────
echo ""
echo "=== 셋업 완료 ==="
echo ""
echo "다음 명령어로 실행할 수 있습니다:"
echo "  source .venv/bin/activate   # 가상환경 활성화"
echo "  python main.py pre          # 장전 브리핑 테스트"
echo "  python main.py intra1       # 장중 1차 테스트"
echo ""
