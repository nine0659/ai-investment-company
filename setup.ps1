# ============================================================
# AI Investment Company — 새 PC 자동 셋업 스크립트 (Windows)
# 사용법: PowerShell에서 실행
#   cd <프로젝트 폴더>
#   .\setup.ps1
# ============================================================

$ErrorActionPreference = "Stop"

Write-Host ""
Write-Host "=== AI Investment Company 셋업 시작 ===" -ForegroundColor Cyan
Write-Host ""

# ── 1. Python 버전 확인 ──────────────────────────────────────
Write-Host "[1/5] Python 버전 확인..." -ForegroundColor Yellow
try {
    $pyver = python --version 2>&1
    Write-Host "  $pyver" -ForegroundColor Green
    if ($pyver -notmatch "3\.(10|11|12|13)") {
        Write-Host "  WARNING: Python 3.10 이상을 권장합니다." -ForegroundColor Yellow
    }
} catch {
    Write-Host "  ERROR: Python이 설치되지 않았습니다." -ForegroundColor Red
    Write-Host "  https://python.org 에서 Python 3.12 이상 설치 후 다시 실행하세요." -ForegroundColor Red
    exit 1
}

# ── 2. 가상환경 생성 및 활성화 ──────────────────────────────
Write-Host ""
Write-Host "[2/5] 가상환경 설정..." -ForegroundColor Yellow
if (-not (Test-Path ".venv")) {
    python -m venv .venv
    Write-Host "  .venv 생성 완료" -ForegroundColor Green
} else {
    Write-Host "  .venv 이미 존재 — 스킵" -ForegroundColor Gray
}

# 가상환경 활성화
& ".venv\Scripts\Activate.ps1"
Write-Host "  가상환경 활성화 완료" -ForegroundColor Green

# ── 3. 패키지 설치 ──────────────────────────────────────────
Write-Host ""
Write-Host "[3/5] 패키지 설치 (requirements.txt)..." -ForegroundColor Yellow
pip install -r requirements.txt --quiet
Write-Host "  패키지 설치 완료" -ForegroundColor Green

# ── 4. .env 파일 확인 ───────────────────────────────────────
Write-Host ""
Write-Host "[4/5] .env 파일 확인..." -ForegroundColor Yellow
if (Test-Path ".env") {
    Write-Host "  .env 파일 존재 — OK" -ForegroundColor Green
} else {
    Write-Host "  .env 파일 없음 — .env.example 복사 후 API 키 입력 필요" -ForegroundColor Red
    Copy-Item ".env.example" ".env"
    Write-Host ""
    Write-Host "  ★ .env 파일이 생성되었습니다. 아래 항목을 직접 입력하세요:" -ForegroundColor Cyan
    Write-Host "     - OPENAI_API_KEY"
    Write-Host "     - TELEGRAM_BOT_TOKEN"
    Write-Host "     - TELEGRAM_CHAT_ID"
    Write-Host "     - KIS_APP_KEY"
    Write-Host "     - KIS_APP_SECRET"
    Write-Host "     - KIS_ACCOUNT_NO"
    Write-Host "     - DART_API_KEY"
    Write-Host ""
    Write-Host "  메모장으로 열기: notepad .env" -ForegroundColor Yellow
}

# ── 5. data 폴더 생성 ───────────────────────────────────────
Write-Host ""
Write-Host "[5/5] data 폴더 확인..." -ForegroundColor Yellow
if (-not (Test-Path "data")) {
    New-Item -ItemType Directory -Path "data" | Out-Null
    Write-Host "  data/ 폴더 생성 완료" -ForegroundColor Green
} else {
    Write-Host "  data/ 폴더 이미 존재 — 스킵" -ForegroundColor Gray
}

# ── 완료 ────────────────────────────────────────────────────
Write-Host ""
Write-Host "=== 셋업 완료 ===" -ForegroundColor Cyan
Write-Host ""
Write-Host "다음 명령어로 실행할 수 있습니다:" -ForegroundColor White
Write-Host "  .venv\Scripts\Activate.ps1   # 가상환경 활성화" -ForegroundColor Gray
Write-Host "  python main.py pre           # 장전 브리핑 테스트" -ForegroundColor Gray
Write-Host "  python main.py intra1        # 장중 1차 테스트" -ForegroundColor Gray
Write-Host ""
if (-not (Test-Path ".env") -or (Get-Content ".env" | Select-String "sk-\.\.\." )) {
    Write-Host "  ★ .env 파일에 API 키를 입력한 후 실행하세요." -ForegroundColor Yellow
}
