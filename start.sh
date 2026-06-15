#!/usr/bin/env bash
# start.sh — 스케줄러 + 최소 헬스체크 서버 동시 실행
# Render Web Service 무료 플랜: 자체 핑으로 슬립 방지

set -e

# DB 초기화 (최초 실행 시)
python main.py --init-db 2>/dev/null || true

# 스케줄러 백그라운드 실행
echo "[start.sh] 스케줄러 시작..."
python scheduler.py &

# 자체 핑 — Render 슬립 방지 (10분마다 자기 자신 호출)
# RENDER_EXTERNAL_URL은 Render가 자동으로 주입하는 공개 URL 환경변수
(
  sleep 60  # 헬스체크 서버 기동 대기
  while true; do
    if [ -n "${RENDER_EXTERNAL_URL:-}" ]; then
      curl -sf "${RENDER_EXTERNAL_URL}/health" -o /dev/null 2>/dev/null || true
    fi
    sleep 600  # 10분
  done
) &

# Render 헬스체크 + UptimeRobot 핑 수신용 최소 HTTP 서버 (포그라운드)
echo "[start.sh] 헬스체크 서버 시작 (포트: ${PORT:-8000})..."
exec python -c "
import os, http.server, socketserver, json
from datetime import datetime

PORT = int(os.environ.get('PORT', 8000))

class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps({'ok': True, 'time': datetime.utcnow().isoformat()}).encode())
    def log_message(self, fmt, *args):
        pass  # 핑 로그 억제

socketserver.TCPServer.allow_reuse_address = True  # 재시작 시 포트 충돌 방지
with socketserver.TCPServer(('', PORT), Handler) as s:
    s.serve_forever()
"
