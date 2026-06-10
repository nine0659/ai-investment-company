#!/usr/bin/env bash
# start.sh — 스케줄러 전용 (웹 대시보드 제거 — 2026-06-10)
# 웹 대시보드는 web/ 디렉토리에 코드 보존. 필요 시 복원 가능.

set -e

# DB 초기화 (최초 실행 시)
python main.py --init-db 2>/dev/null || true

# 스케줄러 실행 (foreground — Render Worker 타입)
echo "[start.sh] 스케줄러 시작..."
exec python scheduler.py
