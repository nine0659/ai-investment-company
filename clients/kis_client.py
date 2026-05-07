import logging
import requests
from datetime import datetime, timedelta
from config.settings import KIS_APP_KEY, KIS_APP_SECRET, KIS_BASE_URL

logger = logging.getLogger(__name__)


class KISClient:
    def __init__(self):
        self._token: str | None = None
        self._token_expires: datetime | None = None

    # ── 인증 ─────────────────────────────────────────────────

    def _get_token(self) -> str:
        if self._token and self._token_expires and datetime.now() < self._token_expires:
            return self._token
        r = requests.post(
            f"{KIS_BASE_URL}/oauth2/tokenP",
            json={"grant_type": "client_credentials", "appkey": KIS_APP_KEY, "appsecret": KIS_APP_SECRET},
            timeout=10,
        )
        r.raise_for_status()
        self._token = r.json()["access_token"]
        self._token_expires = datetime.now() + timedelta(hours=23)
        logger.info("KIS 토큰 갱신 완료")
        return self._token

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
            "/uapi/domestic-stock/v1/ranking/volume",
            "FHPST01710000",
            {"FID_COND_MRKT_DIV_CODE": market, "FID_COND_SCR_DIV_CODE": "20171",
             "FID_INPUT_ISCD": "0000", "FID_DIV_CLS_CODE": "0", "FID_BLNG_CLS_CODE": "0",
             "FID_TRGT_CLS_CODE": "111111111", "FID_TRGT_EXLS_CLS_CODE": "000000",
             "FID_INPUT_PRICE_1": "", "FID_INPUT_PRICE_2": "", "FID_VOL_CNT": "", "FID_INPUT_DATE_1": ""},
            top_n,
        )

    def get_amount_rank(self, market: str = "J", top_n: int = 20) -> list[dict]:
        """거래대금 순위"""
        return self._rank(
            "/uapi/domestic-stock/v1/ranking/value",
            "FHPST01740000",
            {"FID_COND_MRKT_DIV_CODE": market, "FID_COND_SCR_DIV_CODE": "20172",
             "FID_INPUT_ISCD": "0000", "FID_DIV_CLS_CODE": "0", "FID_BLNG_CLS_CODE": "0",
             "FID_TRGT_CLS_CODE": "111111111", "FID_TRGT_EXLS_CLS_CODE": "000000",
             "FID_INPUT_PRICE_1": "", "FID_INPUT_PRICE_2": "", "FID_VOL_CNT": "", "FID_INPUT_DATE_1": ""},
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
        try:
            r = requests.get(f"{KIS_BASE_URL}{path}", headers=self._headers(tr_id), params=params, timeout=10)
            r.raise_for_status()
            return r.json().get("output", [])[:top_n]
        except Exception as e:
            logger.error("KIS 순위 조회 실패 (%s): %s", tr_id, e)
            return []

    # ── 개별 종목 조회 ────────────────────────────────────────────

    def get_stock_price(self, stock_code: str) -> dict:
        """현재가·PER·PBR·EPS·BPS·시가총액 조회"""
        url = f"{KIS_BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-price"
        params = {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": stock_code}
        try:
            r = requests.get(url, headers=self._headers("FHKST01010100"), params=params, timeout=10)
            r.raise_for_status()
            o = r.json().get("output", {})

            def _int(k):   return int(float(o.get(k) or 0))
            def _float(k): return float(o.get(k) or 0)

            return {
                "price":          _int("stck_prpr"),
                "per":            _float("per"),
                "pbr":            _float("pbr"),
                "eps":            _int("eps"),
                "bps":            _int("bps"),
                "issued_shares":  _int("lstg_stqt"),
                "market_cap_억":  _int("hts_avls"),     # 시가총액 (억원)
                "52w_high":       _int("d250_hgpr"),
                "52w_low":        _int("d250_lwpr"),
                "change_pct":     _float("prdy_ctrt"),
            }
        except Exception as e:
            logger.error("KIS 주가 조회 실패 (%s): %s", stock_code, e)
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
