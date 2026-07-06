"""
services/data_guard.py — 수집 데이터 무결성 가드

LLM은 주어진 숫자를 무비판적으로 서술한다. 오염된 수치 하나가 브리핑 전체의
신뢰를 무너뜨린다 (실제 사례: 매출성장률 전 종목 -75%, 배당수익률 291%,
52주 저점 대비 5배 괴리로 "상승 여력 큼" 오판).

원칙: 이상한 숫자는 고치려 하지 말고 제거(N/A)한다. 프롬프트들은 이미
"없는 수치는 만들지 마라"를 강제하므로, N/A로 만들면 해당 수치는
브리핑에서 언급 자체가 사라진다 — 틀린 수치보다 빠진 수치가 낫다.
"""
import logging

logger = logging.getLogger(__name__)

# 필드별 허용 범위 (min, max) — 벗어나면 오염 데이터로 간주하고 제거.
# 범위는 '이론상 불가능'이 아니라 '데이터 오류가 확실시되는' 수준으로 느슨하게 잡는다.
_RANGES: dict[str, tuple[float, float]] = {
    "price":          (1, 10_000_000),   # 원
    "52w_high":       (1, 10_000_000),
    "52w_low":        (1, 10_000_000),
    "market_cap_억":  (10, 50_000_000),  # 억원 (5경 원 상한)
    "per":            (0.1, 500),
    "pbr":            (0.01, 100),
    "roe":            (-100, 150),       # %
    "debt_ratio":     (0, 2000),         # %
    "op_margin":      (-100, 100),       # %
    "q_op_margin":    (-100, 100),
    "revenue_growth": (-90, 300),        # % — 분기/연간 혼합 비교 같은 오류는 대부분 여기 걸린다
    "dividend_yield": (0.01, 20),        # % — 291% 같은 단위 오류 차단
}


def sanitize_stock_data(data: dict) -> tuple[dict, list[str]]:
    """종목 데이터의 이상치를 제거하고 (데이터, 경고목록) 반환. 원본 dict를 수정한다."""
    warnings: list[str] = []
    label = f"{data.get('name', '?')}({data.get('code', '?')})"

    # ── 개별 필드 범위 검사 ───────────────────────────────────
    for field, (lo, hi) in _RANGES.items():
        val = data.get(field)
        if val is None or val == "":
            continue
        try:
            fv = float(val)
        except (TypeError, ValueError):
            fv = None
        if fv is None or not (lo <= fv <= hi):
            warnings.append(f"{label} {field}={val} 허용범위[{lo}~{hi}] 이탈 → 제거")
            data[field] = None

    # ── 교차 정합성 검사: 현재가 vs 52주 밴드 ─────────────────
    # 액면분할·데이터 소스 혼선 시 52주 고저가 현재가와 심하게 어긋난다.
    # 어긋난 52주 값을 남겨두면 "저점 대비 상승 여력 큼" 같은 오판이 나온다.
    price = data.get("price")
    hi52, lo52 = data.get("52w_high"), data.get("52w_low")
    if price:
        inconsistent = (
            (hi52 and price > hi52 * 1.05)          # 현재가가 52주 고점보다 5% 이상 위
            or (lo52 and price < lo52 * 0.95)        # 현재가가 52주 저점보다 아래
            or (lo52 and price / lo52 > 5)           # 저점 대비 5배 초과 — 분할 의심
        )
        if inconsistent:
            warnings.append(f"{label} 52주 고/저({hi52}/{lo52})가 현재가({price})와 모순 → 제거")
            data["52w_high"] = None
            data["52w_low"] = None

    for w in warnings:
        logger.warning("[데이터가드] %s", w)
    return data, warnings


def alert_if_widespread(all_warnings: list[str], source: str, threshold: int = 5) -> None:
    """이상치가 광범위하면 데이터 소스 자체 장애 가능성 — 관리자에게 즉시 경보."""
    if len(all_warnings) < threshold:
        return
    try:
        from clients.telegram_client import send_error_alert
        preview = "\n".join(all_warnings[:5])
        send_error_alert(
            f"[데이터가드] {source}: 이상치 {len(all_warnings)}건 감지 — "
            f"데이터 소스 점검 필요\n{preview}"
        )
    except Exception as e:
        logger.warning("[데이터가드] 경보 발송 실패: %s", e)
