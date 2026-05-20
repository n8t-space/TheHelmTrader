"""Parse filled-order events out of an NT8 trace file.

Each fill in the NT8 trace looks like:

    2026-05-08 06:36:20:995 (Simulation) Cbi.Account.OrderUpdateCallback: \
        realOrderState=Filled orderId='1234567890' account='Sim101' \
        name='Entry' orderState=Filled instrument='CL JUN26' orderAction=Buy \
        limitPrice=94.93 stopPrice=0 quantity=1 orderType='Limit' filled=1 \
        averageFillPrice=94.93 time='2026-05-08 06:36:22' \
        statementDate='1800-01-01' error=NoError comment='' nr=-1

This module extracts those into dicts. No persistence yet -- this is just
the parser, runnable as a CLI against one trace file to verify the fields.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Iterator


# Top-level shape: <timestamp> (<connection>) Cbi.Account.OrderUpdateCallback: <fields>
LINE_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}:\d{3}) "
    r"\((?P<connection>[^)]+)\) "
    r"Cbi\.Account\.OrderUpdateCallback: "
    r"(?P<fields>.*realOrderState=Filled.*)$"
)

# key=value where value is either a single-quoted string or an unquoted scalar
KV_RE = re.compile(r"(\w+)=(?:'([^']*)'|(\S+))")


FIELDS = (
    "ts",
    "connection",
    "orderId",
    "account",
    "name",
    "instrument",
    "orderAction",
    "orderType",
    "quantity",
    "filled",
    "averageFillPrice",
    "limitPrice",
    "stopPrice",
    "time",
)


def parse_fill_line(line: str) -> dict | None:
    m = LINE_RE.match(line)
    if not m:
        return None
    record = {"ts": m.group("ts"), "connection": m.group("connection")}
    for key, quoted, bare in KV_RE.findall(m.group("fields")):
        record[key] = quoted if quoted else bare
    return record


def parse_trace_file(path: Path) -> Iterator[dict]:
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            rec = parse_fill_line(line)
            if rec is not None:
                yield rec


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: parse_fills.py <trace_file>", file=sys.stderr)
        return 2
    path = Path(argv[1])
    if not path.exists():
        print(f"not found: {path}", file=sys.stderr)
        return 1

    count = 0
    for rec in parse_trace_file(path):
        print(" | ".join(f"{k}={rec.get(k, '')}" for k in FIELDS))
        count += 1
    print(f"\n[parsed {count} fills from {path.name}]", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
