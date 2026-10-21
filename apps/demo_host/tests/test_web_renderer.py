from __future__ import annotations

import os
import subprocess

from PIL import Image

from espagent_demo.state import DemoState
from espagent_demo.web_renderer import (
    INK,
    PAPER,
    WebDisplayRenderer,
    _hidden_process_options,
    _is_neutral_image,
    _prepare_web_frame,
    _preserve_grayscale_antialiasing,
    _quantize_duotone,
)


class FakeBrowserPage:
    def __init__(self) -> None:
        self.states: list[dict[str, object]] = []
        self.closed = False

    def render(self, state: dict[str, object]) -> Image.Image:
        self.states.append(state)
        display_ui = state.get("display_ui", {})
        size = (320, 240) if display_ui.get("orientation") == "landscape" else (240, 320)
        return Image.new("RGB", size, (120, 130, 140))

    def close(self) -> None:
        self.closed = True


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class FakeModuleScenes:
    def __init__(self) -> None:
        self.active: list[bool] = []
        self.pending = True
        self.image = Image.new("RGB", (240, 320), (12, 34, 56))

    def sync_display_scene(self, module_id: str | None, scene_key: str, width: int, height: int) -> None:
        self.active.append(module_id == "module.mirror" and scene_key == "mirror")
        self.image = Image.new("RGB", (width, height), (12, 34, 56))

    def scene_renderer(self, module_id: str | None, scene_key: str) -> str | None:
        return "frame_source" if module_id == "module.mirror" and scene_key == "mirror" else None

    def display_frame_pending(self, module_id: str | None, scene_key: str) -> bool | None:
        return self.pending

    def display_frame(self, module_id: str | None, scene_key: str) -> Image.Image:
        self.pending = False
        return self.image

    def deactivate_display_scenes(self) -> None:
        self.active.append(False)


def test_chrome_process_is_explicitly_hidden_on_windows():
    options = _hidden_process_options()

    if os.name != "nt":
        assert options == {}
        return
    assert options["creationflags"] & subprocess.CREATE_NO_WINDOW
    startup_info = options["startupinfo"]
    assert isinstance(startup_info, subprocess.STARTUPINFO)
    assert startup_info.dwFlags & subprocess.STARTF_USESHOWWINDOW
    assert startup_info.wShowWindow == subprocess.SW_HIDE


def test_web_renderer_sends_product_scene_state_to_browser():
    page = FakeBrowserPage()
    renderer = WebDisplayRenderer(page_factory=lambda: page)
    state = DemoState(active_scene="lights")
    state.display_tone.enabled = False
    state.light.on = True
    state.light.brightness = 40

    image = renderer.render(state)

    assert image.size == (240, 320)
    assert page.states[0]["active_scene"] == "lights"
    assert page.states[0]["light"]["brightness"] == 40


def test_web_renderer_rebuilds_pipeline_for_landscape_geometry():
    page = FakeBrowserPage()
    renderer = WebDisplayRenderer(page_factory=lambda: page)
    state = DemoState(active_scene="lights")
    state.display_tone.enabled = False
    renderer.render(state)

    state.display_ui.orientation = "landscape"
    image = renderer.render(state)

    assert image.size == (320, 240)
    assert page.states[-1]["display_ui"]["orientation"] == "landscape"
    assert len(renderer._pending_frames) == 1
    renderer.close()


def test_web_renderer_sends_module_scene_state_to_browser():
    page = FakeBrowserPage()
    renderer = WebDisplayRenderer(page_factory=lambda: page)
    state = DemoState(active_scene="codex")
    state.display_tone.enabled = False
    state.update_module_state("builtin.codex-usage", {"status": "ready", "primary": {"remaining_percent": 82}})

    renderer.render(state)

    assert page.states[0]["active_scene"] == "codex"
    assert page.states[0]["modules"]["builtin.codex-usage"]["primary"]["remaining_percent"] == 82


def test_web_renderer_keeps_test_patterns_on_pixel_renderer():
    page = FakeBrowserPage()
    renderer = WebDisplayRenderer(page_factory=lambda: page)
    state = DemoState(active_scene="test", test_pattern="checker")
    state.display_tone.enabled = False

    image = renderer.render(state)

    assert image.getpixel((0, 0)) == (255, 255, 255)
    assert page.states == []


