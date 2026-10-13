"""LLM 출력(CEO 브리핑)의 숫자가 입력에 실제로 있었는지 사후 대조한다.

배경: "없는 수치는 만들지 마라"는 프롬프트 요청일 뿐 강제 장치가 아니다. data_guard는
입력을, 이 모듈은 출력을 본다 — LLM이 입력에 없던 숫자를 지어내거나 잘못 옮겨 적으면
여기서 걸린다. 발송은 막지 않는다(risk_gate와 같은 철학: 코드 계산 → 경고만). LLM을
쓰지 않는 이유도 같다 — 검증기가 환각하면 안 된다.

한계(의도된 것): 코드가 만든 파생값(합계·차이)이나 "약 3배" 같은 서술은 입력에 없을 수
있어 오탐이 가능하다. 그래서 날짜·시각·번호·작은 정수는 제외하고, 경고는 일정 건수
이상일 때만 사용자에게 보낸다. 건수는 항상 기록한다(품질 추세 지표).
"""
import logging
import re

logger = logging.getLogger(__name__)

# 단위가 붙은 숫자만 '주장'으로 본다. 단위 없는 숫자는 번호·날짜와 구분이 안 돼 제외.
_UNIT = r"(?:%|%p|원|달러|\$|배|조|억|만원|bp|포인트|p)"
_CLAIM_RE = re.compile(r"([+\-−▲▼]?\s?\$?\d[\d,]*(?:\.\d+)?)\s?(" + _UNIT + r")")
_ANY_NUM_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")
_DATE_LIKE_RE = re.compile(r"\d{4}[-./]\d{1,2}[-./]\d{1,2}|\d{1,2}:\d{2}")

# 이 건수 이상 근거 없는 수치가 나오면 사용자 경고 (미만은 로그·기록만)
WARN_THRESHOLD = 5


def _to_float(token: str) -> float | None:
    t = token.replace(",", "").replace("$", "").replace("−", "-").replace("▲", "").replace("▼", "-")
    t = t.replace(" ", "").lstrip("+")
    try:
        return abs(float(t))
    except ValueError:
        return None


def corpus_numbers(*texts: str) -> list[float]:
    """입력 텍스트(프롬프트·컨텍스트)에 등장하는 모든 숫자(절댓값)."""
    nums: list[float] = []
    for text in texts:
        if not text:
            continue
        cleaned = _DATE_LIKE_RE.sub(" ", text)
        for m in _ANY_NUM_RE.finditer(cleaned):
            v = _to_float(m.group(0))
            if v is not None:
                nums.append(v)
    return nums


def _is_grounded(value: float, pool: list[float]) -> bool:
    """반올림 오차를 허용해 입력 숫자 중 하나와 일치하는지."""
    for p in pool:
        if p == value:
            return True
        tol = max(0.051, abs(p) * 0.006)
        if abs(p - value) <= tol:
            return True
    return False


def extract_claims(report: str) -> list[tuple[float, str]]:
    """브리핑에서 '단위 붙은 숫자 주장'을 (값, 원문토큰) 목록으로 추출."""
    cleaned = _DATE_LIKE_RE.sub(" ", report or "")
    claims: list[tuple[float, str]] = []
    for m in _CLAIM_RE.finditer(cleaned):
        v = _to_float(m.group(1))
        if v is None:
            continue
        claims.append((v, (m.group(1) + m.group(2)).replace(" ", "")))
    return claims


def check_report_numbers(report: str, *inputs: str) -> dict:
    """브리핑 속 수치 주장 중 입력(프롬프트·컨텍스트)에 없는 것을 찾는다.

    반환: {"total": 주장 수, "ungrounded": [토큰...], "count": 근거 없는 수, "warn": bool}
    """
    claims = extract_claims(report)
    pool = corpus_numbers(*inputs)
    seen: set[str] = set()
    ungrounded: list[str] = []
    for value, token in claims:
        if token in seen:
            continue
        seen.add(token)
        # 0·1 같은 사소한 값은 서술적 표현("1배", "0%")이 많아 제외
        if value <= 1:
            continue
        if not _is_grounded(value, pool):
            ungrounded.append(token)
    result = {
        "total": len(claims),
        "ungrounded": ungrounded,
        "count": len(ungrounded),
        "warn": len(ungrounded) >= WARN_THRESHOLD,
    }
    if ungrounded:
        logger.warning("[수치검증] 입력에 없는 수치 %d건 / 주장 %d건: %s",
                       len(ungrounded), len(claims), ungrounded[:10])
    return result


def format_warning(result: dict) -> str:
    """사용자 경고 문구 (warn일 때만 호출)."""
    shown = ", ".join(result["ungrounded"][:8])
    return (
        f"⚠️ *수치 검증* — 브리핑 속 숫자 {result['count']}개가 분석 입력에서 확인되지 않았습니다.\n"
        f"{shown}\n"
        "AI가 계산했거나 잘못 옮긴 숫자일 수 있으니, 이 숫자들은 매매 판단 전에 직접 확인하세요."
    )
