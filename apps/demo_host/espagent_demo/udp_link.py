from __future__ import annotations

import socket
import time
from collections import OrderedDict
from dataclasses import dataclass
from threading import Condition, Lock
from typing import Callable, Protocol

from PIL import Image

from espagent_demo.display_geometry import LANDSCAPE, normalize_orientation
from espagent_demo.indexed_rect import IndexedRectEncoder
from espagent_demo.protocol import (
    DISPLAY_ORIENTATION_LANDSCAPE,
    DISPLAY_ORIENTATION_PORTRAIT,
    JPEG_CHUNK_PAYLOAD_SIZE,
    DisplayModePacket,
    EncodedJpegChunk,
    FrameEndPacket,
    JpegChunkPacket,
    TRANSPORT_INDEXED,
    TRANSPORT_MJPEG,
    TRANSPORT_TILES,
    PANEL_GAMMA_10,
    PANEL_GAMMA_18,
    PANEL_GAMMA_22,
    PANEL_GAMMA_25,
    image_to_jpeg_bytes,
)
from espagent_demo.renderer import DisplayRenderer, TILE_HEIGHT, TILE_WIDTH, split_dirty_tiles
from espagent_demo.state import DemoState, indexed_palette_capacity, resize_indexed_color_slots


PANEL_GAMMA_VALUES = {
    "1.0": PANEL_GAMMA_10,
    "1.8": PANEL_GAMMA_18,
    "2.2": PANEL_GAMMA_22,
    "2.5": PANEL_GAMMA_25,
}

INDEXED_MAX_IN_FLIGHT_FRAMES = 3
INDEXED_ACK_WAIT_TIMEOUT_SECONDS = 0.5


class DatagramSocket(Protocol):
    def sendto(self, payload: bytes, target: tuple[str, int]) -> object:
        ...

    def close(self) -> object:
        ...


@dataclass(frozen=True)
class FrameSendResult:
    frame_id: int
    dirty_tiles: int
    elapsed_ms: float


def _normalize_protected_color_slots(
    colors: list[str | None] | tuple[str | None, ...],
    bpp: int,
) -> tuple[str | None, ...]:
    capacity = indexed_palette_capacity(bpp)
    if len(colors) != capacity:
        raise ValueError(f"indexed_protected_colors must contain {capacity} slots for I{bpp}")
    normalized: list[str | None] = []
    for color in colors:
        if color is None:
            normalized.append(None)
            continue
        value = color.strip()
        if len(value) != 7 or not value.startswith("#"):
            raise ValueError("protected colors must be #RRGGBB or null")
        try:
            int(value[1:], 16)
        except ValueError as exc:
            raise ValueError("protected colors must be #RRGGBB or null") from exc
        normalized.append(value.upper())
    return tuple(normalized)


def _hex_color_to_rgb(color: str) -> tuple[int, int, int]:
    return int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16)


def _transport_epoch_is_stale_for_peer(requested: int, active: int) -> bool:
    if requested == active:
        return False
    delta = (requested - active) & 0xFFFFFFFF
    return delta >= 0x80000000


