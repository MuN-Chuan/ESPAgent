from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass
from io import BytesIO

from PIL import Image

MAGIC_DISPLAY = 0xA15C
PROTOCOL_VERSION = 1

PKT_FRAME_BEGIN = 1
PKT_TILE = 2
PKT_FRAME_END = 3
PKT_JPEG_CHUNK = 4
PKT_INDEXED_RECT = 5
PKT_INDEXED_FRAME_END = 6
PKT_STATUS = 7
PKT_INDEXED_FRAME_CHUNK = 8

PIXEL_RGB565 = 1
PIXEL_JPEG = 2
PIXEL_INDEXED1 = 3
PIXEL_INDEXED2 = 4
PIXEL_INDEXED4 = 5
PIXEL_INDEXED8 = 6
COMP_RAW = 0
COMP_RLE = 1
COMP_LZ4 = 2

TRANSPORT_TILES = 0
TRANSPORT_MJPEG = 1
TRANSPORT_INDEXED = 2
DISPLAY_ORIENTATION_PORTRAIT = 0
DISPLAY_ORIENTATION_LANDSCAPE = 1

PANEL_GAMMA_22 = 0x01
PANEL_GAMMA_18 = 0x02
PANEL_GAMMA_25 = 0x04
PANEL_GAMMA_10 = 0x08
PANEL_GAMMA_VALUES = frozenset({PANEL_GAMMA_22, PANEL_GAMMA_18, PANEL_GAMMA_25, PANEL_GAMMA_10})
DISCOVERY_MESSAGE = b"ESPAGENT_DISCOVER 1"

HEADER_STRUCT = struct.Struct("<HBBIHHHHHBBHHI")
HEADER_SIZE = HEADER_STRUCT.size
JPEG_CHUNK_PAYLOAD_SIZE = 1400
INDEXED_FRAME_FLAG_KEYFRAME = 0x01
INDEXED_CHUNK_PAYLOAD_SIZE = 1400
INDEXED_FRAME_MAX_BYTES = 80 * 1024
INDEXED_BPP_TO_PIXEL_FORMAT = {
    1: PIXEL_INDEXED1,
    2: PIXEL_INDEXED2,
    4: PIXEL_INDEXED4,
    8: PIXEL_INDEXED8,
}
INDEXED_PIXEL_FORMAT_TO_BPP = {pixel_format: bpp for bpp, pixel_format in INDEXED_BPP_TO_PIXEL_FORMAT.items()}
MAX_UINT16 = 0xFFFF


def _indexed_wire_raw_len(raw_len: int) -> int:
    if 0 < raw_len <= MAX_UINT16:
        return raw_len
    if raw_len == 240 * 320:
        return 0
    raise ValueError("indexed raw length does not fit protocol")


def _indexed_raw_len_from_wire(wire_raw_len: int, expected: int) -> int:
    if wire_raw_len == 0 and expected == 240 * 320:
        return expected
    return wire_raw_len


def rgb888_to_rgb565(r: int, g: int, b: int) -> int:
    return ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)


@dataclass(frozen=True)
class TilePacket:
    frame_id: int
    tile_id: int
    x: int
    y: int
    w: int
    h: int
    payload: bytes
    type: int = PKT_TILE
    pixel_format: int = PIXEL_RGB565
    compression: int = COMP_RAW
    raw_len: int | None = None

    def __post_init__(self) -> None:
        if self.raw_len is None:
            object.__setattr__(self, "raw_len", len(self.payload))

    def encode(self) -> bytes:
        crc = zlib.crc32(self.payload) & 0xFFFFFFFF
        assert self.raw_len is not None
        header = HEADER_STRUCT.pack(
            MAGIC_DISPLAY,
            PROTOCOL_VERSION,
            self.type,
            self.frame_id,
            self.tile_id,
            self.x,
            self.y,
            self.w,
            self.h,
            self.pixel_format,
            self.compression,
            self.raw_len,
            len(self.payload),
            crc,
        )
        return header + self.payload

    @classmethod
    def decode(cls, data: bytes) -> "TilePacket":
        if len(data) < HEADER_SIZE:
            raise ValueError("packet too short")
        unpacked = HEADER_STRUCT.unpack(data[:HEADER_SIZE])
        (
            magic,
            version,
            packet_type,
            frame_id,
            tile_id,
            x,
            y,
            w,
            h,
            pixel_format,
            compression,
            raw_len,
            payload_len,
            crc,
        ) = unpacked
        if magic != MAGIC_DISPLAY:
            raise ValueError("bad display magic")
        if version != PROTOCOL_VERSION:
            raise ValueError("unsupported protocol version")
        if packet_type != PKT_TILE:
            raise ValueError("unsupported packet type")
        if pixel_format != PIXEL_RGB565:
            raise ValueError("unsupported pixel format")
        if compression not in {COMP_RAW, COMP_RLE, COMP_LZ4}:
            raise ValueError("unsupported compression")
        if len(data) != HEADER_SIZE + payload_len:
            raise ValueError("packet length mismatch")
        payload = data[HEADER_SIZE : HEADER_SIZE + payload_len]
        if len(payload) != payload_len:
            raise ValueError("payload length mismatch")
        if compression == COMP_RAW and raw_len != payload_len:
            raise ValueError("raw packets must have raw_len == payload_len")
        if compression in {COMP_RLE, COMP_LZ4}:
            decode_tile_payload(compression, payload, raw_len)
        if (zlib.crc32(payload) & 0xFFFFFFFF) != crc:
            raise ValueError("payload crc mismatch")
        return cls(
            frame_id=frame_id,
            tile_id=tile_id,
            x=x,
            y=y,
            w=w,
            h=h,
            payload=payload,
            type=packet_type,
            pixel_format=pixel_format,
            compression=compression,
            raw_len=raw_len,
        )


