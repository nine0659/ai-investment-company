FROM python:3.11-slim

# 시스템 패키지 (lxml, cryptography 빌드용)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libxml2-dev libxslt-dev libffi-dev libssl-dev curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 의존성 먼저 설치 (레이어 캐시 활용)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 소스 복사
COPY . .

# 데이터 디렉토리 (볼륨 마운트 또는 ephemeral)
RUN mkdir -p data/logs

# 포트 노출 (Railway/Render 환경변수 $PORT 우선)
EXPOSE 8000

# 시작 스크립트
CMD ["bash", "start.sh"]
