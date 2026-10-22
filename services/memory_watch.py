"""컨테이너 메모리 사용량 측정·정리·경보 (2026-10-13).

배경: 2026-10-12 10:00 Render 무료 플랜(512MB)에서 메모리 초과로 인스턴스가 재시작됐다.
무료 플랜은 메모리 그래프(Metrics)가 유료 전용이라 평소 사용량을 볼 방법이 없었다 —
그래서 코드가 직접 재서 로그에 남긴다. 스케줄러(~200MB)와 웹(~90MB)이 한 컨테이너를
나눠 쓰므로 컨테이너 전체(모든 프로세스 RSS 합)를 본다.

리눅스(/proc)에서만 측정한다. 다른 OS(개발용 Windows)에서는 None을 반환하고 조용히 넘어간다.
이 모듈의 어떤 함수도 예외를 밖으로 던지지 않는다 — 감시 때문에 잡이 죽으면 안 된다.
"""
import gc
import logging
import os
import time

logger = logging.getLogger(__name__)

LIMIT_MB = 512
WARN_MB = 400            # 이 이상이면 텔레그램 경보 (한도의 약 78%)
_ALERT_COOLDOWN_SEC = 6 * 3600
_last_alert_ts = 0.0
# 이 프로세스(스케줄러)가 본 컨테이너 메모리 최고치 — post_deploy_check가 읽는다.
_peak: dict = {"mb": None, "job": "", "at": ""}


def container_rss_mb() -> float | None:
    """컨테이너 안 모든 프로세스의 RSS 합(MB). 측정 불가 환경이면 None."""
    try:
        if not os.path.isdir("/proc"):
            return None
        total_kb = 0
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            try:
                with open(f"/proc/{pid}/status", encoding="utf-8", errors="ignore") as f:
                    for line in f:
                        if line.startswith("VmRSS:"):
                            total_kb += int(line.split()[1])
                            break
            except (OSError, ValueError):
                continue
        return round(total_kb / 1024, 1) if total_kb else None
    except Exception:
        return None


def trim() -> None:
    """파이썬이 쥐고 있는 미사용 메모리를 OS에 돌려준다 (gc + glibc malloc_trim)."""
    try:
        gc.collect()
        import ctypes
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except Exception:
        pass


GUARD_MB = 460           # 이 이상이면 대형 배치 작업이 스스로 중단한다 (한도 512MB — OS가 조용히 프로세스를 죽이기 전에)


def guard(what: str = "", threshold_mb: float = GUARD_MB) -> None:
    """대형 배치(전 종목 스크리너)의 묶음 사이에서 호출: 컨테이너 메모리가 임계값 이상이면 한 번 정리해 보고,
    그래도 높으면 MemoryError로 작업을 중단한다 → 잡이 'fail'로 기록돼 헬스체크가 알린다
    (2026-10-12 무료 플랜 OOM 재시작 전례: 서비스 전체가 조용히 재시작되면 다른 잡도 같이 사라진다).
    측정 불가(Windows 등)면 아무것도 하지 않는다."""
    mb = container_rss_mb()
    if mb is None or mb < threshold_mb:
        return
    trim()
    mb2 = container_rss_mb()
    if mb2 is not None and mb2 >= threshold_mb:
        raise MemoryError(f"메모리 {mb2:.0f}MB ≥ {threshold_mb:.0f}MB — {what or '배치 작업'} 중단 (한도 {LIMIT_MB}MB)")


def after_job(job_name: str) -> float | None:
    """잡 종료 직후 호출: 정리 전/후 메모리를 로그로 남기고, 높으면 경보(6시간 1회)."""
    global _last_alert_ts
    try:
        before = container_rss_mb()
        trim()
        after = container_rss_mb()
        if before is None:
            return None
        logger.info("[메모리] %s 종료 — 컨테이너 %.0fMB → 정리 후 %.0fMB (한도 %dMB)",
                    job_name, before, after if after is not None else before, LIMIT_MB)
        peak = max(before, after or 0)
        if _peak["mb"] is None or peak > _peak["mb"]:
            _peak.update(mb=peak, job=job_name, at=time.strftime("%m-%d %H:%M"))
        if peak >= WARN_MB and time.time() - _last_alert_ts > _ALERT_COOLDOWN_SEC:
            _last_alert_ts = time.time()
            from clients.telegram_client import send_error_alert
            send_error_alert(
                f"[메모리 경고] {job_name} 직후 컨테이너 메모리 {peak:.0f}MB / 한도 {LIMIT_MB}MB.\n"
                f"한도를 넘으면 서버가 자동 재시작됩니다(2026-10-12 사례). 반복되면 동시 실행 축소나 "
                f"플랜 상향을 검토하세요."
            )
        return after if after is not None else before
    except Exception as e:
        logger.debug("[메모리] 측정 실패 (무시): %s", e)
        return None


def peak() -> dict:
    """이 프로세스가 기록한 메모리 최고치 {"mb","job","at"} (측정 전이면 mb=None)."""
    return dict(_peak)
