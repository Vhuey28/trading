from __future__ import annotations

import json
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import os
from models import Bar
from utils import parse_alpaca_timestamp
from datetime import datetime, timezone


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

        for attempt in range(1, 4):
            request = Request(url, data=data, method=method, headers=headers)
            try:
                with urlopen(request, timeout=30) as response:
                    raw = response.read().decode("utf-8")
                    if not raw:
                        return None
                    return json.loads(raw)
            except HTTPError as exc:
                if expected_404 and exc.code == 404:
                    return None
                detail = exc.read().decode("utf-8", errors="replace")
                if exc.code in {429, 500, 502, 503, 504} and attempt < 3:
                    sleep_for = attempt * 2
                    self.logger.warning(
                        "Alpaca returned %s; retrying in %ss. %s",
                        exc.code,
                        sleep_for,
                        detail,
                    )
                    time.sleep(sleep_for)
                    continue
                raise RuntimeError(f"Alpaca request failed with HTTP {exc.code}: {detail}") from exc
            except URLError as exc:
                if attempt < 3:
                    sleep_for = attempt * 2
                    self.logger.warning("Network error; retrying in %ss. %s", sleep_for, exc)
                    time.sleep(sleep_for)
                    continue
                raise RuntimeError(f"Network error contacting Alpaca: {exc}") from exc
        return None

    def _extract_bars(self, payload: dict[str, Any], symbol: str) -> list[Bar]:
        raw_bars = payload.get("bars", {})
        if isinstance(raw_bars, list):
            return [self._bar_from_payload(item) for item in raw_bars]
        if not isinstance(raw_bars, dict):
            return []
        candidates = [
            symbol,
            symbol.replace("/", ""),
            symbol.replace("/", "%2F"),
            symbol.upper(),
            symbol.upper().replace("/", ""),
        ]
        selected = None
        for candidate in candidates:
            if candidate in raw_bars:
                selected = raw_bars[candidate]
                break
        if selected is None and len(raw_bars) == 1:
            selected = next(iter(raw_bars.values()))
        if selected is None:
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
        url = f"{self.config.data_base_url}/v2/stocks/bars"
        payload = self.request_json("GET", url, params=params)
        if not isinstance(payload, dict):
            return []
        return self._extract_bars(payload, symbol)

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
            payload = self.request_json("GET", url, params=params)
            if not isinstance(payload, dict):
                break
            bars.extend(self._extract_bars(payload, symbol))
            page_token = payload.get("next_page_token")
            if not page_token:
                break
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

    def place_market_buy(self, symbol: str, notional_usd: float):
        body = {
            "symbol": symbol,
            "side": "buy",
            "type": "market",
            "time_in_force": "gtc",
            "notional": f"{notional_usd:.2f}",
        }
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
