from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import queue
import socket
import subprocess
import threading
import time
from io import BytesIO
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4
from typing import AsyncIterator, Protocol

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from espagent_demo.config import Settings
from espagent_demo.version import APP_VERSION
from espagent_demo.display_worker import DisplayStreamWorker
from espagent_demo.fish_audio import FishAudioConfig, HermesSpeechController
from espagent_demo.firmware_flash import (
    DisplayGpioInvalid,
    FirmwareFlashBusy,
    FirmwareFlashService,
    FirmwareFlashUnavailable,
    WifiProvisioningInvalid,
)
from espagent_demo.ha_adapter import HomeAssistantLightAdapter
from espagent_demo.ha_sync import HomeAssistantStateWorker
from espagent_demo.hermes_adapter import DemoHermesAdapter, HermesEvent, WslHermesAdapter
from espagent_demo.global_hotkeys import (
    is_supported_shortcut_key,
    is_supported_shortcut_modifier,
)
from espagent_demo.module_runtime import ModuleRegistry, discover_module_definitions
from espagent_demo.paths import app_resource_dir, project_resource_dir, user_data_dir
from espagent_demo.renderer import image_to_rgb565_bytes
from espagent_demo.state import (
    DemoState,
    LightState,
    indexed_palette_capacity,
    resize_indexed_color_slots,
)
from espagent_demo.status_link import StatusReceiver
from espagent_demo.udp_link import FrameStreamer
from espagent_demo.video_source import VideoFrameSource
from espagent_demo.voice_input import HermesVoiceController
from espagent_demo.web_renderer import WebDisplayRenderer

logger = logging.getLogger(__name__)
APP_RESOURCE_DIR = app_resource_dir()
STATIC_INDEX = APP_RESOURCE_DIR / "static" / "index.html"
DISPLAY_UI_DIR = APP_RESOURCE_DIR / "display_ui"
DESKTOP_UI_DIR = APP_RESOURCE_DIR / "desktop_ui"
MODULES_DIR = APP_RESOURCE_DIR / "modules"
LIGHT_STRIP_MODULE_ID = "builtin.ws2812b"
LIGHT_STRIP_API_VERSION = 1
LIGHT_STRIP_ACTIONS = {
    "refresh": {"risk": "read", "fields": {}},
    "set_power": {"risk": "control", "fields": {"power": "boolean"}},
    "set_brightness": {"risk": "control", "fields": {"brightness": "integer 0..100"}},
    "set_color": {"risk": "control", "fields": {"color": "hex #RRGGBB"}},
    "set_state": {
        "risk": "control",
        "fields": {
            "power": "optional boolean",
            "brightness": "optional integer 0..100",
            "color": "optional hex #RRGGBB",
        },
    },
    "set_effect": {"risk": "control", "fields": {"preset_id": "effect preset id from state"}},
    "set_led_count": {"risk": "configuration", "fields": {"led_count": "integer 1..1200"}},
    "configure": {"risk": "configuration", "fields": {"endpoint": "WLED HTTP URL"}},
    "set_firmware_target": {
        "risk": "administrative",
        "fields": {"target": "firmware target id from state"},
    },
    "provision_wifi": {
        "risk": "administrative",
        "fields": {"port": "optional serial port", "ssid": "string", "password": "string"},
    },
    "flash_firmware": {
        "risk": "administrative",
        "fields": {"port": "optional serial port"},
    },
}
MODULE_SETTINGS_PATH = Path(
    os.environ.get("ESPAGENT_MODULE_SETTINGS")
    or user_data_dir() / "modules.json"
)
PROJECT_ROOT = project_resource_dir()
VIDEO_TEST_PATH = PROJECT_ROOT / "test" / "video" / "test.mp4"
CHROME_PROFILE_DIR = user_data_dir() / "chrome_profile"
CHROME_WEBRTC_ARGS = [
    "--new-window",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-features=WebRtcHideLocalIpsWithMdns",
    "--force-webrtc-ip-handling-policy=default_public_and_private_interfaces",
]
DISPLAY_STREAM_RENDER_INTERVAL = 1.0 / 45.0
DISPLAY_STREAM_INDEXED_RENDER_INTERVAL = 1.0 / 30.0
DISPLAY_STREAM_MJPEG_MAX_FPS = 30.0
DISPLAY_PREVIEW_POLL_INTERVAL = 1.0 / 240.0
DISPLAY_STREAM_KEYFRAMES = 6
DISPLAY_UI_FONT_ROLES = {
    "chinese": frozenset({"auto", "modern", "ui", "editorial", "humanist", "pixel"}),
    "latin": frozenset({"auto", "modern", "ui", "editorial", "industrial", "pixel"}),
    "numeric": frozenset({"auto", "modern", "editorial", "industrial", "mono", "pixel"}),
}
DISPLAY_UI_THEME_FONT_ROLES = {
    "first": DISPLAY_UI_FONT_ROLES,
    "second": {
        **DISPLAY_UI_FONT_ROLES,
        "chinese": DISPLAY_UI_FONT_ROLES["chinese"] - {"humanist"},
    },
}
DISPLAY_UI_FONT_PRESETS = {
    "auto": {"chinese": "auto", "latin": "auto", "numeric": "auto"},
    "modern": {"chinese": "modern", "latin": "modern", "numeric": "modern"},
    "ui": {"chinese": "ui", "latin": "ui", "numeric": "modern"},
    "editorial": {"chinese": "editorial", "latin": "editorial", "numeric": "editorial"},
    "humanist": {"chinese": "humanist", "latin": "editorial", "numeric": "modern"},
    "industrial": {"chinese": "modern", "latin": "industrial", "numeric": "industrial"},
    "pixel": {"chinese": "pixel", "latin": "pixel", "numeric": "pixel"},
}
DISPLAY_STREAM_TRANSPORT_OPTIONS = {
    "compression_mode": "auto",
    "resend_frame_end_on_nack": True,
    "tile_width": 80,
    "tile_height": 8,
    "packet_burst_size": 1,
    "packet_gap_seconds": 0.001,
    "frame_end_repeats": 2,
    "full_refresh_tile_repeats": 1,
    "full_refresh_interval": 0,
}


class Renderer(Protocol):
    def render(self, state: DemoState) -> object:
        ...


class Streamer(Protocol):
    previous: object | None

    def render_and_send(self, renderer: Renderer, state: DemoState) -> object:
        ...

    def request_full_refresh(self, frames: int = 3) -> None:
        ...

    def set_unified_flush(self, enabled: bool) -> None:
        ...

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
        indexed_protected_colors: list[str | None] | None = None,
        indexed_keyframe_interval: int | None = None,
        packet_burst_size: int | None = None,
        packet_gap_seconds: float | None = None,
        frame_end_repeats: int | None = None,
        full_refresh_tile_repeats: int | None = None,
        full_refresh_interval: int | None = None,
    ) -> None:
        ...

    def close(self) -> None:
        ...


class LightAdapter(Protocol):
    def health(self) -> dict[str, object]:
        ...

    def set_light(
        self,
        on: bool,
        brightness: int | None = None,
        color_temp_kelvin: int | None = None,
    ) -> None:
        ...

    def get_state(self) -> LightState:
        ...

    def close(self) -> None:
        ...


class HermesAdapter(Protocol):
    def health(self) -> dict[str, object]:
        ...

    def ask(self, prompt: str, on_event: object | None = None) -> str:
        ...

    def new_session(self) -> str:
        ...

    def interrupt(self) -> str:
        ...

    def voice_status(self) -> dict[str, object]:
        ...


@dataclass
class DemoHostDependencies:
    state: DemoState
    renderer: Renderer
    streamer: Streamer
    ha: LightAdapter
    hermes: HermesAdapter
    status_receiver: StatusReceiver | None = None
    display_worker: DisplayStreamWorker | None = None
    ha_reconnect_max_seconds: float | None = None
    ha_sync_worker: HomeAssistantStateWorker | None = None
    speech: HermesSpeechController | None = None
    voice: HermesVoiceController | None = None
    modules: ModuleRegistry | None = None
    firmware_flasher: FirmwareFlashService | None = None


class HermesRequest(BaseModel):
    prompt: str


class HermesInputStateRequest(BaseModel):
    active: bool
    available: bool = True
    method: str = "Microsoft Pinyin"
    draft: str = ""
    caret: int = 0
    preedit: str = ""
    candidates: list[str] = Field(default_factory=list)
    candidate_index: int = 0
    candidate_page: int = 0
    error: str | None = None
    voice_active: bool = False


class VoiceEnabledRequest(BaseModel):
    enabled: bool


class WifiProvisioningRequest(BaseModel):
    ssid: str
    password: str = ""


class DisplayGpioRequest(BaseModel):
    scl: int = Field(ge=0, le=48)
    sda: int = Field(ge=0, le=48)
    cs: int = Field(ge=0, le=48)
    dc: int = Field(ge=0, le=48)
    rst: int = Field(ge=0, le=48)
    bl: int = Field(ge=0, le=48)


class VoiceSilenceRequest(BaseModel):
    seconds: float


class LightRequest(BaseModel):
    on: bool
    brightness: int | None = None
    color_temp_kelvin: int | None = None


class ModuleActivateRequest(BaseModel):
    target: str = "app_and_esp32"


class ModuleActionRequest(BaseModel):
    payload: dict[str, object] = Field(default_factory=dict)


class ModuleEnabledRequest(BaseModel):
    enabled: bool


class UnifiedFlushRequest(BaseModel):
    enabled: bool


class TransportOptionsRequest(BaseModel):
    transport_mode: str | None = None
    compression_mode: str | None = None
    nack_enabled: bool | None = None
    resend_frame_end_on_nack: bool | None = None
    esp_log_enabled: bool | None = None
    backlight_percent: int | None = None
    panel_gamma: str | None = None
    tile_width: int | None = None
    tile_height: int | None = None
    mjpeg_quality: int | None = None
    mjpeg_subsampling: str | None = None
    indexed_bpp: int | None = None
    indexed_protected_colors: list[str | None] | None = None
    indexed_keyframe_interval: int | None = None
    preview_fps_limited: bool | None = None
    preview_fps_limit: int | None = None


class DuotonePatternRequest(BaseModel):
    background: str | None = None
    text_color: str | None = None
    text: str | None = None
    preset: str | None = None