@dataclass(frozen=True)
class JpegChunkPacket:
    frame_id: int
    chunk_id: int
    width: int
    height: int
    payload: bytes
    jpeg_len: int
    type: int = PKT_JPEG_CHUNK
    pixel_format: int = PIXEL_JPEG
    compression: int = COMP_RAW
    raw_len: int | None = None

    def __post_init__(self) -> None:
        if self.raw_len is None:
            object.__setattr__(self, "raw_len", self.jpeg_len)

    def encode(self) -> bytes:
        crc = zlib.crc32(self.payload) & 0xFFFFFFFF
        assert self.raw_len is not None
        header = HEADER_STRUCT.pack(
            MAGIC_DISPLAY,
            PROTOCOL_VERSION,
            self.type,
            self.frame_id,
            self.chunk_id,
            0,
            0,
            self.width,
            self.height,
            self.pixel_format,
            self.compression,
            self.raw_len,
            len(self.payload),
            crc,
        )
        return header + self.payload

    @classmethod
    def decode(cls, data: bytes) -> "JpegChunkPacket":
        if len(data) < HEADER_SIZE:
            raise ValueError("packet too short")
        (
            magic,
            version,
            packet_type,
            frame_id,
            chunk_id,
            _x,
            _y,
            width,
            height,
            pixel_format,
            compression,
            raw_len,
            payload_len,
            crc,
        ) = HEADER_STRUCT.unpack(data[:HEADER_SIZE])
        if magic != MAGIC_DISPLAY:
            raise ValueError("bad display magic")
        if version != PROTOCOL_VERSION:
            raise ValueError("unsupported protocol version")
        if packet_type != PKT_JPEG_CHUNK:
            raise ValueError("unsupported packet type")
        if pixel_format != PIXEL_JPEG:
            raise ValueError("unsupported pixel format")
        if compression != COMP_RAW:
            raise ValueError("jpeg chunks must use raw compression")
        if len(data) != HEADER_SIZE + payload_len:
            raise ValueError("packet length mismatch")
        payload = data[HEADER_SIZE : HEADER_SIZE + payload_len]
        if (zlib.crc32(payload) & 0xFFFFFFFF) != crc:
            raise ValueError("payload crc mismatch")
        return cls(
            frame_id=frame_id,
            chunk_id=chunk_id,
            width=width,
            height=height,
            payload=payload,
            jpeg_len=raw_len,
            type=packet_type,
            pixel_format=pixel_format,
            compression=compression,
            raw_len=raw_len,
        )


@dataclass(frozen=True)
class EncodedJpegChunk:
    packet: JpegChunkPacket