class FrameStreamer:
    def __init__(
        self,
        target_ip: str,
        target_port: int,
        dry_run: bool = False,
        sock: DatagramSocket | None = None,
        full_refresh_interval: int = 10,
        packet_burst_size: int = 1,
        packet_gap_seconds: float = 0.003,
        frame_end_repeats: int = 3,
        full_refresh_tile_repeats: int = 2,
        sleep: Callable[[float], object] = time.sleep,
        clock: Callable[[], float] = time.perf_counter,
        mjpeg_max_fps: float | None = None,
    ) -> None:
        self.target = (target_ip, target_port)
        self.dry_run = dry_run
        self.frame_id = 0
        self.previous: Image.Image | None = None
        self.full_refresh_interval = full_refresh_interval
        self.packet_burst_size = packet_burst_size
        self.packet_gap_seconds = packet_gap_seconds
        self.frame_end_repeats = frame_end_repeats
        self.full_refresh_tile_repeats = full_refresh_tile_repeats
        self.sleep = sleep
        self.clock = clock
        self.mjpeg_frame_interval = None if mjpeg_max_fps is None else 1.0 / mjpeg_max_fps
        self._last_mjpeg_sent_at: float | None = None
        self.sock = sock if sock is not None or dry_run else socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.force_full_refresh_frames = 0
        self.transport_mode = "udp"
        self.transport_epoch = (time.time_ns() // 1_000_000) & 0xFFFFFFFF
        if self.transport_epoch == 0:
            self.transport_epoch = 1
        self.unified_flush = True
        self.compression_mode = "auto"
        self.nack_enabled = True
        self.resend_frame_end_on_nack = True
        self.esp_log_enabled = False
        self.backlight_percent = 100
        self.panel_gamma = "1.8"
        self.orientation = "portrait"
        self.tile_width = TILE_WIDTH
        self.tile_height = TILE_HEIGHT
        self.mjpeg_quality = 60
        self.mjpeg_subsampling = "420"
        self.indexed_bpp = 2
        self.indexed_protected_colors: tuple[str | None, ...] = (None,) * indexed_palette_capacity(self.indexed_bpp)
        self.indexed_keyframe_interval = 60
        self._indexed_encoder = self._create_indexed_encoder()
        self._sent_display_mode: tuple[bool, bool, bool, int, int, int, str, str] | None = None
        self._acknowledged_transport_mode: int | None = None
        self._acknowledged_transport_epoch = 0
        self._last_display_mode_sent_at = 0.0
        self._encoded_tile_cache: dict[int, dict[int, bytes]] = {}
        self._encoded_jpeg_chunk_cache: dict[int, dict[int, bytes]] = {}
        self._frame_end_cache: dict[int, bytes] = {}
        self._latest_mjpeg_lock = Lock()
        self._display_ack_condition = Condition()
        self._last_displayed_frame_id: int | None = None
        self._send_lock = Lock()
        self._latest_mjpeg_frame: tuple[int, bytes] | None = None
        self._displayed_mjpeg_frame: tuple[int, bytes] | None = None
        self._latest_image: Image.Image | None = None
        self._mjpeg_frame_cache: OrderedDict[int, bytes] = OrderedDict()
        self._mjpeg_encode_cache_image: Image.Image | None = None
        self._mjpeg_encode_cache_quality: int | None = None
        self._mjpeg_encode_cache_subsampling: str | None = None
        self._mjpeg_encode_cache_jpeg: bytes | None = None
        self._fps_window_started: float | None = None
        self._fps_window_frames = 0

    def _create_indexed_encoder(self) -> IndexedRectEncoder:
        protected_colors = tuple(
            _hex_color_to_rgb(color)
            for color in self.indexed_protected_colors
            if color is not None
        )
        return IndexedRectEncoder(
            bpp=self.indexed_bpp,
            keyframe_interval=self.indexed_keyframe_interval,
            protected_colors=protected_colors,
        )

    def close(self) -> None:
        if self.sock is not None:
            self.sock.close()

    def set_target_ip(self, target_ip: str) -> None:
        if not target_ip:
            raise ValueError("target_ip must not be empty")
        self.target = (target_ip, self.target[1])

    def __enter__(self) -> FrameStreamer:
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self.close()

    def render_and_send(self, renderer: DisplayRenderer, state: DemoState) -> FrameSendResult:
        with self._send_lock:
            return self._render_and_send_locked(renderer, state)

    def _render_and_send_locked(self, renderer: DisplayRenderer, state: DemoState) -> FrameSendResult:
        started = self.clock()
        self.frame_id += 1
        self._set_orientation(state.display_ui.orientation)
        image = renderer.render(state)
        self._send_display_mode_if_needed()
        if self.transport_mode == "mjpeg":
            return self._send_mjpeg_image(image, state, started)
        if self.transport_mode == "indexed":
            return self._send_indexed_image(image, state, started)
        force_full_refresh = self.force_full_refresh_frames > 0 or self._should_force_full_refresh()
        previous = None if force_full_refresh else self.previous
        tiles = split_dirty_tiles(
            image,
            previous,
            self.frame_id,
            self.compression_mode,
            tile_width=self.tile_width,
            tile_height=self.tile_height,
        )
        send_repeats = self.full_refresh_tile_repeats if force_full_refresh else 1
        total_tile_sends = len(tiles) * send_repeats
        sent_tiles = 0
        for repeat_index in range(send_repeats):
            for tile in self._ordered_tiles(tiles, repeat_index):
                sent_tiles += 1
                if not self.dry_run and self.sock is not None:
                    encoded = tile.packet.encode()
                    if repeat_index == 0:
                        self._cache_tile(self.frame_id, tile.packet.tile_id, encoded)
                    self.sock.sendto(encoded, self.target)
                    if self._should_pause_packet_burst(sent_tiles, total_tile_sends):
                        self.sleep(self.packet_gap_seconds)
        if tiles and not self.dry_run and self.sock is not None:
            frame_end = FrameEndPacket(self.frame_id, len(tiles)).encode()
            self._cache_frame_end(self.frame_id, frame_end)
            for _ in range(max(1, self.frame_end_repeats)):
                if self.packet_gap_seconds > 0:
                    self.sleep(self.packet_gap_seconds)
                self.sock.sendto(frame_end, self.target)
        self.previous = image
        self._set_latest_image(image)
        if self.force_full_refresh_frames > 0:
            self.force_full_refresh_frames -= 1
        completed = self.clock()
        elapsed_ms = (completed - started) * 1000
        state.link.frame_id = self.frame_id
        state.link.dirty_tiles = len(tiles)
        self._update_frame_rate(state, completed)
        return FrameSendResult(self.frame_id, len(tiles), elapsed_ms)

    def _send_indexed_image(self, image: Image.Image, state: DemoState, started: float) -> FrameSendResult:
        force_keyframe = self.force_full_refresh_frames > 0
        frame = self._indexed_encoder.encode(
            image,
            self.frame_id,
            force_keyframe=force_keyframe,
            grayscale_antialiasing=state.display_ui.grayscale_antialiasing,
        )
        self._pace_indexed_frame(state)
        packets = frame.packets()
        for index, encoded in enumerate(packets, start=1):
            if not self.dry_run and self.sock is not None:
                self.sock.sendto(encoded, self.target)
                if self._should_pause_packet_burst(index, len(packets)):
                    self.sleep(self.packet_gap_seconds)
        self.previous = frame.preview
        self._set_latest_image(frame.preview)
        if self.force_full_refresh_frames > 0:
            self.force_full_refresh_frames -= 1
        completed = self.clock()
        state.link.frame_id = self.frame_id
        state.link.dirty_tiles = len(frame.chunks)
        self._update_frame_rate(state, completed)
        return FrameSendResult(self.frame_id, len(frame.chunks), (completed - started) * 1000)

    def _send_mjpeg_image(self, image: Image.Image, state: DemoState, started: float) -> FrameSendResult:
        jpeg = self._mjpeg_bytes_for_image(image)
        chunks = _split_jpeg_bytes(jpeg, self.frame_id, image.size)
        self._pace_mjpeg_frame()
        with self._latest_mjpeg_lock:
            self._latest_image = image
            self._latest_mjpeg_frame = (self.frame_id, jpeg)
            self._mjpeg_frame_cache[self.frame_id] = jpeg
            while len(self._mjpeg_frame_cache) > 120:
                self._mjpeg_frame_cache.popitem(last=False)
        total_chunks = len(chunks)
        for index, chunk in enumerate(chunks, start=1):
            if not self.dry_run and self.sock is not None:
                encoded = chunk.packet.encode()
                self._cache_jpeg_chunk(self.frame_id, chunk.packet.chunk_id, encoded)
                self.sock.sendto(encoded, self.target)
                if self._should_pause_packet_burst(index, total_chunks):
                    self.sleep(self.packet_gap_seconds)
        self.previous = image
        if self.force_full_refresh_frames > 0:
            self.force_full_refresh_frames -= 1
        completed = self.clock()
        elapsed_ms = (completed - started) * 1000
        state.link.frame_id = self.frame_id
        state.link.dirty_tiles = len(chunks)
        self._update_frame_rate(state, completed)
        return FrameSendResult(self.frame_id, len(chunks), elapsed_ms)

    def _pace_mjpeg_frame(self) -> None:
        if self.mjpeg_frame_interval is None:
            return
        now = self.clock()
        if self._last_mjpeg_sent_at is not None:
            remaining = self._last_mjpeg_sent_at + self.mjpeg_frame_interval - now
            if remaining > 0:
                self.sleep(remaining)
                now = self.clock()
        self._last_mjpeg_sent_at = now

    def _update_frame_rate(self, state: DemoState, completed: float) -> None:
        if self._fps_window_started is None:
            self._fps_window_started = completed
            self._fps_window_frames = 1
            return
        self._fps_window_frames += 1
        elapsed = completed - self._fps_window_started
        if elapsed < 2.0:
            return
        measured_fps = (self._fps_window_frames - 1) / elapsed
        state.link.fps = measured_fps if state.link.fps <= 0 else state.link.fps * 0.65 + measured_fps * 0.35
        self._fps_window_started = completed
        self._fps_window_frames = 1

    def latest_mjpeg_frame(self) -> tuple[int, bytes] | None:
        with self._latest_mjpeg_lock:
            return self._latest_mjpeg_frame

    def latest_image(self) -> Image.Image | None:
        with self._latest_mjpeg_lock:
            return self._latest_image.copy() if self._latest_image is not None else None

    def mark_displayed(self, frame_id: int) -> None:
        with self._display_ack_condition:
            self._last_displayed_frame_id = frame_id
            self._display_ack_condition.notify_all()
        with self._latest_mjpeg_lock:
            jpeg = self._mjpeg_frame_cache.get(frame_id)
            if jpeg is not None:
                self._displayed_mjpeg_frame = (frame_id, jpeg)

    def _pace_indexed_frame(self, state: DemoState) -> None:
        if not state.link.esp32_online:
            return
        deadline = time.monotonic() + INDEXED_ACK_WAIT_TIMEOUT_SECONDS
        with self._display_ack_condition:
            while self._last_displayed_frame_id is not None and self._last_displayed_frame_id > 0:
                outstanding = self.frame_id - self._last_displayed_frame_id
                if outstanding <= INDEXED_MAX_IN_FLIGHT_FRAMES:
                    return
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return
                self._display_ack_condition.wait(remaining)

    def latest_displayed_mjpeg_frame(self) -> tuple[int, bytes] | None:
        with self._latest_mjpeg_lock:
            return self._displayed_mjpeg_frame

    def handle_nack(self, frame_id: int, tile_ids: list[int]) -> None:
        if self.dry_run or self.sock is None or not self.nack_enabled:
            return
        cached_frame = self._encoded_tile_cache.get(frame_id)
        cached_jpeg_frame = self._encoded_jpeg_chunk_cache.get(frame_id)
        if cached_frame is None and cached_jpeg_frame is None:
            return
        sent_any = False
        for item_id in tile_ids:
            encoded = cached_frame.get(item_id) if cached_frame is not None else None
            if encoded is None and cached_jpeg_frame is not None:
                encoded = cached_jpeg_frame.get(item_id)
            if encoded is not None:
                self.sock.sendto(encoded, self.target)
                sent_any = True
        frame_end = self._frame_end_cache.get(frame_id) if cached_frame is not None else None
        if sent_any and self.resend_frame_end_on_nack and frame_end is not None:
            self.sock.sendto(frame_end, self.target)

    def _should_force_full_refresh(self) -> bool:
        return self.full_refresh_interval > 0 and self.frame_id % self.full_refresh_interval == 0

    def _should_pause_packet_burst(self, sent_count: int, total_tiles: int) -> bool:
        if self.packet_burst_size <= 0 or self.packet_gap_seconds <= 0:
            return False
        return sent_count < total_tiles and sent_count % self.packet_burst_size == 0

    def request_full_refresh(self, frames: int = 3) -> None:
        self.previous = None
        self.force_full_refresh_frames = max(self.force_full_refresh_frames, frames)
        self._sent_display_mode = None

    def _set_orientation(self, orientation: object) -> None:
        requested = normalize_orientation(orientation)
        if requested == self.orientation:
            return
        self.orientation = requested
        self.transport_epoch = (self.transport_epoch + 1) & 0xFFFFFFFF
        if self.transport_epoch == 0:
            self.transport_epoch = 1
        self._last_mjpeg_sent_at = None
        self._indexed_encoder.reset()
        self._clear_frame_cache()
        self.request_full_refresh()

    def set_unified_flush(self, enabled: bool) -> None:
        self.unified_flush = enabled
        self._sent_display_mode = None
        self.request_full_refresh()

    def set_transport_options(
        self,
        *,
        transport_mode: str | None = None,
        compression_mode: str | None = None,
        nack_enabled: bool | None = None,
        resend_frame_end_on_nack: bool | None = None,
        esp_log_enabled: bool | None = None,
        backlight_percent: int | None = None,
        panel_gamma: str | None = None,
        tile_width: int | None = None,
        tile_height: int | None = None,
        mjpeg_quality: int | None = None,
        mjpeg_subsampling: str | None = None,
        indexed_bpp: int | None = None,
        indexed_protected_colors: list[str | None] | tuple[str | None, ...] | None = None,
        indexed_keyframe_interval: int | None = None,
        packet_burst_size: int | None = None,
        packet_gap_seconds: float | None = None,
        frame_end_repeats: int | None = None,
        full_refresh_tile_repeats: int | None = None,
        full_refresh_interval: int | None = None,
    ) -> None:
        with self._send_lock:
            self._set_transport_options_locked(
                transport_mode=transport_mode,
                compression_mode=compression_mode,
                nack_enabled=nack_enabled,
                resend_frame_end_on_nack=resend_frame_end_on_nack,
                esp_log_enabled=esp_log_enabled,
                backlight_percent=backlight_percent,
                panel_gamma=panel_gamma,
                tile_width=tile_width,
                tile_height=tile_height,
                mjpeg_quality=mjpeg_quality,
                mjpeg_subsampling=mjpeg_subsampling,
                indexed_bpp=indexed_bpp,
                indexed_protected_colors=indexed_protected_colors,
                indexed_keyframe_interval=indexed_keyframe_interval,
                packet_burst_size=packet_burst_size,
                packet_gap_seconds=packet_gap_seconds,
                frame_end_repeats=frame_end_repeats,
                full_refresh_tile_repeats=full_refresh_tile_repeats,
                full_refresh_interval=full_refresh_interval,
            )

    def _set_transport_options_locked(
        self,
        *,
        transport_mode: str | None = None,
        compression_mode: str | None = None,
        nack_enabled: bool | None = None,
        resend_frame_end_on_nack: bool | None = None,
        esp_log_enabled: bool | None = None,
        backlight_percent: int | None = None,
        panel_gamma: str | None = None,
        tile_width: int | None = None,
        tile_height: int | None = None,
        mjpeg_quality: int | None = None,
        mjpeg_subsampling: str | None = None,
        indexed_bpp: int | None = None,
        indexed_protected_colors: list[str | None] | tuple[str | None, ...] | None = None,
        indexed_keyframe_interval: int | None = None,
        packet_burst_size: int | None = None,
        packet_gap_seconds: float | None = None,
        frame_end_repeats: int | None = None,
        full_refresh_tile_repeats: int | None = None,
        full_refresh_interval: int | None = None,
    ) -> None:
        if transport_mode is not None:
            if transport_mode not in {"udp", "webrtc", "mjpeg", "indexed"}:
                raise ValueError("unsupported transport_mode")
            if self.transport_mode != transport_mode:
                self.transport_mode = transport_mode
                self._last_mjpeg_sent_at = None
                self.transport_epoch = (self.transport_epoch + 1) & 0xFFFFFFFF
                if self.transport_epoch == 0:
                    self.transport_epoch = 1
                self.request_full_refresh()
                self._indexed_encoder.reset()
        if compression_mode is not None:
            if compression_mode not in {"auto", "raw", "rle", "lz4"}:
                raise ValueError("compression_mode must be auto, raw, rle, or lz4")
            if self.compression_mode != compression_mode:
                self.compression_mode = compression_mode
                self.request_full_refresh()
        if nack_enabled is not None:
            self.nack_enabled = nack_enabled
            self._sent_display_mode = None
        if resend_frame_end_on_nack is not None:
            self.resend_frame_end_on_nack = resend_frame_end_on_nack
        if esp_log_enabled is not None:
            self.esp_log_enabled = esp_log_enabled
            self._sent_display_mode = None
        if backlight_percent is not None:
            if not 0 <= backlight_percent <= 100:
                raise ValueError("backlight_percent must be between 0 and 100")
            self.backlight_percent = backlight_percent
            self._sent_display_mode = None
        if panel_gamma is not None:
            if panel_gamma not in PANEL_GAMMA_VALUES:
                raise ValueError("panel_gamma must be 1.0, 1.8, 2.2, or 2.5")
            self.panel_gamma = panel_gamma
            self._sent_display_mode = None
        if tile_width is not None:
            if not 1 <= tile_width <= 240:
                raise ValueError("tile_width must be between 1 and 240")
            if self.tile_width != tile_width:
                self.tile_width = tile_width
                self._clear_frame_cache()
                self.request_full_refresh()
        if tile_height is not None:
            if not 1 <= tile_height <= 320:
                raise ValueError("tile_height must be between 1 and 320")
            if self.tile_height != tile_height:
                self.tile_height = tile_height
                self._clear_frame_cache()
                self.request_full_refresh()
        if mjpeg_quality is not None:
            if not 10 <= mjpeg_quality <= 95:
                raise ValueError("mjpeg_quality must be between 10 and 95")
            if self.mjpeg_quality != mjpeg_quality:
                self.mjpeg_quality = mjpeg_quality
                self._clear_mjpeg_encode_cache()
                self._clear_frame_cache()
                self.request_full_refresh()
        if mjpeg_subsampling is not None:
            if mjpeg_subsampling not in {"420", "422", "444"}:
                raise ValueError("mjpeg_subsampling must be 420, 422, or 444")
            if self.mjpeg_subsampling != mjpeg_subsampling:
                self.mjpeg_subsampling = mjpeg_subsampling
                self._clear_mjpeg_encode_cache()
                self._clear_frame_cache()
                self.request_full_refresh()
        if indexed_bpp is not None or indexed_protected_colors is not None or indexed_keyframe_interval is not None:
            requested_bpp = self.indexed_bpp if indexed_bpp is None else indexed_bpp
            if requested_bpp not in {1, 2, 4, 8}:
                raise ValueError("indexed_bpp must be 1, 2, 4, or 8")
            requested_colors = (
                tuple(resize_indexed_color_slots(self.indexed_protected_colors, requested_bpp))
                if indexed_protected_colors is None
                else _normalize_protected_color_slots(indexed_protected_colors, requested_bpp)
            )
            requested_interval = self.indexed_keyframe_interval if indexed_keyframe_interval is None else indexed_keyframe_interval
            if not 1 <= requested_interval <= 3600:
                raise ValueError("indexed_keyframe_interval must be between 1 and 3600")
            if (
                requested_bpp != self.indexed_bpp
                or requested_colors != self.indexed_protected_colors
                or requested_interval != self.indexed_keyframe_interval
            ):
                self.indexed_bpp = requested_bpp
                self.indexed_protected_colors = requested_colors
                self.indexed_keyframe_interval = requested_interval
                self._indexed_encoder = self._create_indexed_encoder()
                self._clear_frame_cache()
                self.request_full_refresh()
        if packet_burst_size is not None:
            if packet_burst_size < 0:
                raise ValueError("packet_burst_size must be non-negative")
            self.packet_burst_size = packet_burst_size
        if packet_gap_seconds is not None:
            if packet_gap_seconds < 0:
                raise ValueError("packet_gap_seconds must be non-negative")
            self.packet_gap_seconds = packet_gap_seconds
        if frame_end_repeats is not None:
            if frame_end_repeats < 1:
                raise ValueError("frame_end_repeats must be at least 1")
            self.frame_end_repeats = frame_end_repeats
        if full_refresh_tile_repeats is not None:
            if full_refresh_tile_repeats < 1:
                raise ValueError("full_refresh_tile_repeats must be at least 1")
            self.full_refresh_tile_repeats = full_refresh_tile_repeats
        if full_refresh_interval is not None:
            if full_refresh_interval < 0:
                raise ValueError("full_refresh_interval must be non-negative")
            self.full_refresh_interval = full_refresh_interval

    def _ordered_tiles(self, tiles: list, repeat_index: int) -> list:
        if len(tiles) <= 1:
            return tiles
        offset = ((self.frame_id - 1) * 17 + repeat_index * 37) % len(tiles)
        return tiles[offset:] + tiles[:offset]

    def _send_display_mode_if_needed(self) -> None:
        firmware_transport_mode = self._firmware_transport_mode()
        display_mode = (
            self.unified_flush,
            self.nack_enabled,
            self.esp_log_enabled,
            self.backlight_percent,
            firmware_transport_mode,
            self.transport_epoch,
            self.panel_gamma,
            self.orientation,
        )
        transport_acknowledged = (
            self._acknowledged_transport_mode == firmware_transport_mode
            and self._acknowledged_transport_epoch == self.transport_epoch
        )
        if self.dry_run or self.sock is None:
            return
        now = self.clock()
        if self._sent_display_mode == display_mode and (
            transport_acknowledged or now - self._last_display_mode_sent_at < 0.2
        ):
            return
        self.sock.sendto(
            DisplayModePacket(
                self.unified_flush,
                self.nack_enabled,
                self.esp_log_enabled,
                self.backlight_percent,
                firmware_transport_mode,
                self.transport_epoch,
                PANEL_GAMMA_VALUES[self.panel_gamma],
                DISPLAY_ORIENTATION_LANDSCAPE if self.orientation == LANDSCAPE else DISPLAY_ORIENTATION_PORTRAIT,
            ).encode(),
            self.target,
        )
        self._sent_display_mode = display_mode
        self._last_display_mode_sent_at = now

    def handle_transport_status(self, transport_mode: int, transport_epoch: int) -> None:
        with self._send_lock:
            self._acknowledged_transport_mode = transport_mode
            self._acknowledged_transport_epoch = transport_epoch
            if _transport_epoch_is_stale_for_peer(self.transport_epoch, transport_epoch):
                self.transport_epoch = (transport_epoch + 1) & 0xFFFFFFFF
                if self.transport_epoch == 0:
                    self.transport_epoch = 1
                self._sent_display_mode = None
                self._indexed_encoder.reset()
                self._clear_frame_cache()
                self.request_full_refresh()

    def _firmware_transport_mode(self) -> int:
        if self.transport_mode == "mjpeg":
            return TRANSPORT_MJPEG
        if self.transport_mode == "indexed":
            return TRANSPORT_INDEXED
        return TRANSPORT_TILES

    def _set_latest_image(self, image: Image.Image) -> None:
        with self._latest_mjpeg_lock:
            self._latest_image = image

    def _mjpeg_bytes_for_image(self, image: Image.Image) -> bytes:
        if (
            self._mjpeg_encode_cache_image is image
            and self._mjpeg_encode_cache_quality == self.mjpeg_quality
            and self._mjpeg_encode_cache_subsampling == self.mjpeg_subsampling
            and self._mjpeg_encode_cache_jpeg is not None
        ):
            return self._mjpeg_encode_cache_jpeg
        jpeg = image_to_jpeg_bytes(image, quality=self.mjpeg_quality, subsampling=self.mjpeg_subsampling)
        if len(jpeg) > 0xFFFF:
            raise ValueError("jpeg frame must fit in 16-bit protocol raw_len")
        self._mjpeg_encode_cache_image = image
        self._mjpeg_encode_cache_quality = self.mjpeg_quality
        self._mjpeg_encode_cache_subsampling = self.mjpeg_subsampling
        self._mjpeg_encode_cache_jpeg = jpeg
        return jpeg

    def _cache_tile(self, frame_id: int, tile_id: int, encoded: bytes) -> None:
        self._encoded_tile_cache.setdefault(frame_id, {})[tile_id] = encoded
        self._trim_frame_cache(frame_id)

    def _cache_frame_end(self, frame_id: int, encoded: bytes) -> None:
        self._frame_end_cache[frame_id] = encoded
        self._trim_frame_cache(frame_id)

    def _cache_jpeg_chunk(self, frame_id: int, chunk_id: int, encoded: bytes) -> None:
        self._encoded_jpeg_chunk_cache.setdefault(frame_id, {})[chunk_id] = encoded
        self._trim_frame_cache(frame_id)

    def _trim_frame_cache(self, frame_id: int) -> None:
        for cached_frame_id in list(self._encoded_tile_cache):
            if cached_frame_id < frame_id - 4:
                del self._encoded_tile_cache[cached_frame_id]
        for cached_frame_id in list(self._encoded_jpeg_chunk_cache):
            if cached_frame_id < frame_id - 4:
                del self._encoded_jpeg_chunk_cache[cached_frame_id]
        for cached_frame_id in list(self._frame_end_cache):
            if cached_frame_id < frame_id - 4:
                del self._frame_end_cache[cached_frame_id]

    def _clear_frame_cache(self) -> None:
        self._encoded_tile_cache.clear()
        self._encoded_jpeg_chunk_cache.clear()
        self._frame_end_cache.clear()

    def _clear_mjpeg_encode_cache(self) -> None:
        self._mjpeg_encode_cache_image = None
        self._mjpeg_encode_cache_quality = None
        self._mjpeg_encode_cache_jpeg = None


def _split_jpeg_bytes(
    jpeg: bytes,
    frame_id: int,
    size: tuple[int, int],
    chunk_size: int = JPEG_CHUNK_PAYLOAD_SIZE,
) -> list[EncodedJpegChunk]:
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    if len(jpeg) > 0xFFFF:
        raise ValueError("jpeg frame must fit in 16-bit protocol raw_len")
    width, height = size
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
