import pytest

from espagent_demo.protocol import (
    COMP_RAW,
    COMP_LZ4,
    COMP_RLE,
    HEADER_SIZE,
    JPEG_CHUNK_PAYLOAD_SIZE,
    JpegChunkPacket,
    IndexedFrameChunkPacket,
    IndexedFrameEndPacket,
    IndexedRectPacket,
    PKT_FRAME_END,
    PKT_FRAME_BEGIN,
    PKT_JPEG_CHUNK,
    PKT_INDEXED_FRAME_END,
    PKT_INDEXED_RECT,
    PIXEL_JPEG,
    PIXEL_INDEXED8,
    PIXEL_RGB565,
    PKT_STATUS,
    PKT_TILE,
    TRANSPORT_INDEXED,
    DisplayModePacket,
    FrameEndPacket,
    TilePacket,
    decode_tile_payload,
    encode_tile_payload,
    image_to_jpeg_bytes,
    rgb888_to_rgb565,
    split_jpeg_chunks,
)
from PIL import Image


def test_rgb888_to_rgb565_red_green_blue():
    assert rgb888_to_rgb565(255, 0, 0) == 0xF800
    assert rgb888_to_rgb565(0, 255, 0) == 0x07E0
    assert rgb888_to_rgb565(0, 0, 255) == 0x001F


def test_tile_packet_round_trip_header():
    payload = bytes([0x00, 0xF8] * 4)
    packet = TilePacket(
        frame_id=7,
        tile_id=2,
        x=32,
        y=0,
        w=2,
        h=2,
        payload=payload,
    )
    encoded = packet.encode()
    assert len(encoded) == HEADER_SIZE + len(payload)
    decoded = TilePacket.decode(encoded)
    assert decoded.frame_id == 7
    assert decoded.tile_id == 2
    assert decoded.x == 32
    assert decoded.type == PKT_TILE
    assert decoded.pixel_format == PIXEL_RGB565
    assert decoded.compression == COMP_RAW
    assert decoded.payload == payload


def test_frame_end_packet_uses_empty_payload_and_frame_end_type():
    encoded = FrameEndPacket(frame_id=9, tile_count=108).encode()

    assert len(encoded) == HEADER_SIZE + 2
    assert encoded[3] == PKT_FRAME_END
    assert encoded[4:8] == bytes([9, 0, 0, 0])
    assert encoded[20:22] == bytes([2, 0])
    assert encoded[22:24] == bytes([2, 0])
    assert encoded[-2:] == bytes([108, 0])


def test_display_mode_packet_encodes_unified_flush_and_nack_flags():
    encoded = DisplayModePacket(unified_flush=False, nack_enabled=False).encode()

    assert len(encoded) == HEADER_SIZE + 6
    assert encoded[3] == PKT_FRAME_BEGIN
    assert encoded[20:22] == bytes([6, 0])
    assert encoded[22:24] == bytes([6, 0])
    assert encoded[-6:] == bytes([0, 0, 0, 100, 0, 0x02])


def test_display_mode_packet_can_enable_esp_logging():
    encoded = DisplayModePacket(unified_flush=True, nack_enabled=True, esp_log_enabled=True).encode()

    assert encoded[-6:] == bytes([1, 1, 1, 100, 0, 0x02])


def test_display_mode_packet_can_set_backlight_percent():
    encoded = DisplayModePacket(unified_flush=True, nack_enabled=True, backlight_percent=35).encode()

    assert encoded[-6:] == bytes([1, 1, 0, 35, 0, 0x02])


def test_display_mode_packet_encodes_transport_and_epoch():
    encoded = DisplayModePacket(
        unified_flush=True,
        transport_mode=TRANSPORT_INDEXED,
        transport_epoch=0x12345678,
    ).encode()

    assert encoded[4:8] == bytes([0x78, 0x56, 0x34, 0x12])
    assert encoded[-2] == TRANSPORT_INDEXED
    assert encoded[-1] == 0x02


def test_display_mode_packet_can_select_standard_panel_gamma():
    encoded = DisplayModePacket(unified_flush=True, panel_gamma=0x01).encode()

    assert encoded[-1] == 0x01


def test_display_mode_packet_adds_landscape_orientation_extension():
    encoded = DisplayModePacket(unified_flush=True, orientation=1).encode()

    assert len(encoded) == HEADER_SIZE + 7
    assert encoded[-7:] == bytes([1, 1, 0, 100, 0, 0x02, 1])


def test_display_mode_packet_rejects_invalid_panel_gamma():
    with pytest.raises(ValueError, match="panel_gamma"):
        DisplayModePacket(unified_flush=True, panel_gamma=0x03).encode()


