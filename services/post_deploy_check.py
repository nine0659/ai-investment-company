"""배포 후 1회성 사후 점검 리포트 (2026-10-13).

사람이 "점검하는 걸 잊어버리는" 문제를 없앤다 — 지정한 날 브리핑이 끝난 뒤 Render 서버가
스스로 점검해 텔레그램으로 결과를 보낸다(PC·Claude 세션 불필요). LLM을 쓰지 않는다:
DB 기록과 메모리 측정값만으로 코드가 판정한다.

점검 항목: ① 장전 브리핑이 실제 돌았는가 ② 브리핑 품질 기록(branch:briefing_quality)
③ 컨테이너 메모리 최고치 ④ 서버가 브리핑 전후로 재시작됐는가.
"""
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from db.database import get_conn
from sqlalchemy import text

logger = logging.getLogger(__name__)
_KST = ZoneInfo("Asia/Seoul")

MEM_WARN_MB = 400
MEM_DANGER_MB = 440


def _rows(conn, date: str, like: str):
    return conn.execute(
        text("SELECT job_name, status, detail, created_at FROM job_runs "
             "WHERE date=:d AND job_name LIKE :j ORDER BY id"),
        {"d": date, "j": like},
    ).fetchall()


def build_report(date: str, started_at: datetime | None, peak: dict) -> tuple[str, list[str]]:
    """(리포트 본문, 조치 필요 항목 목록). 조치 항목이 비면 '이상 없음'."""
    actions: list[str] = []
    lines = [f"🔎 *배포 후 사후 점검* ({date})", ""]

    try:
        with get_conn() as conn:
            pre = _rows(conn, date, "pre_market")
            quality = _rows(conn, date, "branch:briefing_quality")
            branch_fail = [r for r in _rows(conn, date, "branch:%")
                           if r[1] == "fail" and r[0] != "branch:briefing_quality"]
            rep = conn.execute(
                text("SELECT id FROM reports WHERE date=:d AND run_type='pre_market' LIMIT 1"),
                {"d": date}).fetchone()
    except Exception as e:
        return f"🔎 사후 점검 실패 — DB 조회 오류: {str(e)[:100]}", ["DB 조회 실패"]

    # ① 브리핑 실행
    if pre and pre[-1][1] == "skipped":
        lines.append(f"• 장전 브리핑: 휴장으로 건너뜀 ({pre[-1][2]})")
    elif rep:
        lines.append("• 장전 브리핑: ✅ 실행·저장됨")
    else:
        lines.append("• 장전 브리핑: 🚨 저장된 리포트 없음")
        actions.append("장전 브리핑이 돌지 않았음 — Render 로그 확인 후 `python main.py --type pre` 수동 실행")

    # ② 품질 기록
    if quality:
        _, st, detail, _ = quality[-1]
        if st == "success":
            lines.append(f"• 판단 재료 품질: ✅ 정상 ({detail})")
        else:
            lines.append(f"• 판단 재료 품질: ⚠️ {detail}")
            actions.append("상류 분석 리포트 부족/실패 — 해당 브랜치 로그 확인")
    else:
        lines.append("• 판단 재료 품질: 기록 없음")
        if rep:
            actions.append("브리핑은 저장됐는데 품질 기록이 없음 — 새 코드가 배포되지 않았을 수 있음(/api/status commit 확인)")
    if branch_fail:
        names = ", ".join(sorted({r[0].replace("branch:", "") for r in branch_fail}))
        lines.append(f"• 분석 팀 실패: ⚠️ {names}")
        actions.append(f"분석 팀 실패: {names}")

    # ③ 메모리
    mb = peak.get("mb")
    if mb is None:
        lines.append("• 메모리: 측정 기록 없음 (서버가 막 재시작됐거나 아직 잡이 안 끝남)")
    elif mb >= MEM_DANGER_MB:
        lines.append(f"• 메모리 최고치: 🚨 {mb:.0f}MB / 512MB ({peak.get('job')} 직후, {peak.get('at')})")
        actions.append("메모리 위험 구간 — 분석 팀 동시 실행 축소 또는 플랜 상향 필요")
    elif mb >= MEM_WARN_MB:
        lines.append(f"• 메모리 최고치: ⚠️ {mb:.0f}MB / 512MB ({peak.get('job')} 직후, {peak.get('at')})")
        actions.append("메모리 주의 구간 — 며칠 더 관찰하고 반복되면 동시 실행 축소 검토")
    else:
        lines.append(f"• 메모리 최고치: ✅ {mb:.0f}MB / 512MB ({peak.get('job')} 직후)")

    # ④ 재시작
    if started_at is not None and started_at.strftime("%Y-%m-%d") == date and started_at.hour * 60 + started_at.minute > 8 * 60 + 15:
        lines.append(f"• 서버 재시작: ℹ️ {started_at.strftime('%H:%M')}에 시작됨 (브리핑 시각 이후) — 위 메모리 값은 재시작 이후 구간만 반영")

    lines.append("")
    if actions:
        lines.append("*조치 필요*")
        lines += [f"{i}. {a}" for i, a in enumerate(actions, 1)]
    else:
        lines.append("✅ 조치할 사항 없음 — 이 점검은 1회성이라 다시 오지 않습니다.")
    return "\n".join(lines), actions


def run(date: str | None = None, started_at: datetime | None = None) -> bool:
    """점검 실행 + 텔레그램 발송. 같은 날 중복 발송은 claim_report_slot이 막는다."""
    from services.job_ledger import record_job
    from services.report_service import claim_report_slot
    from services.memory_watch import peak
    from clients.telegram_client import send_message

    date = date or datetime.now(_KST).strftime("%Y-%m-%d")
    if not claim_report_slot(date, "post_deploy_check"):
        logger.info("[사후점검] 이미 실행됨 — 스킵")
        return False
    body, actions = build_report(date, started_at, peak())
    try:
        send_message(body)
    except Exception:
        from services.report_service import release_report_slot
        release_report_slot(date, "post_deploy_check")   # 발송 실패면 재시도 가능하게 슬롯 반환
        raise
    record_job("post_deploy_check", "success", f"조치 {len(actions)}건")
    return True
