import logging
import time as _time_module
import requests
from datetime import datetime, timedelta, time
from zoneinfo import ZoneInfo
from config.settings import KIS_APP_KEY, KIS_APP_SECRET, KIS_BASE_URL

logger = logging.getLogger(__name__)

_KST = ZoneInfo("Asia/Seoul")
# KIS 순위 API는 평일 07:00~20:00 KST에만 데이터 제공
_RANK_START = time(7, 0)
_RANK_END   = time(20, 0)

# 토큰 만료 전 갱신 버퍼 (만료 15분 전 미리 갱신)
_TOKEN_BUFFER_MINUTES = 15


def _rank_api_available() -> bool:
    now = datetime.now(_KST)
    return now.weekday() < 5 and _RANK_START <= now.time() <= _RANK_END


class KISClient:
    def __init__(self):
        self._token: str | None = None
        self._token_expires: datetime | None = None

    # ── 인증 ─────────────────────────────────────────────────

    def _get_token(self) -> str:
        # 만료 15분 전 버퍼를 두고 갱신 판단
        buffer = timedelta(minutes=_TOKEN_BUFFER_MINUTES)
        if self._token and self._token_expires and datetime.now() < self._token_expires - buffer:
            return self._token

        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                if attempt > 0:
                    wait = 2 ** attempt  # 2초, 4초 지수 백오프
                    logger.info("KIS 토큰 재시도 %d/3 (%d초 대기)", attempt + 1, wait)
                    _time_module.sleep(wait)

                r = requests.post(
                    f"{KIS_BASE_URL}/oauth2/tokenP",
                    json={"grant_type": "client_credentials",
                          "appkey": KIS_APP_KEY, "appsecret": KIS_APP_SECRET},
                    timeout=15,
                )

                # rate limit(429) 또는 일시적 서버 오류(5xx)는 재시도
                if r.status_code in (429, 500, 502, 503, 504):
                    logger.warning("KIS 토큰 발급 일시 오류 (HTTP %d), 재시도", r.status_code)
                    last_exc = requests.HTTPError(f"HTTP {r.status_code}")
                    continue

                r.raise_for_status()
                data = r.json()
                self._token = data["access_token"]

                # KIS 응답의 실제 만료 시간 사용 (expires_in: 초 단위, 기본 86400=24시간)
                expires_in = int(data.get("expires_in", 86400))
                self._token_expires = datetime.now() + timedelta(seconds=expires_in)
                logger.info(
                    "KIS 토큰 갱신 완료 (유효 %dh, 만료 %s)",
                    expires_in // 3600,
                    self._token_expires.strftime("%Y-%m-%d %H:%M"),
                )
                return self._token

            except requests.HTTPError as e:
                last_exc = e
                logger.warning("KIS 토큰 발급 HTTP 오류 (시도 %d/3): %s", attempt + 1, e)
            except Exception as e:
                last_exc = e
                logger.warning("KIS 토큰 발급 실패 (시도 %d/3): %s", attempt + 1, e)

        raise RuntimeError(f"KIS 토큰 발급 3회 모두 실패: {last_exc}")

    def _headers(self, tr_id: str) -> dict:
        return {
            "content-type":  "application/json; charset=UTF-8",
            "authorization": f"Bearer {self._get_token()}",
            "appkey":        KIS_APP_KEY,
            "appsecret":     KIS_APP_SECRET,
            "tr_id":         tr_id,
            "custtype":      "P",
        }

    # ── 순위 조회 ─────────────────────────────────────────────

    def get_volume_rank(self, market: str = "J", top_n: int = 20) -> list[dict]:
        """거래량 순위 (J=KOSPI, Q=KOSDAQ)"""
        return self._rank(
            "/uapi/domestic-stock/v1/quotations/volume-rank",
            "FHPST01710000",
            {"FID_COND_MRKT_DIV_CODE": market, "FID_COND_SCR_DIV_CODE": "20171",
             "FID_INPUT_ISCD": "0000", "FID_DIV_CLS_CODE": "0", "FID_BLNG_CLS_CODE": "0",
             "FID_TRGT_CLS_CODE": "111111111", "FID_TRGT_EXLS_CLS_CODE": "000000",
             "FID_INPUT_PRICE_1": "", "FID_INPUT_PRICE_2": "", "FID_VOL_CNT": "", "FID_INPUT_DATE_1": ""},
            top_n,
        )

    def get_amount_rank(self, market: str = "J", top_n: int = 20) -> list[dict]:
        """거래대금 순위 (KOSPI: J, KOSDAQ: Q).
        KOSDAQ은 거래대금 전용 TR이 없으므로 volume-rank TR을 거래대금 정렬로 재사용.
        """
        # KOSDAQ은 TR_ID FHPST01710000 + FID_DIV_CLS_CODE=1(거래대금순)으로 대체
        if market == "Q":
            return self._rank(
                "/uapi/domestic-stock/v1/quotations/volume-rank",
                "FHPST01710000",
                {"FID_COND_MRKT_DIV_CODE": market, "FID_COND_SCR_DIV_CODE": "20171",
                 "FID_INPUT_ISCD": "0000", "FID_DIV_CLS_CODE": "1",
                 "FID_BLNG_CLS_CODE": "0",
                 "FID_TRGT_CLS_CODE": "111111111", "FID_TRGT_EXLS_CLS_CODE": "000000",
                 "FID_INPUT_PRICE_1": "", "FID_INPUT_PRICE_2": "", "FID_VOL_CNT": "",
                 "FID_INPUT_DATE_1": ""},
                top_n,
            )
        return self._rank(
            "/uapi/domestic-stock/v1/quotations/volume-rank",
            "FHPST01720000",
            {"FID_COND_MRKT_DIV_CODE": market, "FID_COND_SCR_DIV_CODE": "20172",
             "FID_INPUT_ISCD": "0000", "FID_DIV_CLS_CODE": "0", "FID_BLNG_CLS_CODE": "0",
             "FID_TRGT_CLS_CODE": "111111111", "FID_TRGT_EXLS_CLS_CODE": "000000",
             "FID_INPUT_PRICE_1": "", "FID_INPUT_PRICE_2": "", "FID_VOL_CNT": "",
             "FID_INPUT_DATE_1": "", "FID_RANK_SORT_CLS_CODE": "0"},
            top_n,
        )

    def get_fluctuation_rank(self, market: str = "J", rise: bool = True, top_n: int = 20) -> list[dict]:
        """등락률 순위"""
        return self._rank(
            "/uapi/domestic-stock/v1/ranking/fluctuation",
            "FHPST01760000",
            {"FID_COND_MRKT_DIV_CODE": market, "FID_COND_SCR_DIV_CODE": "20170",
             "FID_INPUT_ISCD": "0000", "FID_DIV_CLS_CODE": "1" if rise else "2",
             "FID_BLNG_CLS_CODE": "0", "FID_TRGT_CLS_CODE": "111111111",
             "FID_TRGT_EXLS_CLS_CODE": "000000",
             "FID_INPUT_PRICE_1": "", "FID_INPUT_PRICE_2": "", "FID_VOL_CNT": "", "FID_INPUT_DATE_1": ""},
            top_n,
        )

    def _rank(self, path: str, tr_id: str, params: dict, top_n: int) -> list[dict]:
        if not _rank_api_available():
            now = datetime.now(_KST)
            logger.debug(
                "KIS 순위 API 가용 시간 외 스킵 (%s) — 현재 %s KST (평일 07:00~20:00만 제공)",
                tr_id, now.strftime("%H:%M"),
            )
            return []
        url = f"{KIS_BASE_URL}{path}"
        try:
            r = requests.get(url, headers=self._headers(tr_id), params=params, timeout=10)
            if r.status_code == 404:
                logger.warning(
                    "KIS 순위 API 404 (%s) — 엔드포인트: %s | 응답: %s",
                    tr_id, url, r.text[:200],
                )
                return []
            r.raise_for_status()
            return r.json().get("output", [])[:top_n]
        except Exception as e:
            logger.warning("KIS 순위 조회 실패 (%s): %s", tr_id, e)
            return []

    def get_foreign_buy_rank(self, market: str = "J", top_n: int = 20) -> list[dict]:
        """외국인 순매수 상위 종목"""
        return self._rank(
            "/uapi/domestic-stock/v1/quotations/foreign-institution-total",
            "FHPTJ04400000",
            {"FID_COND_MRKT_DIV_CODE": market, "FID_COND_SCR_DIV_CODE": "20444",
             "FID_INPUT_ISCD": "0000", "FID_DIV_CLS_CODE": "0",
             "FID_RANK_SORT_CLS_CODE": "0",   # 0=외국인순매수
             "FID_ETC_CLS_CODE": "0",
             "FID_INPUT_DATE_1": "", "FID_INPUT_DATE_2": ""},
            top_n,
        )

    def get_institution_buy_rank(self, market: str = "J", top_n: int = 20) -> list[dict]:
        """기관 순매수 상위 종목"""
        return self._rank(
            "/uapi/domestic-stock/v1/quotations/foreign-institution-total",
            "FHPTJ04400000",
            {"FID_COND_MRKT_DIV_CODE": market, "FID_COND_SCR_DIV_CODE": "20444",
             "FID_INPUT_ISCD": "0000", "FID_DIV_CLS_CODE": "0",
             "FID_RANK_SORT_CLS_CODE": "1",   # 1=기관순매수
             "FID_ETC_CLS_CODE": "0",
             "FID_INPUT_DATE_1": "", "FID_INPUT_DATE_2": ""},
            top_n,
        )

    # ── 개별 종목 조회 ────────────────────────────────────────────

    def get_stock_price(self, stock_code: str, market: str | None = None) -> dict:
        """현재가·PER·PBR·EPS·BPS·시가총액 조회.

        market: "J"(KOSPI) 또는 "Q"(KOSDAQ).
        None이면 J→Q 순으로 자동 시도.
        지정 시 해당 market 먼저, 가격이 0이면 반대 market 재시도.
        """
        url = f"{KIS_BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-price"
        if market:
            markets = (market, "Q" if market == "J" else "J")
        else:
            markets = ("J", "Q")

        for m in markets:
            try:
                r = requests.get(
                    url,
                    headers=self._headers("FHKST01010100"),
                    params={"FID_COND_MRKT_DIV_CODE": m, "FID_INPUT_ISCD": stock_code},
                    timeout=10,
                )
                r.raise_for_status()
                o = r.json().get("output", {})

                def _int(k):   return int(float(o.get(k) or 0))
                def _float(k): return float(o.get(k) or 0)

                # 장 마감·개장 전에는 stck_prpr=0 → 전일 종가 fallback
                price = _int("stck_prpr") or _int("stck_prdy_clpr")
                if price == 0:
                    continue  # 빈 응답 → 다음 market 시도
                return {
                    "price":          price,
                    "per":            _float("per"),
                    "pbr":            _float("pbr"),
                    "eps":            _int("eps"),
                    "bps":            _int("bps"),
                    "issued_shares":  _int("lstg_stqt"),
                    "market_cap_억":  _int("hts_avls"),
                    "52w_high":       _int("d250_hgpr"),
                    "52w_low":        _int("d250_lwpr"),
                    "change_pct":     _float("prdy_ctrt"),
                }
            except Exception as e:
                logger.warning("KIS 주가 조회 실패 (%s/%s): %s", stock_code, m, e)
        return {}

    def get_dividend_info(self, stock_code: str) -> dict:
        """배당 정보 조회 (배당수익률)"""
        url = f"{KIS_BASE_URL}/uapi/domestic-stock/v1/finance/dividend"
        params = {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": stock_code}
        try:
            r = requests.get(url, headers=self._headers("FHKST01010600"), params=params, timeout=10)
            r.raise_for_status()
            o = r.json().get("output", {})
            return {
                "dividend_per_share": float(o.get("per_sto_divi_amt") or 0),
                "dividend_yield":     float(o.get("stck_divi") or 0),
            }
        except Exception as e:
            logger.debug("KIS 배당 조회 실패 (%s): %s", stock_code, e)
            return {}
