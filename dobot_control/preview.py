"""The original three-camera OpenCV window (top | left | right, 640x480 each, window "0").

run_control.py draws it in its own main thread. The TUI owns the main thread, so there the
window is driven by one long-lived preview thread: every HighGUI call (imshow / waitKey /
destroyWindow) for the whole process happens in that thread, which OpenCV's GTK/Qt backends
need. Use get_preview().show(source) / .hide(); closing the window with its X button hides it.
"""
import os
import sys
import threading
import time
from typing import Callable, List, Optional

import cv2
import numpy as np

WINDOW = "0"           # same window name as the original scripts
W, H = 640, 480

Source = Callable[[], List[np.ndarray]]       # returns BGR frames: top, left, right
OnClose = Callable[[Optional[str]], None]     # called with an error message or None


def display_available() -> bool:
    if os.name == "nt" or sys.platform == "darwin":
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def compose(frames: List[np.ndarray], canvas: Optional[np.ndarray] = None) -> np.ndarray:
    """Place up to three BGR frames side by side, as run_control.py does."""
    if canvas is None:
        canvas = np.zeros((H, W * 3, 3), dtype=np.uint8)
    for i, img in enumerate(frames[:3]):
        if img is None:
            continue
        if img.shape[:2] != (H, W):
            img = cv2.resize(img, (W, H))
        canvas[:, W * i:W * (i + 1)] = img
    return canvas


class PreviewWindow:
    def __init__(self, title: str = WINDOW, period_ms: int = 30):
        self._title = title
        self._period = period_ms
        self._lock = threading.Lock()
        self._source: Optional[Source] = None
        self._on_close: Optional[OnClose] = None
        self._thread: Optional[threading.Thread] = None
        self.frames_shown = 0

    @property
    def visible(self) -> bool:
        return self._source is not None

    def show(self, source: Source, on_close: Optional[OnClose] = None) -> None:
        """Show frames from `source`, replacing any current source."""
        if not display_available():
            raise RuntimeError("no graphical display (DISPLAY is not set); the camera window needs a "
                               "desktop session or ssh -X")
        with self._lock:
            self._source, self._on_close = source, on_close
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name="camera-preview", daemon=True)
                self._thread.start()

    def hide(self) -> None:
        with self._lock:
            self._source, self._on_close = None, None

    def _run(self) -> None:
        canvas = np.zeros((H, W * 3, 3), dtype=np.uint8)
        shown = False
        while True:
            with self._lock:
                src, cb = self._source, self._on_close
            if src is None:
                if shown:
                    try:
                        cv2.destroyWindow(self._title)
                        cv2.waitKey(1)
                    except Exception:
                        pass
                    shown = False
                time.sleep(0.05)
                continue
            error, closed = None, False
            try:
                compose(src(), canvas)
                cv2.imshow(self._title, canvas)
                shown = True
                self.frames_shown += 1
                cv2.waitKey(self._period)
                closed = cv2.getWindowProperty(self._title, cv2.WND_PROP_VISIBLE) < 1  # X button
            except Exception as e:
                error, closed = str(e) or type(e).__name__, True
            if closed:
                with self._lock:
                    if self._source is not src:   # replaced or hidden meanwhile: not ours to report
                        continue
                    self._source, self._on_close = None, None
                if cb:
                    try:
                        cb(error)
                    except Exception:
                        pass


_preview: Optional[PreviewWindow] = None
_preview_lock = threading.Lock()


def get_preview() -> PreviewWindow:
    """The process-wide preview window (one HighGUI thread per process)."""
    global _preview
    with _preview_lock:
        if _preview is None:
            _preview = PreviewWindow()
        return _preview
