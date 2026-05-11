"""Test bootstrap.

Adds TradingBot/app/ (parent of src/) to sys.path so test modules can
``from src.feed_store import ...`` etc. without an editable install.
"""
from __future__ import annotations

import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))
