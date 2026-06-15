#!/usr/bin/env bash
# start.sh — 스케줄러 + 헬스체크 서버 동시 실행
# 순서 중요: HTTP 서버를 먼저 올려서 Render 헬스체크가 즉시 200 받도록 함

# 1. 헬스체크 HTTP 서버를 백그라운드에서 먼저 기동 (Render 포트 즉시 바인딩)
echo "[start.sh] 헬스체크 서버 시작 (포트: ${PORT:-8000})..."
python -c "
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
        pass

socketserver.TCPServer.allow_reuse_address = True
with socketserver.TCPServer(('', PORT), Handler) as s:
    s.serve_forever()
" &
HTTP_PID=$!

# 2. DB 초기화 (HTTP 서버가 이미 올라온 상태에서 실행)
python main.py --init-db 2>/dev/null || true

# 3. 스케줄러 백그라운드 실행
echo "[start.sh] 스케줄러 시작..."
python scheduler.py &

# 4. 자체 핑 — Render 슬립 방지 (10분마다 자기 자신 호출)
(
  sleep 60
  while true; do
    if [ -n "${RENDER_EXTERNAL_URL:-}" ]; then
      curl -sf "${RENDER_EXTERNAL_URL}/health" -o /dev/null 2>/dev/null || true
    fi
    sleep 600
  done
) &

# 5. HTTP 서버 프로세스가 종료되면 컨테이너도 종료
echo "[start.sh] 모든 프로세스 시작 완료. HTTP 서버(PID=$HTTP_PID) 대기 중..."
wait $HTTP_PID
