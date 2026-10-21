import numpy as np
import pytest
from PIL import Image, ImageDraw

from espagent_demo.indexed_rect import HEIGHT, WIDTH, IndexedRectEncoder, _pack_indices, _select_palette
from espagent_demo.protocol import INDEXED_CHUNK_PAYLOAD_SIZE, IndexedFrameChunkPacket, decode_tile_payload, rgb888_to_rgb565
from espagent_demo.renderer import DisplayRenderer
from espagent_demo.state import DemoState


def _reassemble(frame) -> tuple[IndexedFrameChunkPacket, bytes]:
    decoded = [IndexedFrameChunkPacket.decode(chunk.encode()) for chunk in frame.chunks]
    blob = b"".join(chunk.payload for chunk in decoded)
    return decoded[0], blob


def test_one_bpp_index_packing_preserves_wire_bit_order_and_padding():
    assert _pack_indices(bytes((1, 0, 1, 1, 0, 0, 1, 0, 1)), 1) == bytes((0b10110010, 0b10000000))


def test_two_bpp_index_packing_preserves_wire_bit_order_and_padding():
    assert _pack_indices(bytes((0, 1, 2, 3, 3)), 2) == bytes((0b00011011, 0b11000000))


def test_indexed_encoder_reuses_same_immutable_source_raster():
    image = Image.new("RGB", (WIDTH, HEIGHT), (248, 250, 252))
    encoder = IndexedRectEncoder(bpp=2)

    first = encoder.encode(image, 1)
    second = encoder.encode(image, 2)

    assert second.preview is first.preview
    assert second.chunks[0].frame_id == 2

    encoder.reset()
    refreshed = encoder.encode(image, 3)

    assert refreshed.preview is not first.preview


def test_indexed_encoder_preserves_landscape_geometry():
    image = Image.new("RGB", (320, 240), (248, 250, 252))
    frame = IndexedRectEncoder(bpp=2).encode(image, 1)
    decoded = IndexedFrameChunkPacket.decode(frame.chunks[0].encode())

    assert frame.preview.size == (320, 240)
    assert (decoded.width, decoded.height) == (320, 240)


def test_four_bpp_index_packing_preserves_wire_nibble_order():
    assert _pack_indices(bytes((0, 1, 14, 15, 8)), 4) == bytes((0x01, 0xEF, 0x80))


def test_eight_bpp_index_packing_is_one_byte_per_pixel():
    indices = bytes((0, 1, 127, 255))
    assert _pack_indices(indices, 8) == indices


def test_four_bpp_preserves_all_sixteen_equal_gray_steps():
    image = Image.new("RGB", (WIDTH, HEIGHT))
    draw = ImageDraw.Draw(image)
    for index in range(16):
        value = round(index * 255 / 15)
        draw.rectangle((index * 15, 0, index * 15 + 14, HEIGHT - 1), fill=(value, value, value))

    frame = IndexedRectEncoder(bpp=4).encode(image, 1, grayscale_antialiasing=True)

    assert len(frame.preview.getcolors(maxcolors=16) or []) == 16