class DisplayToneRequest(BaseModel):
    enabled: bool | None = None
    preset: str | None = None
    contrast_percent: int | None = None
    brightness_percent: int | None = None
    color_percent: int | None = None
    sharpness_percent: int | None = None
    binary_threshold: int | None = None


class DisplayUiRequest(BaseModel):
    theme: str | None = None
    orientation: str | None = None
    font: str | None = None
    font_chinese: str | None = None
    font_latin: str | None = None
    font_numeric: str | None = None
    scale_percent: int | None = None
    motion_enabled: bool | None = None
    font_hinting: str | None = None
    grayscale_antialiasing: bool | None = None


class DisplayScrollRequest(BaseModel):
    delta: int


class DisplayControlRequest(BaseModel):
    enabled: bool | None = None
    global_hotkeys_available: bool | None = None


class ShortcutSettingsRequest(BaseModel):
    toggle_modifier: str | None = None
    toggle_key: str | None = None
    navigation_modifier: str | None = None
    scroll_modifier: str | None = None
    navigation_mode: str | None = None
    navigation_prefix: str | None = None
    scroll_mode: str | None = None
    scroll_prefix: str | None = None
    voice_modifier: str | None = None
    voice_key: str | None = None
    exit_mode: str | None = None
    exit_modifier: str | None = None
    exit_key: str | None = None


class SessionDescriptionRequest(BaseModel):
    sdp: str
    type: str


class IceCandidate(BaseModel):
    foundation: str
    protocol: str
    address: str
    port: int
    type: str


def create_runtime_dependencies(settings: Settings | None = None) -> DemoHostDependencies:
    settings = settings or Settings()
    hermes: HermesAdapter
    if settings.hermes_mode == "real":
        hermes = WslHermesAdapter(
            settings.hermes_wsl_distro,
            settings.hermes_command,
            timeout_seconds=settings.hermes_timeout_seconds,
            stt_language=settings.hermes_stt_language,
            stt_timeout_seconds=settings.hermes_stt_timeout_seconds,
        )
    else:
        hermes = DemoHermesAdapter()
    state = DemoState()
    modules = ModuleRegistry.discover(
        MODULES_DIR,
        on_state=state.update_module_state,
        config_path=MODULE_SETTINGS_PATH,
    )
    streamer = FrameStreamer(
        settings.esp32_target_ip,
        settings.esp32_display_port,
        mjpeg_max_fps=DISPLAY_STREAM_MJPEG_MAX_FPS,
    )
    streamer.set_transport_options(
        transport_mode=state.link.transport_mode,
        nack_enabled=state.link.nack_enabled,
        esp_log_enabled=state.link.esp_log_enabled,
        backlight_percent=state.link.backlight_percent,
        panel_gamma=state.link.panel_gamma,
        mjpeg_quality=state.link.mjpeg_quality,
        mjpeg_subsampling=state.link.mjpeg_subsampling,
        indexed_protected_colors=state.link.indexed_protected_colors,
        **DISPLAY_STREAM_TRANSPORT_OPTIONS,
    )
    state.link.transport_epoch = streamer.transport_epoch
    _apply_display_stream_state(state)
    video_source = VideoFrameSource(VIDEO_TEST_PATH)
    dependencies = DemoHostDependencies(
        state=state,
        renderer=WebDisplayRenderer(video_source=video_source, module_scenes=modules),
        streamer=streamer,
        ha=HomeAssistantLightAdapter(
            settings.home_assistant_url,
            settings.home_assistant_token,
            settings.home_assistant_light_entity,
        ),
        hermes=hermes,
        status_receiver=StatusReceiver(
            settings.esp32_status_port,
            state,
            transport_handler=streamer.handle_transport_status,
            nack_handler=streamer.handle_nack,
            displayed_handler=streamer.mark_displayed,
            target_handler=streamer.set_target_ip,
        ),
        ha_reconnect_max_seconds=settings.home_assistant_reconnect_max_seconds,
        modules=modules,
        firmware_flasher=FirmwareFlashService(
            PROJECT_ROOT / "firmware" / "releases" / "esp32_display"
        ),
    )
    dependencies.speech = HermesSpeechController(
        state,
        FishAudioConfig(
            enabled=settings.fishaudio_enabled,
            api_key=settings.fishaudio_api_key,
            voice_id=settings.fishaudio_voice_id,
            realtime_url=settings.fishaudio_realtime_url,
            sample_rate=settings.fishaudio_sample_rate,
            speed=settings.fishaudio_speed,
            volume=settings.fishaudio_volume,
            language=settings.fishaudio_language,
            chunk_length=settings.fishaudio_chunk_length,
            latency=settings.fishaudio_latency,
        ),
    )
    if callable(getattr(hermes, "wait_voice_event", None)):
        dependencies.voice = HermesVoiceController(
            state,
            hermes,
            lambda transcript: _execute_voice_transcript(dependencies, transcript),
            silence_duration_seconds=settings.hermes_voice_silence_seconds,
        )
    else:
        with state.locked():
            state.hermes.voice_supported = False
            state.hermes.voice_status = "unavailable"
            state.hermes.voice_details = "需要配置外部 Hermes 服务后才能使用语音输入"
    return dependencies


