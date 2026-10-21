from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Protocol

from espagent_demo.state import DemoState, LightState

logger = logging.getLogger(__name__)


class LightStateReader(Protocol):
    def get_state(self) -> LightState:
        ...

    def subscribe_state_changes(
        self,
        stop_event: threading.Event,
        on_state: Callable[[LightState], object],
    ) -> None:
        ...

    def close_event_stream(self) -> None:
        ...


class HomeAssistantStateWorker:
    def __init__(
        self,
        adapter: LightStateReader,
        state: DemoState,
        *,
        reconnect_max_seconds: float = 30.0,
        on_change: Callable[[], object] | None = None,
    ) -> None:
        self.adapter = adapter
        self.state = state
        self.reconnect_max_seconds = reconnect_max_seconds
        self.on_change = on_change
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="home-assistant-state", daemon=True)
        self._thread.start()

    def wake(self) -> None:
        self._wake.set()

    def close(self) -> None:
        self._stop.set()
        self._wake.set()
        close_stream = getattr(self.adapter, "close_event_stream", None)
        if callable(close_stream):
            close_stream()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def sync_once(self) -> bool:
        next_state = self.adapter.get_state()
        return self._apply_state(next_state)

    def _apply_state(self, next_state: LightState) -> bool:
        with self.state.locked():
            changed = _visible_light_state(self.state.light) != _visible_light_state(next_state)
            self.state.light = next_state
        if changed and self.on_change is not None:
            self.on_change()
        return changed

    def _run(self) -> None:
        reconnect_delay = 1.0
        while not self._stop.is_set():
            try:
                self.sync_once()
                self.adapter.subscribe_state_changes(self._stop, self._apply_state)
                reconnect_delay = 1.0
            except Exception as exc:
                if not self._stop.is_set():
                    logger.warning("Home Assistant event stream disconnected: %s", exc.__class__.__name__)
            if self._stop.is_set():
                break
            self._wake.wait(reconnect_delay)
            self._wake.clear()
            reconnect_delay = min(self.reconnect_max_seconds, reconnect_delay * 2)


def _visible_light_state(state: LightState) -> tuple[object, ...]:
    # last_synced_at is diagnostic metadata and must not cause a display frame
    # when event metadata changes but the actual device state has not changed.
    return (
        state.entity_id,
        state.name,
        state.online,
        state.on,
        state.brightness,
        state.color_temp_kelvin,
        state.min_color_temp_kelvin,
        state.max_color_temp_kelvin,
        state.error,
    )