def test_eight_bpp_preserves_full_256_color_cube():
    image = Image.new("RGB", (WIDTH, HEIGHT))
    draw = ImageDraw.Draw(image)
    red_levels = tuple(round(index * 255 / 7) for index in range(8))
    green_levels = tuple(round(index * 255 / 7) for index in range(8))
    blue_levels = tuple(round(index * 255 / 3) for index in range(4))
    for row in range(16):
        for column in range(16):
            blue_index = (column // 8) * 2 + (row // 8)
            draw.rectangle(
                (column * 15, row * 20, column * 15 + 14, row * 20 + 19),
                fill=(red_levels[column % 8], green_levels[row % 8], blue_levels[blue_index]),
            )

    frame = IndexedRectEncoder(bpp=8).encode(image, 1)

    assert len(frame.preview.getcolors(maxcolors=256) or []) == 256


def test_eight_bpp_color_test_avoids_regular_octree_gradient_artifacts():
    state = DemoState(active_scene="test", test_pattern="colors")
    state.display_tone.enabled = False
    source = DisplayRenderer().render(state)

    frame = IndexedRectEncoder(bpp=8).encode(source, 1)
    source_rgb = np.asarray(source, dtype=np.int16)
    preview_rgb = np.asarray(frame.preview, dtype=np.int16)
    gradient_error = np.mean(np.abs(source_rgb[200:288] - preview_rgb[200:288]))

    assert gradient_error < 9.0


def test_eight_bpp_channel_ramps_do_not_leak_between_rgb_channels():
    state = DemoState(active_scene="test", test_pattern="channels")
    state.display_tone.enabled = False
    source = DisplayRenderer().render(state)

    frame = IndexedRectEncoder(bpp=8).encode(source, 1)
    preview = np.asarray(frame.preview, dtype=np.uint8)

    assert np.max(preview[120, :, 1:]) == 0
    assert np.max(preview[200, :, (0, 2)]) == 0
    assert np.max(preview[280, :, :2]) == 0
    assert np.max(preview[40].max(axis=1) - preview[40].min(axis=1)) <= 5


def test_protected_color_slots_preserve_sparse_source_colors_exactly():
    first = (19, 53, 87)
    second = (241, 129, 37)
    image = Image.new("RGB", (WIDTH, HEIGHT))
    pixels = image.load()
    for y in range(HEIGHT):
        for x in range(WIDTH):
            pixels[x, y] = ((x * 7) & 0xFF, (y * 11) & 0xFF, ((x + y) * 13) & 0xFF)
    pixels[3, 4] = first
    pixels[5, 6] = second

    frame = IndexedRectEncoder(bpp=4, protected_colors=(first, second)).encode(image, 1)

    def aligned(color: tuple[int, int, int]) -> tuple[int, int, int]:
        value = rgb888_to_rgb565(*color)
        red = (value >> 11) & 0x1F
        green = (value >> 5) & 0x3F
        blue = value & 0x1F
        return (red << 3) | (red >> 2), (green << 2) | (green >> 4), (blue << 3) | (blue >> 2)

    assert frame.preview.getpixel((3, 4)) == aligned(first)
    assert frame.preview.getpixel((5, 6)) == aligned(second)


def test_absent_protected_color_does_not_consume_an_i8_palette_slot():
    state = DemoState(active_scene="test", test_pattern="palette256")
    state.display_tone.enabled = False
    source = DisplayRenderer().render(state)

    frame = IndexedRectEncoder(bpp=8, protected_colors=((19, 53, 87),)).encode(source, 1)

    assert len(frame.preview.getcolors(maxcolors=256) or []) == 256


def test_indexed_encoder_limits_protected_colors_to_format_capacity():
    protected = ((0, 0, 0), (255, 0, 0), (0, 255, 0), (0, 0, 255))

    encoder = IndexedRectEncoder(bpp=2, protected_colors=protected)

    assert len(encoder.protected_colors) == 4
    with pytest.raises(ValueError, match="at most 4"):
        IndexedRectEncoder(bpp=2, protected_colors=protected + ((5, 5, 5),))


def test_i8_encoder_accepts_all_256_protected_color_slots():
    protected = tuple(((index & 0x1F) << 3, ((index >> 5) & 0x07) << 5, 0) for index in range(256))

    encoder = IndexedRectEncoder(bpp=8, protected_colors=protected)

    assert len(encoder.protected_colors) == 256


def test_one_bpp_encodes_one_independent_full_frame():
    image = Image.new("RGB", (WIDTH, HEIGHT), "white")
    ImageDraw.Draw(image).rectangle((20, 20, 80, 60), fill="black")

    frame = IndexedRectEncoder(bpp=1).encode(image, 1)
    first, blob = _reassemble(frame)

    assert first.raw_len == WIDTH * HEIGHT // 8
    assert len(blob) == first.frame_len
    assert all(len(chunk.payload) <= INDEXED_CHUNK_PAYLOAD_SIZE for chunk in frame.chunks)
    assert len(decode_tile_payload(2, blob[4:], first.raw_len)) == first.raw_len


def test_unchanged_images_still_produce_independent_frames():
    image = Image.new("RGB", (WIDTH, HEIGHT), "white")
    encoder = IndexedRectEncoder(bpp=2)

    first = encoder.encode(image, 1)
    second = encoder.encode(image, 2)

    assert first.chunks
    assert second.chunks
    assert second.frame_id == 2


def test_two_bpp_frame_contains_four_color_palette_and_full_indices():
    image = Image.new("RGB", (WIDTH, HEIGHT), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 59, HEIGHT - 1), fill="black")
    draw.rectangle((60, 0, 119, HEIGHT - 1), fill="red")
    draw.rectangle((120, 0, 179, HEIGHT - 1), fill="green")

    frame = IndexedRectEncoder(bpp=2).encode(image, 7)
    first, blob = _reassemble(frame)

    assert first.bpp == 2
    assert first.raw_len == WIDTH * HEIGHT * 2 // 8
    assert len(decode_tile_payload(2, blob[8:], first.raw_len)) == first.raw_len
    assert frame.preview.getcolors(maxcolors=4) is not None
    assert (0, 0, 0) in {color for _count, color in frame.preview.getcolors(maxcolors=4) or []}
    assert (255, 255, 255) in {color for _count, color in frame.preview.getcolors(maxcolors=4) or []}


