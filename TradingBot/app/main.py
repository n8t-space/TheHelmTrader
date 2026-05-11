"""TradeBot v1 — manual-trigger chart-to-trade-proposal pipeline (terminal flow).

For the web flow, launch the unified dashboard (run_dashboard.bat, which
forwards to Trade_Perf/dashboard/run_dev.ps1) and click Snip & Analyze
on the Signal Analysis page.
"""
import json
import logging
import sys
from pathlib import Path

from src.pipeline import run_pipeline
from src.signal_storage import append_update

APP = Path(__file__).parent
SCREENSHOTS = APP / "data" / "screenshots"
SIGNALS_LOG = APP / "data" / "signals.jsonl"
DIAG_LOG = APP / "data" / "tradebot.log"
PROMPT_FILE = APP / "prompts" / "analyzer.txt"


def configure_logging() -> None:
    DIAG_LOG.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
        handlers=[
            logging.FileHandler(DIAG_LOG, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


def collect_journal() -> dict:
    """Capture user's reaction to the proposal. Builds the corpus for strategy formation."""
    print("\nYour take?  [a] agree   [d] disagree   [s] skip   (Enter = skip)")
    choice = input("> ").strip().lower()
    verdict = {"a": "agree", "d": "disagree"}.get(choice, "skip")
    note = input("One-line reason (or Enter to skip): ").strip() or None
    return {"verdict": verdict, "note": note}


def main() -> int:
    configure_logging()
    log = logging.getLogger("tradebot")
    log.info("=== TradeBot run starting ===")

    print("Opening Snipping overlay — drag a rectangle around the chart...")

    prompt = PROMPT_FILE.read_text(encoding="utf-8")

    try:
        record = run_pipeline(SCREENSHOTS, SIGNALS_LOG, prompt)
    except RuntimeError as e:
        log.error("Capture failed: %s", e)
        return 1
    except Exception:
        log.exception("Pipeline failed")
        return 2

    print("\n--- PROPOSAL ---")
    print(json.dumps(record["proposal"], indent=2))

    journal = collect_journal()
    log.info("Journal: %s", journal)
    append_update(SIGNALS_LOG, record["timestamp"], journal=journal)

    log.info("=== Run complete ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
