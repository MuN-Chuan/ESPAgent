import warnings
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from espagent_demo.protocol import TilePacket
from espagent_demo.renderer import DisplayRenderer, split_dirty_tiles
from espagent_demo.state import DemoState
from espagent_demo.video_source import VideoFrameSource
from PIL import Image, ImageDraw


def test_renderer_outputs_240_by_320_rgb_image():
    image = DisplayRenderer().render(DemoState())
    assert image.size == (240, 320)
    assert image.mode == "RGB"


def test_renderer_outputs_320_by_240_landscape_image():
    state = DemoState(active_scene="test", test_pattern="checker")
    state.display_ui.orientation = "landscape"

    image = DisplayRenderer().render(state)

    assert image.size == (320, 240)
    assert image.mode == "RGB"


def test_chatgpt_watermark_uses_only_opaque_ink_pixels() -> None:
    path = Path(__file__).resolve().parents[1] / "display_ui" / "assets" / "chatgpt-icon-dither.png"
    with Image.open(path) as source:
        watermark = source.convert("RGBA")

    pixels = list(watermark.get_flattened_data())
    visible = [pixel for pixel in pixels if pixel[3]]
    assert visible
    assert {pixel[3] for pixel in pixels} <= {0, 255}
    assert {pixel[:3] for pixel in visible} == {(17, 24, 39)}


def test_landscape_codex_number_overlays_watermark_without_erasing_its_box() -> None:
    renderer = DisplayRenderer()
    renderer._chatgpt_icon = Image.new("RGBA", (132, 132), (220, 20, 60, 255))
    state = DemoState(active_scene="codex")
    state.display_ui.orientation = "landscape"
    state.modules["builtin.codex-usage"] = {"quota": {"remaining_percent": 17}}

    image = renderer.render(state)
    overlap = image.crop((21, 60, 90, 122))

    assert (220, 20, 60) in {color for _, color in overlap.getcolors(maxcolors=16)}
    assert (17, 24, 39) in {color for _, color in overlap.getcolors(maxcolors=16)}


def test_renderer_font_renders_chinese_glyphs_instead_of_question_marks():
    renderer = DisplayRenderer()
    font_name = " ".join(renderer.font.getname())
    chinese = Image.new("L", (80, 24), 0)
    fallback = Image.new("L", (80, 24), 0)

    ImageDraw.Draw(chinese).text((0, 0), "你好", font=renderer.font, fill=255)
    ImageDraw.Draw(fallback).text((0, 0), "??", font=renderer.font, fill=255)

    assert any(name in font_name for name in ("Noto Sans CJK", "Noto Sans SC", "Microsoft YaHei", "SimHei", "SimSun", "DengXian"))
    assert chinese.tobytes() != fallback.tobytes()


def test_dirty_tile_first_frame_sends_all_tiles():
    image = DisplayRenderer().render(DemoState())
    tiles = split_dirty_tiles(image, previous=None, frame_id=1)
    assert len(tiles) == 108
    assert tiles[0].x == 0
    assert tiles[-1].w == 120


def test_dirty_tile_split_accepts_runtime_tile_size():
    image = DisplayRenderer().render(DemoState())

    tiles = split_dirty_tiles(image, previous=None, frame_id=1, tile_width=80, tile_height=8)

    assert len(tiles) == 120
    assert tiles[0].packet.w == 80
    assert tiles[0].packet.h == 8
    assert tiles[-1].packet.x == 160
    assert tiles[-1].packet.y == 312


def test_dirty_tile_split_clips_custom_edge_tiles():
    image = DisplayRenderer().render(DemoState())

    tiles = split_dirty_tiles(image, previous=None, frame_id=1, tile_width=100, tile_height=7)

    right_edge = next(tile for tile in tiles if tile.packet.x == 200 and tile.packet.y == 0)
    bottom_edge = next(tile for tile in tiles if tile.packet.x == 0 and tile.packet.y == 315)

    assert right_edge.packet.w == 40
    assert bottom_edge.packet.h == 5


def test_all_scenes_render_without_crashing():
    renderer = DisplayRenderer()

    for scene in ("hermes", "lights", "link", "codex", "video"):
        image = renderer.render(DemoState(active_scene=scene))

        assert image.size == (240, 320)
        assert image.mode == "RGB"


def test_all_product_scenes_render_in_landscape_without_crashing():
    renderer = DisplayRenderer()

    for scene in ("hermes", "lights", "link", "codex"):
        state = DemoState(active_scene=scene)
        state.display_ui.orientation = "landscape"
        image = renderer.render(state)

        assert image.size == (320, 240)
        assert image.mode == "RGB"


