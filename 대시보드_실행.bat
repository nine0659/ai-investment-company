@echo off
chcp 65001 > nul
title AI 투자 어시스턴트 대시보드
cd /d "%~dp0"

echo.
echo  ============================================
echo   AI 투자 어시스턴트 대시보드 시작 중...
echo  ============================================
echo.

:: 기존 포트 8000 사용 중인 프로세스 종료
for /f "tokens=5" %%a in ('netstat -aon 2^>nul ^| findstr ":8000"') do (
    taskkill /F /PID %%a > nul 2>&1
)

echo  서버 시작 중 (잠시 대기)...
start /B python -m uvicorn web.app:app --host 0.0.0.0 --port 8000

:: 서버 시작 대기
timeout /t 3 /nobreak > nul

:: 브라우저 열기
echo  브라우저 열기: http://localhost:8000
start http://localhost:8000

echo.
echo  대시보드가 브라우저에서 열렸습니다.
echo  이 창을 닫으면 서버도 종료됩니다.
echo  (계속 실행하려면 이 창을 열어두세요)
echo.
echo  종료하려면 Ctrl+C 를 누르세요.
echo.

:: 서버 포그라운드 실행 (창 닫을 때까지 유지)
python -m uvicorn web.app:app --host 0.0.0.0 --port 8000