@dataclass(frozen=True)
class IndexedRectPacket:
    frame_id: int
    rect_id: int
    x: int
    y: int
    w: int
    h: int
    bpp: int
    palette: tuple[int, ...]
    indices: bytes
    compression: int = COMP_LZ4

    def encode(self) -> bytes:
        if self.bpp not in INDEXED_BPP_TO_PIXEL_FORMAT:
            raise ValueError("indexed bpp must be 1, 2, 4, or 8")
        color_count = 1 << self.bpp
        if len(self.palette) != color_count:
            raise ValueError("indexed palette size does not match bpp")
        expected = (self.w * self.h * self.bpp + 7) // 8
        if len(self.indices) != expected:
            raise ValueError("indexed payload size does not match rectangle")
        if self.compression == COMP_RAW:
            encoded_indices = self.indices
        elif self.compression == COMP_LZ4:
            encoded_indices = _encode_lz4_block(self.indices)
        else:
            raise ValueError("indexed rectangles support raw or lz4 compression")
        palette_bytes = struct.pack(f"<{color_count}H", *self.palette)
        payload = palette_bytes + encoded_indices
        if len(payload) > MAX_UINT16:
            raise ValueError("indexed rectangle payload does not fit protocol")
        crc = zlib.crc32(payload) & 0xFFFFFFFF
        pixel_format = INDEXED_BPP_TO_PIXEL_FORMAT[self.bpp]
        return HEADER_STRUCT.pack(
            MAGIC_DISPLAY,
            PROTOCOL_VERSION,
            PKT_INDEXED_RECT,
            self.frame_id,
            self.rect_id,
            self.x,
            self.y,
            self.w,
            self.h,
            pixel_format,
            self.compression,
            _indexed_wire_raw_len(len(self.indices)),
            len(payload),
            crc,
        ) + payload

    @classmethod
    def decode(cls, data: bytes) -> "IndexedRectPacket":
        if len(data) < HEADER_SIZE:
            raise ValueError("packet too short")
        fields = HEADER_STRUCT.unpack(data[:HEADER_SIZE])
        magic, version, packet_type = fields[:3]
        frame_id, rect_id, x, y, w, h = fields[3:9]
        pixel_format, compression, raw_len, payload_len, crc = fields[9:]
        if magic != MAGIC_DISPLAY or version != PROTOCOL_VERSION or packet_type != PKT_INDEXED_RECT:
            raise ValueError("unsupported indexed rectangle packet")
        if pixel_format not in INDEXED_PIXEL_FORMAT_TO_BPP:
            raise ValueError("unsupported indexed pixel format")
        if compression not in {COMP_RAW, COMP_LZ4}:
            raise ValueError("unsupported indexed compression")
        if w == 0 or h == 0 or len(data) != HEADER_SIZE + payload_len:
            raise ValueError("indexed rectangle length mismatch")
        bpp = INDEXED_PIXEL_FORMAT_TO_BPP[pixel_format]
        expected = (w * h * bpp + 7) // 8
        raw_len = _indexed_raw_len_from_wire(raw_len, expected)
        if raw_len != expected:
            raise ValueError("indexed raw length mismatch")
        payload = data[HEADER_SIZE:]
        if zlib.crc32(payload) & 0xFFFFFFFF != crc:
            raise ValueError("indexed payload crc mismatch")
        color_count = 1 << bpp
        palette_len = color_count * 2
        if len(payload) < palette_len:
            raise ValueError("indexed palette is truncated")
        palette = struct.unpack(f"<{color_count}H", payload[:palette_len])
        indices = decode_tile_payload(compression, payload[palette_len:], raw_len)
        return cls(frame_id, rect_id, x, y, w, h, bpp, palette, indices, compression)


@dataclass(frozen=True)
class IndexedFrameEndPacket:
    frame_id: int
    rect_count: int
    keyframe: bool

    def encode(self) -> bytes:
        payload = struct.pack("<HB", self.rect_count, INDEXED_FRAME_FLAG_KEYFRAME if self.keyframe else 0)
        crc = zlib.crc32(payload) & 0xFFFFFFFF
        return HEADER_STRUCT.pack(
            MAGIC_DISPLAY, PROTOCOL_VERSION, PKT_INDEXED_FRAME_END, self.frame_id,
            0, 0, 0, 0, 0, PIXEL_INDEXED1, COMP_RAW,
            len(payload), len(payload), crc,
        ) + payload