def test_display_mode_packet_rejects_invalid_backlight_percent():
    with pytest.raises(ValueError, match="backlight"):
        DisplayModePacket(unified_flush=True, backlight_percent=101).encode()


def test_jpeg_chunk_packet_round_trips_header():
    packet = JpegChunkPacket(
        frame_id=11,
        chunk_id=3,
        width=240,
        height=320,
        jpeg_len=4096,
        payload=b"jpeg",
    )

    encoded = packet.encode()
    decoded = JpegChunkPacket.decode(encoded)

    assert encoded[3] == PKT_JPEG_CHUNK
    assert decoded.frame_id == 11
    assert decoded.chunk_id == 3
    assert decoded.width == 240
    assert decoded.height == 320
    assert decoded.pixel_format == PIXEL_JPEG
    assert decoded.raw_len == 4096
    assert decoded.payload == b"jpeg"


def test_indexed_rect_packet_round_trips_lz4_payload():
    packet = IndexedRectPacket(
        frame_id=12,
        rect_id=3,
        x=8,
        y=16,
        w=8,
        h=2,
        bpp=2,
        palette=(0x0000, 0xFFFF, 0xF800, 0x07E0),
        indices=bytes([0x1B, 0xE4, 0x1B, 0xE4]),
    )

    encoded = packet.encode()
    decoded = IndexedRectPacket.decode(encoded)

    assert encoded[3] == PKT_INDEXED_RECT
    assert decoded == packet


def test_i8_frame_chunk_uses_zero_wire_sentinel_for_full_raw_length():
    packet = IndexedFrameChunkPacket(
        frame_id=20,
        chunk_id=0,
        chunk_count=1,
        frame_len=600,
        bpp=8,
        raw_len=240 * 320,
        payload=b"indexed-i8",
    )

    encoded = packet.encode()
    decoded = IndexedFrameChunkPacket.decode(encoded)

    assert encoded[20:22] == b"\x00\x00"
    assert encoded[18] == PIXEL_INDEXED8
    assert decoded == packet


def test_i8_frame_chunk_supports_extended_frame_length():
    packet = IndexedFrameChunkPacket(
        frame_id=21,
        chunk_id=54,
        chunk_count=55,
        frame_len=77_000,
        bpp=8,
        raw_len=240 * 320,
        payload=bytes(100),
    )

    decoded = IndexedFrameChunkPacket.decode(packet.encode())

    assert decoded == packet


def test_indexed_frame_chunk_carries_landscape_dimensions():
    packet = IndexedFrameChunkPacket(
        frame_id=22,
        chunk_id=0,
        chunk_count=1,
        frame_len=600,
        bpp=2,
        raw_len=320 * 240 * 2 // 8,
        payload=b"indexed-landscape",
        width=320,
        height=240,
    )

    decoded = IndexedFrameChunkPacket.decode(packet.encode())

    assert decoded == packet


def test_indexed_frame_end_marks_keyframe_and_rectangle_count():
    encoded = IndexedFrameEndPacket(frame_id=13, rect_count=20, keyframe=True).encode()

    assert encoded[3] == PKT_INDEXED_FRAME_END
    assert encoded[-3:] == bytes([20, 0, 1])


def test_split_jpeg_chunks_splits_full_frame_jpeg_under_mtu():
    image = Image.new("RGB", (240, 320), (20, 40, 80))

    chunks = split_jpeg_chunks(image, frame_id=5, quality=60)

    assert chunks
    assert chunks[0].packet.chunk_id == 0
    assert chunks[0].packet.payload.startswith(b"\xff\xd8")
    assert chunks[-1].packet.payload.endswith(b"\xff\xd9")
    assert all(len(chunk.packet.payload) <= JPEG_CHUNK_PAYLOAD_SIZE for chunk in chunks)
    assert [chunk.packet.chunk_id for chunk in chunks] == list(range(len(chunks)))


def test_image_to_jpeg_bytes_accepts_chroma_subsampling_modes():
    image = Image.new("RGB", (32, 32), (128, 64, 32))

    encoded_420 = image_to_jpeg_bytes(image, quality=80, subsampling="420")
    encoded_444 = image_to_jpeg_bytes(image, quality=80, subsampling="444")

    assert encoded_420.startswith(b"\xff\xd8")
    assert encoded_444.startswith(b"\xff\xd8")
    assert encoded_420 != encoded_444


