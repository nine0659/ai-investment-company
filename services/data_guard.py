"""
services/data_guard.py — 수집 데이터 무결성 가드

LLM은 주어진 숫자를 무비판적으로 서술한다. 오염된 수치 하나가 브리핑 전체의
신뢰를 무너뜨린다 (실제 사례: 매출성장률 전 종목 -75%, 배당수익률 291%,
52주 저점 대비 5배 괴리로 "상승 여력 큼" 오판).

원칙: 이상한 숫자는 고치려 하지 말고 제거(N/A)한다. 프롬프트들은 이미
"없는 수치는 만들지 마라"를 강제하므로, N/A로 만들면 해당 수치는
브리핑에서 언급 자체가 사라진다 — 틀린 수치보다 빠진 수치가 낫다.

커버리지 감사 (2026-08, 4개 표준 카테고리 기준 — 과거 사고 전부 이 중 하나였음):
  1) 범위(range)      — _RANGES 아래. valuation_service.format_for_prompt가
                          LLM에 주입하는 숫자 필드는 전부 여기 있어야 한다
                          (2026-08 감사에서 revenue_억/op_income_억/net_income_억/
                          q_revenue_억/q_op_income_억 절대금액 필드가 빠진 걸 발견해 추가 —
                          비율 지표만 검사하고 스케일 오류 절대금액은 무방비였다).
  2) 단위(unit)        — 이 파일 밖에 있다: yfinance dividendYield의
                          0.0291 vs 2.91 이중 단위 문제는 agents/us_invest_agent.py의
                          _normalize_dividend_yield()가 진입점에서 정규화한다(KIS 기반
                          한국 종목은 단위가 고정이라 이 문제 자체가 없음). data_guard의
                          dividend_yield 범위(0.01~20%)는 그 이후의 최종 백스톱.
  3) 교차필드(cross)   — 가격 vs 52주 밴드(아래). DART 분기/연간 혼합 비교는
                          services/valuation_service.py가 애초에 연간끼리만 계산하도록
                          구조적으로 분리했고(진짜 수정), revenue_growth 범위는 그래도
                          새는 값에 대한 백스톱일 뿐. 알파/수익률 기간 불일치는 이
                          모듈이 다루는 "종목 데이터"가 아니라 리포트 계산값이라
                          services/nav_service.py 쪽에서 별도로 막는다(tests/test_nav_report.py).
  4) 신선도(freshness) — 이 파일에는 없다. clients/market_data_client.check_data_freshness()가
                          raw_market_data(글로벌 시장)에 대해 별도로 수행 — data_guard와
                          이름만 다를 뿐 같은 계열 가드다. 종목별 KIS/DART 데이터에는
                          신선도 가드가 없음 — 장 마감 후 오래된 캐시가 섞여도 못 잡는다
                          (알려진 빈틈, 발생 빈도 낮아 후순위로 남김).
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
    # 절대금액(억원) — 2026-08 감사에서 발견된 빈틈: 비율 지표만 검사하고
    # 있었고, DART 파싱 자릿수 오류 같은 절대금액 스케일 오류는 무방비였다.
    # 상한은 삼성전자 연매출(~300조=3,000,000억) 대비 여유를 둔 값.
    "revenue_억":      (-500_000, 5_000_000),
    "op_income_억":    (-500_000, 5_000_000),
    "net_income_억":   (-500_000, 5_000_000),
    "q_revenue_억":    (-500_000, 5_000_000),
    "q_op_income_억":  (-500_000, 5_000_000),
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