def create_app(deps: DemoHostDependencies | None = None, *, render_interval: float = DISPLAY_STREAM_RENDER_INTERVAL) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.deps = deps if deps is not None else create_runtime_dependencies()
        modules = getattr(app.state.deps, "modules", None) or ModuleRegistry()
        app.state.deps.modules = modules
        modules.start_all()
        status_receiver = getattr(app.state.deps, "status_receiver", None)
        if status_receiver is not None:
            status_receiver.start()
        hermes_start = getattr(app.state.deps.hermes, "start", None)
        if callable(hermes_start):
            try:
                hermes_start()
            except Exception:
                logger.exception("Hermes gateway startup failed")
        voice = getattr(app.state.deps, "voice", None)
        if voice is not None:
            try:
                voice.refresh_status()
            except Exception:
                logger.exception("Hermes voice capability refresh failed")
            voice.start()
        display_worker = DisplayStreamWorker(
            app.state.deps.renderer,
            app.state.deps.streamer,
            app.state.deps.state,
            interval_seconds=render_interval,
            indexed_interval_seconds=max(render_interval, DISPLAY_STREAM_INDEXED_RENDER_INTERVAL),
        )
        app.state.deps.display_worker = display_worker
        display_worker.start()
        ha_sync_worker = None
        ha_reconnect_max = getattr(app.state.deps, "ha_reconnect_max_seconds", None)
        if ha_reconnect_max is not None:
            ha_sync_worker = HomeAssistantStateWorker(
                app.state.deps.ha,
                app.state.deps.state,
                reconnect_max_seconds=ha_reconnect_max,
                on_change=lambda: _request_and_push_refresh(app.state.deps, frames=DISPLAY_STREAM_KEYFRAMES),
            )
            app.state.deps.ha_sync_worker = ha_sync_worker
            ha_sync_worker.start()
        try:
            yield
        finally:
            try:
                if ha_sync_worker is not None:
                    ha_sync_worker.close()
                display_worker.close()
            finally:
                _close_resource(app.state.deps.streamer, "Frame streamer")
                _close_resource(app.state.deps.renderer, "Web display renderer")
                _close_resource(app.state.deps.ha, "Home Assistant adapter")
                if voice is not None:
                    _close_resource(voice, "Hermes voice controller")
                _close_resource(app.state.deps.hermes, "Hermes adapter")
                speech = getattr(app.state.deps, "speech", None)
                if speech is not None:
                    _close_resource(speech, "Fish Audio speech controller")
                if status_receiver is not None:
                    _close_resource(status_receiver, "ESP32 status receiver")
                firmware_flasher = getattr(app.state.deps, "firmware_flasher", None)
                if firmware_flasher is not None:
                    _close_resource(firmware_flasher, "Firmware flasher")
                modules.close()

    app = FastAPI(lifespan=lifespan)
    app.mount("/screen", StaticFiles(directory=DISPLAY_UI_DIR, html=True), name="display-ui")
    app.mount("/app", StaticFiles(directory=DESKTOP_UI_DIR, html=True), name="desktop-ui")
    for definition in discover_module_definitions(MODULES_DIR):
        app_page = definition.default_app_page()
        if app_page is not None:
            app.mount(
                f"/module-assets/{definition.manifest.id}/app",
                StaticFiles(directory=definition.root / app_page.path, html=True),
                name=f"module-{definition.manifest.id}-app",
            )
        scene = definition.default_esp32_scene()
        if scene is not None:
            app.mount(
                f"/module-assets/{definition.manifest.id}/esp32",
                StaticFiles(directory=definition.root / scene.path, html=True),
                name=f"module-{definition.manifest.id}-esp32",
            )
    app.state.webrtc_offers = {}
    app.state.webrtc_answers = {}
    app.state.latest_webrtc_offer_id = None
    app.state.preview_cache_lock = threading.Lock()
    app.state.preview_jpeg_cache = None

    @app.get("/")
    def index() -> RedirectResponse:
        return RedirectResponse(url="/app/", status_code=307)

    @app.get("/debug/")
    def debug_index() -> FileResponse:
        return FileResponse(STATIC_INDEX, headers={"Cache-Control": "no-store"})

    @app.get("/api/state")
    def get_state() -> DemoState:
        return _deps(app).state.snapshot()

    @app.get("/api/health")
    def get_health() -> dict[str, object]:
        app_deps = _deps(app)
        state = app_deps.state.snapshot()
        hermes = _adapter_health(app_deps.hermes, "hermes")
        home_assistant = _adapter_health(app_deps.ha, "home_assistant")
        speech = getattr(app_deps, "speech", None)
        return {
            "ok": bool(hermes.get("ok")) and bool(home_assistant.get("ok")),
            "version": APP_VERSION,
            "services": {
                "hermes": hermes,
                "home_assistant": home_assistant,
                "fish_audio": _adapter_health(speech, "fish_audio") if speech is not None else {
                    "ok": False,
                    "service": "fish_audio",
                    "mode": "disabled",
                },
                "esp32": {
                    "ok": state.link.esp32_online,
                    "service": "esp32",
                    "ip": state.link.esp32_ip,
                },
            },
        }

    @app.get("/api/firmware/status")
    def firmware_status() -> dict[str, object]:
        flasher = getattr(_deps(app), "firmware_flasher", None)
        if flasher is None:
            return {
                "status": "unavailable",
                "phase": "unavailable",
                "progress": 0,
                "message": "当前运行模式未启用固件烧录",
                "running": False,
                "can_flash": False,
                "runtime_ready": False,
                "environment_ready": False,
                "package_ready": False,
                "project_ready": False,
                "target": "ESP32-S3",
                "ports": [],
                "device": None,
                "device_count": 0,
                "logs": [],
                "wifi": {
                    "status": "unavailable",
                    "message": "当前运行模式未启用 USB 配网",
                    "running": False,
                    "can_configure": False,
                    "ssid": None,
                    "ip": None,
                    "connected": None,
                    "device": None,
                },
                "display_gpio": {
                    "status": "unavailable",
                    "message": "当前运行模式未启用 USB 屏幕引脚设置",
                    "running": False,
                    "can_configure": False,
                    "values": {"scl": 12, "sda": 11, "cs": 10, "dc": 8, "rst": 7, "bl": 6},
                    "defaults": {"scl": 12, "sda": 11, "cs": 10, "dc": 8, "rst": 7, "bl": 6},
                    "device": None,
                },
            }
        return flasher.snapshot()

    @app.post("/api/firmware/flash", status_code=202)
    def flash_firmware() -> dict[str, object]:
        flasher = getattr(_deps(app), "firmware_flasher", None)
        if flasher is None:
            raise HTTPException(status_code=503, detail="当前运行模式未启用固件烧录")
        try:
            return flasher.start_flash()
        except FirmwareFlashBusy as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except FirmwareFlashUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.post("/api/firmware/wifi", status_code=202)
    def provision_wifi(request: WifiProvisioningRequest) -> dict[str, object]:
        flasher = getattr(_deps(app), "firmware_flasher", None)
        if flasher is None:
            raise HTTPException(status_code=503, detail="当前运行模式未启用 USB 配网")
        try:
            return flasher.start_wifi_provisioning(request.ssid, request.password)
        except WifiProvisioningInvalid as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except FirmwareFlashBusy as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except FirmwareFlashUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.post("/api/firmware/display-gpio/read", status_code=202)
    def read_display_gpio() -> dict[str, object]:
        flasher = getattr(_deps(app), "firmware_flasher", None)
        if flasher is None:
            raise HTTPException(status_code=503, detail="当前运行模式未启用 USB 屏幕引脚设置")
        try:
            return flasher.start_display_gpio_read()
        except FirmwareFlashBusy as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except FirmwareFlashUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.post("/api/firmware/display-gpio", status_code=202)
    def configure_display_gpio(request: DisplayGpioRequest) -> dict[str, object]:
        flasher = getattr(_deps(app), "firmware_flasher", None)
        if flasher is None:
            raise HTTPException(status_code=503, detail="当前运行模式未启用 USB 屏幕引脚设置")
        try:
            return flasher.start_display_gpio_configuration(request.model_dump())
        except DisplayGpioInvalid as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except FirmwareFlashBusy as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except FirmwareFlashUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.get("/api/modules")
    def get_modules() -> dict[str, object]:
        return {"modules": _module_registry(app).list_modules()}

    @app.get("/api/modules/{module_id}/state")
    def get_module_state(module_id: str) -> dict[str, object]:
        try:
            return _module_registry(app).get_state(module_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/modules/{module_id}/enabled")
    def set_module_enabled(module_id: str, request: ModuleEnabledRequest) -> dict[str, object]:
        registry = _module_registry(app)
        app_deps = _deps(app)
        if not request.enabled and app_deps.state.active_module_id == module_id:
            with app_deps.state.locked():
                _set_active_scene(app_deps.state, "link")
            _apply_display_stream_profile(app_deps)
            _request_and_push_refresh(app_deps, frames=DISPLAY_STREAM_KEYFRAMES)
        try:
            module = registry.set_enabled(module_id, request.enabled)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except OSError as exc:
            raise HTTPException(status_code=503, detail=f"module settings could not be saved: {exc}") from exc

        return {"module": module}

    @app.post("/api/modules/{module_id}/actions/{action}")
    def invoke_module_action(
        module_id: str,
        action: str,
        request: ModuleActionRequest | None = None,
    ) -> dict[str, object]:
        try:
            result = _module_registry(app).invoke(module_id, action, request.payload if request else {})
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        app_deps = _deps(app)
        if app_deps.state.active_module_id == module_id:
            _apply_active_module_display_profile(app_deps)
            _request_and_push_refresh(app_deps, frames=1)
        return {"module_id": module_id, "action": action, "result": result}

    @app.get("/api/light-strip")
    def get_light_strip_api() -> dict[str, object]:
        try:
            module = _module_registry(app).get_state(LIGHT_STRIP_MODULE_ID)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="light strip module is unavailable") from exc
        return {
            "api_version": LIGHT_STRIP_API_VERSION,
            "module_id": LIGHT_STRIP_MODULE_ID,
            "actions": LIGHT_STRIP_ACTIONS,
            "module": module,
        }

    @app.post("/api/light-strip/actions/{action}")
    def invoke_light_strip_action(
        action: str,
        request: ModuleActionRequest | None = None,
    ) -> dict[str, object]:
        if action not in LIGHT_STRIP_ACTIONS:
            raise HTTPException(status_code=400, detail=f"unsupported light strip action: {action}")
        try:
            result = _module_registry(app).invoke(
                LIGHT_STRIP_MODULE_ID,
                action,
                request.payload if request else {},
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="light strip module is unavailable") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {
            "api_version": LIGHT_STRIP_API_VERSION,
            "module_id": LIGHT_STRIP_MODULE_ID,
            "action": action,
            "result": result,
        }

    @app.post("/api/modules/{module_id}/activate")
    def activate_module(module_id: str, request: ModuleActivateRequest) -> dict[str, object]:
        if request.target not in {"app", "esp32", "app_and_esp32"}:
            raise HTTPException(status_code=400, detail="target must be app, esp32, or app_and_esp32")
        try:
            registry = _module_registry(app)
            definition = registry.definition(module_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if not registry.is_enabled(module_id):
            raise HTTPException(status_code=409, detail="module is disabled")
        scene = definition.default_esp32_scene()
        if request.target in {"esp32", "app_and_esp32"}:
            if scene is None:
                raise HTTPException(status_code=409, detail="module does not provide an ESP32 scene")
            app_deps = _deps(app)
            with app_deps.state.locked():
                _set_active_scene(app_deps.state, scene.scene_key, module_id=module_id)
            _apply_display_stream_profile(app_deps)
            _apply_active_module_display_profile(app_deps)
            _request_and_push_refresh(app_deps, frames=DISPLAY_STREAM_KEYFRAMES)
        return {
            "module_id": module_id,
            "target": request.target,
            "scene": scene.scene_key if scene is not None else None,
            "app_route": definition.app_route(),
        }

    @app.get("/api/preview.png")
    def preview_png() -> Response:
        app_deps = _deps(app)
        frame_id = app_deps.state.link.frame_id
        _record_preview_frame(app, app_deps, frame_id=frame_id)
        image = _latest_preview_image(app_deps)
        buffer = BytesIO()
        image.save(buffer, format="PNG")
        return Response(
            content=buffer.getvalue(),
            media_type="image/png",
            headers={"X-Frame-Id": str(frame_id), "Cache-Control": "no-store"},
        )

    @app.get("/api/display/frame.rgb565")
    def display_frame_rgb565() -> Response:
        app_deps = _deps(app)
        image = _latest_preview_image(app_deps)
        return Response(content=image_to_rgb565_bytes(image), media_type="application/octet-stream")

    @app.get("/api/display/mjpeg")
    def display_mjpeg(frames: int | None = None) -> StreamingResponse:
        if frames is not None and frames < 1:
            raise HTTPException(status_code=400, detail="frames must be at least 1")
        return StreamingResponse(
            _mjpeg_stream(_deps(app), max_frames=frames),
            media_type="multipart/x-mixed-replace; boundary=frame",
        )

    @app.get("/api/display/preview.jpg")
    def display_preview_jpeg() -> Response:
        app_deps = _deps(app)
        frame_id, jpeg = _cached_preview_jpeg(app, app_deps)
        headers = {}
        if frame_id is not None:
            headers["X-Frame-Id"] = str(frame_id)
        return Response(
            content=jpeg,
            media_type="image/jpeg",
            headers={**headers, "Cache-Control": "no-store"},
        )

    @app.post("/api/scene/{scene}")
    def set_scene(scene: str) -> dict[str, str]:
        if scene not in {"hermes", "lights", "link", "test", "video"}:
            raise HTTPException(status_code=400, detail="scene must be hermes, lights, link, test, or video")
        app_deps = _deps(app)
        with app_deps.state.locked():
            _set_active_scene(app_deps.state, scene)
        _apply_display_stream_profile(app_deps)
        _request_and_push_refresh(app_deps, frames=DISPLAY_STREAM_KEYFRAMES)
        return {"scene": scene}

    @app.post("/api/video/start")
    def start_video_test() -> dict[str, str]:
        app_deps = _deps(app)
        with app_deps.state.locked():
            _set_active_scene(app_deps.state, "video")
        _apply_display_stream_profile(app_deps)
        _request_and_push_refresh(app_deps, frames=DISPLAY_STREAM_KEYFRAMES)
        return {"scene": "video"}

    @app.post("/api/test-pattern/{pattern}")
    def set_test_pattern(pattern: str, request: DuotonePatternRequest | None = None) -> dict[str, object]:
        if pattern not in {"colorbars", "palette256", "channels", "colors", "grid", "checker", "duotone"}:
            raise HTTPException(
                status_code=400,
                detail="pattern must be colorbars, palette256, channels, colors, grid, checker, or duotone",
            )
        app_deps = _deps(app)
        if pattern == "duotone" and request is not None:
            if request.background is not None:
                app_deps.state.duotone.background = _normalize_hex_color(request.background, "background")
            if request.text_color is not None:
                app_deps.state.duotone.text_color = _normalize_hex_color(request.text_color, "text_color")
            if request.text is not None:
                app_deps.state.duotone.text = request.text[:80]
            if request.preset is not None:
                app_deps.state.duotone.preset = request.preset[:32]
        app_deps.state.test_pattern = pattern
        with app_deps.state.locked():
            _set_active_scene(app_deps.state, "test")
        _apply_display_stream_profile(app_deps)
        _request_and_push_refresh(app_deps, frames=DISPLAY_STREAM_KEYFRAMES)
        if pattern == "duotone":
            return {"pattern": pattern, "duotone": app_deps.state.duotone}
        return {"pattern": pattern}

    @app.post("/api/display/tone")
    def set_display_tone(request: DisplayToneRequest) -> dict[str, object]:
        app_deps = _deps(app)
        tone = app_deps.state.display_tone
        if request.enabled is not None:
            tone.enabled = request.enabled
        if request.preset is not None:
            tone.preset = request.preset[:32]
        if request.contrast_percent is not None:
            if not 50 <= request.contrast_percent <= 220:
                raise HTTPException(status_code=400, detail="contrast_percent must be between 50 and 220")
            tone.contrast_percent = request.contrast_percent
        if request.brightness_percent is not None:
            if not 50 <= request.brightness_percent <= 160:
                raise HTTPException(status_code=400, detail="brightness_percent must be between 50 and 160")
            tone.brightness_percent = request.brightness_percent
        if request.color_percent is not None:
            if not 0 <= request.color_percent <= 200:
                raise HTTPException(status_code=400, detail="color_percent must be between 0 and 200")
            tone.color_percent = request.color_percent
        if request.sharpness_percent is not None:
            if not 0 <= request.sharpness_percent <= 250:
                raise HTTPException(status_code=400, detail="sharpness_percent must be between 0 and 250")
            tone.sharpness_percent = request.sharpness_percent
        if request.binary_threshold is not None:
            if not 0 <= request.binary_threshold <= 254:
                raise HTTPException(status_code=400, detail="binary_threshold must be between 0 and 254")
            tone.binary_threshold = request.binary_threshold
        _request_and_push_refresh(app_deps, frames=DISPLAY_STREAM_KEYFRAMES)
        return {"display_tone": tone}

    @app.post("/api/display/ui")
    def set_display_ui(request: DisplayUiRequest) -> dict[str, object]:
        app_deps = _deps(app)
        display_ui = app_deps.state.display_ui
        next_theme = request.theme or display_ui.theme
        if request.theme is not None:
            if request.theme not in {"first", "second"}:
                raise HTTPException(status_code=400, detail="theme must be first or second")
        if request.orientation is not None and request.orientation not in {"portrait", "landscape"}:
            raise HTTPException(status_code=400, detail="orientation must be portrait or landscape")
        if request.font_hinting is not None and request.font_hinting not in {"auto", "crisp", "smooth"}:
            raise HTTPException(status_code=400, detail="font_hinting must be auto, crisp, or smooth")
        next_fonts = {
            "chinese": display_ui.font_chinese,
            "latin": display_ui.font_latin,
            "numeric": display_ui.font_numeric,
        }
        explicit_roles: set[str] = set()
        if request.font is not None:
            preset = DISPLAY_UI_FONT_PRESETS.get(request.font)
            if preset is None:
                raise HTTPException(status_code=400, detail="unknown display UI font preset")
            next_fonts.update(preset)
            explicit_roles.update(next_fonts)
        for role in next_fonts:
            requested = getattr(request, f"font_{role}")
            if requested is not None:
                next_fonts[role] = requested
                explicit_roles.add(role)
            if next_fonts[role] not in DISPLAY_UI_FONT_ROLES[role]:
                raise HTTPException(status_code=400, detail=f"unknown {role} display UI font")
            if next_fonts[role] not in DISPLAY_UI_THEME_FONT_ROLES[next_theme][role]:
                if role in explicit_roles:
                    raise HTTPException(status_code=400, detail=f"{role} font is not supported by {next_theme} theme")
                next_fonts[role] = "auto"
        display_ui.theme = next_theme
        if request.orientation is not None:
            display_ui.orientation = request.orientation
        display_ui.font_chinese = next_fonts["chinese"]
        display_ui.font_latin = next_fonts["latin"]
        display_ui.font_numeric = next_fonts["numeric"]
        if request.scale_percent is not None:
            if request.scale_percent not in {90, 95, 100, 105, 110}:
                raise HTTPException(status_code=400, detail="scale_percent must be 90, 95, 100, 105, or 110")
            display_ui.scale_percent = request.scale_percent
        if request.motion_enabled is not None:
            display_ui.motion_enabled = request.motion_enabled
        if request.font_hinting is not None:
            display_ui.font_hinting = request.font_hinting
        if request.grayscale_antialiasing is not None:
            display_ui.grayscale_antialiasing = request.grayscale_antialiasing
        _request_and_push_refresh(app_deps, frames=DISPLAY_STREAM_KEYFRAMES)
        return {"display_ui": display_ui}

    @app.post("/api/display/scroll")
    def scroll_display(request: DisplayScrollRequest) -> dict[str, int]:
        if request.delta == 0 or not -120 <= request.delta <= 120:
            raise HTTPException(status_code=400, detail="delta must be between -120 and 120 and must not be zero")
        app_deps = _deps(app)
        with app_deps.state.locked():
            app_deps.state.display_ui.scroll_delta = request.delta
            app_deps.state.display_ui.scroll_revision += 1
            revision = app_deps.state.display_ui.scroll_revision
            hermes = app_deps.state.hermes
            if not hermes.input_draft and not hermes.input_preedit:
                hermes.input_active = False
                hermes.input_caret = 0
                hermes.input_candidates = []
                hermes.input_candidate_index = 0
                hermes.input_candidate_page = 0
        _request_and_push_refresh(app_deps, frames=2)
        return {"delta": request.delta, "revision": revision}

    @app.post("/api/display/control")
    def set_display_control(request: DisplayControlRequest) -> dict[str, bool]:
        app_deps = _deps(app)
        with app_deps.state.locked():
            display_ui = app_deps.state.display_ui
            if request.enabled is True and not app_deps.state.link.esp32_online:
                display_ui.keyboard_control_enabled = False
                raise HTTPException(status_code=409, detail="ESP32 is offline")
            if request.enabled is not None:
                display_ui.keyboard_control_enabled = request.enabled
            if request.global_hotkeys_available is not None:
                display_ui.global_hotkeys_available = request.global_hotkeys_available
            return {
                "enabled": display_ui.keyboard_control_enabled,
                "global_hotkeys_available": display_ui.global_hotkeys_available,
            }

    @app.post("/api/settings/shortcuts")
    def set_shortcut_settings(request: ShortcutSettingsRequest) -> dict[str, object]:
        app_deps = _deps(app)
        display_ui = app_deps.state.display_ui
        validators = {
            "toggle_modifier": is_supported_shortcut_modifier,
            "toggle_key": is_supported_shortcut_key,
            "navigation_modifier": is_supported_shortcut_modifier,
            "scroll_modifier": is_supported_shortcut_modifier,
            "navigation_mode": lambda value: value in {"none", "custom", "disabled"},
            "navigation_prefix": is_supported_shortcut_key,
            "scroll_mode": lambda value: value in {"none", "custom", "disabled"},
            "scroll_prefix": is_supported_shortcut_key,
            "voice_modifier": is_supported_shortcut_modifier,
            "voice_key": is_supported_shortcut_key,
            "exit_mode": lambda value: value in {"same", "custom", "disabled"},
            "exit_modifier": is_supported_shortcut_modifier,
            "exit_key": is_supported_shortcut_key,
        }
        updates = {
            field: getattr(request, field)
            for field in validators
            if getattr(request, field) is not None
        }
        for field, value in updates.items():
            if not validators[field](value):
                raise HTTPException(status_code=400, detail=f"unsupported {field}")
        with app_deps.state.locked():
            for field, value in updates.items():
                setattr(display_ui, f"shortcut_{field}", value)
        return {
            "shortcuts": {
                "toggle_modifier": display_ui.shortcut_toggle_modifier,
                "toggle_key": display_ui.shortcut_toggle_key,
                "navigation_modifier": display_ui.shortcut_navigation_modifier,
                "scroll_modifier": display_ui.shortcut_scroll_modifier,
                "navigation_mode": display_ui.shortcut_navigation_mode,
                "navigation_prefix": display_ui.shortcut_navigation_prefix,
                "scroll_mode": display_ui.shortcut_scroll_mode,
                "scroll_prefix": display_ui.shortcut_scroll_prefix,
                "voice_modifier": display_ui.shortcut_voice_modifier,
                "voice_key": display_ui.shortcut_voice_key,
                "exit_mode": display_ui.shortcut_exit_mode,
                "exit_modifier": display_ui.shortcut_exit_modifier,
                "exit_key": display_ui.shortcut_exit_key,
            }
        }

    @app.post("/api/hermes")
    def ask_hermes(request: HermesRequest) -> dict[str, str]:
        app_deps = _deps(app)
        prompt = request.prompt.strip()
        if not prompt:
            raise HTTPException(status_code=400, detail="prompt must not be empty")
        _prepare_hermes_turn(app_deps, prompt)
        return _execute_hermes_request(app_deps, prompt)

    @app.post("/api/hermes/input/state")
    def set_hermes_input_state(request: HermesInputStateRequest) -> dict[str, object]:
        app_deps = _deps(app)
        candidates = [str(value)[:24] for value in request.candidates[:32]]
        with app_deps.state.locked():
            hermes = app_deps.state.hermes
            hermes.input_active = request.active
            hermes.input_available = request.available
            hermes.input_method = request.method[:48] or "Microsoft Pinyin"
            hermes.input_draft = request.draft[:240]
            hermes.input_caret = max(0, min(request.caret, len(hermes.input_draft)))
            hermes.input_preedit = request.preedit[:96]
            hermes.input_candidates = candidates
            hermes.input_candidate_index = max(
                0,
                min(request.candidate_index, max(0, len(candidates) - 1)),
            )
            hermes.input_candidate_page = max(0, request.candidate_page)
            hermes.input_error = request.error[:160] if request.error else None
            hermes.input_voice_active = request.voice_active
        worker = getattr(app_deps, "display_worker", None)
        if worker is not None:
            worker.wake()
        return {
            "active": request.active,
            "available": request.available,
            "candidate_count": len(candidates),
        }

    @app.post("/api/hermes/input/submit", status_code=202)
    def submit_hermes_input(request: HermesRequest) -> dict[str, object]:
        app_deps = _deps(app)
        prompt = request.prompt.strip()
        if not prompt:
            raise HTTPException(status_code=400, detail="prompt must not be empty")
        _prepare_hermes_turn(app_deps, prompt)
        with app_deps.state.locked():
            hermes = app_deps.state.hermes
            hermes.input_active = False
            hermes.input_draft = ""
            hermes.input_caret = 0
            hermes.input_preedit = ""
            hermes.input_candidates = []
            hermes.input_candidate_index = 0
            hermes.input_candidate_page = 0
            hermes.input_voice_active = False
        threading.Thread(
            target=_execute_hermes_request,
            args=(app_deps, prompt),
            name="hermes-keyboard-submit",
            daemon=True,
        ).start()
        worker = getattr(app_deps, "display_worker", None)
        if worker is not None:
            worker.wake()
        return {"accepted": True, "prompt": prompt}

    @app.post("/api/hermes/stream")
    def stream_hermes(request: HermesRequest) -> StreamingResponse:
        app_deps = _deps(app)
        prompt = request.prompt.strip()
        if not prompt:
            raise HTTPException(status_code=400, detail="prompt must not be empty")
        _prepare_hermes_turn(app_deps, prompt)

        def generate() -> object:
            event_queue: queue.Queue[dict[str, object]] = queue.Queue()

            def publish(event: HermesEvent) -> None:
                event_queue.put({"type": event.type, "payload": event.payload})

            def run() -> None:
                result = _execute_hermes_request(app_deps, prompt, event_sink=publish)
                event_queue.put({"type": "done", "payload": result})

            threading.Thread(target=run, name="hermes-http-stream", daemon=True).start()
            while True:
                event = event_queue.get()
                yield f"event: hermes\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
                if event["type"] == "done":
                    break

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/hermes/session/new")
    def new_hermes_session() -> dict[str, object]:
        app_deps = _deps(app)
        _interrupt_hermes_and_audio(app_deps)
        create_session = getattr(app_deps.hermes, "new_session", None)
        if not callable(create_session):
            raise HTTPException(status_code=501, detail="Hermes adapter does not support sessions")
        try:
            session_id = create_session()
        except Exception as exc:
            logger.exception("Hermes session creation failed")
            raise HTTPException(status_code=503, detail=f"Hermes session creation failed: {exc.__class__.__name__}") from exc
        with app_deps.state.locked():
            hermes = app_deps.state.hermes
            hermes.online = True
            hermes.status = "idle"
            hermes.last_reply = "等待 Hermes 回复"
            hermes.streaming = False
            hermes.activity = "新会话已就绪"
            hermes.active_tool = None
            hermes.tool_progress = None
            hermes.interim_message = None
            hermes.session_id = session_id
            hermes.last_event_at = time.time()
            hermes.last_user_message = ""
            hermes.input_active = False
            hermes.input_draft = ""
            hermes.input_caret = 0
            hermes.input_preedit = ""
            hermes.input_candidates = []
            hermes.input_candidate_index = 0
            hermes.input_candidate_page = 0
            hermes.input_voice_active = False
            _set_active_scene(app_deps.state, "hermes")
        _apply_display_stream_profile(app_deps)
        _request_and_push_refresh(app_deps, frames=DISPLAY_STREAM_KEYFRAMES)
        return {"session_id": session_id, "status": "ready"}

    @app.post("/api/hermes/interrupt")
    def interrupt_hermes() -> dict[str, str]:
        app_deps = _deps(app)
        hermes_status = _interrupt_hermes_and_audio(app_deps)
        with app_deps.state.locked():
            hermes = app_deps.state.hermes
            hermes.streaming = False
            hermes.status = "idle"
            hermes.activity = "已打断"
            hermes.active_tool = None
            hermes.tool_progress = None
            hermes.interim_message = None
        _request_and_push_refresh(app_deps, frames=DISPLAY_STREAM_KEYFRAMES)
        return {"status": "interrupted", "hermes": hermes_status}

    @app.get("/api/voice/status")
    def get_voice_status() -> dict[str, object]:
        controller = _voice_controller(_deps(app))
        try:
            return controller.refresh_status()
        except Exception as exc:
            logger.exception("Hermes voice status failed")
            raise HTTPException(
                status_code=503,
                detail=_voice_http_error("Hermes voice status failed", exc),
            ) from exc

    @app.post("/api/voice/enable")
    def set_voice_enabled(request: VoiceEnabledRequest) -> dict[str, object]:
        controller = _voice_controller(_deps(app))
        try:
            return controller.set_enabled(request.enabled)
        except Exception as exc:
            logger.exception("Hermes voice toggle failed")
            raise HTTPException(
                status_code=503,
                detail=_voice_http_error("Hermes voice toggle failed", exc),
            ) from exc

    @app.post("/api/voice/record/start")
    def start_voice_recording() -> dict[str, object]:
        app_deps = _deps(app)
        controller = _voice_controller(app_deps)
        _interrupt_hermes_and_audio(app_deps)
        try:
            result = controller.start_recording()
        except Exception as exc:
            logger.exception("Hermes voice recording failed to start")
            raise HTTPException(
                status_code=503,
                detail=_voice_http_error("Hermes voice recording failed", exc),
            ) from exc
        _request_and_push_refresh(app_deps, frames=DISPLAY_STREAM_KEYFRAMES)
        return result

    @app.post("/api/hermes/input/voice/start")
    def start_hermes_input_voice_recording() -> dict[str, object]:
        app_deps = _deps(app)
        with app_deps.state.locked():
            control_enabled = app_deps.state.display_ui.keyboard_control_enabled
            active_scene = app_deps.state.active_scene
        if not control_enabled or active_scene != "hermes":
            raise HTTPException(status_code=409, detail="ESP32 Hermes control is not active")
        controller = _voice_controller(app_deps)
        _interrupt_hermes_and_audio(app_deps)
        try:
            result = controller.start_recording(destination="draft")
        except Exception as exc:
            logger.exception("Hermes draft voice recording failed to start")
            raise HTTPException(
                status_code=503,
                detail=_voice_http_error("Hermes voice recording failed", exc),
            ) from exc
        _request_and_push_refresh(app_deps, frames=DISPLAY_STREAM_KEYFRAMES)
        return result

    @app.post("/api/hermes/input/voice/stop")
    def stop_hermes_input_voice_recording() -> dict[str, object]:
        app_deps = _deps(app)
        controller = _voice_controller(app_deps)
        try:
            result = controller.stop_recording()
        except Exception as exc:
            logger.exception("Hermes draft voice recording failed to stop")
            raise HTTPException(
                status_code=503,
                detail=_voice_http_error("Hermes voice recording failed", exc),
            ) from exc
        _request_and_push_refresh(app_deps, frames=DISPLAY_STREAM_KEYFRAMES)
        return result

    @app.post("/api/voice/silence")
    def set_voice_silence(request: VoiceSilenceRequest) -> dict[str, object]:
        controller = _voice_controller(_deps(app))
        try:
            return controller.set_silence_duration(request.seconds)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            logger.exception("Hermes voice silence setting failed")
            raise HTTPException(
                status_code=503,
                detail=_voice_http_error("Hermes voice silence setting failed", exc),
            ) from exc

    @app.post("/api/voice/record/stop")
    def stop_voice_recording() -> dict[str, object]:
        app_deps = _deps(app)
        controller = _voice_controller(app_deps)
        try:
            result = controller.stop_recording()
        except Exception as exc:
            logger.exception("Hermes voice recording failed to stop")
            raise HTTPException(
                status_code=503,
                detail=_voice_http_error("Hermes voice recording failed", exc),
            ) from exc
        _request_and_push_refresh(app_deps, frames=DISPLAY_STREAM_KEYFRAMES)
        return result

    @app.post("/api/light")
    def set_light(request: LightRequest) -> dict[str, object]:
        app_deps = _deps(app)
        color_temp_kelvin = request.color_temp_kelvin
        if color_temp_kelvin is not None:
            with app_deps.state.locked():
                low = app_deps.state.light.min_color_temp_kelvin
                high = app_deps.state.light.max_color_temp_kelvin
            low, high = sorted((low, high))
            color_temp_kelvin = max(low, min(high, color_temp_kelvin))
        try:
            app_deps.ha.set_light(request.on, request.brightness, color_temp_kelvin)
            light_state = app_deps.ha.get_state()
            with app_deps.state.locked():
                app_deps.state.light = light_state
        except Exception as exc:
            with app_deps.state.locked():
                app_deps.state.light.error = f"Light command failed: {exc.__class__.__name__}"
        with app_deps.state.locked():
            _set_active_scene(app_deps.state, "lights")
        _apply_display_stream_profile(app_deps)
        _request_and_push_refresh(app_deps, frames=DISPLAY_STREAM_KEYFRAMES)
        ha_sync_worker = getattr(app_deps, "ha_sync_worker", None)
        if ha_sync_worker is not None:
            ha_sync_worker.wake()
        return {"light": app_deps.state.light}

    @app.post("/api/display/unified-flush")
    def set_unified_flush(request: UnifiedFlushRequest) -> dict[str, bool]:
        app_deps = _deps(app)
        app_deps.state.link.unified_flush = request.enabled
        setter = getattr(app_deps.streamer, "set_unified_flush", None)
        if callable(setter):
            setter(request.enabled)
        else:
            _request_full_refresh(app_deps.streamer)
        return {"unified_flush": request.enabled}

    @app.post("/api/display/transport")
    def set_transport_options(request: TransportOptionsRequest) -> dict[str, object]:
        app_deps = _deps(app)
        if request.transport_mode is not None:
            if request.transport_mode not in {"udp", "webrtc", "mjpeg", "indexed"}:
                raise HTTPException(status_code=400, detail="unsupported transport_mode")
            app_deps.state.link.transport_mode = request.transport_mode
        if request.compression_mode is not None:
            if request.compression_mode not in {"auto", "raw", "rle", "lz4"}:
                raise HTTPException(status_code=400, detail="compression_mode must be auto, raw, rle, or lz4")
            app_deps.state.link.compression_mode = request.compression_mode
        if request.nack_enabled is not None:
            app_deps.state.link.nack_enabled = request.nack_enabled
        if request.resend_frame_end_on_nack is not None:
            app_deps.state.link.resend_frame_end_on_nack = request.resend_frame_end_on_nack
        if request.esp_log_enabled is not None:
            app_deps.state.link.esp_log_enabled = request.esp_log_enabled
        if request.backlight_percent is not None:
            if not 0 <= request.backlight_percent <= 100:
                raise HTTPException(status_code=400, detail="backlight_percent must be between 0 and 100")
            app_deps.state.link.backlight_percent = request.backlight_percent
        if request.panel_gamma is not None:
            if request.panel_gamma not in {"1.0", "1.8", "2.2", "2.5"}:
                raise HTTPException(status_code=400, detail="panel_gamma must be 1.0, 1.8, 2.2, or 2.5")
            app_deps.state.link.panel_gamma = request.panel_gamma
        if request.tile_width is not None:
            if not 1 <= request.tile_width <= 240:
                raise HTTPException(status_code=400, detail="tile_width must be between 1 and 240")
            app_deps.state.link.tile_width = request.tile_width
        if request.tile_height is not None:
            if not 1 <= request.tile_height <= 320:
                raise HTTPException(status_code=400, detail="tile_height must be between 1 and 320")
            app_deps.state.link.tile_height = request.tile_height
        if request.mjpeg_quality is not None:
            if not 10 <= request.mjpeg_quality <= 95:
                raise HTTPException(status_code=400, detail="mjpeg_quality must be between 10 and 95")
            app_deps.state.link.mjpeg_quality = request.mjpeg_quality
        if request.mjpeg_subsampling is not None:
            if request.mjpeg_subsampling not in {"420", "422", "444"}:
                raise HTTPException(status_code=400, detail="mjpeg_subsampling must be 420, 422, or 444")
            app_deps.state.link.mjpeg_subsampling = request.mjpeg_subsampling
        effective_indexed_bpp = app_deps.state.link.indexed_bpp
        if request.indexed_bpp is not None:
            if request.indexed_bpp not in {1, 2, 4, 8}:
                raise HTTPException(status_code=400, detail="indexed_bpp must be 1, 2, 4, or 8")
            effective_indexed_bpp = request.indexed_bpp
        protected_color_capacity = indexed_palette_capacity(effective_indexed_bpp)
        normalized_protected_colors: list[str | None] | None = None
        if request.indexed_protected_colors is not None:
            if len(request.indexed_protected_colors) != protected_color_capacity:
                raise HTTPException(
                    status_code=400,
                    detail=f"indexed_protected_colors must contain {protected_color_capacity} slots for I{effective_indexed_bpp}",
                )
            normalized_protected_colors = [
                None if color is None else _normalize_hex_color(color, f"indexed_protected_colors[{index}]")
                for index, color in enumerate(request.indexed_protected_colors)
            ]
        elif request.indexed_bpp is not None:
            normalized_protected_colors = resize_indexed_color_slots(
                app_deps.state.link.indexed_protected_colors,
                effective_indexed_bpp,
            )
        if request.indexed_bpp is not None:
            app_deps.state.link.indexed_bpp = effective_indexed_bpp
        if normalized_protected_colors is not None:
            app_deps.state.link.indexed_protected_colors = normalized_protected_colors
        if request.indexed_keyframe_interval is not None:
            if not 1 <= request.indexed_keyframe_interval <= 3600:
                raise HTTPException(status_code=400, detail="indexed_keyframe_interval must be between 1 and 3600")
            app_deps.state.link.indexed_keyframe_interval = request.indexed_keyframe_interval
        if request.preview_fps_limited is not None:
            app_deps.state.link.preview_fps_limited = request.preview_fps_limited
        if request.preview_fps_limit is not None:
            if not 1 <= request.preview_fps_limit <= 60:
                raise HTTPException(status_code=400, detail="preview_fps_limit must be between 1 and 60")
            app_deps.state.link.preview_fps_limit = request.preview_fps_limit
        setter = getattr(app_deps.streamer, "set_transport_options", None)
        if callable(setter):
            setter(
                transport_mode=request.transport_mode,
                compression_mode=request.compression_mode,
                nack_enabled=request.nack_enabled,
                resend_frame_end_on_nack=request.resend_frame_end_on_nack,
                esp_log_enabled=request.esp_log_enabled,
                backlight_percent=request.backlight_percent,
                panel_gamma=request.panel_gamma,
                tile_width=request.tile_width,
                tile_height=request.tile_height,
                mjpeg_quality=request.mjpeg_quality,
                mjpeg_subsampling=request.mjpeg_subsampling,
                indexed_bpp=request.indexed_bpp,
                indexed_protected_colors=normalized_protected_colors,
                indexed_keyframe_interval=request.indexed_keyframe_interval,
            )
            app_deps.state.link.transport_epoch = getattr(
                app_deps.streamer,
                "transport_epoch",
                app_deps.state.link.transport_epoch,
            )
        elif (
            request.compression_mode is not None
            or request.tile_width is not None
            or request.tile_height is not None
            or request.mjpeg_quality is not None
            or request.mjpeg_subsampling is not None
        ):
            _request_full_refresh(app_deps.streamer)
        return {
            "transport_mode": app_deps.state.link.transport_mode,
            "compression_mode": app_deps.state.link.compression_mode,
            "nack_enabled": app_deps.state.link.nack_enabled,
            "resend_frame_end_on_nack": app_deps.state.link.resend_frame_end_on_nack,
            "esp_log_enabled": app_deps.state.link.esp_log_enabled,
            "backlight_percent": app_deps.state.link.backlight_percent,
            "panel_gamma": app_deps.state.link.panel_gamma,
            "tile_width": app_deps.state.link.tile_width,
            "tile_height": app_deps.state.link.tile_height,
            "mjpeg_quality": app_deps.state.link.mjpeg_quality,
            "mjpeg_subsampling": app_deps.state.link.mjpeg_subsampling,
            "indexed_bpp": app_deps.state.link.indexed_bpp,
            "indexed_protected_colors": app_deps.state.link.indexed_protected_colors,
            "indexed_keyframe_interval": app_deps.state.link.indexed_keyframe_interval,
            "preview_fps_limited": app_deps.state.link.preview_fps_limited,
            "preview_fps_limit": app_deps.state.link.preview_fps_limit,
        }

    @app.post("/api/webrtc/offer")
    def create_webrtc_offer(request: SessionDescriptionRequest) -> dict[str, str]:
        if request.type != "offer":
            raise HTTPException(status_code=400, detail="type must be offer")
        offer_id = uuid4().hex
        app.state.webrtc_offers[offer_id] = {"offer_id": offer_id, "sdp": request.sdp, "type": request.type}
        app.state.latest_webrtc_offer_id = offer_id
        return {"offer_id": offer_id}

    @app.get("/api/webrtc/offer/latest")
    def get_latest_webrtc_offer() -> dict[str, str]:
        offer_id = app.state.latest_webrtc_offer_id
        if offer_id is None:
            raise HTTPException(status_code=404, detail="no webrtc offer")
        return app.state.webrtc_offers[offer_id]

    @app.post("/api/webrtc/answer/{offer_id}")
    def create_webrtc_answer(offer_id: str, request: SessionDescriptionRequest) -> dict[str, str]:
        if request.type != "answer":
            raise HTTPException(status_code=400, detail="type must be answer")
        if offer_id not in app.state.webrtc_offers:
            raise HTTPException(status_code=404, detail="unknown webrtc offer")
        app.state.webrtc_answers[offer_id] = {"offer_id": offer_id, "sdp": request.sdp, "type": request.type}
        return {"offer_id": offer_id}

    @app.get("/api/webrtc/answer/{offer_id}")
    def get_webrtc_answer(offer_id: str) -> dict[str, str]:
        answer = app.state.webrtc_answers.get(offer_id)
        if answer is None:
            raise HTTPException(status_code=404, detail="no webrtc answer")
        return answer

    @app.post("/api/webrtc/reset")
    def reset_webrtc_signaling() -> dict[str, bool]:
        app.state.webrtc_offers.clear()
        app.state.webrtc_answers.clear()
        app.state.latest_webrtc_offer_id = None
        return {"reset": True}

    @app.get("/api/webrtc/diagnostics")
    def get_webrtc_diagnostics() -> dict[str, object]:
        offer_id = app.state.latest_webrtc_offer_id
        offer = app.state.webrtc_offers.get(offer_id) if offer_id is not None else None
        answer = app.state.webrtc_answers.get(offer_id) if offer_id is not None else None
        return {
            "offer_id": offer_id,
            "offer_candidates": _parse_ice_candidates(offer["sdp"] if offer else ""),
            "answer_candidates": _parse_ice_candidates(answer["sdp"] if answer else ""),
        }

    @app.get("/api/webrtc/local-candidate-address")
    def get_webrtc_local_candidate_address() -> dict[str, str | None]:
        app_deps = _deps(app)
        esp32_ip = app_deps.state.link.esp32_ip
        address = _local_udp_address_for(esp32_ip) if esp32_ip else None
        return {"address": address, "esp32_ip": esp32_ip}

    @app.post("/api/webrtc/launch-chrome")
    def launch_webrtc_chrome(request: Request) -> dict[str, object]:
        chrome = _find_chrome_executable()
        if chrome is None:
            raise HTTPException(status_code=404, detail="Chrome or Edge executable not found")
        CHROME_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        url = str(request.base_url)
        args = [
            chrome,
            f"--user-data-dir={CHROME_PROFILE_DIR}",
            *CHROME_WEBRTC_ARGS,
            url,
        ]
        process = subprocess.Popen(args)
        return {"pid": process.pid, "url": url, "profile": str(CHROME_PROFILE_DIR)}

    return app


def _parse_ice_candidates(sdp: str) -> list[dict[str, object]]:
    candidates: list[dict[str, object]] = []
    for raw_line in sdp.splitlines():
        line = raw_line.strip()
        if line.startswith("a=candidate:"):
            line = line[len("a="):]
        elif not line.startswith("candidate:"):
            continue
        parts = line.split()
        if len(parts) < 8 or parts[0].startswith("candidate:") is False:
            continue
        try:
            port = int(parts[5])
        except ValueError:
            continue
        candidate_type = ""
        if "typ" in parts:
            type_index = parts.index("typ") + 1
            if type_index < len(parts):
                candidate_type = parts[type_index]
        candidates.append(
            {
                "foundation": parts[0].split(":", 1)[1],
                "protocol": parts[2].lower(),
                "address": parts[4],
                "port": port,
                "type": candidate_type,
            }
        )
    return candidates


def _find_chrome_executable() -> str | None:
    candidates = [
        Path("C:/Program Files/Google/Chrome/Application/chrome.exe"),
        Path("C:/Program Files (x86)/Google/Chrome/Application/chrome.exe"),
        Path("C:/Program Files/Microsoft/Edge/Application/msedge.exe"),
        Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return None


def _normalize_hex_color(value: str, field: str) -> str:
    color = value.strip()
    if len(color) == 7 and color.startswith("#"):
        try:
            int(color[1:], 16)
        except ValueError:
            pass
        else:
            return color.upper()
    raise HTTPException(status_code=400, detail=f"{field} must be #RRGGBB")


def _local_udp_address_for(remote_ip: str) -> str | None:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect((remote_ip, 9))
            return probe.getsockname()[0]
    except OSError:
        return None


async def _mjpeg_stream(
    app_deps: DemoHostDependencies,
    *,
    max_frames: int | None = None,
    interval_seconds: float = DISPLAY_PREVIEW_POLL_INTERVAL,
) -> AsyncIterator[bytes]:
    sent = 0
    last_frame_id: int | None = None
    fps_started = asyncio.get_running_loop().time()
    fps_frames = 0
    while max_frames is None or sent < max_frames:
        cached_frame = _preview_mjpeg_frame(app_deps)
        if cached_frame is not None:
            frame_id, jpeg = cached_frame
            if frame_id == last_frame_id:
                await asyncio.sleep(interval_seconds)
                continue
            last_frame_id = frame_id
        else:
            worker = getattr(app_deps, "display_worker", None)
            if worker is not None:
                worker.wake()
                await asyncio.sleep(interval_seconds)
                cached_frame = _preview_mjpeg_frame(app_deps)
            if cached_frame is not None:
                last_frame_id, jpeg = cached_frame
            else:
                image = app_deps.renderer.render(app_deps.state)
                jpeg = _jpeg_bytes(image)
        yield _mjpeg_frame_part(jpeg, frame_id=last_frame_id)
        sent += 1
        fps_frames += 1
        now = asyncio.get_running_loop().time()
        fps_elapsed = now - fps_started
        if fps_elapsed >= 2.0:
            measured_fps = fps_frames / fps_elapsed
            previous_fps = app_deps.state.link.preview_fps
            app_deps.state.link.preview_fps = measured_fps if previous_fps <= 0 else previous_fps * 0.65 + measured_fps * 0.35
            fps_started = now
            fps_frames = 0
        if max_frames is None or sent < max_frames:
            await asyncio.sleep(0)


def _preview_mjpeg_frame(app_deps: DemoHostDependencies) -> tuple[int, bytes] | None:
    if app_deps.state.link.displayed_frame_id > 0:
        getter = getattr(app_deps.streamer, "latest_displayed_mjpeg_frame", None)
        return getter() if callable(getter) else None
    return _latest_mjpeg_frame(app_deps.streamer)


def _latest_preview_image(app_deps: DemoHostDependencies) -> object:
    getter = getattr(app_deps.streamer, "latest_image", None)
    if callable(getter):
        image = getter()
        if image is not None:
            return image
    return app_deps.renderer.render(app_deps.state)


def _preview_jpeg_frame(app_deps: DemoHostDependencies) -> tuple[int | None, bytes]:
    cached_frame = _preview_mjpeg_frame(app_deps)
    if cached_frame is not None:
        return cached_frame
    return None, _jpeg_bytes(_latest_preview_image(app_deps))


def _cached_preview_jpeg(app: FastAPI, app_deps: DemoHostDependencies) -> tuple[int | None, bytes]:
    now = time.perf_counter()
    with app.state.preview_cache_lock:
        cached = getattr(app.state, "preview_jpeg_cache", None)
        cache_interval = _preview_cache_interval(app_deps)
        if cache_interval > 0 and cached is not None:
            frame_id, jpeg, cached_at = cached
            if now - cached_at < cache_interval:
                return frame_id, jpeg
        frame_id, jpeg = _preview_jpeg_frame(app_deps)
        app.state.preview_jpeg_cache = (frame_id, jpeg, now)
        _record_preview_frame(app, app_deps, now=now, frame_id=frame_id)
        return frame_id, jpeg


def _preview_cache_interval(app_deps: DemoHostDependencies) -> float:
    if not app_deps.state.link.preview_fps_limited:
        return 0.0
    return 1.0 / max(1, app_deps.state.link.preview_fps_limit)


def _latest_mjpeg_frame(streamer: Streamer) -> tuple[int, bytes] | None:
    getter = getattr(streamer, "latest_mjpeg_frame", None)
    if not callable(getter):
        return None
    return getter()


def _jpeg_bytes(image: object, quality: int = 70) -> bytes:
    buffer = BytesIO()
    image.save(buffer, format="JPEG", quality=quality)
    return buffer.getvalue()


def _mjpeg_frame_part(jpeg: bytes, frame_id: int | None = None) -> bytes:
    frame_header = b"" if frame_id is None else f"X-Frame-Id: {frame_id}\r\n".encode("ascii")
    return (
        b"--frame\r\n"
        b"Content-Type: image/jpeg\r\n"
        + frame_header
        + f"Content-Length: {len(jpeg)}\r\n\r\n".encode("ascii")
        + jpeg
        + b"\r\n"
    )


def _record_preview_frame(
    app: FastAPI,
    app_deps: DemoHostDependencies,
    *,
    now: float | None = None,
    frame_id: int | None = None,
) -> None:
    now = time.perf_counter() if now is None else now
    if frame_id is not None:
        if getattr(app.state, "preview_last_frame_id", None) == frame_id:
            return
        app.state.preview_last_frame_id = frame_id
    started = getattr(app.state, "preview_fps_started", None)
    frames = getattr(app.state, "preview_fps_frames", 0) + 1
    if started is None:
        app.state.preview_fps_started = now
        app.state.preview_fps_frames = frames
        return
    elapsed = now - started
    if elapsed < 2.0:
        app.state.preview_fps_frames = frames
        return
    measured_fps = frames / elapsed
    previous_fps = app_deps.state.link.preview_fps
    app_deps.state.link.preview_fps = measured_fps if previous_fps <= 0 else previous_fps * 0.65 + measured_fps * 0.35
    app.state.preview_fps_started = now
    app.state.preview_fps_frames = 0


def _deps(app: FastAPI) -> DemoHostDependencies:
    return app.state.deps


def _module_registry(app: FastAPI) -> ModuleRegistry:
    modules = _deps(app).modules
    if modules is None:
        raise RuntimeError("module registry is not initialized")
    return modules


def _adapter_health(adapter: object, service: str) -> dict[str, object]:
    health = getattr(adapter, "health", None)
    if not callable(health):
        return {"ok": True, "service": service, "mode": "injected"}
    try:
        result = health()
    except Exception as exc:
        logger.exception("%s health check failed", service)
        return {"ok": False, "service": service, "reason": exc.__class__.__name__}
    if not isinstance(result, dict):
        return {"ok": False, "service": service, "reason": "invalid_health_response"}
    return result


def _execute_hermes_request(
    app_deps: DemoHostDependencies,
    prompt: str,
    *,
    event_sink: object | None = None,
) -> dict[str, str]:
    last_display_wake = 0.0
    with app_deps.state.locked():
        hermes = app_deps.state.hermes
        hermes.online = True
        hermes.status = "thinking"
        hermes.streaming = True
        hermes.activity = "正在思考"
        hermes.active_tool = None
        hermes.tool_progress = None
        hermes.interim_message = "正在思考…"
        hermes.last_event_at = time.time()
        _set_active_scene(app_deps.state, "hermes")
    _apply_display_stream_profile(app_deps)
    _request_and_push_refresh(app_deps, frames=DISPLAY_STREAM_KEYFRAMES)

    def handle_event(event: HermesEvent) -> None:
        nonlocal last_display_wake
        event_type = event.type
        payload = event.payload
        now = time.monotonic()
        with app_deps.state.locked():
            hermes = app_deps.state.hermes
            hermes.online = True
            hermes.last_event_at = time.time()
            session_id = payload.get("session_id")
            if session_id:
                hermes.session_id = str(session_id)
            if event_type == "message.start":
                hermes.status = "thinking"
                hermes.streaming = True
                hermes.activity = "正在思考"
                hermes.interim_message = "正在思考…"
                hermes.active_tool = None
                hermes.tool_progress = None
                hermes.last_reply = ""
            elif event_type in {"thinking.delta", "reasoning.delta"}:
                hermes.status = "thinking"
                hermes.activity = "正在思考"
            elif event_type == "tool.generating":
                hermes.status = "thinking"
                hermes.activity = "正在准备工具"
                hermes.interim_message = "正在准备工具…"
            elif event_type == "tool.start":
                tool_name = str(payload.get("name") or "工具")
                context = str(payload.get("context") or "").strip()
                hermes.status = "thinking"
                hermes.activity = f"正在使用 {tool_name}"
                hermes.active_tool = tool_name
                hermes.tool_progress = "running"
                hermes.interim_message = context or f"正在使用 {tool_name}…"
            elif event_type == "tool.complete":
                tool_name = str(payload.get("name") or hermes.active_tool or "工具")
                summary = str(payload.get("summary") or "").strip()
                hermes.status = "thinking"
                hermes.activity = f"{tool_name} 已完成"
                hermes.active_tool = tool_name
                hermes.tool_progress = "complete"
                hermes.interim_message = summary or f"{tool_name} 已完成，正在整理回复…"
            elif event_type == "status.update":
                text = str(payload.get("text") or "").strip()
                if text:
                    hermes.activity = text
                    hermes.interim_message = text
            elif event_type == "message.delta":
                delta = str(payload.get("text") or "")
                hermes.status = "replying"
                hermes.activity = "正在回复"
                hermes.streaming = True
                hermes.active_tool = None
                hermes.tool_progress = None
                hermes.interim_message = None
                hermes.last_reply += delta
            elif event_type == "message.complete":
                text = str(payload.get("text") or "").strip()
                if text:
                    hermes.last_reply = text
                hermes.status = "replying"
                hermes.activity = "回复完成"
                hermes.streaming = False
                hermes.active_tool = None
                hermes.tool_progress = None
                hermes.interim_message = None
            elif event_type == "error":
                hermes.status = "error"
                hermes.activity = "Hermes 出错"
                hermes.streaming = False
                hermes.interim_message = str(payload.get("message") or "Hermes 网关错误")

        speech = getattr(app_deps, "speech", None)
        if speech is not None:
            speech.handle_hermes_event(event)
        if callable(event_sink):
            event_sink(event)
        if event_type in {"tool.start", "tool.complete", "status.update"} or now - last_display_wake >= 0.15:
            worker = getattr(app_deps, "display_worker", None)
            if worker is not None:
                worker.wake()
            last_display_wake = now

    try:
        parameters = inspect.signature(app_deps.hermes.ask).parameters
        if "on_event" in parameters:
            reply = app_deps.hermes.ask(prompt, on_event=handle_event)
        else:
            reply = app_deps.hermes.ask(prompt)
        with app_deps.state.locked():
            hermes = app_deps.state.hermes
            hermes.online = True
            hermes.status = "replying"
            hermes.streaming = False
            hermes.activity = "回复完成"
            hermes.active_tool = None
            hermes.tool_progress = None
            hermes.interim_message = None
            hermes.last_reply = reply
        return {"reply": reply}
    except Exception as exc:
        speech = getattr(app_deps, "speech", None)
        if speech is not None:
            speech.interrupt()
        error = f"Hermes command failed: {exc.__class__.__name__}"
        with app_deps.state.locked():
            hermes = app_deps.state.hermes
            hermes.status = "error"
            hermes.streaming = False
            hermes.activity = "Hermes 出错"
            hermes.active_tool = None
            hermes.tool_progress = None
            hermes.interim_message = error
            hermes.last_reply = error
        logger.exception("Hermes command failed")
        return {"error": error}
    finally:
        with app_deps.state.locked():
            _set_active_scene(app_deps.state, "hermes")
        _apply_display_stream_profile(app_deps)
        _request_and_push_refresh(app_deps, frames=DISPLAY_STREAM_KEYFRAMES)


def _prepare_hermes_turn(app_deps: DemoHostDependencies, prompt: str | None = None) -> None:
    speech = getattr(app_deps, "speech", None)
    if speech is not None:
        speech.interrupt()
    if app_deps.state.snapshot().hermes.streaming:
        interrupt = getattr(app_deps.hermes, "interrupt", None)
        if callable(interrupt):
            try:
                interrupt()
            except Exception:
                logger.exception("Hermes pre-turn interrupt failed")
    if prompt is not None:
        with app_deps.state.locked():
            app_deps.state.hermes.last_user_message = prompt


def _execute_voice_transcript(app_deps: DemoHostDependencies, transcript: str) -> dict[str, str]:
    _prepare_hermes_turn(app_deps, transcript)
    return _execute_hermes_request(app_deps, transcript)


def _voice_controller(app_deps: DemoHostDependencies) -> HermesVoiceController:
    controller = getattr(app_deps, "voice", None)
    if controller is None:
        raise HTTPException(status_code=501, detail="Hermes adapter does not support voice input")
    return controller


def _voice_http_error(prefix: str, exc: Exception) -> str:
    message = " ".join(str(exc).split())
    if not message:
        message = exc.__class__.__name__
    return f"{prefix}: {message[:500]}"


def _interrupt_hermes_and_audio(app_deps: DemoHostDependencies) -> str:
    speech = getattr(app_deps, "speech", None)
    if speech is not None:
        speech.interrupt()
    interrupt = getattr(app_deps.hermes, "interrupt", None)
    if not callable(interrupt):
        return "unsupported"
    try:
        return str(interrupt())
    except Exception as exc:
        logger.exception("Hermes interrupt failed")
        return f"failed:{exc.__class__.__name__}"


def _close_resource(resource: object, label: str) -> None:
    try:
        close = getattr(resource, "close")
        close()
    except Exception:
        logger.exception("%s close failed", label)


def _request_full_refresh(streamer: Streamer, frames: int = 3) -> None:
    request = getattr(streamer, "request_full_refresh", None)
    if callable(request):
        request(frames)
    else:
        streamer.previous = None


def _request_and_push_refresh(app_deps: DemoHostDependencies, frames: int = 3) -> None:
    _request_full_refresh(app_deps.streamer, frames=frames)
    worker = getattr(app_deps, "display_worker", None)
    if worker is not None:
        worker.wake()


def _set_active_scene(state: DemoState, scene: str, *, module_id: str | None = None) -> None:
    state.active_scene = scene
    state.active_module_id = module_id
    state.link.scene_transport_override = None


def _apply_active_module_display_profile(app_deps: DemoHostDependencies) -> None:
    modules = getattr(app_deps, "modules", None)
    provider = getattr(modules, "display_transport_profile", None)
    if not callable(provider):
        return
    with app_deps.state.locked():
        module_id = app_deps.state.active_module_id
        scene_key = app_deps.state.active_scene
    if module_id is None:
        return
    _apply_module_display_profile(app_deps, provider(module_id, scene_key))


def _apply_module_display_profile(app_deps: DemoHostDependencies, profile: object) -> None:
    if not isinstance(profile, dict):
        return
    transport_value = profile.get("transport_mode")
    quality_value = profile.get("jpeg_quality")
    subsampling_value = profile.get("jpeg_subsampling")
    indexed_bpp_value = profile.get("indexed_bpp")
    transport_mode = str(transport_value) if transport_value in {"mjpeg", "indexed"} else None
    quality = int(quality_value) if isinstance(quality_value, (int, float)) else None
    subsampling = str(subsampling_value) if subsampling_value in {"420", "422", "444"} else None
    indexed_bpp = int(indexed_bpp_value) if indexed_bpp_value in {1, 2, 4, 8} else None
    if transport_mode is None and quality is None and subsampling is None and indexed_bpp is None:
        return
    with app_deps.state.locked():
        module_active = app_deps.state.active_module_id is not None
        if module_active and transport_mode is not None:
            app_deps.state.link.scene_transport_override = transport_mode
        if quality is not None:
            app_deps.state.link.mjpeg_quality = quality
        if subsampling is not None:
            app_deps.state.link.mjpeg_subsampling = subsampling
        if indexed_bpp is not None:
            app_deps.state.link.indexed_protected_colors = resize_indexed_color_slots(
                app_deps.state.link.indexed_protected_colors,
                indexed_bpp,
            )
            app_deps.state.link.indexed_bpp = indexed_bpp
    setter = getattr(app_deps.streamer, "set_transport_options", None)
    if callable(setter):
        setter(
            transport_mode=transport_mode if module_active else None,
            mjpeg_quality=quality,
            mjpeg_subsampling=subsampling,
            indexed_bpp=indexed_bpp,
            indexed_protected_colors=(
                app_deps.state.link.indexed_protected_colors if indexed_bpp is not None else None
            ),
        )
        app_deps.state.link.transport_epoch = getattr(
            app_deps.streamer,
            "transport_epoch",
            app_deps.state.link.transport_epoch,
        )


async def _render_loop(deps: DemoHostDependencies, render_interval: float) -> None:
    while True:
        started = asyncio.get_running_loop().time()
        try:
            await asyncio.to_thread(deps.streamer.render_and_send, deps.renderer, deps.state)
            deps.state.link.transport_epoch = getattr(
                deps.streamer,
                "transport_epoch",
                deps.state.link.transport_epoch,
            )
        except Exception as exc:
            deps.state.link.error = f"Render loop failed: {exc.__class__.__name__}"
            logger.exception("Render loop frame failed")
        elapsed = asyncio.get_running_loop().time() - started
        await asyncio.sleep(_render_sleep_seconds(deps.state, render_interval, elapsed))


def _render_sleep_seconds(state: DemoState, render_interval: float, elapsed_seconds: float) -> float:
    return max(0.0, render_interval - elapsed_seconds)


def _apply_display_stream_state(state: DemoState) -> None:
    state.link.video_fast_enabled = True
    state.link.compression_mode = str(DISPLAY_STREAM_TRANSPORT_OPTIONS["compression_mode"])
    state.link.resend_frame_end_on_nack = bool(DISPLAY_STREAM_TRANSPORT_OPTIONS["resend_frame_end_on_nack"])
    state.link.tile_width = int(DISPLAY_STREAM_TRANSPORT_OPTIONS["tile_width"])
    state.link.tile_height = int(DISPLAY_STREAM_TRANSPORT_OPTIONS["tile_height"])
    state.link.packet_burst_size = int(DISPLAY_STREAM_TRANSPORT_OPTIONS["packet_burst_size"])
    state.link.packet_gap_ms = float(DISPLAY_STREAM_TRANSPORT_OPTIONS["packet_gap_seconds"]) * 1000.0
    state.link.frame_end_repeats = int(DISPLAY_STREAM_TRANSPORT_OPTIONS["frame_end_repeats"])
    state.link.full_refresh_tile_repeats = int(DISPLAY_STREAM_TRANSPORT_OPTIONS["full_refresh_tile_repeats"])
    state.link.full_refresh_interval = int(DISPLAY_STREAM_TRANSPORT_OPTIONS["full_refresh_interval"])
    state.link.unified_flush = True


def _apply_display_stream_profile(app_deps: DemoHostDependencies) -> None:
    _apply_display_stream_state(app_deps.state)
    unified_setter = getattr(app_deps.streamer, "set_unified_flush", None)
    if callable(unified_setter):
        unified_setter(True)
    setter = getattr(app_deps.streamer, "set_transport_options", None)
    if callable(setter):
        setter(**DISPLAY_STREAM_TRANSPORT_OPTIONS)


app = create_app()