@dataclass(frozen=True)
class IndexedFrameChunkPacket:
    frame_id: int
    chunk_id: int
    chunk_count: int
    frame_len: int
    bpp: int
    raw_len: int
    payload: bytes
    width: int = 240
    height: int = 320

    def encode(self) -> bytes:
        if self.bpp not in INDEXED_BPP_TO_PIXEL_FORMAT or not self.payload or len(self.payload) > INDEXED_CHUNK_PAYLOAD_SIZE:
            raise ValueError("invalid indexed frame chunk")
        if not 0 < self.frame_len <= INDEXED_FRAME_MAX_BYTES:
            raise ValueError("indexed frame does not fit protocol")
        if self.bpp != 8 and self.frame_len > MAX_UINT16:
            raise ValueError("legacy indexed frame length does not fit protocol")
        if self.bpp == 8:
            frame_len_low = self.frame_len & MAX_UINT16
            frame_len_high = self.frame_len >> 16
            chunk_count_field = self.chunk_count
            height_field = 0
        else:
            frame_len_low = self.frame_len
            frame_len_high = self.chunk_count
            chunk_count_field = self.width
            height_field = self.height
        crc = zlib.crc32(self.payload) & 0xFFFFFFFF
        return HEADER_STRUCT.pack(
            MAGIC_DISPLAY, PROTOCOL_VERSION, PKT_INDEXED_FRAME_CHUNK, self.frame_id,
            self.chunk_id, frame_len_low, frame_len_high, chunk_count_field, height_field,
            INDEXED_BPP_TO_PIXEL_FORMAT[self.bpp],
            COMP_LZ4, _indexed_wire_raw_len(self.raw_len), len(self.payload), crc,
        ) + self.payload

    @classmethod
    def decode(cls, data: bytes) -> "IndexedFrameChunkPacket":
        if len(data) < HEADER_SIZE:
            raise ValueError("packet too short")
        fields = HEADER_STRUCT.unpack(data[:HEADER_SIZE])
        magic, version, packet_type, frame_id, chunk_id, x, y, width, height, pixel_format, compression, raw_len, payload_len, crc = fields
        payload = data[HEADER_SIZE:]
        if magic != MAGIC_DISPLAY or version != PROTOCOL_VERSION or packet_type != PKT_INDEXED_FRAME_CHUNK:
            raise ValueError("unsupported indexed frame packet")
        if pixel_format not in INDEXED_PIXEL_FORMAT_TO_BPP or compression != COMP_LZ4:
            raise ValueError("invalid indexed frame metadata")
        bpp = INDEXED_PIXEL_FORMAT_TO_BPP[pixel_format]
        if bpp == 8:
            frame_len = x | (y << 16)
            chunk_count = width
            if height != 0:
                raise ValueError("invalid I8 frame metadata")
        else:
            frame_len = x
            chunk_count = y
            if (width, height) not in {(240, 320), (320, 240)}:
                raise ValueError("invalid indexed frame dimensions")
        if (
            chunk_count == 0
            or chunk_id >= chunk_count
            or frame_len == 0
            or frame_len > INDEXED_FRAME_MAX_BYTES
            or len(payload) != payload_len
        ):
            raise ValueError("invalid indexed frame chunk bounds")
        if chunk_id * INDEXED_CHUNK_PAYLOAD_SIZE + payload_len > frame_len:
            raise ValueError("indexed frame chunk exceeds frame")
        if zlib.crc32(payload) & 0xFFFFFFFF != crc:
            raise ValueError("indexed frame chunk crc mismatch")
        expected_raw_len = width * height * bpp // 8 if bpp != 8 else 240 * 320 * bpp // 8
        raw_len = _indexed_raw_len_from_wire(raw_len, expected_raw_len)
        if raw_len != expected_raw_len:
            raise ValueError("invalid indexed frame raw length")
        packet_width, packet_height = (width, height) if bpp != 8 else (240, 320)
        return cls(frame_id, chunk_id, chunk_count, frame_len, bpp, raw_len, payload, packet_width, packet_height)


def split_jpeg_chunks(
    image: Image.Image,
    frame_id: int,
    *,
    quality: int = 60,
    subsampling: str = "420",
    chunk_size: int = JPEG_CHUNK_PAYLOAD_SIZE,
) -> list[EncodedJpegChunk]:
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    jpeg = image_to_jpeg_bytes(image, quality=quality, subsampling=subsampling)
    if len(jpeg) > 0xFFFF:
        raise ValueError("jpeg frame must fit in 16-bit protocol raw_len")
    width, height = image.size
    chunks: list[EncodedJpegChunk] = []
    for offset in range(0, len(jpeg), chunk_size):
        payload = jpeg[offset : offset + chunk_size]
        chunks.append(
            EncodedJpegChunk(
                JpegChunkPacket(
                    frame_id=frame_id,
                    chunk_id=len(chunks),
                    width=width,
                    height=height,
                    jpeg_len=len(jpeg),
                    payload=payload,
                )
            )
        )
    return chunks