def test_image_to_jpeg_bytes_rejects_invalid_chroma_subsampling():
    image = Image.new("RGB", (32, 32), (128, 64, 32))

    with pytest.raises(ValueError, match="subsampling"):
        image_to_jpeg_bytes(image, subsampling="411")


def test_tile_packet_matches_golden_wire_packet():
    payload = bytes([0x00, 0xF8] * 4)
    packet = TilePacket(
        frame_id=7,
        tile_id=2,
        x=32,
        y=0,
        w=2,
        h=2,
        payload=payload,
    )

    encoded = packet.encode()

    assert HEADER_SIZE == 28
    assert encoded == bytes.fromhex(
        "5c a1 01 02"
        "07 00 00 00"
        "02 00"
        "20 00"
        "00 00"
        "02 00"
        "02 00"
        "01"
        "00"
        "08 00"
        "08 00"
        "0d e8 46 42"
        "00 f8 00 f8 00 f8 00 f8"
    )


def test_tile_packet_decode_rejects_trailing_bytes():
    packet = TilePacket(
        frame_id=7,
        tile_id=2,
        x=32,
        y=0,
        w=2,
        h=2,
        payload=bytes([0x00, 0xF8] * 4),
    )

    with pytest.raises(ValueError):
        TilePacket.decode(packet.encode() + b"extra")


def test_tile_packet_decode_rejects_unsupported_type():
    encoded = bytearray(
        TilePacket(
            frame_id=7,
            tile_id=2,
            x=32,
            y=0,
            w=2,
            h=2,
            payload=bytes([0x00, 0xF8] * 4),
        ).encode()
    )
    encoded[3] = PKT_STATUS

    with pytest.raises(ValueError):
        TilePacket.decode(bytes(encoded))


def test_tile_packet_decode_rejects_unsupported_pixel_format():
    encoded = bytearray(
        TilePacket(
            frame_id=7,
            tile_id=2,
            x=32,
            y=0,
            w=2,
            h=2,
            payload=bytes([0x00, 0xF8] * 4),
        ).encode()
    )
    encoded[18] = 2

    with pytest.raises(ValueError):
        TilePacket.decode(bytes(encoded))


def test_tile_packet_decode_rejects_unsupported_compression():
    encoded = bytearray(
        TilePacket(
            frame_id=7,
            tile_id=2,
            x=32,
            y=0,
            w=2,
            h=2,
            payload=bytes([0x00, 0xF8] * 4),
        ).encode()
    )
    encoded[19] = 99

    with pytest.raises(ValueError):
        TilePacket.decode(bytes(encoded))


def test_rle_payload_round_trips_and_is_chosen_when_smaller():
    raw = bytes([0x00, 0xF8] * 12 + [0xE0, 0x07] * 6)

    compression, payload = encode_tile_payload(raw)

    assert compression == COMP_RLE
    assert len(payload) < len(raw)
    assert decode_tile_payload(compression, payload, len(raw)) == raw


def test_encode_tile_payload_supports_forced_modes():
    raw = bytes([0x00, 0xF8] * 4)

    raw_compression, raw_payload = encode_tile_payload(raw, "raw")
    rle_compression, rle_payload = encode_tile_payload(raw, "rle")

    assert raw_compression == COMP_RAW
    assert raw_payload == raw
    assert rle_compression == COMP_RLE
    assert decode_tile_payload(rle_compression, rle_payload, len(raw)) == raw


def test_lz4_payload_round_trips_and_can_be_forced():
    raw = (b"abcd" * 80) + (b"efgh" * 40)

    compression, payload = encode_tile_payload(raw, "lz4")

    assert compression == COMP_LZ4
    assert len(payload) < len(raw)
    assert decode_tile_payload(compression, payload, len(raw)) == raw


def test_auto_can_choose_lz4_for_repeated_byte_patterns():
    raw = (bytes(range(32)) * 12) + (bytes(range(16)) * 8)

    compression, payload = encode_tile_payload(raw, "auto")

    assert compression == COMP_LZ4
    assert decode_tile_payload(compression, payload, len(raw)) == raw


def test_tile_packet_rle_round_trip_decodes_to_raw_pixels():
    raw = bytes([0x1F, 0x00] * 10)
    compression, payload = encode_tile_payload(raw)
    packet = TilePacket(
        frame_id=3,
        tile_id=1,
        x=0,
        y=0,
        w=5,
        h=2,
        payload=payload,
        raw_len=len(raw),
        compression=compression,
    )

    decoded = TilePacket.decode(packet.encode())

    assert decoded.compression == COMP_RLE
    assert decode_tile_payload(decoded.compression, decoded.payload, decoded.raw_len) == raw
