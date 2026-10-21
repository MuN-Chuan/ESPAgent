from __future__ import annotations

from pathlib import Path
from threading import Lock
from time import monotonic

from PIL import Image

from espagent_demo.renderer import HEIGHT, WIDTH


class VideoFrameSource:
    _cv2_module = None

    def __init__(self, path: Path) -> None:
        self.path = path
        self._capture = None
        self._fps = 25.0
        self._frame_count = 0
        self._started_at = monotonic()
        self._last_index: int | None = None
        self._last_size: tuple[int, int] | None = None
        self._last_frame: Image.Image | None = None
        self._lock = Lock()

    def current_frame(self, *, size: tuple[int, int] = (WIDTH, HEIGHT)) -> Image.Image:
        with self._lock:
            capture = self._open()
            if isinstance(capture, _MissingCapture):
                return capture.frame.copy()
            frame_index = self._frame_index()
            if self._last_index == frame_index and self._last_size == size and self._last_frame is not None:
                return self._last_frame.copy()
            self._advance_to_frame(capture, frame_index)
            ok, frame = capture.read()
            if not ok:
                self._started_at = monotonic()
                capture.set(self._cv2().CAP_PROP_POS_FRAMES, 0)
                self._last_index = None
                ok, frame = capture.read()
            if not ok:
                return self._fallback("VIDEO READ FAILED")
            rgb = self._cv2().cvtColor(frame, self._cv2().COLOR_BGR2RGB)
            image = Image.fromarray(rgb).resize(size, Image.Resampling.BILINEAR)
            self._last_index = frame_index
            self._last_size = size
            self._last_frame = image
            return image.copy()

    def close(self) -> None:
        if self._capture is not None:
            self._capture.release()
            self._capture = None

    def _open(self):
        if self._capture is not None:
            return self._capture
        if not self.path.exists():
            return _MissingCapture(self._fallback(f"MISSING VIDEO: {self.path.name}"))
        cv2 = self._cv2()
        capture = cv2.VideoCapture(str(self.path))
        if not capture.isOpened():
            return _MissingCapture(self._fallback("VIDEO OPEN FAILED"))
        fps = float(capture.get(cv2.CAP_PROP_FPS) or 0)
        frame_count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        self._fps = fps if fps > 0 else 25.0
        self._frame_count = max(frame_count, 1)
        self._started_at = monotonic()
        self._last_index = None
        self._last_size = None
        self._capture = capture
        return capture

    def _advance_to_frame(self, capture, frame_index: int) -> None:
        next_index = 0 if self._last_index is None else self._last_index + 1
        if frame_index < next_index:
            capture.set(self._cv2().CAP_PROP_POS_FRAMES, 0)
            next_index = 0
        while next_index < frame_index:
            if not capture.grab():
                capture.set(self._cv2().CAP_PROP_POS_FRAMES, 0)
                next_index = 0
                continue
            next_index += 1

    def _frame_index(self) -> int:
        if self._frame_count <= 0:
            return 0
        elapsed = monotonic() - self._started_at
        return int(elapsed * self._fps) % self._frame_count

    def _fallback(self, message: str) -> Image.Image:
        image = Image.new("RGB", (WIDTH, HEIGHT), (8, 13, 24))
        return image

    @classmethod
    def _cv2(cls):
        if cls._cv2_module is None:
            import cv2

            cv2.setNumThreads(1)
            cls._cv2_module = cv2
        return cls._cv2_module


class _MissingCapture:
    def __init__(self, frame: Image.Image) -> None:
        self.frame = frame

    def set(self, *_args: object) -> None:
        return None

    def read(self):
        return True, self.frame