def test_web_renderer_reads_module_frame_source_without_starting_browser():
    page = FakeBrowserPage()
    module_scenes = FakeModuleScenes()
    renderer = WebDisplayRenderer(module_scenes=module_scenes, page_factory=lambda: page)
    state = DemoState(active_scene="mirror", active_module_id="module.mirror")

    assert renderer.should_render(state) is True
    image = renderer.render(state)

    assert image is module_scenes.image
    assert renderer.should_render(state) is False
    assert page.states == []


def test_web_renderer_closes_persistent_browser_page():
    page = FakeBrowserPage()
    renderer = WebDisplayRenderer(page_factory=lambda: page)
    state = DemoState(active_scene="hermes")
    state.display_tone.enabled = False
    renderer.render(state)

    renderer.close()

    assert page.closed is True


def test_static_web_scene_stops_rendering_until_visible_state_changes():
    page = FakeBrowserPage()
    clock = FakeClock()
    renderer = WebDisplayRenderer(page_factory=lambda: page, clock=clock)
    state = DemoState(active_scene="lights")
    state.display_ui.motion_enabled = False

    renderer.render(state)
    clock.now = 0.5

    assert renderer.should_render(state) is False

    state.light.brightness = 40

    assert renderer.should_render(state) is True
    renderer.close()


def test_static_web_scene_sends_idle_keepalive_frame():
    page = FakeBrowserPage()
    clock = FakeClock()
    renderer = WebDisplayRenderer(page_factory=lambda: page, clock=clock)
    state = DemoState(active_scene="lights")
    state.display_ui.motion_enabled = False

    renderer.render(state)
    clock.now = 0.99

    assert renderer.should_render(state) is False

    clock.now = 1.0

    assert renderer.should_render(state) is True
    renderer.close()


def test_hermes_scene_ignores_unrelated_link_frame_counters():
    page = FakeBrowserPage()
    clock = FakeClock()
    renderer = WebDisplayRenderer(page_factory=lambda: page, clock=clock)
    state = DemoState(active_scene="hermes")
    state.display_ui.motion_enabled = False

    renderer.render(state)
    clock.now = 0.5
    state.link.frame_id += 1

    assert renderer.should_render(state) is False

    state.hermes.last_reply = "new reply"

    assert renderer.should_render(state) is True
    renderer.close()


def test_hermes_input_keeps_capturing_cursor_motion():
    page = FakeBrowserPage()
    clock = FakeClock()
    renderer = WebDisplayRenderer(page_factory=lambda: page, clock=clock)
    state = DemoState(active_scene="hermes")
    state.display_ui.motion_enabled = False
    state.hermes.input_active = True

    renderer.render(state)
    clock.now = 1.0

    assert renderer.should_render(state) is True
    renderer.close()


def test_link_scene_relies_on_display_worker_pacing():
    page = FakeBrowserPage()
    clock = FakeClock()
    renderer = WebDisplayRenderer(page_factory=lambda: page, clock=clock)
    state = DemoState(active_scene="link")
    state.display_ui.motion_enabled = False

    renderer.render(state)
    state.link.frame_id += 1
    clock.now = 0.001

    assert renderer.should_render(state) is True
    renderer.close()


def test_codex_transition_relies_on_display_worker_pacing():
    page = FakeBrowserPage()
    clock = FakeClock()
    renderer = WebDisplayRenderer(page_factory=lambda: page, clock=clock)
    state = DemoState(active_scene="codex")
    state.display_ui.motion_enabled = True

    renderer.render(state)
    clock.now = 0.001

    assert renderer.should_render(state) is True
    renderer.close()


def test_link_scene_reuses_raster_between_ten_fps_browser_captures():
    page = FakeBrowserPage()
    clock = FakeClock()
    renderer = WebDisplayRenderer(page_factory=lambda: page, clock=clock)
    state = DemoState(active_scene="link")
    state.display_ui.motion_enabled = False

    first = renderer.render(state)
    rendered_states = len(page.states)
    state.link.frame_id += 1
    clock.now = 0.04

    cached = renderer.render(state)

    assert cached is first
    assert len(page.states) == rendered_states

    clock.now = 0.1
    refreshed = renderer.render(state)

    assert refreshed is not first
    renderer.close()


