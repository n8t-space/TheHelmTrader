"""Guard: NT8 sometimes reports a garbage futures expiry (e.g. Expiry=199211
on a rolled MCL contract), which used to render "MCL NOV92". Implausible
years fall back to the bare master symbol; valid near-dated expiries render."""
from __future__ import annotations

from datetime import datetime, timezone

import recorder

YR = datetime.now(timezone.utc).year % 100  # 2-digit current year


def test_bogus_1992_expiry_falls_back_to_blank():
    assert recorder.expiry_to_contract(199211) == ""      # YYYYMM
    assert recorder.expiry_to_contract(19921120) == ""    # YYYYMMDD


def test_missing_or_short_expiry_is_blank():
    assert recorder.expiry_to_contract(None) == ""
    assert recorder.expiry_to_contract(0) == ""
    assert recorder.expiry_to_contract(2026) == ""        # too short


def test_valid_near_dated_expiry_renders():
    yr = datetime.now(timezone.utc).year
    assert recorder.expiry_to_contract(yr * 100 + 8) == f"AUG{yr % 100:02d}"
    # 8-digit YYYYMMDD form renders the same month/year
    assert recorder.expiry_to_contract(yr * 10000 + 820) == f"AUG{yr % 100:02d}"


def test_invalid_month_is_blank():
    yr = datetime.now(timezone.utc).year
    assert recorder.expiry_to_contract(yr * 100 + 13) == ""