def image_to_jpeg_bytes(image: Image.Image, quality: int = 60, subsampling: str = "420") -> bytes:
    subsampling_map = {"444": 0, "422": 1, "420": 2}
    if subsampling not in subsampling_map:
        raise ValueError("subsampling must be 420, 422, or 444")
    buffer = BytesIO()
    image.convert("RGB").save(
        buffer,
        format="JPEG",
        quality=quality,
        subsampling=subsampling_map[subsampling],
        optimize=False,
        progressive=False,
    )
    return buffer.getvalue()


def encode_tile_payload(raw: bytes, mode: str = "auto") -> tuple[int, bytes]:
    if mode == "raw":
        return COMP_RAW, raw
    rle = _encode_rle_rgb565(raw)
    if mode == "rle":
        return COMP_RLE, rle
    lz4 = _encode_lz4_block(raw)
    if mode == "lz4":
        return COMP_LZ4, lz4
    if mode == "auto":
        best_compression, best_payload = min(
            ((COMP_RAW, raw), (COMP_RLE, rle), (COMP_LZ4, lz4)),
            key=lambda item: len(item[1]),
        )
        return best_compression, best_payload
    if mode != "auto":
        raise ValueError("compression mode must be auto, raw, rle, or lz4")
    return COMP_RAW, raw


def decode_tile_payload(compression: int, payload: bytes, raw_len: int) -> bytes:
    if compression == COMP_RAW:
        if len(payload) != raw_len:
            raise ValueError("raw payload length mismatch")
        return payload
    if compression == COMP_RLE:
        return _decode_rle_rgb565(payload, raw_len)
    if compression == COMP_LZ4:
        return _decode_lz4_block(payload, raw_len)
    raise ValueError("unsupported compression")


def _encode_rle_rgb565(raw: bytes) -> bytes:
    if len(raw) % 2 != 0:
        raise ValueError("rgb565 payload must have even length")
    out = bytearray()
    index = 0
    while index < len(raw):
        pixel = raw[index : index + 2]
        run = 1
        next_index = index + 2
        while next_index < len(raw) and run < 255 and raw[next_index : next_index + 2] == pixel:
            run += 1
            next_index += 2
        out.append(run)
        out.extend(pixel)
        index = next_index
    return bytes(out)


def _decode_rle_rgb565(payload: bytes, raw_len: int) -> bytes:
    if len(payload) % 3 != 0:
        raise ValueError("rle payload length mismatch")
    out = bytearray()
    for index in range(0, len(payload), 3):
        run = payload[index]
        if run == 0:
            raise ValueError("rle run must be non-zero")
        pixel = payload[index + 1 : index + 3]
        out.extend(pixel * run)
    if len(out) != raw_len:
        raise ValueError("rle decoded length mismatch")
    return bytes(out)


def _encode_lz4_block(raw: bytes) -> bytes:
    out = bytearray()
    literals = bytearray()
    table: dict[bytes, int] = {}
    index = 0
    while index + 4 <= len(raw):
        key = raw[index : index + 4]
        match_index = table.get(key)
        table[key] = index
        if match_index is None or index - match_index > 0xFFFF:
            literals.append(raw[index])
            index += 1
            continue

        match_len = 4
        max_len = len(raw) - index
        while match_len < max_len and raw[match_index + match_len] == raw[index + match_len]:
            match_len += 1
        if match_len < 4:
            literals.append(raw[index])
            index += 1
            continue

        _write_lz4_sequence(out, literals, index - match_index, match_len)
        literals.clear()
        for backfill in range(index + 1, min(index + match_len, len(raw) - 3)):
            table[raw[backfill : backfill + 4]] = backfill
        index += match_len

    literals.extend(raw[index:])
    _write_lz4_last_literals(out, literals)
    return bytes(out)


def _write_lz4_sequence(out: bytearray, literals: bytearray, offset: int, match_len: int) -> None:
    literal_len = len(literals)
    token_pos = len(out)
    token = min(literal_len, 15) << 4
    match_nibble = min(match_len - 4, 15)
    token |= match_nibble
    out.append(token)
    if literal_len >= 15:
        _write_lz4_extended_len(out, literal_len - 15)
    out.extend(literals)
    out.append(offset & 0xFF)
    out.append((offset >> 8) & 0xFF)
    if match_len - 4 >= 15:
        _write_lz4_extended_len(out, match_len - 4 - 15)
    out[token_pos] = token