def test_duotone_pattern_renders_only_background_and_text_colors():
    state = DemoState(active_scene="test", test_pattern="duotone")
    state.display_tone.enabled = False
    state.duotone.background = "#FFFFFF"
    state.duotone.text_color = "#000000"
    state.duotone.text = "Aa 汉字 123"

    image = DisplayRenderer().render(state)
    colors = {color for _, color in image.getcolors(maxcolors=4)}

    assert colors == {(255, 255, 255), (0, 0, 0)}


def test_duotone_pattern_uses_custom_colors():
    state = DemoState(active_scene="test", test_pattern="duotone")
    state.display_tone.enabled = False
    state.duotone.background = "#F8FAFC"
    state.duotone.text_color = "#111827"

    image = DisplayRenderer().render(state)
    colors = {color for _, color in image.getcolors(maxcolors=4)}

    assert (248, 250, 252) in colors
    assert (17, 24, 39) in colors


def test_display_tone_boost_changes_low_contrast_output():
    disabled = DemoState(active_scene="test", test_pattern="duotone")
    disabled.display_tone.enabled = False
    disabled.duotone.background = "#777777"
    disabled.duotone.text_color = "#888888"
    disabled.duotone.text = "Aa"

    boosted = DemoState(active_scene="test", test_pattern="duotone")
    boosted.duotone.background = disabled.duotone.background
    boosted.duotone.text_color = disabled.duotone.text_color
    boosted.duotone.text = disabled.duotone.text
    boosted.display_tone.enabled = True
    boosted.display_tone.contrast_percent = 180
    boosted.display_tone.brightness_percent = 100
    boosted.display_tone.color_percent = 100
    boosted.display_tone.sharpness_percent = 100

    renderer = DisplayRenderer()

    assert renderer.render(boosted).tobytes() != renderer.render(disabled).tobytes()


def test_display_tone_binary_threshold_outputs_black_and_white_only():
    state = DemoState(active_scene="test", test_pattern="colorbars")
    state.display_tone.enabled = True
    state.display_tone.binary_threshold = 128

    image = DisplayRenderer().render(state)
    colors = {color for _, color in image.getcolors(maxcolors=4)}

    assert colors == {(255, 255, 255), (0, 0, 0)}


def test_color_pattern_contains_exact_256_color_cube_and_extended_gradients():
    state = DemoState(active_scene="test", test_pattern="colors")
    state.display_tone.enabled = False

    image = DisplayRenderer().render(state)
    cube = image.crop((0, 0, 240, 128))
    gray_steps = image.crop((0, 288, 240, 320))

    assert image.size == (240, 320)
    assert image.mode == "RGB"
    assert len(cube.getcolors(maxcolors=257)) == 256
    assert len(gray_steps.getcolors(maxcolors=17)) == 16
    assert image.getcolors(maxcolors=256) is None


def test_palette256_pattern_contains_exactly_256_colors():
    state = DemoState(active_scene="test", test_pattern="palette256")
    state.display_tone.enabled = False

    image = DisplayRenderer().render(state)

    assert len(image.getcolors(maxcolors=256) or []) == 256


def test_channel_ramp_pattern_stays_within_i8_palette_capacity():
    state = DemoState(active_scene="test", test_pattern="channels")
    state.display_tone.enabled = False

    image = DisplayRenderer().render(state)
    data = image.tobytes()
    rgb565 = {
        ((data[index] >> 3) << 11) | ((data[index + 1] >> 2) << 5) | (data[index + 2] >> 3)
        for index in range(0, len(data), 3)
    }

    assert len(rgb565) <= 256


@pytest.mark.parametrize("pattern", ["palette256", "channels", "colors"])
def test_raw_color_diagnostics_bypass_display_tone(pattern: str):
    raw = DemoState(active_scene="test", test_pattern=pattern)
    raw.display_tone.enabled = False
    tuned = DemoState(active_scene="test", test_pattern=pattern)
    tuned.display_tone.enabled = True
    tuned.display_tone.contrast_percent = 180
    tuned.display_tone.brightness_percent = 120
    tuned.display_tone.color_percent = 150
    tuned.display_tone.sharpness_percent = 200

    renderer = DisplayRenderer()

    assert renderer.render(tuned).tobytes() == renderer.render(raw).tobytes()


def test_video_scene_uses_video_frame_source():
    class FakeVideoSource:
        def current_frame(self):
            return Image.new("RGB", (240, 320), (12, 34, 56))

    state = DemoState(active_scene="video")
    state.display_tone.enabled = False

    image = DisplayRenderer(video_source=FakeVideoSource()).render(state)

    assert image.getpixel((20, 20)) == (12, 34, 56)


