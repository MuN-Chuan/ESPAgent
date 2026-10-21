from __future__ import annotations

import threading
import time

from PIL import Image

from espagent_demo.desktop_mirror import CaptureOutput, DesktopMirrorRuntime, fit_desktop_frame


class FakeSession:
    def __init__(self) -> None:
        self.closed = False
        self.frames = 0

    def grab(self):
        self.frames += 1
        if self.frames == 1:
            return Image.new("RGB", (160, 90), (240, 240, 240))
        return None

    def close(self) -> None:
        self.closed = True


class FakeBackend:
    name = "fake-dxgi"

    def __init__(self) -> None:
        self.session = FakeSession()

    def outputs(self):
        return [CaptureOutput("0:0", "Test display", 0, 0, 160, 90, True)]

    def open(self, output: CaptureOutput, *, cursor: bool):
        assert output.id == "0:0"
        assert cursor is False
        return self.session


def test_fit_desktop_frame_preserves_landscape_aspect_ratio_with_letterbox() -> None:
    image = fit_desktop_frame(Image.new("RGB", (1920, 1080), (255, 255, 255)))

    assert image.size == (240, 320)
    assert image.getpixel((120, 0)) == (0, 0, 0)
    assert image.getpixel((120, 92)) == (255, 255, 255)
    assert image.getpixel((120, 226)) == (255, 255, 255)
    assert image.getpixel((120, 319)) == (0, 0, 0)


def test_fit_desktop_frame_preserves_portrait_aspect_ratio_with_side_bars() -> None:
    image = fit_desktop_frame(Image.new("RGB", (1080, 1920), (255, 255, 255)))

    assert image.getpixel((0, 160)) == (0, 0, 0)
    assert image.getpixel((30, 160)) == (255, 255, 255)
    assert image.getpixel((209, 160)) == (255, 255, 255)
    assert image.getpixel((239, 160)) == (0, 0, 0)


def test_fit_desktop_frame_supports_landscape_output() -> None:
    image = fit_desktop_frame(Image.new("RGB", (1920, 1080), (255, 255, 255)), size=(320, 240))

    assert image.size == (320, 240)
    assert image.getpixel((160, 0)) == (0, 0, 0)
    assert image.getpixel((160, 30)) == (255, 255, 255)


def test_runtime_captures_only_while_scene_is_active_and_releases_session() -> None:
    published: list[dict[str, object]] = []
    backend = FakeBackend()
    runtime = DesktopMirrorRuntime(on_state=published.append, backend=backend)
    runtime.start()
    runtime.set_scene_active(True)
    runtime.invoke(
        "start",
        {
            "target_fps": 0,
            "transport_mode": "indexed",
            "indexed_bpp": 8,
            "jpeg_quality": 84,
        },
    )
    deadline = time.monotonic() + 1.0
    while runtime.snapshot()["frames_captured"] == 0 and time.monotonic() < deadline:
        threading.Event().wait(0.01)

    assert runtime.snapshot()["capture_active"] is True
    assert runtime.snapshot()["target_fps"] == 0
    assert runtime.snapshot()["transport_mode"] == "indexed"
    assert runtime.snapshot()["indexed_bpp"] == 8
    assert runtime.snapshot()["jpeg_quality"] == 84
    assert runtime.has_pending_frame() is True
    assert runtime.current_frame().size == (240, 320)
    assert runtime.has_pending_frame() is False

    runtime.set_scene_active(False)

    assert runtime.snapshot()["capture_active"] is False
    assert runtime.snapshot()["status"] == "paused"
    assert backend.session.closed is True
    runtime.close()


def test_runtime_validates_capture_limit_and_transport_options() -> None:
    runtime = DesktopMirrorRuntime(on_state=lambda _state: None, backend=FakeBackend())
    runtime.start()

    for payload in (
        {"target_fps": -1},
        {"target_fps": 241},
        {"transport_mode": "udp"},
        {"indexed_bpp": 3},
    ):
        try:
            runtime.invoke("configure", payload)
        except ValueError:
            pass
        else:
            raise AssertionError(f"expected invalid settings to fail: {payload}")

    runtime.close()