@pytest.mark.parametrize("bpp,color_count", [(4, 16), (8, 256)])
def test_high_color_indexed_frame_round_trips_full_indices(bpp: int, color_count: int):
    image = Image.new("RGB", (WIDTH, HEIGHT))
    pixels = image.load()
    for y in range(HEIGHT):
        for x in range(WIDTH):
            slot = (x + y) % color_count
            pixels[x, y] = ((slot * 47) & 0xFF, (slot * 89) & 0xFF, (slot * 137) & 0xFF)

    frame = IndexedRectEncoder(bpp=bpp).encode(image, 12)
    first, blob = _reassemble(frame)
    palette_len = color_count * 2

    assert first.bpp == bpp
    assert first.raw_len == WIDTH * HEIGHT * bpp // 8
    assert len(decode_tile_payload(2, blob[palette_len:], first.raw_len)) == first.raw_len
    assert frame.preview.getcolors(maxcolors=color_count) is not None


def test_extra_sparse_subpixel_colors_are_reassigned_to_dominant_frame_colors():
    image = Image.new("RGB", (WIDTH, HEIGHT), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((10, 10, 80, 30), fill="black")
    draw.line((10, 31, 80, 31), fill=(0, 220, 220))
    draw.line((10, 32, 80, 32), fill=(230, 210, 0))
    draw.line((10, 33, 80, 33), fill=(220, 0, 180))
    draw.line((10, 34, 80, 34), fill=(0, 170, 70))

    palette = _select_palette(image, 2)

    assert len(set(palette)) == 4
    assert (0, 0, 0) in palette
    assert (255, 255, 255) in palette


def test_large_accent_area_keeps_a_color_palette():
    image = Image.new("RGB", (WIDTH, HEIGHT), "white")
    ImageDraw.Draw(image).rectangle((0, 0, WIDTH - 1, 31), fill=(0, 120, 220))

    palette = _select_palette(image, 2)

    assert any(max(color) - min(color) >= 48 for color in palette)


def test_dynamic_palette_uses_the_most_common_source_color_not_an_average():
    image = Image.new("RGB", (WIDTH, HEIGHT), (13, 82, 143))
    ImageDraw.Draw(image).rectangle((0, 0, 20, 20), fill=(20, 88, 150))

    palette = _select_palette(image, 2)

    assert palette[0] == (8, 81, 140)
    assert (16, 89, 148) not in palette


def test_adjacent_colors_across_a_visible_edge_do_not_snap_together():
    image = Image.new("RGB", (WIDTH, HEIGHT), (100, 100, 100))
    ImageDraw.Draw(image).rectangle((WIDTH // 2, 0, WIDTH - 1, HEIGHT - 1), fill=(130, 130, 130))

    palette = _select_palette(image, 1)

    assert len(set(palette)) == 2


def test_non_adjacent_similar_colors_snap_to_the_more_common_color():
    image = Image.new("RGB", (WIDTH, HEIGHT), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((10, 10, 100, 100), fill=(100, 100, 100))
    draw.rectangle((130, 130, 160, 160), fill=(130, 130, 130))

    palette = _select_palette(image, 2)
    gray_colors = {color for color in palette if 50 < sum(color) / 3 < 200}

    assert gray_colors == {(99, 101, 99)}


def test_smallest_excess_cluster_is_reassigned_to_a_first_round_color():
    image = Image.new("RGB", (WIDTH, HEIGHT), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 59, HEIGHT - 1), fill="black")
    draw.rectangle((60, 0, 119, HEIGHT - 1), fill="red")
    draw.rectangle((120, 0, 179, HEIGHT - 1), fill="blue")
    draw.rectangle((180, 0, 219, HEIGHT - 1), fill="green")
    draw.rectangle((220, 0, WIDTH - 1, HEIGHT - 1), fill="yellow")

    frame = IndexedRectEncoder(bpp=2).encode(image, 9)
    preview_colors = {color for _count, color in frame.preview.getcolors(maxcolors=4) or []}

    assert len(preview_colors) == 4
    assert (255, 255, 0) not in preview_colors


def test_gray_antialiasing_off_produces_crisp_binary_pixels():
    image = Image.new("RGB", (WIDTH, HEIGHT), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((10, 10, 80, 30), fill=(20, 20, 20))
    draw.line((10, 31, 80, 31), fill=(120, 120, 120))
    encoder = IndexedRectEncoder(bpp=2)

    frame = encoder.encode(image, 1, grayscale_antialiasing=False)

    assert {color for _count, color in frame.preview.getcolors(maxcolors=4) or []} == {
        (16, 20, 16),
        (255, 255, 255),
    }


def test_gray_antialiasing_on_preserves_neutral_edge_levels():
    image = Image.new("RGB", (WIDTH, HEIGHT), "white")
    ImageDraw.Draw(image).line((10, 31, 80, 31), fill=(120, 120, 120))
    encoder = IndexedRectEncoder(bpp=2)

    frame = encoder.encode(image, 1, grayscale_antialiasing=True)

    colors = {color for _count, color in frame.preview.getcolors(maxcolors=4) or []}
    edge_colors = colors - {(0, 0, 0), (255, 255, 255)}
    assert edge_colors
    assert all(max(color) - min(color) <= 2 for color in edge_colors)


def test_cool_tinted_neutral_ui_uses_binary_edges_without_gray_palette_entries():
    image = Image.new("RGB", (WIDTH, HEIGHT), (254, 251, 255))
    draw = ImageDraw.Draw(image)
    draw.rectangle((10, 10, 100, 40), fill=(18, 24, 32))
    draw.line((10, 41, 100, 41), fill=(42, 48, 65))
    draw.line((10, 42, 100, 42), fill=(100, 105, 114))

    frame = IndexedRectEncoder(bpp=2).encode(image, 1, grayscale_antialiasing=False)
    colors = {color for _count, color in frame.preview.getcolors(maxcolors=4) or []}

    assert colors == {(16, 24, 33), (255, 251, 255)}


def test_cool_tinted_gray_antialiasing_uses_stable_dynamic_levels():
    image = Image.new("RGB", (WIDTH, HEIGHT), (254, 251, 255))
    draw = ImageDraw.Draw(image)
    draw.rectangle((10, 10, 100, 40), fill=(18, 24, 32))
    draw.line((10, 41, 100, 41), fill=(42, 48, 65))
    draw.line((10, 42, 100, 42), fill=(100, 105, 114))
    encoder = IndexedRectEncoder(bpp=2)

    first = encoder.encode(image, 1, grayscale_antialiasing=True)
    second = encoder.encode(image, 2, grayscale_antialiasing=True)

    assert first.preview.getcolors(maxcolors=4) == second.preview.getcolors(maxcolors=4)
    assert 2 < len(first.preview.getcolors(maxcolors=4) or []) <= 4
