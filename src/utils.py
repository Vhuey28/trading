from __future__ import annotations

import subprocess
from datetime import datetime, time as clock_time, timedelta, timezone, date
from typing import Any


def parse_hhmm(value: str) -> clock_time:
    hour, minute = value.split(":", 1)
    return clock_time(hour=int(hour), minute=int(minute))


def is_trade_window_open(now: datetime, start: str, end: str) -> bool:
    current = now.timetz().replace(tzinfo=None)
    start_time = parse_hhmm(start)
    end_time = parse_hhmm(end)
    if start_time <= end_time:
        return start_time <= current < end_time
    return current >= start_time or current < end_time


def previous_data_day(now: datetime) -> date:
    target = now.date() - timedelta(days=1)
    while target.weekday() >= 5:  # 5 is Saturday, 6 is Sunday
        target -= timedelta(days=1)
    return target


def parse_alpaca_timestamp(value: str) -> datetime:
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _escape_apple_script_string(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def send_macos_notification(title: str, message: str) -> None:
    script = (
        f'display notification "{_escape_apple_script_string(message)}"'
        f' with title "{_escape_apple_script_string(title)}"'
    )
    subprocess.run(["osascript", "-e", script], check=False)
