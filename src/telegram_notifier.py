"""
telegram_notifier.py — Minimal Telegram push notifications for the trading bot
================================================================================
Sends startup/shutdown, trade, and error notifications to a Telegram chat via
the Bot API's sendMessage endpoint. Uses only the standard library (no extra
dependency). Every send is wrapped so a Telegram outage can NEVER take down
the trading loop — failures are logged locally and swallowed.
"""

from __future__ import annotations

import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


class TelegramNotifier:
    API_ROOT = "https://api.telegram.org"

    def __init__(
        self,
        bot_token: str | None,
        chat_id: str | None,
        logger: logging.Logger,
        enabled: bool = True,
        min_seconds_between_errors: int = 300,
    ) -> None:
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.logger = logger
        self.min_seconds_between_errors = min_seconds_between_errors
        self._last_error_sent_at: dict[str, float] = {}

        self.enabled = enabled and bool(bot_token) and bool(chat_id)
        if enabled and not self.enabled:
            self.logger.warning(
                "Telegram notifications requested but bot_token/chat_id "
                "missing — notifications disabled."
            )

    def _send(self, text: str, parse_mode: str = "Markdown") -> bool:
        if not self.enabled:
            return False
        url = f"{self.API_ROOT}/bot{self.bot_token}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": True,
        }
        data = urllib.parse.urlencode(payload).encode("utf-8")
        try:
            req = urllib.request.Request(url, data=data, method="POST")
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status != 200:
                    self.logger.warning("Telegram sendMessage HTTP %s: %s", resp.status, resp.read())
                    return False
                return True
        except urllib.error.HTTPError as exc:
            try:
                raw = exc.read()
                detail = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
            except:
                detail = str(exc)
            self.logger.warning("Telegram sendMessage HTTP %s: %s", exc.code, detail)
            return False
        except (urllib.error.URLError, TimeoutError) as exc:
            self.logger.warning("Telegram notification failed (network): %s", exc)
            return False
        except Exception as exc:  # noqa: BLE001 — notifications must never raise
            self.logger.warning("Unexpected error sending Telegram notification: %s", exc)
            return False

    def send_message(self, text: str) -> bool:
        return self._send(text)

    def poll_approvals(self, pending_path) -> bool:
        """
        Poll Telegram getUpdates for /approve or /reject commands for scanner rotations.
        Writes status to pending_path JSON if found.
        Requires bot_token/chat_id.
        """
        if not self.enabled:
            return False
        try:
            url = f"{self.API_ROOT}/bot{self.bot_token}/getUpdates?timeout=5"
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw = resp.read().decode("utf-8")
                import json
                data = json.loads(raw)
                results = data.get("result", [])
                for upd in reversed(results[-10:]):  # check last 10
                    msg = upd.get("message", {})
                    if str(msg.get("chat", {}).get("id")) != str(self.chat_id):
                        continue
                    text = (msg.get("text") or "").strip()
                    lower = text.lower()
                    if lower.startswith("/approve"):
                        parts = text.split()
                        sym = parts[1].upper() if len(parts) > 1 else ""
                        # update pending file to approved
                        from pathlib import Path
                        p = Path(pending_path)
                        if p.exists():
                            import json as js
                            cur = js.loads(p.read_text())
                            proposal_add = cur.get("proposal", {}).get("add", {}).get("symbol", "")
                            # if sym specified, must match; else approve whatever pending
                            if not sym or sym == proposal_add:
                                cur["status"] = "approved"
                                if sym:
                                    cur["approved_symbol"] = sym
                                p.write_text(js.dumps(cur, indent=2))
                                self._send(f"✅ Approved rotation to `{sym or proposal_add}` — will apply next scan.")
                                # ack update
                                self._ack_update(upd["update_id"])
                                return True
                    elif lower.startswith("/reject"):
                        from pathlib import Path
                        p = Path(pending_path)
                        if p.exists():
                            import json as js
                            cur = js.loads(p.read_text())
                            cur["status"] = "rejected"
                            p.write_text(js.dumps(cur, indent=2))
                            self._send("❌ Rotation rejected — keeping current symbols.")
                            self._ack_update(upd["update_id"])
                            return True
                return False
        except Exception as e:
            self.logger.debug("Telegram poll_approvals failed: %s", e)
            return False

    def _ack_update(self, update_id: int):
        try:
            url = f"{self.API_ROOT}/bot{self.bot_token}/getUpdates?offset={int(update_id)+1}"
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=5) as resp:
                resp.read()
        except:
            pass

    # ── public events ────────────────────────────────────────────────────

    def startup(self, symbols: list[str], config: Any) -> None:
        text = (
            "🟢 *Bot started*\n"
            f"Mode: {'PAPER' if config.paper else 'LIVE'}"
            f"{' (dry-run)' if config.dry_run else ''}\n"
            f"Symbols: {', '.join(symbols)}\n"
            f"Notional: ${config.notional_usd:.2f}/trade"
        )
        self._send(text)

    def shutdown(self, reason: str = "normal exit") -> None:
        self._send(f"🔴 *Bot stopped* ({reason})")

    def trade_opened(self, symbol: str, price: float, reason: str) -> None:
        text = f"🟩 *BUY* {symbol} @ ${price:.2f}\n_{reason}_"
        self._send(text)

    def trade_closed(
        self, symbol: str, entry_price: float, exit_price: float, pnl: float, reason: str,
    ) -> None:
        emoji = "✅" if pnl >= 0 else "❌"
        sign = "+" if pnl >= 0 else ""
        text = (
            f"{emoji} *SELL* {symbol} @ ${exit_price:.2f}\n"
            f"Entry: ${entry_price:.2f}   P&L: {sign}{pnl:.2f}\n"
            f"_{reason}_"
        )
        self._send(text)

    def error(self, source: str, message: str) -> None:
        """Rate-limited per `source` so a crash loop can't spam every poll cycle."""
        now = time.time()
        last = self._last_error_sent_at.get(source, 0.0)
        if now - last < self.min_seconds_between_errors:
            self.logger.debug("Suppressing repeat Telegram error for %s (rate-limited)", source)
            return
        self._last_error_sent_at[source] = now
        text = f"⚠️ *Error in {source}*\n```\n{message[:500]}\n```"
        self._send(text)

    def daily_summary(self, results: dict[str, dict[str, Any]]) -> None:
        lines = ["📊 *Daily summary*"]
        for symbol, r in results.items():
            lines.append(f"{symbol}: {r['trades']} trades, P&L ${r['pnl']:+.2f}")
        self._send("\n".join(lines))