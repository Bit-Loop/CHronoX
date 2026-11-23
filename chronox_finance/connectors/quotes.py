"""
60-Second TTL Official Quote Service for ChronoX Finance (Proposal A §8 + Gate 1 #53).

Strict official-only policy (ZERO unofficial endpoints — no Yahoo Finance scraping):
- Cash (`USD`): par value `price_e8 = 100_000_000` (`$1.00`).
- Crypto: Coinbase public zero-auth spot (`GET https://api.coinbase.com/v2/prices/{BASE}-USD/spot`)
  with fallback to user manual override.
- Equities / ETFs / Funds: Charles Schwab Trader API (when connected) -> Polygon.io BYOK
  (`GET https://api.polygon.io/v2/aggs/ticker/{SYMBOL}/prev`) -> user manual override.
- All prices stored as exact integer `price_e8` (`10^-8` USD) to support sub-cent crypto (SHIB)
  and fractional valuations without float drift.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import asdict, dataclass, replace
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import httpx

from chronox_finance.connectors.base import (
    SCALE_E8,
    ConnectorError,
    NormalizedHolding,
    build_http_client,
    compute_market_value_cents,
    decimal_to_e8,
    request_with_retry,
    utc_now_iso,
    validate_strict_external_url,
)
from chronox_finance.connectors.brokers import COINBASE_BASE_URL, COINBASE_HOST, SchwabConnector

logger = logging.getLogger(__name__)

POLYGON_HOST = "api.polygon.io"
POLYGON_BASE_URL = f"https://{POLYGON_HOST}"
DEFAULT_QUOTE_TTL_SECONDS = 60.0

_SYMBOL_SAFE_RE = re.compile(r"^[A-Z0-9.\-]{1,20}$")


@dataclass
class QuoteResult:
    symbol: str
    asset_class: str
    price_e8: int
    source: str  # coinbase_spot | schwab | polygon | manual | par
    fetched_at: str
    cached: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class _CacheEntry:
    quote: QuoteResult
    expires_at_mono: float


def _sanitize_symbol(symbol: str) -> str:
    cleaned = (symbol or "").strip().upper()
    if not cleaned or not _SYMBOL_SAFE_RE.match(cleaned):
        raise ValueError(f"Invalid ticker symbol: {symbol!r}")
    return cleaned


class QuoteService:
    """
    Thread-safe 60-second TTL official market quote service (`price_e8`).

    Official sources ONLY:
    - Crypto: Coinbase public spot -> manual override
    - Equities/ETFs: Schwab -> Polygon.io BYOK -> manual override
    """

    def __init__(
        self,
        *,
        ttl_seconds: float = DEFAULT_QUOTE_TTL_SECONDS,
        schwab_connector: Optional[SchwabConnector] = None,
        schwab_access_token: Optional[str] = None,
        polygon_api_key: Optional[str] = None,
        manual_overrides: Optional[Dict[str, int]] = None,
        timeout: float = 15.0,
        max_retries: int = 1,
        backoff_base: float = 0.2,
        transport: Optional[httpx.BaseTransport] = None,
        clock: Optional[Callable[[], float]] = None,
        sleep_fn: Optional[Callable[[float], None]] = None,
    ) -> None:
        self.ttl_seconds = max(1.0, float(ttl_seconds))
        self._schwab = schwab_connector
        self._schwab_access_token = (schwab_access_token or "").strip() or None
        self._polygon_api_key = (polygon_api_key or "").strip() or None
        self._manual_overrides: Dict[str, int] = {}
        if manual_overrides:
            for sym, price_e8 in manual_overrides.items():
                self._manual_overrides[_sanitize_symbol(sym)] = max(0, int(price_e8))
        self._timeout = float(timeout)
        self._max_retries = max(0, int(max_retries))
        self._backoff_base = max(0.0, float(backoff_base))
        self._transport = transport
        self._clock = clock or time.monotonic
        self._sleep_fn = sleep_fn
        self._cache: Dict[Tuple[str, str], _CacheEntry] = {}
        self._lock = threading.Lock()

    def set_manual_quote(self, symbol: str, price_e8: int, *, asset_class: str = "equity") -> QuoteResult:
        """Set or update a manual fallback/override quote in exact `price_e8`."""
        sym = _sanitize_symbol(symbol)
        val = max(0, int(price_e8))
        norm_class = (asset_class or "equity").strip().lower()
        with self._lock:
            self._manual_overrides[sym] = val
            # Invalidate any stale cached entry for this symbol across classes
            for key in [k for k in self._cache if k[0] == sym]:
                self._cache.pop(key, None)
        return QuoteResult(
            symbol=sym,
            asset_class=norm_class,
            price_e8=val,
            source="manual",
            fetched_at=utc_now_iso(),
            cached=False,
        )

    def clear_manual_quote(self, symbol: str) -> None:
        sym = _sanitize_symbol(symbol)
        with self._lock:
            self._manual_overrides.pop(sym, None)
            for key in [k for k in self._cache if k[0] == sym]:
                self._cache.pop(key, None)

    def clear_cache(self) -> None:
        with self._lock:
            self._cache.clear()

    def _get_cached(self, symbol: str, asset_class: str) -> Optional[QuoteResult]:
        now = self._clock()
        with self._lock:
            entry = self._cache.get((symbol, asset_class))
            if entry is None:
                return None
            if now >= entry.expires_at_mono:
                self._cache.pop((symbol, asset_class), None)
                return None
            return replace(entry.quote, cached=True)

    def _put_cached(self, quote: QuoteResult) -> None:
        now = self._clock()
        with self._lock:
            self._cache[(quote.symbol, quote.asset_class)] = _CacheEntry(
                quote=replace(quote, cached=False),
                expires_at_mono=now + self.ttl_seconds,
            )

    def _fetch_coinbase_spot(self, symbol: str) -> Optional[int]:
        base = symbol.split("-", 1)[0]
        url = f"{COINBASE_BASE_URL}/v2/prices/{base}-USD/spot"
        validate_strict_external_url(
            url,
            provider="coinbase_spot",
            allowed_hosts={COINBASE_HOST},
            require_https=True,
        )
        retry_kwargs: Dict[str, Any] = {
            "provider": "coinbase_spot",
            "max_retries": self._max_retries,
            "backoff_base": self._backoff_base,
        }
        if self._sleep_fn is not None:
            retry_kwargs["sleep_fn"] = self._sleep_fn

        try:
            with build_http_client(
                timeout=self._timeout,
                transport=self._transport,
                headers={"Accept": "application/json"},
            ) as client:
                resp = request_with_retry(client, "GET", url, **retry_kwargs)
            if resp.status_code != 200:
                return None
            data = resp.json()
            amt = (data.get("data") or {}).get("amount") if isinstance(data, dict) else None
            if amt is None:
                return None
            price_e8 = decimal_to_e8(amt)
            return price_e8 if price_e8 > 0 else None
        except (ConnectorError, ValueError) as exc:
            logger.debug("Coinbase spot quote unavailable for %s: %s", symbol, exc)
            return None

    def _fetch_polygon_prev_close(self, symbol: str) -> Optional[int]:
        if not self._polygon_api_key:
            return None
        url = f"{POLYGON_BASE_URL}/v2/aggs/ticker/{symbol}/prev"
        validate_strict_external_url(
            url,
            provider="polygon",
            allowed_hosts={POLYGON_HOST},
            require_https=True,
        )
        retry_kwargs: Dict[str, Any] = {
            "provider": "polygon",
            "max_retries": self._max_retries,
            "backoff_base": self._backoff_base,
            "params": {"adjusted": "true"},
        }
        if self._sleep_fn is not None:
            retry_kwargs["sleep_fn"] = self._sleep_fn

        try:
            with build_http_client(
                timeout=self._timeout,
                transport=self._transport,
                headers={
                    "Authorization": f"Bearer {self._polygon_api_key}",
                    "Accept": "application/json",
                },
            ) as client:
                resp = request_with_retry(client, "GET", url, **retry_kwargs)
            if resp.status_code != 200:
                return None
            data = resp.json()
            results = data.get("results") if isinstance(data, dict) else None
            if not results or not isinstance(results, list) or not isinstance(results[0], dict):
                return None
            close_price = results[0].get("c")
            if close_price is None:
                return None
            price_e8 = decimal_to_e8(close_price)
            return price_e8 if price_e8 > 0 else None
        except (ConnectorError, ValueError) as exc:
            logger.debug("Polygon BYOK quote unavailable for %s: %s", symbol, exc)
            return None

    def _fetch_schwab_batch(self, symbols: Sequence[str]) -> Dict[str, int]:
        if not self._schwab or not self._schwab_access_token or not symbols:
            return {}
        try:
            return self._schwab.fetch_quotes(self._schwab_access_token, symbols)
        except ConnectorError as exc:
            logger.debug("Schwab quote batch unavailable: %s", exc)
            return {}

    def get_quote(
        self,
        symbol: str,
        *,
        asset_class: str = "equity",
        force_refresh: bool = False,
    ) -> Optional[QuoteResult]:
        """Fetch a single official quote (`price_e8`) respecting the 60s TTL cache."""
        sym = _sanitize_symbol(symbol)
        norm_class = (asset_class or "equity").strip().lower()

        if norm_class == "cash" or sym == "USD":
            return QuoteResult(
                symbol=sym,
                asset_class="cash",
                price_e8=SCALE_E8,
                source="par",
                fetched_at=utc_now_iso(),
                cached=False,
            )

        if not force_refresh:
            cached = self._get_cached(sym, norm_class)
            if cached is not None:
                return cached

        price_e8: Optional[int] = None
        source = ""

        if norm_class == "crypto":
            price_e8 = self._fetch_coinbase_spot(sym)
            if price_e8 is not None:
                source = "coinbase_spot"
        else:
            # Official equity/ETF priority: Schwab -> Polygon.io BYOK -> Manual
            schwab_map = self._fetch_schwab_batch([sym])
            if sym in schwab_map and schwab_map[sym] > 0:
                price_e8 = schwab_map[sym]
                source = "schwab"
            else:
                poly_price = self._fetch_polygon_prev_close(sym)
                if poly_price is not None:
                    price_e8 = poly_price
                    source = "polygon"

        if price_e8 is None:
            with self._lock:
                manual_val = self._manual_overrides.get(sym)
            if manual_val is not None:
                price_e8 = manual_val
                source = "manual"

        if price_e8 is None:
            return None

        result = QuoteResult(
            symbol=sym,
            asset_class=norm_class,
            price_e8=price_e8,
            source=source,
            fetched_at=utc_now_iso(),
            cached=False,
        )
        self._put_cached(result)
        return result

    def get_quotes(
        self,
        items: Sequence[Tuple[str, str]],
        *,
        force_refresh: bool = False,
    ) -> Dict[str, QuoteResult]:
        """
        Batch-fetch quotes for `[(symbol, asset_class), ...]`.
        Batches uncached equity requests through Schwab in one call when Schwab is connected.
        """
        results: Dict[str, QuoteResult] = {}
        uncached_equities: List[Tuple[str, str]] = []
        uncached_other: List[Tuple[str, str]] = []

        seen: set[Tuple[str, str]] = set()
        for raw_sym, raw_cls in items:
            sym = _sanitize_symbol(raw_sym)
            norm_cls = (raw_cls or "equity").strip().lower()
            if (sym, norm_cls) in seen:
                continue
            seen.add((sym, norm_cls))

            if norm_cls == "cash" or sym == "USD":
                results[sym] = QuoteResult(
                    symbol=sym,
                    asset_class="cash",
                    price_e8=SCALE_E8,
                    source="par",
                    fetched_at=utc_now_iso(),
                    cached=False,
                )
                continue

            if not force_refresh:
                cached = self._get_cached(sym, norm_cls)
                if cached is not None:
                    results[sym] = cached
                    continue

            if norm_cls == "crypto":
                uncached_other.append((sym, norm_cls))
            else:
                uncached_equities.append((sym, norm_cls))

        # Batch Schwab quotes for all uncached equities in a single request
        schwab_prices: Dict[str, int] = {}
        if uncached_equities and self._schwab and self._schwab_access_token:
            schwab_prices = self._fetch_schwab_batch([sym for sym, _ in uncached_equities])

        for sym, norm_cls in uncached_equities:
            if sym in schwab_prices and schwab_prices[sym] > 0:
                q = QuoteResult(
                    symbol=sym,
                    asset_class=norm_cls,
                    price_e8=schwab_prices[sym],
                    source="schwab",
                    fetched_at=utc_now_iso(),
                    cached=False,
                )
                self._put_cached(q)
                results[sym] = q
                continue
            # Fallback to Polygon -> Manual
            poly = self._fetch_polygon_prev_close(sym)
            if poly is not None:
                q = QuoteResult(
                    symbol=sym,
                    asset_class=norm_cls,
                    price_e8=poly,
                    source="polygon",
                    fetched_at=utc_now_iso(),
                    cached=False,
                )
                self._put_cached(q)
                results[sym] = q
                continue
            with self._lock:
                manual_val = self._manual_overrides.get(sym)
            if manual_val is not None:
                q = QuoteResult(
                    symbol=sym,
                    asset_class=norm_cls,
                    price_e8=manual_val,
                    source="manual",
                    fetched_at=utc_now_iso(),
                    cached=False,
                )
                self._put_cached(q)
                results[sym] = q

        for sym, norm_cls in uncached_other:
            q = self.get_quote(sym, asset_class=norm_cls, force_refresh=force_refresh)
            if q is not None:
                results[sym] = q

        return results

    def refresh_holdings(
        self,
        holdings: Sequence[NormalizedHolding],
        *,
        force_refresh: bool = False,
    ) -> List[NormalizedHolding]:
        """Refresh `price_e8` & exact `market_value_cents` (`ROUND_HALF_EVEN`) on a list of holdings."""
        pairs = [(h.symbol, h.asset_class) for h in holdings if h.symbol]
        quotes = self.get_quotes(pairs, force_refresh=force_refresh)
        refreshed: List[NormalizedHolding] = []
        now_iso = utc_now_iso()
        for h in holdings:
            q = quotes.get(h.symbol.upper())
            if q is not None:
                refreshed.append(
                    replace(
                        h,
                        price_e8=q.price_e8,
                        market_value_cents=compute_market_value_cents(h.quantity_e8, q.price_e8),
                        updated_at=now_iso,
                    )
                )
            else:
                refreshed.append(h)
        return refreshed
