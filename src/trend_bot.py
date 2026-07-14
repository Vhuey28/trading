"""Top-level compatibility shim for the refactored modules.

This module re-exports a small set of functions and classes used by tests
while the implementation lives in separate files under src/.
"""

try:
    # Preferred when package is imported (e.g. tests adjusting sys.path)
    from .models import Bar, PreviousDayLevels, TradeSignal  # type: ignore
    from .config import BotConfig  # type: ignore
    from .indicators import compute_previous_day_levels  # type: ignore
    from .strategy import build_signal  # type: ignore
    from .utils import is_trade_window_open  # type: ignore
    from .alpaca_client import AlpacaClient  # type: ignore
except Exception:
    # Fallback when running as a script: add the src directory to sys.path
    import sys
    from pathlib import Path

    _SRC_DIR = Path(__file__).resolve().parent
    if str(_SRC_DIR) not in sys.path:
        sys.path.insert(0, str(_SRC_DIR))
    from models import Bar, PreviousDayLevels, TradeSignal  # type: ignore
    from config import BotConfig  # type: ignore
    from indicators import compute_previous_day_levels  # type: ignore
    from strategy import build_signal  # type: ignore
    from utils import is_trade_window_open  # type: ignore
    from alpaca_client import AlpacaClient  # type: ignore

__all__ = [
    "Bar",
    "PreviousDayLevels",
    "TradeSignal",
    "BotConfig",
    "compute_previous_day_levels",
    "build_signal",
    "is_trade_window_open",
    "AlpacaClient",
]