def _write_lz4_last_literals(out: bytearray, literals: bytearray) -> None:
    literal_len = len(literals)
    out.append(min(literal_len, 15) << 4)
    if literal_len >= 15:
        _write_lz4_extended_len(out, literal_len - 15)
    out.extend(literals)


def _write_lz4_extended_len(out: bytearray, value: int) -> None:
    while value >= 255:
        out.append(255)
        value -= 255
    out.append(value)


def _decode_lz4_block(payload: bytes, raw_len: int) -> bytes:
    out = bytearray()
    index = 0
    while index < len(payload):
        token = payload[index]
        index += 1
        literal_len = token >> 4
        if literal_len == 15:
            extra, index = _read_lz4_extended_len(payload, index)
            literal_len += extra
        if index + literal_len > len(payload):
            raise ValueError("lz4 literal length mismatch")
        out.extend(payload[index : index + literal_len])
        index += literal_len
        if index == len(payload):
            break
        if index + 2 > len(payload):
            raise ValueError("lz4 missing offset")
        offset = payload[index] | (payload[index + 1] << 8)
        index += 2
        if offset == 0 or offset > len(out):
            raise ValueError("lz4 invalid offset")
        match_len = token & 0x0F
        if match_len == 15:
            extra, index = _read_lz4_extended_len(payload, index)
            match_len += extra
        match_len += 4
        for _ in range(match_len):
            out.append(out[-offset])
    if len(out) != raw_len:
        raise ValueError("lz4 decoded length mismatch")
    return bytes(out)


def _read_lz4_extended_len(payload: bytes, index: int) -> tuple[int, int]:
    value = 0
    while True:
        if index >= len(payload):
            raise ValueError("lz4 extended length mismatch")
        item = payload[index]
        index += 1
        value += item
        if item != 255:
            return value, index


@dataclass(frozen=True)
class FrameEndPacket:
    frame_id: int
    tile_count: int

    def encode(self) -> bytes:
        payload = struct.pack("<H", self.tile_count)
        crc = zlib.crc32(payload) & 0xFFFFFFFF
        return (
            HEADER_STRUCT.pack(
                MAGIC_DISPLAY,
                PROTOCOL_VERSION,
                PKT_FRAME_END,
                self.frame_id,
                0,
                0,
                0,
                0,
                0,
                PIXEL_RGB565,
                COMP_RAW,
                len(payload),
                len(payload),
                crc,
            )
            + payload
        )


@dataclass(frozen=True)
class DisplayModePacket:
    unified_flush: bool
    nack_enabled: bool = True
    esp_log_enabled: bool = False
    backlight_percent: int = 100
    transport_mode: int = TRANSPORT_TILES
    transport_epoch: int = 0
    panel_gamma: int = PANEL_GAMMA_18
    orientation: int = DISPLAY_ORIENTATION_PORTRAIT

    def encode(self) -> bytes:
        if not 0 <= self.backlight_percent <= 100:
            raise ValueError("backlight_percent must be between 0 and 100")
        if self.transport_mode not in {TRANSPORT_TILES, TRANSPORT_MJPEG, TRANSPORT_INDEXED}:
            raise ValueError("unsupported transport_mode")
        if not 0 <= self.transport_epoch <= 0xFFFFFFFF:
            raise ValueError("transport_epoch must fit in uint32")
        if self.panel_gamma not in PANEL_GAMMA_VALUES:
            raise ValueError("unsupported panel_gamma")
        if self.orientation not in {DISPLAY_ORIENTATION_PORTRAIT, DISPLAY_ORIENTATION_LANDSCAPE}:
            raise ValueError("unsupported display orientation")
        payload_values = [
            1 if self.unified_flush else 0,
            1 if self.nack_enabled else 0,
            1 if self.esp_log_enabled else 0,
            self.backlight_percent,
            self.transport_mode,
            self.panel_gamma,
        ]
        if self.orientation == DISPLAY_ORIENTATION_LANDSCAPE:
            payload_values.append(self.orientation)
        payload = bytes(payload_values)
        crc = zlib.crc32(payload) & 0xFFFFFFFF
        return (
            HEADER_STRUCT.pack(
                MAGIC_DISPLAY,
                PROTOCOL_VERSION,
                PKT_FRAME_BEGIN,
                self.transport_epoch,
                0,
                0,
                0,
                0,
                0,
                PIXEL_RGB565,
                COMP_RAW,
                len(payload),
                len(payload),
                crc,
            )
            + payload
        )