def test_video_source_allows_concurrent_frame_reads():
    video_path = Path(__file__).resolve().parents[3] / "test" / "video" / "fps_test.mp4"
    if not video_path.exists():
        return
    source = VideoFrameSource(video_path)
    try:
        with ThreadPoolExecutor(max_workers=4) as executor:
            frames = list(executor.map(lambda _: source.current_frame(), range(8)))
    finally:
        source.close()

    assert all(frame.size == (240, 320) and frame.mode == "RGB" for frame in frames)


def test_first_frame_tile_payload_lengths_match_dimensions():
    image = DisplayRenderer().render(DemoState())
    tiles = split_dirty_tiles(image, previous=None, frame_id=1)

    edge_tile = next(tile for tile in tiles if tile.packet.x == 120 and tile.packet.y == 0)
    bottom_tile = next(tile for tile in tiles if tile.packet.x == 0 and tile.packet.y == 318)

    assert edge_tile.packet.w == 120
    assert edge_tile.packet.raw_len == edge_tile.packet.w * edge_tile.packet.h * 2
    assert bottom_tile.packet.h == 2
    assert bottom_tile.packet.raw_len == bottom_tile.packet.w * bottom_tile.packet.h * 2


def test_tile_packets_stay_below_typical_mtu_payload_budget():
    image = DisplayRenderer().render(DemoState())
    tiles = split_dirty_tiles(image, previous=None, frame_id=1)

    assert max(len(tile.packet.encode()) for tile in tiles) <= 1472


def test_rendered_tile_packet_round_trips():
    image = DisplayRenderer().render(DemoState())
    tile = split_dirty_tiles(image, previous=None, frame_id=7)[0]

    decoded = TilePacket.decode(tile.packet.encode())

    assert decoded == tile.packet


def test_unchanged_frame_diff_returns_no_tiles():
    image = DisplayRenderer().render(DemoState())

    assert split_dirty_tiles(image, previous=image, frame_id=2) == []


def test_changed_scene_produces_dirty_tiles():
    renderer = DisplayRenderer()
    previous = renderer.render(DemoState(active_scene="hermes"))
    current = renderer.render(DemoState(active_scene="lights"))

    assert split_dirty_tiles(current, previous=previous, frame_id=2)


def test_changed_frame_dirty_tile_ids_are_dense():
    renderer = DisplayRenderer()
    previous = renderer.render(DemoState(active_scene="hermes"))
    current = renderer.render(DemoState(active_scene="lights"))

    tiles = split_dirty_tiles(current, previous=previous, frame_id=2)

    assert [tile.packet.tile_id for tile in tiles] == list(range(len(tiles)))


def test_link_scene_body_text_has_visible_pixels():
    state = DemoState(active_scene="link")
    state.display_tone.enabled = False
    image = DisplayRenderer().render(state)
    background = (248, 250, 252)
    content_pixels = image.crop((10, 45, 230, 220)).tobytes()

    assert any(tuple(content_pixels[i : i + 3]) != background for i in range(0, len(content_pixels), 3))


def test_product_scenes_use_exactly_the_paper_and_ink_palette():
    renderer = DisplayRenderer()

    for scene in ("hermes", "lights", "link"):
        state = DemoState(active_scene=scene)
        state.display_tone.enabled = False
        colors = {color for _, color in renderer.render(state).getcolors(maxcolors=4)}

        assert colors == {(248, 250, 252), (17, 24, 39)}


def test_long_hermes_reply_stays_inside_content_area():
    state = DemoState(active_scene="hermes")
    state.display_tone.enabled = False
    state.hermes.last_reply = "这是一段用于验证小屏幕自动换行和截断逻辑的超长回复。" * 20

    image = DisplayRenderer().render(state)

    footer_colors = {color for _, color in image.crop((0, 287, 240, 320)).getcolors(maxcolors=4)}
    assert (17, 24, 39) in footer_colors


def test_link_scene_reuses_short_lived_snapshot():
    timestamps = iter([1.0, 1.1, 1.4])
    renderer = DisplayRenderer(clock=lambda: next(timestamps), link_snapshot_interval=0.25)
    state = DemoState(active_scene="link")
    state.display_tone.enabled = False
    first = renderer.render(state)

    state.link.frame_id = 99
    second = renderer.render(state)
    third = renderer.render(state)

    assert second is first
    assert third is not first


def test_dirty_tile_split_does_not_emit_deprecation_warnings():
    image = DisplayRenderer().render(DemoState())

    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        split_dirty_tiles(image, previous=None, frame_id=1)
