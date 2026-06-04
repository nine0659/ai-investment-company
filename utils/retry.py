"""
utils/retry.py
외부 API 호출용 지수 백오프 재시도 데코레이터

사용:
  from utils.retry import with_retry

  @with_retry(max_attempts=3, base_delay=1.0, exceptions=(requests.RequestException,))
  def fetch_data():
      ...
"""
import functools
import logging
import time

logger = logging.getLogger(__name__)


def with_retry(
    max_attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    exceptions: tuple = (Exception,),
    on_retry=None,
):
    """지수 백오프 재시도 데코레이터.

    Args:
        max_attempts: 최대 시도 횟수 (첫 시도 포함)
        base_delay:   1차 재시도 대기 초 (이후 지수적으로 증가)
        max_delay:    최대 대기 초 (이 값을 초과하지 않음)
        exceptions:   재시도할 예외 타입 튜플
        on_retry:     재시도 시 호출할 콜백 fn(attempt, exc)
    """
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            last_exc = None
            for attempt in range(1, max_attempts + 1):
                try:
                    return fn(*args, **kwargs)
                except exceptions as e:
                    last_exc = e
                    if attempt == max_attempts:
                        break
                    delay = min(base_delay * (2 ** (attempt - 1)), max_delay)
                    logger.warning(
                        "[retry] %s 실패 (시도 %d/%d) — %.1f초 후 재시도: %s",
                        fn.__name__, attempt, max_attempts, delay, e,
                    )
                    if on_retry:
                        try:
                            on_retry(attempt, e)
                        except Exception:
                            pass
                    time.sleep(delay)
            raise last_exc
        return wrapper
    return decorator
