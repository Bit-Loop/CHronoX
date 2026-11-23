"""
Polygon.io API Base Client.

Handles Bearer-header authentication (no query-string API key leaks),
thread-safe per-instance rate limiting, bounded retries on HTTP 429,
and RFC-compliant cursor pagination.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from email.utils import parsedate_to_datetime
from typing import Deque, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

import requests
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

logger = logging.getLogger(__name__)


class PolygonAPIError(Exception):
    """Custom exception for Polygon.io API errors."""


class _RateLimiter:
    """Thread-safe sliding-window rate limiter honoring per-client configuration."""

    def __init__(self, calls: int, period: float):
        self.calls = max(1, int(calls))
        self.period = max(0.01, float(period))
        self._timestamps: Deque[float] = deque()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                while self._timestamps and now - self._timestamps[0] >= self.period:
                    self._timestamps.popleft()
                if len(self._timestamps) < self.calls:
                    self._timestamps.append(now)
                    return
                sleep_for = self.period - (now - self._timestamps[0])
            if sleep_for > 0:
                time.sleep(sleep_for)


def _parse_retry_after(header_value: Optional[str], default: int = 5, max_wait: int = 120) -> int:
    """Safely parse Retry-After header (seconds or HTTP-date), clamped to [1, max_wait]."""
    if not header_value:
        return default
    raw = header_value.strip()
    try:
        return max(1, min(max_wait, int(float(raw))))
    except ValueError:
        try:
            dt = parsedate_to_datetime(raw)
            delta = int(dt.timestamp() - time.time())
            return max(1, min(max_wait, delta))
        except (TypeError, ValueError, OverflowError):
            return default


def _sanitize_params(params: Dict) -> Dict:
    """Redact any secret query parameters before logging."""
    return {
        k: ("***" if "key" in k.lower() or "token" in k.lower() or "secret" in k.lower() else v)
        for k, v in params.items()
    }


class PolygonClient:
    """Base client for Polygon.io API with retry logic and per-instance rate limiting."""

    BASE_URL = "https://api.polygon.io"

    def __init__(
        self,
        api_key: str,
        rate_limit_calls: int = 5,
        rate_limit_period: float = 1.0,
        timeout: int = 30,
        max_rate_limit_retries: int = 3,
    ):
        if not api_key or not api_key.strip():
            raise ValueError("API key is required")

        self.api_key = api_key.strip()
        self.timeout = timeout
        self.max_rate_limit_retries = max(0, max_rate_limit_retries)
        self.rate_limit_calls = rate_limit_calls
        self.rate_limit_period = rate_limit_period
        self._limiter = _RateLimiter(rate_limit_calls, rate_limit_period)

        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {self.api_key}",
                "User-Agent": "ChronoX/1.0",
                "Accept": "application/json",
            }
        )
        logger.info("Polygon.io client initialized")

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type(
            (requests.exceptions.ConnectionError, requests.exceptions.Timeout)
        ),
        reraise=True,
    )
    def _send_request(self, url: str, params: Dict) -> requests.Response:
        self._limiter.acquire()
        return self.session.get(url, params=params, timeout=self.timeout)

    def _get(self, endpoint: str, params: Optional[Dict] = None) -> Dict:
        """Make a GET request using Authorization Bearer header (never leaking apiKey in URL)."""
        req_params = {k: v for k, v in (params or {}).items() if k.lower() != "apikey"}
        url = f"{self.BASE_URL}{endpoint}"

        for attempt in range(self.max_rate_limit_retries + 1):
            logger.debug("GET %s with params=%s", endpoint, _sanitize_params(req_params))
            response = self._send_request(url, req_params)

            if response.status_code == 429:
                if attempt >= self.max_rate_limit_retries:
                    raise PolygonAPIError("Polygon API rate limit exceeded after retries")
                retry_after = _parse_retry_after(response.headers.get("Retry-After"))
                logger.warning("Rate limited (429). Sleeping %ss (attempt %s).", retry_after, attempt + 1)
                time.sleep(retry_after)
                continue

            response.raise_for_status()
            data = response.json()

            if data.get("status") == "ERROR":
                error_msg = data.get("error", "Unknown API error")
                logger.error("Polygon API error: %s", error_msg)
                raise PolygonAPIError(error_msg)

            return data

        raise PolygonAPIError("Exceeded maximum retries")

    def get_with_pagination(
        self,
        endpoint: str,
        params: Optional[Dict] = None,
        max_results: Optional[int] = None,
    ) -> List[Dict]:
        """Handle cursor-based pagination automatically using safe URL parsing."""
        all_results: List[Dict] = []
        req_params = dict(params or {})

        while True:
            data = self._get(endpoint, req_params)
            results = data.get("results", [])
            if not results:
                break

            all_results.extend(results)
            if max_results and len(all_results) >= max_results:
                return all_results[:max_results]

            next_url = data.get("next_url")
            if not next_url:
                break

            parsed_qs = parse_qs(urlparse(next_url).query)
            cursors = parsed_qs.get("cursor")
            if not cursors:
                break
            req_params["cursor"] = cursors[0]

        return all_results

    def test_connection(self) -> bool:
        """Test API connection and authentication."""
        try:
            self._get("/v3/reference/tickers", {"limit": 1})
            return True
        except Exception as exc:
            logger.error("Polygon API connection failed: %s", exc)
            return False

    def close(self) -> None:
        """Close the HTTP session."""
        self.session.close()

    def __enter__(self) -> "PolygonClient":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()
