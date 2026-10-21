from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, field, fields
from threading import RLock
from time import time
from typing import Iterator

from espagent_demo.version import APP_VERSION


INDEXED_BPP_VALUES = frozenset({1, 2, 4, 8})


def indexed_palette_capacity(bpp: int) -> int:
    if bpp not in INDEXED_BPP_VALUES:
        raise ValueError("indexed bpp must be 1, 2, 4, or 8")
    return 1 << bpp


def resize_indexed_color_slots(
    colors: list[str | None] | tuple[str | None, ...],
    bpp: int,
) -> list[str | None]:
    capacity = indexed_palette_capacity(bpp)
    return list(colors[:capacity]) + [None] * max(0, capacity - len(colors))


@dataclass
class HermesState:
    online: bool = True
    status: str = "idle"
    last_reply: str = "等待 Hermes 回复"
    audio_status: str = "disabled"
    audio_stream_id: int = 0
    audio_queue_depth: int = 0
    audio_units: int = 0
    audio_bytes: int = 0
    audio_error: str | None = None
    voice_enabled: bool = False
    voice_supported: bool = False
    voice_available: bool = False
    voice_audio_available: bool = False
    voice_stt_available: bool = False
    voice_status: str = "disabled"
    voice_transcript: str = ""
    voice_error: str | None = None
    voice_details: str = ""
    voice_silence_seconds: float = 2.0
    streaming: bool = False
    activity: str = "待命"
    active_tool: str | None = None
    tool_progress: str | None = None
    interim_message: str | None = None
    session_id: str | None = None
    last_event_at: float | None = None
    input_active: bool = False
    input_available: bool = False
    input_method: str = "Microsoft Pinyin"
    input_draft: str = ""
    input_caret: int = 0
    input_preedit: str = ""
    input_candidates: list[str] = field(default_factory=list)
    input_candidate_index: int = 0
    input_candidate_page: int = 0
    input_error: str | None = None
    input_voice_active: bool = False
    input_voice_revision: int = 0
    last_user_message: str = ""


@dataclass
class LightState:
    entity_id: str = "light.demo_light"
    name: str = "客厅主灯"
    online: bool = False
    on: bool = False
    brightness: int = 0
    color_temp_kelvin: int | None = None
    min_color_temp_kelvin: int = 2000
    max_color_temp_kelvin: int = 6500
    last_synced_at: float | None = None
    error: str | None = None


@dataclass
class LinkState:
    transport_mode: str = "indexed"
    scene_transport_override: str | None = None
    transport_epoch: int = 0
    esp_transport_mode: str | None = None
    esp_transport_epoch: int = 0
    esp32_online: bool = False
    esp32_ip: str | None = None
    fps: float = 0.0
    frame_id: int = 0
    displayed_frame_id: int = 0
    display_fps: float = 0.0
    perf_window_ms: int = 0
    perf_frames: int = 0
    decode_avg_us: int = 0
    decode_max_us: int = 0
    lcd_avg_us: int = 0
    lcd_max_us: int = 0
    frame_avg_us: int = 0
    frame_max_us: int = 0
    jpeg_queued_frames: int = 0
    jpeg_incomplete_frames: int = 0
    jpeg_dropped_frames: int = 0
    jpeg_worker_frame_id: int = 0
    jpeg_worker_stage: int = 0
    indexed_queued_frames: int = 0
    indexed_incomplete_frames: int = 0
    indexed_dropped_frames: int = 0
    indexed_no_buffer_frames: int = 0
    indexed_queue_depth: int = 0
    indexed_worker_frame_id: int = 0
    indexed_worker_stage: int = 0
    preview_fps: float = 0.0
    preview_fps_limited: bool = False
    preview_fps_limit: int = 30
    dirty_tiles: int = 0
    rx_packets: int = 0
    accepted_packets: int = 0
    rejected_packets: int = 0
    draw_failed_packets: int = 0
    frame_end_packets: int = 0
    flushed_frames: int = 0
    stale_frame_end_packets: int = 0
    unified_flush: bool = True
    compression_mode: str = "auto"
    nack_enabled: bool = False
    resend_frame_end_on_nack: bool = True
    esp_log_enabled: bool = False
    backlight_percent: int = 100
    panel_gamma: str = "1.8"
    tile_width: int = 120
    tile_height: int = 6
    mjpeg_quality: int = 90
    mjpeg_subsampling: str = "420"
    indexed_bpp: int = 2
    indexed_protected_colors: list[str | None] = field(
        default_factory=lambda: [None] * indexed_palette_capacity(2)
    )
    indexed_keyframe_interval: int = 60
    video_fast_enabled: bool = False
    packet_burst_size: int = 1
    packet_gap_ms: float = 3.0
    frame_end_repeats: int = 3
    full_refresh_tile_repeats: int = 2
    full_refresh_interval: int = 10
    last_status_at: float | None = None
    error: str | None = None


