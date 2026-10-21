from __future__ import annotations

import threading

import pytest

from espagent_demo.display_worker import DisplayStreamWorker
from espagent_demo.state import DemoState


class BlockingStreamer:
    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.transport_epoch = 43

    def render_and_send(self, renderer: object, state: DemoState) -> None:
        self.entered.set()
        self.release.wait(timeout=1.0)
        state.link.frame_id = 42
        state.link.dirty_tiles = 7
        state.link.fps = 29.5


def test_worker_does_not_hold_state_lock_while_streaming() -> None:
    state = DemoState()
    streamer = BlockingStreamer()
    worker = DisplayStreamWorker(object(), streamer, state, interval_seconds=60.0)
    worker.start()
    try:
        assert streamer.entered.wait(timeout=1.0)
        assert state._lock.acquire(timeout=0.2)
        state._lock.release()

        streamer.release.set()
        worker.wake()
        for _ in range(100):
            with state.locked():
                if state.link.frame_id == 42:
                    break
            threading.Event().wait(0.01)

        assert state.link.frame_id == 42
        assert state.link.dirty_tiles == 7
        assert state.link.fps == 29.5
        assert state.link.transport_epoch == 43
    finally:
        streamer.release.set()
        worker.close()


def test_worker_wait_uses_short_precise_sleep_slices() -> None:
    now = [0.0]
    sleeps: list[float] = []

    def clock() -> float:
        return now[0]

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now[0] += seconds

    worker = DisplayStreamWorker(object(), BlockingStreamer(), DemoState(), interval_seconds=1.0, clock=clock, sleep=sleep)

    worker._wait_until(0.025)

    assert sleeps == pytest.approx([0.01, 0.01, 0.005])
