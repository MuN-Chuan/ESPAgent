from __future__ import annotations

import numpy as np

from espagent_demo.video_source import VideoFrameSource


class FakeCapture:
    def __init__(self) -> None:
        self.position = 0
        self.set_calls: list[tuple[int, int]] = []
        self.grab_calls = 0
        self.frames = [np.full((320, 240, 3), value, dtype=np.uint8) for value in (10, 20, 30)]

    def isOpened(self) -> bool:
        return True

    def get(self, prop: int) -> float:
        return 30.0 if prop == FakeCv2.CAP_PROP_FPS else float(len(self.frames))

    def set(self, prop: int, value: int) -> bool:
        self.set_calls.append((prop, value))
        self.position = value
        return True

    def grab(self) -> bool:
        if self.position >= len(self.frames):
            return False
        self.position += 1
        self.grab_calls += 1
        return True

    def read(self):
        if self.position >= len(self.frames):
            return False, None
        frame = self.frames[self.position]
        self.position += 1
        return True, frame

    def release(self) -> None:
        return None


class FakeCv2:
    CAP_PROP_FPS = 1
    CAP_PROP_FRAME_COUNT = 2
    CAP_PROP_POS_FRAMES = 3
    COLOR_BGR2RGB = 4

    def __init__(self, capture: FakeCapture) -> None:
        self.capture = capture

    def VideoCapture(self, _path: str) -> FakeCapture:
        return self.capture

    def cvtColor(self, frame: np.ndarray, _mode: int) -> np.ndarray:
        return frame


def test_video_source_decodes_forward_without_seeking_each_frame(tmp_path, monkeypatch) -> None:
    path = tmp_path / "video.mp4"
    path.touch()
    capture = FakeCapture()
    fake_cv2 = FakeCv2(capture)
    times = iter((0.0, 0.0, 0.0, 0.04, 0.075, 0.11))
    monkeypatch.setattr("espagent_demo.video_source.monotonic", lambda: next(times))
    monkeypatch.setattr(VideoFrameSource, "_cv2_module", fake_cv2)
    source = VideoFrameSource(path)

    source.current_frame()
    source.current_frame()
    source.current_frame()

    assert capture.set_calls == []
    assert capture.grab_calls == 0

    source.current_frame()

    assert capture.set_calls == [(FakeCv2.CAP_PROP_POS_FRAMES, 0)]


def test_video_source_grabs_skipped_frames_instead_of_seeking(tmp_path, monkeypatch) -> None:
    path = tmp_path / "video.mp4"
    path.touch()
    capture = FakeCapture()
    fake_cv2 = FakeCv2(capture)
    times = iter((0.0, 0.0, 0.0, 0.075))
    monkeypatch.setattr("espagent_demo.video_source.monotonic", lambda: next(times))
    monkeypatch.setattr(VideoFrameSource, "_cv2_module", fake_cv2)
    source = VideoFrameSource(path)

    source.current_frame()
    source.current_frame()

    assert capture.set_calls == []
    assert capture.grab_calls == 1
