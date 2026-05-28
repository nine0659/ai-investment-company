#!/usr/bin/env bash
# start.sh — 웹 서버 + 스케줄러를 한 컨테이너에서 동시 실행

set -e

# DB 초기화 (최초 실행 시)
python main.py --init-db 2>/dev/null || true

# 스케줄러를 백그라운드로 실행
echo "[start.sh] 스케줄러 시작..."
python scheduler.py &
SCHED_PID=$!

# 웹 서버 실행 (foreground — Railway/Render의 $PORT 사용)
echo "[start.sh] 웹 서버 시작 (포트: ${PORT:-8000})..."
exec uvicorn web.app:app \
    --host 0.0.0.0 \
    --port "${PORT:-8000}" \
    --workers 1 \
    --log-level info
