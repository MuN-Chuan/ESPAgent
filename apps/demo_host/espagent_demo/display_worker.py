from __future__ import annotations

import logging
import os
import threading
import time
from typing import Callable, Protocol

from espagent_demo.state import DemoState

logger = logging.getLogger(__name__)


class Renderer(Protocol):
    def render(self, state: DemoState) -> object:
        ...


class Streamer(Protocol):
    def render_and_send(self, renderer: Renderer, state: DemoState) -> object:
        ...


class DisplayStreamWorker:
    def __init__(
        self,
        renderer: Renderer,
        streamer: Streamer,
        state: DemoState,
        *,
        interval_seconds: float,
        indexed_interval_seconds: float | None = None,
        clock: Callable[[], float] = time.perf_counter,
        sleep: Callable[[float], object] = time.sleep,
    ) -> None:
        self.renderer = renderer
        self.streamer = streamer
        self.state = state
        self.interval_seconds = interval_seconds
        self.indexed_interval_seconds = indexed_interval_seconds
        self.clock = clock
        self.sleep = sleep
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="esp32-display-stream", daemon=True)
        self._thread.start()

    def wake(self) -> None:
        self._wake.set()

    def close(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def _run(self) -> None:
        _raise_current_thread_priority()
        next_frame_at = self.clock()
        while not self._stop.is_set():
            self._wait_until(next_frame_at)
            self._wake.clear()
            if self._stop.is_set():
                break

            started = self.clock()
            frame_state = self.state.snapshot()
            transport_mode = frame_state.link.scene_transport_override or frame_state.link.transport_mode
            try:
                transport_setter = getattr(self.streamer, "set_transport_options", None)
                if callable(transport_setter) and getattr(self.streamer, "transport_mode", transport_mode) != transport_mode:
                    transport_setter(transport_mode=transport_mode)
                should_render = getattr(self.renderer, "should_render", None)
                if callable(should_render) and not should_render(frame_state):
                    next_frame_at = max(started + self._interval_for(transport_mode), self.clock())
                    continue
                self.streamer.render_and_send(self.renderer, frame_state)
                with self.state.locked():
                    self.state.link.frame_id = frame_state.link.frame_id
                    self.state.link.dirty_tiles = frame_state.link.dirty_tiles
                    self.state.link.fps = frame_state.link.fps
                    self.state.link.transport_epoch = getattr(
                        self.streamer,
                        "transport_epoch",
                        self.state.link.transport_epoch,
                    )
            except Exception as exc:
                with self.state.locked():
                    self.state.link.error = f"Render loop failed: {exc.__class__.__name__}"
                logger.exception("Display stream worker frame failed")

            elapsed = self.clock() - started
            interval_seconds = self._interval_for(transport_mode)
            next_frame_at = max(started + interval_seconds, self.clock())
            if elapsed >= interval_seconds:
                next_frame_at = self.clock()

    def _wait_until(self, deadline: float) -> None:
        while not self._stop.is_set() and not self._wake.is_set():
            remaining = deadline - self.clock()
            if remaining <= 0:
                return
            self.sleep(min(remaining, 0.01))

    def _interval_for(self, transport_mode: str) -> float:
        if transport_mode == "indexed" and self.indexed_interval_seconds is not None:
            return self.indexed_interval_seconds
        return self.interval_seconds


def _raise_current_thread_priority() -> None:
    if os.name != "nt":
        return
    try:
        import ctypes

        THREAD_PRIORITY_HIGHEST = 2
        handle = ctypes.windll.kernel32.GetCurrentThread()
        ctypes.windll.kernel32.SetThreadPriority(handle, THREAD_PRIORITY_HIGHEST)
    except Exception:
        logger.debug("Failed to raise display worker thread priority", exc_info=True)