@dataclass
class DuotoneTestState:
    background: str = "#FFFFFF"
    text_color: str = "#111111"
    text: str = "Aa 汉字 123 字号排版"
    preset: str = "paper"


@dataclass
class DisplayToneState:
    enabled: bool = False
    preset: str = "off"
    contrast_percent: int = 100
    brightness_percent: int = 100
    color_percent: int = 100
    sharpness_percent: int = 100
    binary_threshold: int = 0


@dataclass
class DisplayUiState:
    theme: str = "first"
    orientation: str = "portrait"
    font_chinese: str = "auto"
    font_latin: str = "auto"
    font_numeric: str = "auto"
    scale_percent: int = 100
    motion_enabled: bool = True
    font_hinting: str = "auto"
    grayscale_antialiasing: bool = False
    scroll_delta: int = 0
    scroll_revision: int = 0
    keyboard_control_enabled: bool = False
    global_hotkeys_available: bool = False
    shortcut_toggle_modifier: str = "alt"
    shortcut_toggle_key: str = "backquote"
    shortcut_navigation_modifier: str = "none"
    shortcut_scroll_modifier: str = "alt"
    shortcut_navigation_mode: str = "none"
    shortcut_navigation_prefix: str = "chord:vk:18:Alt"
    shortcut_scroll_mode: str = "custom"
    shortcut_scroll_prefix: str = "chord:vk:18:Alt"
    shortcut_voice_modifier: str = "ctrl_alt"
    shortcut_voice_key: str = "v"
    shortcut_exit_mode: str = "same"
    shortcut_exit_modifier: str = "alt"
    shortcut_exit_key: str = "backquote"


@dataclass
class DemoState:
    software_version: str = APP_VERSION
    active_scene: str = "hermes"
    active_module_id: str | None = None
    test_pattern: str = "colorbars"
    hermes: HermesState = field(default_factory=HermesState)
    light: LightState = field(default_factory=LightState)
    link: LinkState = field(default_factory=LinkState)
    duotone: DuotoneTestState = field(default_factory=DuotoneTestState)
    display_tone: DisplayToneState = field(default_factory=DisplayToneState)
    display_ui: DisplayUiState = field(default_factory=DisplayUiState)
    modules: dict[str, dict[str, object]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Runtime-only synchronization. It is intentionally not a dataclass
        # field, so FastAPI serialization never exposes or copies the lock.
        self._lock = RLock()

    @contextmanager
    def locked(self) -> Iterator[DemoState]:
        with self._lock:
            yield self

    def snapshot(self) -> DemoState:
        with self._lock:
            values = {item.name: deepcopy(getattr(self, item.name)) for item in fields(self)}
        return DemoState(**values)

    def mark_status_seen(self, ip: str) -> None:
        with self._lock:
            self.link.esp32_online = True
            self.link.esp32_ip = ip
            self.link.last_status_at = time()

    def expire_status(self, max_age_seconds: float, *, now: float | None = None) -> bool:
        with self._lock:
            last_seen = self.link.last_status_at
            current = time() if now is None else now
            if not self.link.esp32_online or last_seen is None or current - last_seen <= max_age_seconds:
                return False
            self.link.esp32_online = False
            self.display_ui.keyboard_control_enabled = False
            return True

    def update_module_state(self, module_id: str, state: dict[str, object]) -> None:
        with self._lock:
            self.modules[module_id] = deepcopy(state)

    def update_link_counters(
        self,
        *,
        rx_packets: int,
        accepted_packets: int,
        rejected_packets: int,
        draw_failed_packets: int,
        frame_end_packets: int | None = None,
        flushed_frames: int | None = None,
        stale_frame_end_packets: int | None = None,
    ) -> None:
        with self._lock:
            self.link.rx_packets = rx_packets
            self.link.accepted_packets = accepted_packets
            self.link.rejected_packets = rejected_packets
            self.link.draw_failed_packets = draw_failed_packets
            if frame_end_packets is not None:
                self.link.frame_end_packets = frame_end_packets
            if flushed_frames is not None:
                self.link.flushed_frames = flushed_frames
            if stale_frame_end_packets is not None:
                self.link.stale_frame_end_packets = stale_frame_end_packets
