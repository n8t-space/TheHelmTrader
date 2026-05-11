"""Test bootstrap.

Adds NT8_Trade_Perf/ to sys.path so tests can ``from dashboard.api.main
import app``. The api package's _tradebot_bridge takes care of pulling
TradingBot/app/ in for src.* imports.
"""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
