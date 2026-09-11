#!/usr/bin/env bash
set -euo pipefail

# start.sh — 웹 대시보드 + 스케줄러 동시 실행
# 두 프로세스 중 하나라도 죽으면 컨테이너를 실패로 종료해 배포 플랫폼이 재시작하게 한다.

PORT="${PORT:-8000}"

if [ -z "${WEB_PASSWORD:-}" ]; then
  echo "[start.sh] WEB_PASSWORD 미설정 — 웹 대시보드 보호를 위해 시작 중단"
  exit 1
fi

echo "[start.sh] DB 초기화..."
python main.py --init-db

echo "[start.sh] 스케줄러 시작..."
python scheduler.py &
SCHEDULER_PID=$!

echo "[start.sh] 웹 대시보드 시작 (포트: ${PORT})..."
uvicorn web.app:app --host 0.0.0.0 --port "${PORT}" &
WEB_PID=$!

cleanup() {
  echo "[start.sh] 종료 신호 수신 — 하위 프로세스 정리"
  kill "${SCHEDULER_PID}" "${WEB_PID}" 2>/dev/null || true
}
trap cleanup INT TERM EXIT

echo "[start.sh] 실행 중: scheduler=${SCHEDULER_PID}, web=${WEB_PID}"
wait -n "${SCHEDULER_PID}" "${WEB_PID}"

echo "[start.sh] 핵심 프로세스 중 하나가 종료됨 — 컨테이너 재시작 필요"
exit 1
