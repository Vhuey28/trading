from __future__ import annotations

import json
import time
import uuid
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import os
from dotenv import load_dotenv
from src.models import Bar
from src.utils import parse_alpaca_timestamp
from datetime import datetime, timezone


# Load .env file automatically
load_dotenv()


class AlpacaClient:
    def __init__(self, config, logger) -> None:
        self.config = config
        self.logger = logger
        self.key_id = os.getenv("ALPACA_API_KEY") or os.getenv("APCA_API_KEY_ID")
        self.secret_key = os.getenv("ALPACA_SECRET_KEY") or os.getenv("APCA_API_SECRET_KEY")

    def require_credentials(self) -> None:
        if not self.key_id or not self.secret_key:
            raise RuntimeError(
                "Missing Alpaca credentials. Add ALPACA_API_KEY and ALPACA_SECRET_KEY to .env."
            )

    def request_json(
        self,
        method: str,
        url: str,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        expected_404: bool = False,
    ) -> dict[str, Any] | list[Any] | None:
        self.require_credentials()
        if params:
            url = f"{url}?{urlencode(params)}"
        data = None
        headers = {
            "APCA-API-KEY-ID": self.key_id or "",
            "APCA-API-SECRET-KEY": self.secret_key or "",
            "Accept": "application/json",
        }
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"

        self.logger.debug("Alpaca API request: %s %s", method, url)

        # Only non-idempotent writes (order POSTs / DELETE closes) are unsafe to
        # auto-retry: if Alpaca applied the first attempt but the response was
        # lost, retrying submits a second identical order. So we retry solely on
        # safe, repeatable reads (GET/HEAD). Order placement relies on the
        # broker-side client_order_id for idempotency instead of telemetry retry.
        idempotent = method in {"GET", "HEAD"}

        for attempt in range(1, 4):
            request = Request(url, data=data, method=method, headers=headers)
            try:
                with urlopen(request, timeout=30) as response:
                    raw = response.read().decode("utf-8")
                    self.logger.debug("Alpaca API response (%s): %s", response.status, raw[:500] if raw else "<empty>")
                    if not raw:
                        return None
                    return json.loads(raw)
            except HTTPError as exc:
                if expected_404 and exc.code == 404:
                    return None
                detail = exc.read().decode("utf-8", errors="replace")
                self.logger.warning(
                    "Alpaca API error: %s %s -> HTTP %s: %s",
                    method,
                    url,
                    exc.code,
                    detail,
                )
                if idempotent and exc.code in {429, 500, 502, 503, 504} and attempt < 3:
                    sleep_for = attempt * 2
                    self.logger.warning(
                        "Alpaca returned %s; retrying in %ss (attempt %d/3)",
                        exc.code,
                        sleep_for,
                        attempt,
                    )
                    time.sleep(sleep_for)
                    continue
                raise RuntimeError(f"Alpaca request failed with HTTP {exc.code}: {detail}") from exc
            except URLError as exc:
                self.logger.warning(
                    "Network error on %s %s: %s",
                    method,
                    url,
                    exc,
                )
                if idempotent and attempt < 3:
                    sleep_for = attempt * 2
                    self.logger.warning("Retrying in %ss (attempt %d/3)", sleep_for, attempt)
                    time.sleep(sleep_for)
                    continue
                raise RuntimeError(f"Network error contacting Alpaca: {exc}") from exc
        return None

    def _extract_bars(self, payload: dict[str, Any], symbol: str) -> list[Bar]:
        raw_bars = payload.get("bars", {})
        if isinstance(raw_bars, list):
            return [self._bar_from_payload(item) for item in raw_bars]
        if not isinstance(raw_bars, dict):
            self.logger.debug("[%s] Unexpected bars format: %s", symbol, type(raw_bars))
            return []
        candidates = [
            symbol,
            symbol.replace("/", ""),
            symbol.replace("/", "%2F"),
            symbol.upper(),
            symbol.upper().replace("/", ""),
        ]
        self.logger.debug("[%s] Available bar keys in response: %s", symbol, list(raw_bars.keys()))
        selected = None
        for candidate in candidates:
            if candidate in raw_bars:
                selected = raw_bars[candidate]
                break
        if selected is None and len(raw_bars) == 1:
            selected = next(iter(raw_bars.values()))
        if selected is None:
            self.logger.debug("[%s] No matching bars found for candidates: %s", symbol, candidates)
            return []
        if isinstance(selected, dict):
            return [self._bar_from_payload(selected)]
        if isinstance(selected, list):
            return [self._bar_from_payload(item) for item in selected]
        return []

    def _bar_from_payload(self, raw: dict[str, Any]) -> Bar:
        return Bar(
            timestamp=parse_alpaca_timestamp(raw["t"]),
            open=float(raw["o"]),
            high=float(raw["h"]),
            low=float(raw["l"]),
            close=float(raw["c"]),
            volume=float(raw.get("v", 0.0)),
        )

    # Lightweight wrappers used in tests
    def fetch_stock_bars(self, symbol: str, start: datetime, end: datetime, timeframe: str) -> list[Bar]:
        params = {
            "symbols": symbol,
            "timeframe": timeframe,
            "start": start.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "end": end.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "limit": 10000,
            "sort": "asc",
        }
        # include optional stock feed/adjustment params if configured
        if getattr(self.config, "stock_feed", None):
            params["feed"] = self.config.stock_feed
        if getattr(self.config, "stock_adjustment", None):
            params["adjustment"] = self.config.stock_adjustment

        # Note: v2/stocks/bars does NOT support extended_hours parameter.
        # Extended hours data is included based on feed type:
        # - 'sip' feed includes pre-market (4am-9:30am ET) and after-hours (4pm-8pm ET)
        # - 'iex' feed only includes regular hours (9:30am-4pm ET) and limited extended
        # - 'otc' feed for OTC markets
        # The extended_hours config option is used for order placement, not data retrieval.
        url = f"{self.config.data_base_url}/v2/stocks/bars"

        # Detailed logging for debugging live bars issue
        self.logger.debug(
            "[%s] Fetching %s bars from %s to %s (feed=%s)",
            symbol,
            timeframe,
            start.astimezone(timezone.utc).isoformat(),
            end.astimezone(timezone.utc).isoformat(),
            params.get("feed", "default"),
        )

        payload = self.request_json("GET", url, params=params)
        if not isinstance(payload, dict):
            self.logger.warning(
                "[%s] Unexpected response from Alpaca bars API: %s",
                symbol,
                type(payload).__name__,
            )
            return []
        bars = self._extract_bars(payload, symbol)
        self.logger.debug(
            "[%s] Fetched %d bars (requested range: %s to %s, timeframe: %s, feed: %s, extended_hours: %s)",
            symbol,
            len(bars),
            start.astimezone(timezone.utc).isoformat(),
            end.astimezone(timezone.utc).isoformat(),
            timeframe,
            params.get("feed", "default"),
            params.get("extended_hours", "false"),
        )
        return bars

    def fetch_crypto_bars(self, symbol: str, start, end, timeframe: str) -> list[Bar]:
        bars = []
        page_token = None
        while True:
            params = {
                "symbols": symbol,
                "timeframe": timeframe,
                "start": start.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
                "end": end.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
                "limit": 10000,
                "sort": "asc",
            }
            if page_token:
                params["page_token"] = page_token
            url = f"{self.config.data_base_url}/v1beta3/crypto/{self.config.crypto_location}/bars"

            self.logger.debug(
                "[%s] Fetching crypto %s bars from %s to %s",
                symbol,
                timeframe,
                start.astimezone(timezone.utc).isoformat(),
                end.astimezone(timezone.utc).isoformat(),
            )

            payload = self.request_json("GET", url, params=params)
            if not isinstance(payload, dict):
                self.logger.warning(
                    "[%s] Unexpected response from Alpaca crypto bars API: %s",
                    symbol,
                    type(payload).__name__,
                )
                break
            bars.extend(self._extract_bars(payload, symbol))
            page_token = payload.get("next_page_token")
            if not page_token:
                break
        self.logger.debug(
            "[%s] Fetched %d total crypto bars (range: %s to %s, timeframe: %s)",
            symbol,
            len(bars),
            start.astimezone(timezone.utc).isoformat(),
            end.astimezone(timezone.utc).isoformat(),
            timeframe,
        )
        return bars

    def fetch_latest_bar(self, symbol: str):
        url = f"{self.config.data_base_url}/v1beta3/crypto/{self.config.crypto_location}/latest/bars"
        payload = self.request_json("GET", url, params={"symbols": symbol})
        if not isinstance(payload, dict):
            return None
        bars = self._extract_bars(payload, symbol)
        return bars[-1] if bars else None

    def get_position(self, symbol: str):
        encoded = symbol.replace("/", "%2F")
        url = f"{self.config.trading_base_url}/v2/positions/{encoded}"
        payload = self.request_json("GET", url, expected_404=True)
        return payload if isinstance(payload, dict) else None

    def get_clock(self) -> dict[str, Any] | None:
        """Get current market clock status from Alpaca."""
        url = f"{self.config.trading_base_url}/v2/clock"
        payload = self.request_json("GET", url)
        if isinstance(payload, dict):
            self.logger.debug(
                "Market clock: is_open=%s, next_open=%s, next_close=%s, timestamp=%s",
                payload.get("is_open"),
                payload.get("next_open"),
                payload.get("next_close"),
                payload.get("timestamp"),
            )
            return payload
        self.logger.warning("Unexpected response from Alpaca clock API: %s", type(payload).__name__)
        return None

    def is_market_open(self) -> bool:
        """Check if the market is currently open (regular hours only)."""
        clock = self.get_clock()
        return bool(clock and clock.get("is_open"))

    def is_market_open_extended(self) -> bool:
        """
        Check if the market is currently open for trading including extended hours.
        Alpaca's clock API returns is_open for regular hours only. For extended hours,
        we check if current time is within the extended session (next_open to next_close).
        """
        from datetime import datetime, timezone
        clock = self.get_clock()
        if not clock:
            return False

        # If regular market is open, extended is definitely open
        if clock.get("is_open"):
            return True

        # Otherwise check if we're in extended hours (pre-market or after-hours)
        now = datetime.now(timezone.utc)
        timestamp_str = clock.get("timestamp")
        next_open_str = clock.get("next_open")
        next_close_str = clock.get("next_close")

        if not next_open_str or not next_close_str or not timestamp_str:
            self.logger.debug("Clock missing next_open/next_close/timestamp; falling back to is_open")
            return False

        try:
            # Use Alpaca's server timestamp to avoid clock skew
            server_time = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
            next_open = datetime.fromisoformat(next_open_str.replace("Z", "+00:00"))
            next_close = datetime.fromisoformat(next_close_str.replace("Z", "+00:00"))

            # When market is closed (is_open=false), next_open/next_close refer to the
            # current/next extended session. Check if server time is within that session.
            return next_open <= server_time <= next_close
        except Exception as e:
            self.logger.warning("Failed to parse clock times: %s", e)
            return False

    def place_market_buy(self, symbol: str, notional_usd: float, client_order_id: str | None = None):
        body = {
            "symbol": symbol,
            "side": "buy",
            "type": "market",
            "time_in_force": "day",
            "notional": f"{notional_usd:.2f}",
        }
        if client_order_id:
            body["client_order_id"] = client_order_id
        else:
            # Stable idempotency key: Alpaca will not execute a second order
            # bearing the same client_order_id, so a lost-but-filled response
            # cannot result in a duplicate entry.
            body["client_order_id"] = uuid.uuid4().hex
        url = f"{self.config.trading_base_url}/v2/orders"
        payload = self.request_json("POST", url, body=body)
        return payload if isinstance(payload, dict) else None

    def place_limit_buy(self, symbol: str, notional_usd: float, reference_price: float) -> None:
        # compute quantity and limit price based on config
        offset = float(self.config.limit_price_offset_pct or 0.0)
        limit_price = reference_price * (1.0 + offset)
        qty = notional_usd / reference_price
        body = {
            "symbol": symbol,
            "side": "buy",
            "type": "limit",
            "time_in_force": "day",
            "qty": f"{qty:.2f}",
            "limit_price": f"{limit_price:.2f}",
        }
        if getattr(self.config, "extended_hours", False):
            body["extended_hours"] = True
        url = f"{self.config.trading_base_url}/v2/orders"
        self.request_json("POST", url, body=body)

    def close_position(self, symbol: str):
        encoded = symbol.replace("/", "%2F")
        url = f"{self.config.trading_base_url}/v2/positions/{encoded}"
        payload = self.request_json("DELETE", url, params={"percentage": "100"})
        return payload if isinstance(payload, dict) else None
