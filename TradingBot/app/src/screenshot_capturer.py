"""Open the Windows Snipping overlay and capture the resulting clip."""
import logging
import subprocess
import time
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageGrab

logger = logging.getLogger(__name__)

MAX_EDGE = 1280
SNIP_TIMEOUT_SECONDS = 30
POLL_INTERVAL_SECONDS = 0.25


def capture_via_snip(screenshots_dir: Path) -> Path:
    """Trigger the Win+Shift+S overlay, wait for a NEW snip to land in the clipboard, save it.

    Returns the path the caller should send to the LLM. Raises RuntimeError if no new
    image arrives within SNIP_TIMEOUT_SECONDS (user cancelled or didn't drag).
    """
    screenshots_dir.mkdir(parents=True, exist_ok=True)

    initial_hash = _clipboard_image_hash()
    logger.info("Opening Snipping overlay (initial clipboard hash=%s)", initial_hash)
    subprocess.Popen(["explorer.exe", "ms-screenclip:"])

    clip = _wait_for_new_clip(initial_hash)
    logger.info("Snip received: %dx%d", clip.size[0], clip.size[1])

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = screenshots_dir / f"{stamp}.png"
    clip.save(path, "PNG")

    if max(clip.size) > MAX_EDGE:
        scale = MAX_EDGE / max(clip.size)
        new_size = (int(clip.size[0] * scale), int(clip.size[1] * scale))
        clip = clip.resize(new_size, Image.LANCZOS)
        path = path.with_name(path.stem + "_small.png")
        clip.save(path, "PNG", optimize=True)
        logger.info("Downscaled to %dx%d: %s", clip.size[0], clip.size[1], path)

    return path


def _grab_clipboard_safe():
    """ImageGrab.grabclipboard() can raise OSError on Windows when another process
    (typically the Snipping Tool itself) has the clipboard open. Treat that as
    "not ready yet" and let the caller retry."""
    try:
        return ImageGrab.grabclipboard()
    except OSError:
        return None


def _safe_image_hash(image):
    """tobytes() can race with the clipboard being modified mid-grab. Same treatment."""
    try:
        return hash(image.tobytes())
    except OSError:
        return None


def _wait_for_new_clip(initial_hash):
    deadline = time.time() + SNIP_TIMEOUT_SECONDS
    while time.time() < deadline:
        clip = _grab_clipboard_safe()
        if clip is not None and not isinstance(clip, list):
            current_hash = _safe_image_hash(clip)
            if current_hash is not None and current_hash != initial_hash:
                return clip
        time.sleep(POLL_INTERVAL_SECONDS)
    raise RuntimeError(
        f"No new snip detected within {SNIP_TIMEOUT_SECONDS}s. "
        "Drag a rectangle in the Snipping overlay, or press Esc to cancel."
    )


def _clipboard_image_hash():
    clip = _grab_clipboard_safe()
    if clip is None or isinstance(clip, list):
        return None
    return _safe_image_hash(clip)