def test_browser_antialiasing_is_reduced_to_exact_duotone_palette():
    source = Image.new("RGB", (3, 1))
    source.putdata([(20, 20, 20), (150, 150, 150), (245, 245, 245)])

    image = _quantize_duotone(source)

    assert [image.getpixel((x, 0)) for x in range(3)] == [INK, INK, PAPER]


def test_neutral_indexed_frame_uses_same_binary_raster_as_mjpeg():
    source = Image.new("RGB", (4, 1))
    source.putdata([(18, 24, 32), (150, 150, 150), (160, 160, 160), (254, 251, 255)])
    indexed_state = DemoState()
    indexed_state.link.transport_mode = "indexed"
    indexed_state.display_tone.enabled = False
    mjpeg_state = DemoState()
    mjpeg_state.link.transport_mode = "mjpeg"
    mjpeg_state.display_tone.enabled = False

    indexed = _prepare_web_frame(source, indexed_state)
    mjpeg = _prepare_web_frame(source, mjpeg_state)

    assert indexed.tobytes() == mjpeg.tobytes()
    assert [indexed.getpixel((x, 0)) for x in range(4)] == [INK, INK, PAPER, PAPER]


def test_colored_indexed_frame_keeps_adaptive_palette_source_colors():
    source = Image.new("RGB", (20, 20), PAPER)
    for y in range(5, 13):
        for x in range(5, 13):
            source.putpixel((x, y), (220, 40, 70))
    state = DemoState()
    state.link.transport_mode = "indexed"

    image = _prepare_web_frame(source, state)

    assert image.tobytes() == source.tobytes()
    assert _is_neutral_image(source) is False


def test_subpixel_color_fringes_still_count_as_neutral_ui():
    source = Image.new("RGB", (20, 20), PAPER)
    for y in range(2, 18):
        source.putpixel((8, y), (20, 80, 220))

    assert _is_neutral_image(source) is True


def test_grayscale_antialiasing_preserves_coverage_on_neutral_duotone_ramp():
    source = Image.new("RGB", (3, 1))
    source.putdata([INK, (132, 80, 190), PAPER])

    image = _preserve_grayscale_antialiasing(source)
    colors = [image.getpixel((x, 0)) for x in range(3)]

    assert colors[0] == INK
    assert colors[2] == PAPER
    assert colors[1] not in {INK, PAPER}
    for channel, paper_channel, ink_channel in zip(colors[1], PAPER, INK, strict=True):
        assert ink_channel < channel < paper_channel


def test_web_renderer_can_enable_grayscale_antialiasing():
    page = FakeBrowserPage()
    renderer = WebDisplayRenderer(page_factory=lambda: page)
    state = DemoState(active_scene="hermes")
    state.display_tone.enabled = False
    state.display_ui.grayscale_antialiasing = True

    image = renderer.render(state)

    assert image.getpixel((0, 0)) not in {INK, PAPER}


def test_scene_transition_uses_one_browser_until_animation_finishes():
    pages: list[FakeBrowserPage] = []
    clock = FakeClock()

    def make_page() -> FakeBrowserPage:
        page = FakeBrowserPage()
        pages.append(page)
        return page

    renderer = WebDisplayRenderer(page_factory=make_page, clock=clock)
    state = DemoState(active_scene="hermes")
    state.link.transport_mode = "indexed"
    renderer.render(state)

    state.active_scene = "lights"
    renderer.render(state)
    counts_after_switch = [len(page.states) for page in pages]
    assert len(renderer._pending_frames) == 1
    clock.now = 0.2
    renderer.render(state)

    assert len(pages[0].states) == counts_after_switch[0] + 1
    assert [len(page.states) for page in pages[1:]] == counts_after_switch[1:]
    assert len(renderer._pending_frames) == 1

    clock.now = 1.0
    renderer.render(state)

    assert len(renderer._pending_frames) == 1
    assert all(page.states[-1]["active_scene"] == "lights" for page in pages)
    assert all(
        any(rendered["display_ui"]["motion_enabled"] is False for rendered in page.states[counts_after_switch[index]:])
        for index, page in enumerate(pages[1:], start=1)
    )
    renderer.close()
