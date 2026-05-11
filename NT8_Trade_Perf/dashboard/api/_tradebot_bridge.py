"""Bridge to TradingBot's app/src/ modules during the dashboard merger.

Adds TradingBot/app/ to sys.path so we can `from src.signal_storage import ...`
without copying or duplicating its code. Also exports the canonical paths to
TradingBot's data/, screenshots/, prompts/.

Will be removed when the merger is complete and TradingBot's Flask app is
retired (Checkpoint 8).
"""
from __future__ import annotations

import sys
from pathlib import Path

# parents: api -> dashboard -> NT8_Trade_Perf -> Projects
TRADEBOT_APP = (Path(__file__).resolve().parents[3] / "TradingBot" / "app").resolve()

if not TRADEBOT_APP.exists():
    raise RuntimeError(
        f"TradingBot/app not found at expected path: {TRADEBOT_APP}. "
        f"The signal-analysis bridge requires the sibling project to be present."
    )

if str(TRADEBOT_APP) not in sys.path:
    sys.path.insert(0, str(TRADEBOT_APP))

SIGNALS_LOG = TRADEBOT_APP / "data" / "signals.jsonl"
SCREENSHOTS_DIR = TRADEBOT_APP / "data" / "screenshots"
PROMPT_FILE = TRADEBOT_APP / "prompts" / "analyzer.txt"
MARKET_CONTEXT_PATH = TRADEBOT_APP / "data" / "market_context.json"
