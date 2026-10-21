from __future__ import annotations

import importlib
import asyncio
import json
import socket
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncIterator

import httpx
import pytest

from espagent_demo.firmware_flash import (
    DisplayGpioInvalid,
    FirmwareFlashBusy,
    FirmwareFlashUnavailable,
    WifiProvisioningInvalid,
)
from espagent_demo.hermes_adapter import HermesEvent
from espagent_demo.state import DemoState, LightState
from espagent_demo.version import APP_VERSION


class FakeRenderer:
    def render(self, state: DemoState):
        from PIL import Image

        return Image.new("RGB", (240, 320), (255, 0, 0))


class FakeStreamer:
    def __init__(self, fail_render: bool = False, fail_once: bool = False, fail_close: bool = False) -> None:
        self.closed = False
        self.previous = object()
        self.render_calls = 0
        self.fail_render = fail_render
        self.fail_once = fail_once
        self.fail_close = fail_close

    def render_and_send(self, renderer: object, state: DemoState) -> None:
        self.render_calls += 1
        state.link.frame_id = self.render_calls
        if self.fail_once and self.render_calls == 1:
            raise RuntimeError("transient render failed")
        if self.fail_render:
            raise RuntimeError("render failed")

    def close(self) -> None:
        self.closed = True
        if self.fail_close:
            raise RuntimeError("streamer close failed")

    def set_unified_flush(self, enabled: bool) -> None:
        self.unified_flush = enabled
        self.previous = None

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
        self.transport_mode = transport_mode
        self.compression_mode = compression_mode
        self.nack_enabled = nack_enabled
        self.resend_frame_end_on_nack = resend_frame_end_on_nack
        self.esp_log_enabled = esp_log_enabled
        self.backlight_percent = backlight_percent
        self.panel_gamma = panel_gamma
        self.tile_width = tile_width
        self.tile_height = tile_height
        self.mjpeg_quality = mjpeg_quality
        self.mjpeg_subsampling = mjpeg_subsampling
        self.indexed_bpp = indexed_bpp
        self.indexed_protected_colors = indexed_protected_colors
        self.indexed_keyframe_interval = indexed_keyframe_interval
        self.packet_burst_size = packet_burst_size
        self.packet_gap_seconds = packet_gap_seconds
        self.frame_end_repeats = frame_end_repeats
        self.full_refresh_tile_repeats = full_refresh_tile_repeats
        self.full_refresh_interval = full_refresh_interval
        self.previous = None

    def set_transport_mode(self, mode: str) -> None:
        self.transport_mode = mode
        self.previous = None


class FakeHermes:
    def __init__(self, fail: bool = False) -> None:
        self.prompts: list[str] = []
        self.fail = fail
        self.interrupts = 0

    def ask(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if self.fail:
            raise RuntimeError("wsl command included secret-token")
        return f"fake reply: {prompt}"

    def close(self) -> None:
        return None

    def new_session(self) -> str:
        return "fake-session-new"

    def interrupt(self) -> str:
        self.interrupts += 1
        return "interrupted"


class FakeStreamingHermes(FakeHermes):
    def ask(self, prompt: str, on_event=None) -> str:
        self.prompts.append(prompt)
        events = [
            HermesEvent("message.start", {}),
            HermesEvent("tool.start", {"name": "terminal", "context": "Running probe"}),
            HermesEvent("tool.complete", {"name": "terminal", "summary": "Probe complete"}),
            HermesEvent("message.delta", {"text": "stream "}),
            HermesEvent("message.delta", {"text": "reply"}),
            HermesEvent("message.complete", {"text": "stream reply", "status": "complete"}),
        ]
        if on_event is not None:
            for event in events:
                on_event(event)
        return "stream reply"


class FakeVoiceController:
    def __init__(self) -> None:
        self.started = False
        self.closed = False
        self.enabled = False
        self.status = "disabled"
        self.silence_duration_seconds = 2.0
        self.destinations: list[str] = []

    def start(self) -> None:
        self.started = True

    def close(self) -> None:
        self.closed = True

    def refresh_status(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "available": True,
            "audio_available": True,
            "stt_available": True,
            "status": self.status,
            "silence_duration_seconds": self.silence_duration_seconds,
        }

    def set_enabled(self, enabled: bool) -> dict[str, object]:
        self.enabled = enabled
        self.status = "ready" if enabled else "disabled"
        return self.refresh_status()

    def start_recording(self, *, destination: str = "submit") -> dict[str, object]:
        self.destinations.append(destination)
        self.enabled = True
        self.status = "listening"
        return self.refresh_status()

    def stop_recording(self) -> dict[str, object]:
        self.status = "transcribing"
        return self.refresh_status()

    def set_silence_duration(self, seconds: float) -> dict[str, object]:
        if not 0.5 <= seconds <= 10:
            raise ValueError("voice silence duration must be between 0.5 and 10 seconds")
        self.silence_duration_seconds = seconds
        return self.refresh_status()


class FakeLightAdapter:
    def __init__(self, fail: bool = False) -> None:
        self.closed = False
        self.commands: list[tuple[bool, int | None, int | None]] = []
        self.fail = fail
        self.token = "secret-token"

    def set_light(
        self,
        on: bool,
        brightness: int | None = None,
        color_temp_kelvin: int | None = None,
    ) -> None:
        self.commands.append((on, brightness, color_temp_kelvin))
        if self.fail:
            raise RuntimeError("no real Home Assistant in tests")

    def get_state(self) -> LightState:
        return LightState(entity_id="light.fake", online=True, on=True, brightness=40)

    def close(self) -> None:
        self.closed = True


class FakeFirmwareFlasher:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.started = 0
        self.wifi_started = 0
        self.gpio_started = 0
        self.closed = False

    def snapshot(self) -> dict[str, object]:
        return {
            "status": "idle",
            "phase": "ready",
            "progress": 0,
            "message": "已发现 ESP32-S3，等待烧录",
            "running": False,
            "can_flash": True,
            "runtime_ready": True,
            "runtime_error": None,
            "environment_ready": True,
            "flasher_version": "4.11.0",
            "firmware_version": "test-1",
            "package_ready": True,
            "project_ready": True,
            "target": "ESP32-S3",
            "ports": [{"device": "COM10", "description": "USB-SERIAL CH340"}],
            "device": {"device": "COM10", "description": "USB-SERIAL CH340"},
            "device_count": 1,
            "logs": [],
            "wifi": {
                "status": "idle",
                "message": "连接 USB 后可写入网络",
                "running": False,
                "can_configure": True,
                "ssid": None,
                "ip": None,
                "connected": None,
                "device": {"device": "COM10", "description": "USB-SERIAL CH340"},
            },
            "display_gpio": {
                "status": "idle",
                "message": "连接 USB 后可读取或写入屏幕引脚",
                "running": False,
                "can_configure": True,
                "values": {"scl": 12, "sda": 11, "cs": 10, "dc": 8, "rst": 7, "bl": 6},
                "defaults": {"scl": 12, "sda": 11, "cs": 10, "dc": 8, "rst": 7, "bl": 6},
                "device": {"device": "COM10", "description": "USB-SERIAL CH340"},
            },
        }

    def start_flash(self) -> dict[str, object]:
        if self.error is not None:
            raise self.error
        self.started += 1
        return {**self.snapshot(), "status": "detecting", "running": True, "can_flash": False}

    def start_wifi_provisioning(self, ssid: str, password: str) -> dict[str, object]:
        if self.error is not None:
            raise self.error
        self.wifi_started += 1
        snapshot = self.snapshot()
        snapshot["wifi"] = {
            **snapshot["wifi"],
            "status": "detecting",
            "running": True,
            "can_configure": False,
            "ssid": ssid,
        }
        assert password == "secret123"
        return snapshot

    def start_display_gpio_read(self) -> dict[str, object]:
        if self.error is not None:
            raise self.error
        self.gpio_started += 1
        snapshot = self.snapshot()
        snapshot["display_gpio"] = {**snapshot["display_gpio"], "status": "detecting", "running": True}
        return snapshot

    def start_display_gpio_configuration(self, values: dict[str, int]) -> dict[str, object]:
        if self.error is not None:
            raise self.error
        self.gpio_started += 1
        snapshot = self.snapshot()
        snapshot["display_gpio"] = {
            **snapshot["display_gpio"], "status": "applying", "running": True, "values": values,
        }
        return snapshot

    def close(self) -> None:
        self.closed = True


@dataclass
class FakeDeps:
    state: DemoState
    renderer: FakeRenderer
    streamer: FakeStreamer
    ha: FakeLightAdapter
    hermes: FakeHermes
    firmware_flasher: FakeFirmwareFlasher | None = None


class FakeModuleDefinition:
    def default_esp32_scene(self):
        from types import SimpleNamespace

        return SimpleNamespace(scene_key="codex")

    def app_route(self) -> str:
        return "/module-assets/builtin.codex-usage/app/"


class FakeModuleRegistry:
    def __init__(self) -> None:
        self.started = False
        self.closed = False
        self.enabled = True

    def start_all(self) -> None:
        self.started = True

    def close(self) -> None:
        self.closed = True

    def list_modules(self) -> list[dict[str, object]]:
        return [{
            "id": "builtin.codex-usage",
            "name": "Codex 额度",
            "enabled": self.enabled,
            "lifecycle": "running" if self.enabled else "disabled",
        }]

    def get_state(self, module_id: str) -> dict[str, object]:
        if module_id == "builtin.ws2812b":
            return {
                "module_id": module_id,
                "lifecycle": "running",
                "state": {
                    "status": "online",
                    "online": True,
                    "power": False,
                    "brightness": 100,
                    "color": "#ffffff",
                },
            }
        if module_id != "builtin.codex-usage":
            raise KeyError(module_id)
        return {"module_id": module_id, "lifecycle": "running", "state": {"status": "ready"}}

    def invoke(self, module_id: str, action: str, payload: dict[str, object]) -> dict[str, object]:
        if module_id == "builtin.ws2812b":
            return {"online": True, "accepted": action, **payload}
        if module_id != "builtin.codex-usage":
            raise KeyError(module_id)
        return {"accepted": action == "refresh", "payload": payload}

    def definition(self, module_id: str) -> FakeModuleDefinition:
        if module_id != "builtin.codex-usage":
            raise KeyError(module_id)
        return FakeModuleDefinition()

    def is_enabled(self, module_id: str) -> bool:
        if module_id != "builtin.codex-usage":
            raise KeyError(module_id)
        return self.enabled

    def set_enabled(self, module_id: str, enabled: bool) -> dict[str, object]:
        if module_id != "builtin.codex-usage":
            raise KeyError(module_id)
        self.enabled = enabled
        return self.list_modules()[0]


def make_deps(
    *,
    fail_render: bool = False,
    fail_render_once: bool = False,
    fail_streamer_close: bool = False,
    fail_light: bool = False,
    fail_hermes: bool = False,
) -> FakeDeps:
    return FakeDeps(
        state=DemoState(),
        renderer=FakeRenderer(),
        streamer=FakeStreamer(fail_render=fail_render, fail_once=fail_render_once, fail_close=fail_streamer_close),
        ha=FakeLightAdapter(fail=fail_light),
        hermes=FakeHermes(fail=fail_hermes),
    )


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@asynccontextmanager
async def make_client(deps: FakeDeps) -> AsyncIterator[httpx.AsyncClient]:
    from espagent_demo import server

    app = server.create_app(deps, render_interval=0.01)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            yield client


def test_importing_server_does_not_create_real_udp_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_socket(*args: object, **kwargs: object) -> socket.socket:
        raise AssertionError("server import must not create UDP sockets")

    monkeypatch.setattr(socket, "socket", fail_socket)

    from espagent_demo import server

    importlib.reload(server)


def test_default_video_test_uses_demo_video_file() -> None:
    from espagent_demo import server

    assert server.VIDEO_TEST_PATH.name == "test.mp4"


@pytest.mark.anyio
async def test_state_and_scene_endpoints_use_injected_dependencies_without_exposing_token() -> None:
    deps = make_deps()
    async with make_client(deps) as client:
        response = await client.post("/api/scene/link")
        state_response = await client.get("/api/state")

    assert response.status_code == 200
    assert response.json() == {"scene": "link"}
    assert state_response.status_code == 200
    assert state_response.json()["active_scene"] == "link"
    assert state_response.json()["software_version"] == APP_VERSION
    assert deps.streamer.previous is None
    assert state_response.json()["link"]["video_fast_enabled"] is True
    assert deps.streamer.tile_width == 80
    assert deps.streamer.tile_height == 8
    assert deps.streamer.packet_burst_size == 1
    assert deps.streamer.packet_gap_seconds == 0.001
    assert deps.streamer.render_calls >= 1
    assert "secret-token" not in state_response.text
    assert deps.streamer.closed is True
    assert deps.ha.closed is True


@pytest.mark.anyio
async def test_health_endpoint_reports_injected_services_and_esp32_state() -> None:
    deps = make_deps()
    deps.state.mark_status_seen("192.168.1.50")

    async with make_client(deps) as client:
        response = await client.get("/api/health")

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["version"] == APP_VERSION
    assert payload["services"]["hermes"]["mode"] == "injected"
    assert payload["services"]["home_assistant"]["mode"] == "injected"
    assert payload["services"]["esp32"] == {
        "ok": True,
        "service": "esp32",
        "ip": "192.168.1.50",
    }


@pytest.mark.anyio
async def test_firmware_api_scans_and_starts_without_client_parameters() -> None:
    deps = make_deps()
    flasher = FakeFirmwareFlasher()
    deps.firmware_flasher = flasher

    async with make_client(deps) as client:
        status = await client.get("/api/firmware/status")
        flash = await client.post("/api/firmware/flash")

    assert status.status_code == 200
    assert status.json()["device"]["device"] == "COM10"
    assert status.json()["can_flash"] is True
    assert flash.status_code == 202
    assert flash.json()["status"] == "detecting"
    assert flasher.started == 1
    assert flasher.closed is True


@pytest.mark.anyio
async def test_firmware_api_starts_usb_wifi_provisioning() -> None:
    deps = make_deps()
    flasher = FakeFirmwareFlasher()
    deps.firmware_flasher = flasher

    async with make_client(deps) as client:
        response = await client.post(
            "/api/firmware/wifi",
            json={"ssid": "Studio Wi-Fi", "password": "secret123"},
        )

    assert response.status_code == 202
    assert response.json()["wifi"]["status"] == "detecting"
    assert response.json()["wifi"]["ssid"] == "Studio Wi-Fi"
    assert flasher.wifi_started == 1


@pytest.mark.anyio
async def test_firmware_wifi_api_maps_validation_error() -> None:
    deps = make_deps()
    deps.firmware_flasher = FakeFirmwareFlasher(WifiProvisioningInvalid("bad wifi"))

    async with make_client(deps) as client:
        response = await client.post("/api/firmware/wifi", json={"ssid": "x", "password": ""})

    assert response.status_code == 422
    assert response.json()["detail"] == "bad wifi"


@pytest.mark.anyio
async def test_firmware_api_reads_and_writes_display_gpio() -> None:
    deps = make_deps()
    flasher = FakeFirmwareFlasher()
    deps.firmware_flasher = flasher
    values = {"scl": 18, "sda": 17, "cs": 16, "dc": 15, "rst": 14, "bl": 13}

    async with make_client(deps) as client:
        read_response = await client.post("/api/firmware/display-gpio/read")
        write_response = await client.post("/api/firmware/display-gpio", json=values)

    assert read_response.status_code == 202
    assert write_response.status_code == 202
    assert write_response.json()["display_gpio"]["values"] == values
    assert flasher.gpio_started == 2


@pytest.mark.anyio
async def test_firmware_display_gpio_api_maps_validation_error() -> None:
    deps = make_deps()
    deps.firmware_flasher = FakeFirmwareFlasher(DisplayGpioInvalid("duplicate gpio"))

    async with make_client(deps) as client:
        response = await client.post(
            "/api/firmware/display-gpio",
            json={"scl": 12, "sda": 11, "cs": 10, "dc": 8, "rst": 7, "bl": 6},
        )

    assert response.status_code == 422
    assert response.json()["detail"] == "duplicate gpio"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("error", "expected_status"),
    [
        (FirmwareFlashBusy("already running"), 409),
        (FirmwareFlashUnavailable("device missing"), 503),
    ],
)
async def test_firmware_api_maps_busy_and_unavailable_errors(
    error: Exception,
    expected_status: int,
) -> None:
    deps = make_deps()
    deps.firmware_flasher = FakeFirmwareFlasher(error)

    async with make_client(deps) as client:
        response = await client.post("/api/firmware/flash")

    assert response.status_code == expected_status
    assert response.json()["detail"] == str(error)


@pytest.mark.anyio
async def test_hermes_endpoint_rejects_blank_prompt() -> None:
    deps = make_deps()

    async with make_client(deps) as client:
        response = await client.post("/api/hermes", json={"prompt": "   "})

    assert response.status_code == 400
    assert response.json() == {"detail": "prompt must not be empty"}
    assert deps.hermes.prompts == []


@pytest.mark.anyio
async def test_invalid_scene_returns_400() -> None:
    deps = make_deps()
    async with make_client(deps) as client:
        response = await client.post("/api/scene/invalid")

    assert response.status_code == 400
    assert response.json() == {"detail": "scene must be hermes, lights, link, test, or video"}


@pytest.mark.anyio
async def test_hermes_endpoint_updates_state_with_adapter_reply() -> None:
    deps = make_deps()
    async with make_client(deps) as client:
        response = await client.post("/api/hermes", json={"prompt": "hello"})
        state_response = await client.get("/api/state")

    assert response.status_code == 200
    assert response.json() == {"reply": "fake reply: hello"}
    assert deps.hermes.prompts == ["hello"]
    state = state_response.json()
    assert state["active_scene"] == "hermes"
    assert state["hermes"]["status"] == "replying"
    assert state["hermes"]["last_reply"] == "fake reply: hello"
    assert state["hermes"]["last_user_message"] == "hello"


@pytest.mark.anyio
async def test_hermes_endpoint_preserves_chinese_prompt_text() -> None:
    deps = make_deps()
    prompt = "打开客厅主灯并设置亮度为 40%"

    async with make_client(deps) as client:
        response = await client.post("/api/hermes", json={"prompt": prompt})
        state_response = await client.get("/api/state")

    assert response.status_code == 200
    assert response.json() == {"reply": f"fake reply: {prompt}"}
    assert deps.hermes.prompts == [prompt]
    assert state_response.json()["hermes"]["last_reply"] == f"fake reply: {prompt}"


@pytest.mark.anyio
async def test_hermes_input_state_exposes_system_ime_composition() -> None:
    deps = make_deps()

    async with make_client(deps) as client:
        response = await client.post(
            "/api/hermes/input/state",
            json={
                "active": True,
                "available": True,
                "method": "Microsoft Pinyin",
                "draft": "请",
                "caret": 1,
                "preedit": "ni hao",
                "candidates": ["你好", "你号", "拟好"],
                "candidate_index": 1,
                "candidate_page": 0,
            },
        )
        state_response = await client.get("/api/state")

    assert response.status_code == 200
    assert response.json()["candidate_count"] == 3
    hermes = state_response.json()["hermes"]
    assert hermes["input_active"] is True
    assert hermes["input_draft"] == "请"
    assert hermes["input_caret"] == 1
    assert hermes["input_preedit"] == "ni hao"
    assert hermes["input_candidates"] == ["你好", "你号", "拟好"]
    assert hermes["input_candidate_index"] == 1


@pytest.mark.anyio
async def test_hermes_input_state_keeps_extended_candidate_list() -> None:
    deps = make_deps()
    candidates = [f"候选{index}" for index in range(40)]

    async with make_client(deps) as client:
        response = await client.post(
            "/api/hermes/input/state",
            json={"active": True, "preedit": "ni", "candidates": candidates, "candidate_index": 31},
        )
        state_response = await client.get("/api/state")

    assert response.json()["candidate_count"] == 32
    assert state_response.json()["hermes"]["input_candidates"] == candidates[:32]
    assert state_response.json()["hermes"]["input_candidate_index"] == 31


@pytest.mark.anyio
async def test_hermes_keyboard_submit_returns_immediately_and_records_user_message() -> None:
    deps = make_deps()
    prompt = "打开客厅主灯"
    deps.state.hermes.input_active = True
    deps.state.hermes.input_draft = prompt

    async with make_client(deps) as client:
        response = await client.post("/api/hermes/input/submit", json={"prompt": prompt})
        for _ in range(50):
            if deps.hermes.prompts:
                break
            await asyncio.sleep(0.01)
        state_response = await client.get("/api/state")

    assert response.status_code == 202
    assert response.json() == {"accepted": True, "prompt": prompt}
    assert deps.hermes.prompts == [prompt]
    hermes = state_response.json()["hermes"]
    assert hermes["last_user_message"] == prompt
    assert hermes["input_active"] is False
    assert hermes["input_draft"] == ""


@pytest.mark.anyio
async def test_hermes_stream_endpoint_forwards_gateway_events_and_finishes_state() -> None:
    deps = make_deps()
    deps.hermes = FakeStreamingHermes()

    async with make_client(deps) as client:
        response = await client.post("/api/hermes/stream", json={"prompt": "run probe"})
        state_response = await client.get("/api/state")

    assert response.status_code == 200
    events = [
        json.loads(line.removeprefix("data: "))
        for line in response.text.splitlines()
        if line.startswith("data: ")
    ]
    assert [event["type"] for event in events] == [
        "message.start",
        "tool.start",
        "tool.complete",
        "message.delta",
        "message.delta",
        "message.complete",
        "done",
    ]
    assert events[-1]["payload"] == {"reply": "stream reply"}
    hermes = state_response.json()["hermes"]
    assert hermes["streaming"] is False
    assert hermes["activity"] == "回复完成"
    assert hermes["last_reply"] == "stream reply"
    assert hermes["last_user_message"] == "run probe"


@pytest.mark.anyio
async def test_new_hermes_session_endpoint_resets_visible_state() -> None:
    deps = make_deps()
    deps.state.hermes.last_reply = "old reply"
    deps.state.hermes.last_user_message = "old user message"
    deps.state.hermes.streaming = True
    deps.state.hermes.active_tool = "terminal"
    deps.state.hermes.input_active = True
    deps.state.hermes.input_draft = "unfinished input"
    deps.state.hermes.input_preedit = "input"
    deps.state.hermes.input_candidates = ["输入"]
    deps.state.hermes.input_voice_active = True

    async with make_client(deps) as client:
        response = await client.post("/api/hermes/session/new")
        state_response = await client.get("/api/state")

    assert response.status_code == 200
    assert response.json() == {"session_id": "fake-session-new", "status": "ready"}
    hermes = state_response.json()["hermes"]
    assert hermes["session_id"] == "fake-session-new"
    assert hermes["last_reply"] == "等待 Hermes 回复"
    assert hermes["streaming"] is False
    assert hermes["active_tool"] is None
    assert hermes["activity"] == "新会话已就绪"
    assert hermes["last_user_message"] == ""
    assert hermes["input_active"] is False
    assert hermes["input_draft"] == ""
    assert hermes["input_preedit"] == ""
    assert hermes["input_candidates"] == []
    assert hermes["input_voice_active"] is False


@pytest.mark.anyio
async def test_interrupt_endpoint_stops_the_active_hermes_turn() -> None:
    deps = make_deps()
    deps.state.hermes.streaming = True

    async with make_client(deps) as client:
        response = await client.post("/api/hermes/interrupt")
        state_response = await client.get("/api/state")

    assert response.status_code == 200
    assert response.json() == {"status": "interrupted", "hermes": "interrupted"}
    assert deps.hermes.interrupts == 1
    assert state_response.json()["hermes"]["streaming"] is False
    assert state_response.json()["hermes"]["activity"] == "已打断"


@pytest.mark.anyio
async def test_voice_endpoints_control_pc_microphone_and_interrupt_before_recording() -> None:
    deps = make_deps()
    voice = FakeVoiceController()
    deps.voice = voice

    async with make_client(deps) as client:
        status_response = await client.get("/api/voice/status")
        enable_response = await client.post("/api/voice/enable", json={"enabled": True})
        start_response = await client.post("/api/voice/record/start")
        silence_response = await client.post("/api/voice/silence", json={"seconds": 1.5})
        stop_response = await client.post("/api/voice/record/stop")

    assert status_response.status_code == 200
    assert status_response.json()["available"] is True
    assert enable_response.json()["enabled"] is True
    assert start_response.json()["status"] == "listening"
    assert silence_response.json()["silence_duration_seconds"] == 1.5
    assert stop_response.json()["status"] == "transcribing"
    assert deps.hermes.interrupts == 1
    assert voice.started is True
    assert voice.closed is True


@pytest.mark.anyio
async def test_voice_silence_endpoint_rejects_out_of_range_value() -> None:
    deps = make_deps()
    deps.voice = FakeVoiceController()

    async with make_client(deps) as client:
        response = await client.post("/api/voice/silence", json={"seconds": 0.25})

    assert response.status_code == 400
    assert "between 0.5 and 10 seconds" in response.json()["detail"]


@pytest.mark.anyio
async def test_hermes_screen_voice_endpoint_uses_editable_draft_mode() -> None:
    deps = make_deps()
    voice = FakeVoiceController()
    deps.voice = voice
    deps.state.active_scene = "hermes"
    deps.state.display_ui.keyboard_control_enabled = True

    async with make_client(deps) as client:
        response = await client.post("/api/hermes/input/voice/start")
        stop_response = await client.post("/api/hermes/input/voice/stop")

    assert response.status_code == 200
    assert response.json()["status"] == "listening"
    assert stop_response.status_code == 200
    assert stop_response.json()["status"] == "transcribing"
    assert voice.destinations == ["draft"]
    assert deps.hermes.interrupts == 1


@pytest.mark.anyio
async def test_hermes_screen_voice_endpoint_requires_active_control_mode() -> None:
    deps = make_deps()
    deps.voice = FakeVoiceController()

    async with make_client(deps) as client:
        response = await client.post("/api/hermes/input/voice/start")

    assert response.status_code == 409


@pytest.mark.anyio
async def test_light_endpoint_records_errors_without_raising() -> None:
    deps = make_deps(fail_light=True)
    async with make_client(deps) as client:
        response = await client.post("/api/light", json={"on": True, "brightness": 40})

    assert response.status_code == 200
    payload = response.json()
    assert payload["light"]["error"] == "Light command failed: RuntimeError"
    assert deps.state.active_scene == "lights"
    assert deps.ha.commands == [(True, 40, None)]


@pytest.mark.anyio
async def test_light_endpoint_clamps_and_forwards_color_temperature() -> None:
    deps = make_deps()
    deps.state.light.min_color_temp_kelvin = 2600
    deps.state.light.max_color_temp_kelvin = 5100

    async with make_client(deps) as client:
        response = await client.post(
            "/api/light",
            json={"on": True, "brightness": 55, "color_temp_kelvin": 5800},
        )

    assert response.status_code == 200
    assert deps.ha.commands == [(True, 55, 5100)]


@pytest.mark.anyio
async def test_render_loop_continues_after_transient_render_failure() -> None:
    deps = make_deps(fail_render_once=True)

    async with make_client(deps):
        for _ in range(100):
            if deps.streamer.render_calls >= 2:
                break
            await asyncio.sleep(0.01)

    assert deps.streamer.render_calls >= 2
    assert deps.state.link.error == "Render loop failed: RuntimeError"
    assert deps.streamer.closed is True
    assert deps.ha.closed is True


@pytest.mark.anyio
async def test_hermes_endpoint_records_sanitized_adapter_error() -> None:
    deps = make_deps(fail_hermes=True)

    async with make_client(deps) as client:
        response = await client.post("/api/hermes", json={"prompt": "hello"})
        state_response = await client.get("/api/state")

    assert response.status_code == 200
    assert response.json() == {"error": "Hermes command failed: RuntimeError"}
    assert deps.streamer.previous is None
    state = state_response.json()
    assert state["active_scene"] == "hermes"
    assert state["hermes"]["status"] == "error"
    assert state["hermes"]["last_reply"] == "Hermes command failed: RuntimeError"
    assert "secret-token" not in response.text
    assert "wsl command" not in response.text


@pytest.mark.anyio
async def test_lifespan_cleanup_closes_ha_when_streamer_close_fails() -> None:
    deps = make_deps(fail_streamer_close=True)

    async with make_client(deps):
        pass

    assert deps.streamer.closed is True
    assert deps.ha.closed is True


@pytest.mark.anyio
async def test_preview_endpoint_returns_current_screen_png() -> None:
    deps = make_deps()
    deps.state.link.frame_id = 42

    async with make_client(deps) as client:
        response = await client.get("/api/preview.png")

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert int(response.headers["x-frame-id"]) > 0
    assert response.headers["cache-control"] == "no-store"
    assert response.content.startswith(b"\x89PNG")


@pytest.mark.anyio
async def test_test_pattern_endpoint_sets_scene_and_resets_stream_baseline() -> None:
    deps = make_deps()

    async with make_client(deps) as client:
        response = await client.post("/api/test-pattern/colorbars")
        state_response = await client.get("/api/state")

    assert response.status_code == 200
    assert response.json() == {"pattern": "colorbars"}
    assert deps.streamer.previous is None
    state = state_response.json()
    assert state["active_scene"] == "test"
    assert state["test_pattern"] == "colorbars"


@pytest.mark.anyio
async def test_color_test_pattern_endpoint_selects_color_pattern() -> None:
    deps = make_deps()

    async with make_client(deps) as client:
        response = await client.post("/api/test-pattern/colors")
        state_response = await client.get("/api/state")

    assert response.status_code == 200
    assert response.json() == {"pattern": "colors"}
    assert state_response.json()["test_pattern"] == "colors"


@pytest.mark.anyio
@pytest.mark.parametrize("pattern", ["palette256", "channels"])
async def test_indexed_color_diagnostic_pattern_endpoints(pattern: str) -> None:
    deps = make_deps()

    async with make_client(deps) as client:
        response = await client.post(f"/api/test-pattern/{pattern}")

    assert response.status_code == 200
    assert response.json() == {"pattern": pattern}
    assert deps.state.test_pattern == pattern


@pytest.mark.anyio
async def test_duotone_test_pattern_endpoint_updates_custom_parameters() -> None:
    deps = make_deps()

    async with make_client(deps) as client:
        response = await client.post(
            "/api/test-pattern/duotone",
            json={
                "background": "#f8fafc",
                "text_color": "#111827",
                "text": "Aa 汉字 123",
                "preset": "paper",
            },
        )
        state_response = await client.get("/api/state")

    assert response.status_code == 200
    payload = response.json()
    assert payload["pattern"] == "duotone"
    assert payload["duotone"]["background"] == "#F8FAFC"
    assert payload["duotone"]["text_color"] == "#111827"
    assert deps.streamer.previous is None
    state = state_response.json()
    assert state["active_scene"] == "test"
    assert state["test_pattern"] == "duotone"
    assert state["duotone"]["text"] == "Aa 汉字 123"


@pytest.mark.anyio
async def test_duotone_test_pattern_endpoint_rejects_invalid_colors() -> None:
    deps = make_deps()

    async with make_client(deps) as client:
        response = await client.post("/api/test-pattern/duotone", json={"background": "white"})

    assert response.status_code == 400
    assert response.json() == {"detail": "background must be #RRGGBB"}


@pytest.mark.anyio
async def test_video_start_endpoint_sets_scene_and_resets_stream_baseline() -> None:
    deps = make_deps()

    async with make_client(deps) as client:
        response = await client.post("/api/video/start")
        state_response = await client.get("/api/state")

    assert response.status_code == 200
    assert response.json() == {"scene": "video"}
    assert deps.streamer.previous is None
    state = state_response.json()
    assert state["active_scene"] == "video"
    assert state["link"]["video_fast_enabled"] is True
    assert deps.streamer.tile_width == 80
    assert deps.streamer.tile_height == 8
    assert deps.streamer.packet_burst_size == 1
    assert deps.streamer.packet_gap_seconds == 0.001
    assert deps.streamer.frame_end_repeats == 2
    assert deps.streamer.full_refresh_tile_repeats == 1
    assert deps.streamer.full_refresh_interval == 0
    assert deps.streamer.resend_frame_end_on_nack is True


def test_render_sleep_uses_fast_interval_for_all_scenes() -> None:
    from espagent_demo.server import _render_sleep_seconds

    video_state = DemoState(active_scene="video")
    normal_state = DemoState(active_scene="hermes")

    assert _render_sleep_seconds(video_state, render_interval=0.2, elapsed_seconds=0.01) == pytest.approx(0.19)
    assert _render_sleep_seconds(video_state, render_interval=0.2, elapsed_seconds=0.25) == 0
    assert _render_sleep_seconds(normal_state, render_interval=0.2, elapsed_seconds=0.01) == pytest.approx(0.19)


def test_refresh_request_wakes_display_worker_without_sync_render() -> None:
    from espagent_demo import server

    class FakeDisplayWorker:
        def __init__(self) -> None:
            self.wake_calls = 0

        def wake(self) -> None:
            self.wake_calls += 1

    deps = make_deps()
    worker = FakeDisplayWorker()
    deps.display_worker = worker

    server._request_and_push_refresh(deps, frames=6)

    assert worker.wake_calls == 1
    assert deps.streamer.render_calls == 0
    assert deps.streamer.previous is None


def test_preview_cache_interval_uses_runtime_limit() -> None:
    from espagent_demo import server

    deps = make_deps()
    deps.state.link.preview_fps_limited = True
    deps.state.link.preview_fps_limit = 20

    assert server._preview_cache_interval(deps) == pytest.approx(0.05)

    deps.state.link.preview_fps_limited = False
    assert server._preview_cache_interval(deps) == 0.0


@pytest.mark.anyio
async def test_unified_flush_endpoint_updates_state_and_streamer() -> None:
    deps = make_deps()

    async with make_client(deps) as client:
        response = await client.post("/api/display/unified-flush", json={"enabled": False})
        state_response = await client.get("/api/state")

    assert response.status_code == 200
    assert response.json() == {"unified_flush": False}
    assert state_response.json()["link"]["unified_flush"] is False
    assert deps.streamer.unified_flush is False


@pytest.mark.anyio
async def test_transport_endpoint_updates_state_and_streamer() -> None:
    deps = make_deps()

    async with make_client(deps) as client:
        response = await client.post(
            "/api/display/transport",
            json={
                "compression_mode": "raw",
                "nack_enabled": False,
                "resend_frame_end_on_nack": False,
                "esp_log_enabled": True,
                "backlight_percent": 55,
            },
        )
        state_response = await client.get("/api/state")

    assert response.status_code == 200
    assert response.json() == {
        "transport_mode": "indexed",
        "compression_mode": "raw",
        "nack_enabled": False,
        "resend_frame_end_on_nack": False,
        "esp_log_enabled": True,
        "backlight_percent": 55,
        "panel_gamma": "1.8",
        "tile_width": 120,
        "tile_height": 6,
        "mjpeg_quality": 90,
        "mjpeg_subsampling": "420",
        "indexed_bpp": 2,
        "indexed_protected_colors": [None, None, None, None],
        "indexed_keyframe_interval": 60,
        "preview_fps_limited": False,
        "preview_fps_limit": 30,
    }
    link = state_response.json()["link"]
    assert link["compression_mode"] == "raw"
    assert link["nack_enabled"] is False
    assert link["resend_frame_end_on_nack"] is False
    assert link["esp_log_enabled"] is True
    assert link["backlight_percent"] == 55
    assert deps.streamer.compression_mode == "raw"
    assert deps.streamer.esp_log_enabled is True
    assert deps.streamer.backlight_percent == 55


@pytest.mark.anyio
async def test_transport_endpoint_accepts_i4_and_i8_indexed_formats() -> None:
    deps = make_deps()

    async with make_client(deps) as client:
        i4_response = await client.post("/api/display/transport", json={"indexed_bpp": 4})
        i8_response = await client.post("/api/display/transport", json={"indexed_bpp": 8})
        invalid_response = await client.post("/api/display/transport", json={"indexed_bpp": 3})

    assert i4_response.status_code == 200
    assert i4_response.json()["indexed_bpp"] == 4
    assert len(i4_response.json()["indexed_protected_colors"]) == 16
    assert i8_response.status_code == 200
    assert i8_response.json()["indexed_bpp"] == 8
    assert len(i8_response.json()["indexed_protected_colors"]) == 256
    assert deps.streamer.indexed_bpp == 8
    assert len(deps.streamer.indexed_protected_colors) == 256
    assert invalid_response.status_code == 400


@pytest.mark.anyio
async def test_transport_endpoint_updates_generic_protected_color_slots() -> None:
    deps = make_deps()

    async with make_client(deps) as client:
        first_response = await client.post(
            "/api/display/transport",
            json={"indexed_protected_colors": ["#123456", None, None, None]},
        )
        second_response = await client.post(
            "/api/display/transport",
            json={"indexed_protected_colors": [None, "#abcdef", None, None]},
        )

    assert first_response.status_code == 200
    assert first_response.json()["indexed_protected_colors"] == ["#123456", None, None, None]
    assert second_response.status_code == 200
    assert second_response.json()["indexed_protected_colors"] == [None, "#ABCDEF", None, None]
    assert deps.state.link.indexed_protected_colors == [None, "#ABCDEF", None, None]
    assert deps.streamer.indexed_protected_colors == [None, "#ABCDEF", None, None]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "colors",
    [
        ["#000000"],
        ["#000000", "#FFFFFF", None, None, None],
        ["not-a-color", None, None, None],
    ],
)
async def test_transport_endpoint_rejects_invalid_protected_color_slots(colors: list[str | None]) -> None:
    deps = make_deps()

    async with make_client(deps) as client:
        response = await client.post(
            "/api/display/transport",
            json={"indexed_protected_colors": colors},
        )

    assert response.status_code == 400


@pytest.mark.anyio
async def test_transport_endpoint_updates_runtime_tile_size() -> None:
    deps = make_deps()

    async with make_client(deps) as client:
        response = await client.post("/api/display/transport", json={"tile_width": 80, "tile_height": 8})
        state_response = await client.get("/api/state")

    assert response.status_code == 200
    assert response.json()["tile_width"] == 80
    assert response.json()["tile_height"] == 8
    link = state_response.json()["link"]
    assert link["tile_width"] == 80
    assert link["tile_height"] == 8
    assert deps.streamer.tile_width == 80
    assert deps.streamer.tile_height == 8


@pytest.mark.anyio
async def test_transport_endpoint_updates_mjpeg_quality() -> None:
    deps = make_deps()

    async with make_client(deps) as client:
        response = await client.post("/api/display/transport", json={"mjpeg_quality": 35})
        state_response = await client.get("/api/state")

    assert response.status_code == 200
    assert response.json()["mjpeg_quality"] == 35
    assert state_response.json()["link"]["mjpeg_quality"] == 35
    assert deps.streamer.mjpeg_quality == 35


@pytest.mark.anyio
async def test_transport_endpoint_updates_mjpeg_subsampling() -> None:
    deps = make_deps()

    async with make_client(deps) as client:
        response = await client.post("/api/display/transport", json={"mjpeg_subsampling": "444"})
        state_response = await client.get("/api/state")

    assert response.status_code == 200
    assert response.json()["mjpeg_subsampling"] == "444"
    assert state_response.json()["link"]["mjpeg_subsampling"] == "444"
    assert deps.streamer.mjpeg_subsampling == "444"


@pytest.mark.anyio
async def test_transport_endpoint_updates_preview_fps_limit() -> None:
    deps = make_deps()

    async with make_client(deps) as client:
        response = await client.post(
            "/api/display/transport",
            json={"preview_fps_limited": False, "preview_fps_limit": 30},
        )
        state_response = await client.get("/api/state")

    assert response.status_code == 200
    assert response.json()["preview_fps_limited"] is False
    assert response.json()["preview_fps_limit"] == 30
    link = state_response.json()["link"]
    assert link["preview_fps_limited"] is False
    assert link["preview_fps_limit"] == 30


@pytest.mark.anyio
async def test_display_tone_endpoint_updates_runtime_tone_controls() -> None:
    deps = make_deps()

    async with make_client(deps) as client:
        response = await client.post(
            "/api/display/tone",
            json={
                "enabled": True,
                "preset": "high-contrast",
                "contrast_percent": 165,
                "brightness_percent": 108,
                "color_percent": 125,
                "sharpness_percent": 150,
                "binary_threshold": 0,
            },
        )
        state_response = await client.get("/api/state")

    assert response.status_code == 200
    payload = response.json()["display_tone"]
    assert payload["enabled"] is True
    assert payload["preset"] == "high-contrast"
    assert payload["contrast_percent"] == 165
    assert payload["brightness_percent"] == 108
    assert payload["color_percent"] == 125
    assert payload["sharpness_percent"] == 150
    assert payload["binary_threshold"] == 0
    assert state_response.json()["display_tone"] == payload
    assert deps.streamer.previous is None


@pytest.mark.anyio
async def test_display_tone_endpoint_rejects_invalid_values() -> None:
    deps = make_deps()

    async with make_client(deps) as client:
        contrast_response = await client.post("/api/display/tone", json={"contrast_percent": 49})
        binary_response = await client.post("/api/display/tone", json={"binary_threshold": 255})

    assert contrast_response.status_code == 400
    assert binary_response.status_code == 400


@pytest.mark.anyio
async def test_transport_endpoint_rejects_invalid_tile_size() -> None:
    deps = make_deps()

    async with make_client(deps) as client:
        width_response = await client.post("/api/display/transport", json={"tile_width": 0})
        height_response = await client.post("/api/display/transport", json={"tile_height": 321})

    assert width_response.status_code == 400
    assert height_response.status_code == 400


@pytest.mark.anyio
async def test_transport_endpoint_rejects_invalid_mjpeg_quality() -> None:
    deps = make_deps()

    async with make_client(deps) as client:
        low_response = await client.post("/api/display/transport", json={"mjpeg_quality": 9})
        high_response = await client.post("/api/display/transport", json={"mjpeg_quality": 96})

    assert low_response.status_code == 400
    assert high_response.status_code == 400


@pytest.mark.anyio
async def test_transport_endpoint_rejects_invalid_mjpeg_subsampling() -> None:
    deps = make_deps()

    async with make_client(deps) as client:
        response = await client.post("/api/display/transport", json={"mjpeg_subsampling": "411"})

    assert response.status_code == 400


@pytest.mark.anyio
async def test_display_ui_defaults_to_first_theme_and_switches_at_runtime() -> None:
    deps = make_deps()

    async with make_client(deps) as client:
        initial = await client.get("/api/state")
        response = await client.post(
            "/api/display/ui",
            json={
                "theme": "second",
                "orientation": "landscape",
                "font_chinese": "editorial",
                "font_latin": "industrial",
                "font_numeric": "mono",
                "scale_percent": 110,
                "motion_enabled": False,
                "font_hinting": "smooth",
                "grayscale_antialiasing": True,
            },
        )
        updated = await client.get("/api/state")

    assert initial.json()["display_ui"] == {
        "theme": "first",
        "orientation": "portrait",
        "font_chinese": "auto",
        "font_latin": "auto",
        "font_numeric": "auto",
        "scale_percent": 100,
        "motion_enabled": True,
        "font_hinting": "auto",
        "grayscale_antialiasing": False,
        "scroll_delta": 0,
        "scroll_revision": 0,
        "keyboard_control_enabled": False,
        "global_hotkeys_available": False,
        "shortcut_toggle_modifier": "alt",
        "shortcut_toggle_key": "backquote",
        "shortcut_navigation_modifier": "none",
        "shortcut_scroll_modifier": "alt",
        "shortcut_navigation_mode": "none",
        "shortcut_navigation_prefix": "chord:vk:18:Alt",
        "shortcut_scroll_mode": "custom",
        "shortcut_scroll_prefix": "chord:vk:18:Alt",
        "shortcut_voice_modifier": "ctrl_alt",
        "shortcut_voice_key": "v",
        "shortcut_exit_mode": "same",
        "shortcut_exit_modifier": "alt",
        "shortcut_exit_key": "backquote",
    }
    assert response.status_code == 200
    assert response.json()["display_ui"] == {
        "theme": "second",
        "orientation": "landscape",
        "font_chinese": "editorial",
        "font_latin": "industrial",
        "font_numeric": "mono",
        "scale_percent": 110,
        "motion_enabled": False,
        "font_hinting": "smooth",
        "grayscale_antialiasing": True,
        "scroll_delta": 0,
        "scroll_revision": 0,
        "keyboard_control_enabled": False,
        "global_hotkeys_available": False,
        "shortcut_toggle_modifier": "alt",
        "shortcut_toggle_key": "backquote",
        "shortcut_navigation_modifier": "none",
        "shortcut_scroll_modifier": "alt",
        "shortcut_navigation_mode": "none",
        "shortcut_navigation_prefix": "chord:vk:18:Alt",
        "shortcut_scroll_mode": "custom",
        "shortcut_scroll_prefix": "chord:vk:18:Alt",
        "shortcut_voice_modifier": "ctrl_alt",
        "shortcut_voice_key": "v",
        "shortcut_exit_mode": "same",
        "shortcut_exit_modifier": "alt",
        "shortcut_exit_key": "backquote",
    }
    assert updated.json()["display_ui"] == response.json()["display_ui"]


@pytest.mark.anyio
async def test_display_ui_rejects_unknown_theme() -> None:
    deps = make_deps()

    async with make_client(deps) as client:
        response = await client.post("/api/display/ui", json={"theme": "unknown"})
        font_response = await client.post("/api/display/ui", json={"font": "unknown"})
        hinting_response = await client.post("/api/display/ui", json={"font_hinting": "strong"})

    assert response.status_code == 400
    assert response.json() == {"detail": "theme must be first or second"}
    assert font_response.status_code == 400
    assert font_response.json() == {"detail": "unknown display UI font preset"}
    assert hinting_response.status_code == 400
    assert hinting_response.json() == {"detail": "font_hinting must be auto, crisp, or smooth"}


@pytest.mark.anyio
async def test_display_scroll_publishes_bounded_relative_command() -> None:
    deps = make_deps()
    deps.state.hermes.input_active = True

    async with make_client(deps) as client:
        first = await client.post("/api/display/scroll", json={"delta": 36})
        second = await client.post("/api/display/scroll", json={"delta": -36})
        invalid = await client.post("/api/display/scroll", json={"delta": 0})
        state = await client.get("/api/state")

    assert first.json() == {"delta": 36, "revision": 1}
    assert second.json() == {"delta": -36, "revision": 2}
    assert invalid.status_code == 400
    assert state.json()["display_ui"]["scroll_delta"] == -36
    assert state.json()["display_ui"]["scroll_revision"] == 2
    assert state.json()["hermes"]["input_active"] is False
    assert deps.streamer.previous is None


@pytest.mark.anyio
async def test_display_control_state_synchronizes_desktop_and_global_runtime() -> None:
    deps = make_deps()
    deps.state.link.esp32_online = True

    async with make_client(deps) as client:
        enabled = await client.post(
            "/api/display/control",
            json={"enabled": True, "global_hotkeys_available": True},
        )
        disabled = await client.post("/api/display/control", json={"enabled": False})
        state = await client.get("/api/state")

    assert enabled.json() == {"enabled": True, "global_hotkeys_available": True}
    assert disabled.json() == {"enabled": False, "global_hotkeys_available": True}
    assert state.json()["display_ui"]["keyboard_control_enabled"] is False
    assert state.json()["display_ui"]["global_hotkeys_available"] is True


@pytest.mark.anyio
async def test_display_control_rejects_enable_while_esp32_is_offline() -> None:
    deps = make_deps()

    async with make_client(deps) as client:
        response = await client.post("/api/display/control", json={"enabled": True})
        state = await client.get("/api/state")

    assert response.status_code == 409
    assert response.json()["detail"] == "ESP32 is offline"
    assert state.json()["display_ui"]["keyboard_control_enabled"] is False


@pytest.mark.anyio
async def test_shortcut_settings_accept_arbitrary_supported_key_combinations_atomically() -> None:
    deps = make_deps()

    async with make_client(deps) as client:
        initial = await client.get("/api/state")
        updated = await client.post(
            "/api/settings/shortcuts",
            json={
                "toggle_modifier": "ctrl_shift_alt_win",
                "toggle_key": "chord:vk:17:ControlLeft,vk:179:MediaPlayPause",
                "navigation_modifier": "shift",
                "scroll_modifier": "shift_alt",
                "navigation_mode": "custom",
                "navigation_prefix": "chord:vk:17:ControlLeft,vk:16:ShiftLeft",
                "scroll_mode": "disabled",
                "scroll_prefix": "chord:vk:18:Alt",
                "voice_modifier": "shift",
                "voice_key": "chord:vk:18:AltLeft,vk:226:IntlBackslash",
                "exit_mode": "custom",
                "exit_modifier": "win",
                "exit_key": "vk:166:BrowserBack",
            },
        )
        invalid = await client.post(
            "/api/settings/shortcuts",
            json={"toggle_key": "unsupported-key"},
        )
        state = await client.get("/api/state")

    assert initial.json()["display_ui"]["shortcut_scroll_modifier"] == "alt"
    assert updated.json()["shortcuts"] == {
        "toggle_modifier": "ctrl_shift_alt_win",
        "toggle_key": "chord:vk:17:ControlLeft,vk:179:MediaPlayPause",
        "navigation_modifier": "shift",
        "scroll_modifier": "shift_alt",
        "navigation_mode": "custom",
        "navigation_prefix": "chord:vk:17:ControlLeft,vk:16:ShiftLeft",
        "scroll_mode": "disabled",
        "scroll_prefix": "chord:vk:18:Alt",
        "voice_modifier": "shift",
        "voice_key": "chord:vk:18:AltLeft,vk:226:IntlBackslash",
        "exit_mode": "custom",
        "exit_modifier": "win",
        "exit_key": "vk:166:BrowserBack",
    }
    assert invalid.status_code == 400
    assert state.json()["display_ui"]["shortcut_toggle_key"] == "chord:vk:17:ControlLeft,vk:179:MediaPlayPause"
    assert state.json()["display_ui"]["shortcut_scroll_modifier"] == "shift_alt"
    assert state.json()["display_ui"]["shortcut_navigation_mode"] == "custom"
    assert state.json()["display_ui"]["shortcut_scroll_mode"] == "disabled"
    assert state.json()["display_ui"]["shortcut_voice_key"] == "chord:vk:18:AltLeft,vk:226:IntlBackslash"
    assert state.json()["display_ui"]["shortcut_exit_mode"] == "custom"


@pytest.mark.anyio
async def test_display_ui_enforces_theme_font_allowlist_and_falls_back_on_theme_change() -> None:
    deps = make_deps()

    async with make_client(deps) as client:
        humanist = await client.post(
            "/api/display/ui",
            json={"theme": "first", "font_chinese": "humanist", "font_latin": "industrial", "font_numeric": "mono"},
        )
        switched = await client.post("/api/display/ui", json={"theme": "second"})
        invalid = await client.post("/api/display/ui", json={"theme": "second", "font_chinese": "humanist"})

    assert humanist.json()["display_ui"]["font_chinese"] == "humanist"
    assert switched.json()["display_ui"]["font_chinese"] == "auto"
    assert switched.json()["display_ui"]["font_latin"] == "industrial"
    assert switched.json()["display_ui"]["font_numeric"] == "mono"
    assert invalid.status_code == 400
    assert invalid.json() == {"detail": "chinese font is not supported by second theme"}


@pytest.mark.anyio
async def test_first_display_ui_theme_accepts_every_registered_font_and_scale_is_validated() -> None:
    deps = make_deps()
    role_fonts = {
        "font_chinese": ("auto", "modern", "ui", "editorial", "humanist", "pixel"),
        "font_latin": ("auto", "modern", "ui", "editorial", "industrial", "pixel"),
        "font_numeric": ("auto", "modern", "editorial", "industrial", "mono", "pixel"),
    }

    async with make_client(deps) as client:
        responses = {
            role: [await client.post("/api/display/ui", json={"theme": "first", role: font}) for font in fonts]
            for role, fonts in role_fonts.items()
        }
        pixel_preset = await client.post("/api/display/ui", json={"theme": "first", "font": "pixel"})
        invalid_scale = await client.post("/api/display/ui", json={"scale_percent": 111})

    for role, fonts in role_fonts.items():
        assert [response.json()["display_ui"][role] for response in responses[role]] == list(fonts)
    assert pixel_preset.json()["display_ui"]["font_chinese"] == "pixel"
    assert pixel_preset.json()["display_ui"]["font_latin"] == "pixel"
    assert pixel_preset.json()["display_ui"]["font_numeric"] == "pixel"
    assert invalid_scale.status_code == 400
    assert invalid_scale.json() == {"detail": "scale_percent must be 90, 95, 100, 105, or 110"}


@pytest.mark.anyio
async def test_transport_endpoint_rejects_invalid_preview_fps_limit() -> None:
    deps = make_deps()

    async with make_client(deps) as client:
        low_response = await client.post("/api/display/transport", json={"preview_fps_limit": 0})
        high_response = await client.post("/api/display/transport", json={"preview_fps_limit": 61})

    assert low_response.status_code == 400
    assert high_response.status_code == 400


@pytest.mark.anyio
async def test_transport_endpoint_rejects_invalid_backlight_percent() -> None:
    deps = make_deps()

    async with make_client(deps) as client:
        low_response = await client.post("/api/display/transport", json={"backlight_percent": -1})
        high_response = await client.post("/api/display/transport", json={"backlight_percent": 101})

    assert low_response.status_code == 400
    assert high_response.status_code == 400


@pytest.mark.anyio
async def test_transport_endpoint_updates_and_validates_panel_gamma() -> None:
    deps = make_deps()

    async with make_client(deps) as client:
        response = await client.post("/api/display/transport", json={"panel_gamma": "2.2"})
        invalid = await client.post("/api/display/transport", json={"panel_gamma": "3.0"})

    assert response.status_code == 200
    assert response.json()["panel_gamma"] == "2.2"
    assert deps.state.link.panel_gamma == "2.2"
    assert deps.streamer.panel_gamma == "2.2"
    assert invalid.status_code == 400


@pytest.mark.anyio
async def test_transport_endpoint_accepts_lz4_compression_mode() -> None:
    deps = make_deps()

    async with make_client(deps) as client:
        response = await client.post("/api/display/transport", json={"compression_mode": "lz4"})

    assert response.status_code == 200
    assert response.json()["compression_mode"] == "lz4"
    assert deps.streamer.compression_mode == "lz4"


@pytest.mark.anyio
async def test_transport_endpoint_switches_between_udp_and_webrtc() -> None:
    deps = make_deps()

    async with make_client(deps) as client:
        response = await client.post("/api/display/transport", json={"transport_mode": "webrtc"})
        state_response = await client.get("/api/state")

    assert response.status_code == 200
    assert response.json()["transport_mode"] == "webrtc"
    assert state_response.json()["link"]["transport_mode"] == "webrtc"
    assert deps.streamer.transport_mode == "webrtc"


@pytest.mark.anyio
async def test_transport_endpoint_accepts_mjpeg_mode() -> None:
    deps = make_deps()

    async with make_client(deps) as client:
        response = await client.post("/api/display/transport", json={"transport_mode": "mjpeg"})
        state_response = await client.get("/api/state")

    assert response.status_code == 200
    assert response.json()["transport_mode"] == "mjpeg"
    assert state_response.json()["link"]["transport_mode"] == "mjpeg"
    assert deps.streamer.transport_mode == "mjpeg"


@pytest.mark.anyio
async def test_webrtc_mode_keeps_udp_render_fallback_for_demo() -> None:
    deps = make_deps()

    async with make_client(deps) as client:
        await client.post("/api/display/transport", json={"transport_mode": "webrtc"})
        calls_after_switch = deps.streamer.render_calls
        await asyncio.sleep(0.07)

    assert deps.streamer.render_calls > calls_after_switch


@pytest.mark.anyio
async def test_rgb565_frame_endpoint_returns_full_rendered_frame() -> None:
    deps = make_deps()

    async with make_client(deps) as client:
        response = await client.get("/api/display/frame.rgb565")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/octet-stream"
    assert len(response.content) == 240 * 320 * 2


@pytest.mark.anyio
async def test_mjpeg_stream_endpoint_returns_multipart_jpeg_frame() -> None:
    deps = make_deps()

    async with make_client(deps) as client:
        response = await client.get("/api/display/mjpeg?frames=1")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("multipart/x-mixed-replace")
    assert b"--frame" in response.content
    assert b"Content-Type: image/jpeg" in response.content
    assert b"\xff\xd8" in response.content
    assert b"\xff\xd9" in response.content


@pytest.mark.anyio
async def test_mjpeg_stream_reuses_frame_encoded_for_esp32() -> None:
    deps = make_deps()
    cached_jpeg = b"\xff\xd8cached-for-esp32\xff\xd9"
    deps.streamer.latest_mjpeg_frame = lambda: (17, cached_jpeg)

    async with make_client(deps) as client:
        response = await client.get("/api/display/mjpeg?frames=1")

    assert response.status_code == 200
    assert cached_jpeg in response.content


@pytest.mark.anyio
async def test_mjpeg_stream_follows_frame_confirmed_by_esp32() -> None:
    deps = make_deps()
    sent_jpeg = b"\xff\xd8newer-sent-frame\xff\xd9"
    displayed_jpeg = b"\xff\xd8frame-on-lcd\xff\xd9"
    deps.state.link.displayed_frame_id = 17
    deps.streamer.latest_mjpeg_frame = lambda: (18, sent_jpeg)
    deps.streamer.latest_displayed_mjpeg_frame = lambda: (17, displayed_jpeg)

    async with make_client(deps) as client:
        response = await client.get("/api/display/mjpeg?frames=1")

    assert response.status_code == 200
    assert b"X-Frame-Id: 17" in response.content
    assert displayed_jpeg in response.content
    assert sent_jpeg not in response.content


@pytest.mark.anyio
async def test_preview_jpeg_reuses_frame_confirmed_by_esp32() -> None:
    deps = make_deps()
    displayed_jpeg = b"\xff\xd8preview-frame-on-lcd\xff\xd9"
    deps.state.link.displayed_frame_id = 23
    deps.streamer.latest_displayed_mjpeg_frame = lambda: (23, displayed_jpeg)

    async with make_client(deps) as client:
        response = await client.get("/api/display/preview.jpg")

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/jpeg"
    assert response.headers["x-frame-id"] == "23"
    assert response.headers["cache-control"] == "no-store"
    assert response.content == displayed_jpeg


@pytest.mark.anyio
async def test_webrtc_signaling_mailbox_round_trips_offer_and_answer() -> None:
    deps = make_deps()

    async with make_client(deps) as client:
        offer_response = await client.post(
            "/api/webrtc/offer",
            json={"sdp": "v=0\r\n", "type": "offer"},
        )
        offer_id = offer_response.json()["offer_id"]
        latest_offer = await client.get("/api/webrtc/offer/latest")
        answer_response = await client.post(
            f"/api/webrtc/answer/{offer_id}",
            json={"sdp": "v=0\r\n", "type": "answer"},
        )
        answer = await client.get(f"/api/webrtc/answer/{offer_id}")

    assert latest_offer.status_code == 200
    assert latest_offer.json() == {"offer_id": offer_id, "sdp": "v=0\r\n", "type": "offer"}
    assert answer_response.status_code == 200
    assert answer.json() == {"offer_id": offer_id, "sdp": "v=0\r\n", "type": "answer"}


@pytest.mark.anyio
async def test_webrtc_signaling_reset_clears_mailbox() -> None:
    deps = make_deps()

    async with make_client(deps) as client:
        offer_response = await client.post("/api/webrtc/offer", json={"sdp": "v=0\r\n", "type": "offer"})
        offer_id = offer_response.json()["offer_id"]
        await client.post(f"/api/webrtc/answer/{offer_id}", json={"sdp": "v=0\r\n", "type": "answer"})
        reset_response = await client.post("/api/webrtc/reset")
        diagnostics = await client.get("/api/webrtc/diagnostics")
        latest_offer = await client.get("/api/webrtc/offer/latest")

    assert reset_response.status_code == 200
    assert reset_response.json() == {"reset": True}
    assert diagnostics.json() == {"offer_id": None, "offer_candidates": [], "answer_candidates": []}
    assert latest_offer.status_code == 404


@pytest.mark.anyio
async def test_webrtc_diagnostics_extracts_latest_candidates() -> None:
    deps = make_deps()
    offer_sdp = (
        "v=0\r\n"
        "a=candidate:1 1 udp 2113937151 172.18.0.1 56404 typ host generation 0\r\n"
        "a=candidate:2 1 udp 2113937151 192.168.1.4 50000 typ host generation 0\r\n"
    )
    answer_sdp = "v=0\r\na=candidate:3 1 udp 2113937151 192.168.1.7 64333 typ host generation 0\r\n"

    async with make_client(deps) as client:
        offer_response = await client.post("/api/webrtc/offer", json={"sdp": offer_sdp, "type": "offer"})
        offer_id = offer_response.json()["offer_id"]
        await client.post(f"/api/webrtc/answer/{offer_id}", json={"sdp": answer_sdp, "type": "answer"})
        response = await client.get("/api/webrtc/diagnostics")

    assert response.status_code == 200
    payload = response.json()
    assert payload["offer_id"] == offer_id
    assert payload["offer_candidates"] == [
        {"foundation": "1", "protocol": "udp", "address": "172.18.0.1", "port": 56404, "type": "host"},
        {"foundation": "2", "protocol": "udp", "address": "192.168.1.4", "port": 50000, "type": "host"},
    ]
    assert payload["answer_candidates"] == [
        {"foundation": "3", "protocol": "udp", "address": "192.168.1.7", "port": 64333, "type": "host"}
    ]


@pytest.mark.anyio
async def test_webrtc_chrome_launch_uses_isolated_profile_and_ip_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    from espagent_demo import server

    deps = make_deps()
    launched: dict[str, object] = {}

    class FakeProcess:
        pid = 1234

    def fake_popen(args: list[str], **kwargs: object) -> FakeProcess:
        launched["args"] = args
        launched["kwargs"] = kwargs
        return FakeProcess()

    monkeypatch.setattr(server, "_find_chrome_executable", lambda: "C:\\Chrome\\chrome.exe")
    monkeypatch.setattr(server.subprocess, "Popen", fake_popen)

    async with make_client(deps) as client:
        response = await client.post("/api/webrtc/launch-chrome")

    assert response.status_code == 200
    args = launched["args"]
    assert isinstance(args, list)
    assert args[0] == "C:\\Chrome\\chrome.exe"
    assert any(arg.startswith("--user-data-dir=") for arg in args)
    assert "--disable-features=WebRtcHideLocalIpsWithMdns" in args
    assert "--force-webrtc-ip-handling-policy=default_public_and_private_interfaces" in args
    assert args[-1] == "http://testserver/"
    assert response.json()["pid"] == 1234


@pytest.mark.anyio
async def test_webrtc_local_candidate_address_uses_route_to_esp(monkeypatch: pytest.MonkeyPatch) -> None:
    from espagent_demo import server

    deps = make_deps()
    deps.state.link.esp32_ip = "192.168.1.7"

    monkeypatch.setattr(server, "_local_udp_address_for", lambda remote_ip: "192.168.1.4")

    async with make_client(deps) as client:
        response = await client.get("/api/webrtc/local-candidate-address")

    assert response.status_code == 200
    assert response.json() == {"address": "192.168.1.4", "esp32_ip": "192.168.1.7"}


@pytest.mark.anyio
async def test_index_contains_preview_test_controls_and_reply_box() -> None:
    deps = make_deps()

    async with make_client(deps) as client:
        response = await client.get("/debug/")

    html = response.text
    assert response.headers["cache-control"] == "no-store"
    assert 'id="screen-preview"' in html
    assert 'id="preview-sync"' in html
    assert "testPattern('colorbars')" in html
    assert "testPattern('colors')" in html
    assert "testPattern('palette256')" in html
    assert "testPattern('channels')" in html
    assert "256 Colors" in html
    assert "Channel Ramps" in html
    assert "Color Gamut" in html
    assert "Tone: Bypassed" in html
    assert '>Tone: Off</button>' in html
    assert '<option value="off" selected>Off</option>' in html
    assert 'id="tone-contrast" type="number" min="50" max="220" step="1" value="100"' in html
    assert "updateDuotonePattern()" in html
    assert 'id="duotone-preset"' in html
    assert 'id="duotone-bg"' in html
    assert 'id="duotone-fg"' in html
    assert 'id="duotone-text"' in html
    assert "duotonePresets" in html
    assert 'id="hermes-reply"' in html
    assert 'id="voice-enabled-toggle"' in html
    assert 'id="voice-record"' in html
    assert 'id="voice-input-status"' in html
    assert 'id="voice-silence-seconds"' in html
    assert "/api/voice/silence" in html
    assert "/api/voice/record/start" in html
    assert "/api/voice/record/stop" in html
    assert 'id="unified-flush-toggle"' in html
    assert 'id="compression-mode"' in html
    assert 'id="tile-width"' in html
    assert 'id="tile-height"' in html
    assert 'id="mjpeg-quality"' in html
    assert 'id="mjpeg-subsampling"' in html
    assert '<option value="444">4:4:4</option>' in html
    assert 'id="indexed-protected-color-count"' in html
    assert 'id="indexed-protected-color-add"' in html
    assert 'id="indexed-protected-color-list"' in html
    assert "indexed_protected_colors" in html
    assert "indexedPaletteCapacity" in html
    assert "addIndexedProtectedColor" in html
    assert 'id="preview-fps-toggle"' in html
    assert 'id="preview-fps-limit"' in html
    assert 'id="esp-log-toggle"' in html
    assert 'id="backlight-percent"' in html
    assert 'id="panel-gamma"' in html
    assert 'id="display-tone-toggle"' in html
    assert 'id="display-tone-preset"' in html
    assert 'id="tone-contrast"' in html
    assert 'id="tone-binary"' in html
    assert "displayTonePresets" in html
    assert "/api/display/tone" in html
    assert "toggleEspLog()" in html
    assert "beginNumberEdit(this)" in html
    assert "commitNumberEdit(this)" in html
    assert "handleNumberKey(event)" in html
    assert "setNumberInputValue" in html
    assert "requestAnimationFrame" in html
    assert "cancelAnimationFrame" in html
    assert "previewLastFrameId" in html
    assert 'id="transport-mode"' in html
    assert '<option value="mjpeg">MJPEG</option>' in html
    assert '<option value="indexed" selected>Indexed LZ4</option>' in html
    assert '<option value="4">I4 / 16 colors</option>' in html
    assert '<option value="8">I8 / 256 colors</option>' in html
    assert 'id="transport-sync"' in html
    assert "/api/display/preview.jpg" in html
    assert 'data-mode-option="udp"' in html
    assert 'data-mode-option="udp,mjpeg"' in html
    assert 'data-mode-option="webrtc"' in html
    assert "updateTransportOptionVisibility" in html
    assert 'id="ice-diagnostics"' in html
    assert "startVideoTest()" in html
    assert "Video Fast" in html
    assert 'id="display-ui-theme"' in html
    assert 'id="ui-motion-toggle"' in html
    assert 'id="display-ui-font-chinese"' in html
    assert 'id="display-ui-font-latin"' in html
    assert 'id="display-ui-font-numeric"' in html
    assert 'id="display-ui-scale"' in html
    assert 'id="display-ui-hinting"' in html
    assert 'id="ui-edge-binary"' in html
    assert 'id="ui-edge-grayscale"' in html
    assert 'aria-label="Text edge rendering"' in html
    assert "/api/display/ui" in html


@pytest.mark.anyio
async def test_root_redirects_to_desktop_application() -> None:
    deps = make_deps()

    async with make_client(deps) as client:
        response = await client.get("/", follow_redirects=False)

    assert response.status_code == 307
    assert response.headers["location"] == "/app/"


@pytest.mark.anyio
async def test_desktop_application_assets_are_served() -> None:
    deps = make_deps()

    async with make_client(deps) as client:
        response = await client.get("/app/")
        css = await client.get("/app/app.css")
        script = await client.get("/app/app.js")

    assert response.status_code == 200
    assert '<title>ESPAgent</title>' in response.text
    assert 'data-view="assistant" data-scene="hermes"' in response.text
    assert 'data-view="home" data-scene="lights"' in response.text
    assert 'id="assistant-activity">在线 - 待命<' in response.text
    assert 'id="light-temperature-control"' in response.text
    assert 'data-view-panel="diagnostics"' in response.text
    assert 'data-view="modules"' in response.text
    assert 'data-view="settings"' in response.text
    assert 'data-view="firmware"' in response.text
    assert 'data-view-panel="settings"' in response.text
    assert 'data-view-panel="firmware"' in response.text
    assert 'id="firmware-flash"' in response.text
    assert 'id="firmware-progress"' in response.text
    assert 'id="firmware-log"' in response.text
    assert 'id="firmware-wifi-form"' in response.text
    assert 'id="firmware-wifi-ssid"' in response.text
    assert 'id="firmware-wifi-password"' in response.text
    assert 'id="firmware-gpio-form"' in response.text
    assert 'id="firmware-gpio-read"' in response.text
    assert 'data-gpio-key="scl"' in response.text
    assert 'data-gpio-key="bl"' in response.text
    assert "FIRMWARE PACKAGE" in response.text
    assert "ESP-IDF" not in response.text
    assert 'data-view="appearance"' not in response.text
    assert 'data-theme="second" aria-pressed="false" hidden disabled' in response.text
    assert 'id="shortcut-toggle-capture"' in response.text
    assert 'id="shortcut-toggle-preview"' in response.text
    assert 'id="shortcut-voice-capture"' in response.text
    assert 'id="shortcut-voice-preview"' in response.text
    assert 'id="shortcut-exit-mode"' in response.text
    assert 'id="shortcut-exit-capture"' in response.text
    assert 'id="shortcut-exit-preview"' in response.text
    assert 'id="shortcut-navigation-mode"' in response.text
    assert 'id="shortcut-navigation-capture"' in response.text
    assert 'id="shortcut-scroll-mode"' in response.text
    assert 'id="shortcut-scroll-capture"' in response.text
    assert 'id="module-frame"' in response.text
    assert css.status_code == 200
    assert ".app-shell" in css.text
    assert script.status_code == 200
    assert 'api("/api/state")' in script.text
    assert "color_temp_kelvin" in script.text
    assert 'if (result) setView("display")' not in script.text
    assert 'backquote: "Backquote"' in script.text
    assert 'api("/api/firmware/display-gpio/read"' in script.text
    assert "function directionBindingMatches(event, mode, prefix, directions)" in script.text
    assert "shortcutSettings.scroll_prefix" in script.text
    assert "function encodedShortcutKey(event)" in script.text
    assert "shortcutCaptureKeys = new Map()" in script.text
    assert 'button.textContent = active ? "停止" : "录入"' in script.text
    assert 'shortcutSettings.exit_mode === "custom"' in script.text
    assert 'postJson("/api/settings/shortcuts"' in script.text
    assert 'api("/api/display/scroll"' in script.text
    assert 'api("/api/display/control"' in script.text
    assert "button.disabled = !espControlAvailable" in script.text
    assert "if (!espControlAvailable) return false" in script.text
    assert "ESP32 离线，屏幕控制不可用" in script.text
    assert 'api("/api/firmware/status")' in script.text
    assert 'api("/api/firmware/flash", { method: "POST" })' in script.text
    assert 'api("/api/firmware/wifi"' in script.text
    assert "renderFirmware" in script.text
    assert "flasher_version" in script.text
    assert "/enabled`" in script.text
    assert "module-enabled-toggle" in script.text
    assert "module.enabled && module.navigation?.pinned" in script.text
    assert "if (!selectedModule) activeModuleId = null;" in script.text
    assert 'id="esp-control-toggle"' in response.text


@pytest.mark.anyio
async def test_desktop_mirror_start_activates_esp32_scene() -> None:
    deps = make_deps()

    async with make_client(deps) as client:
        script = await client.get("/module-assets/builtin.desktop-mirror/app/app.js")

    assert script.status_code == 200
    assert "async function startMirror()" in script.text
    assert 'body: JSON.stringify({ target: "esp32" })' in script.text
    assert 'const operation = active ? invoke("stop") : startMirror();' in script.text


@pytest.mark.anyio
async def test_module_api_lists_invokes_and_activates_esp32_scene() -> None:
    deps = make_deps()
    modules = FakeModuleRegistry()
    deps.modules = modules

    async with make_client(deps) as client:
        listed = await client.get("/api/modules")
        state = await client.get("/api/modules/builtin.codex-usage/state")
        action = await client.post(
            "/api/modules/builtin.codex-usage/actions/refresh",
            json={"payload": {"source": "test"}},
        )
        activated = await client.post(
            "/api/modules/builtin.codex-usage/activate",
            json={"target": "app_and_esp32"},
        )
        disabled = await client.post(
            "/api/modules/builtin.codex-usage/enabled",
            json={"enabled": False},
        )
        rejected = await client.post(
            "/api/modules/builtin.codex-usage/activate",
            json={"target": "esp32"},
        )
        module_page = await client.get("/module-assets/builtin.codex-usage/app/")
        module_script = await client.get("/module-assets/builtin.codex-usage/app/app.js")

    assert listed.json()["modules"][0]["id"] == "builtin.codex-usage"
    assert state.json()["state"]["status"] == "ready"
    assert action.json()["result"]["payload"] == {"source": "test"}
    assert activated.json()["scene"] == "codex"
    assert disabled.json()["module"]["enabled"] is False
    assert rejected.status_code == 409
    assert deps.state.active_scene == "link"
    assert deps.state.active_module_id is None
    assert module_page.status_code == 200
    assert 'type: "espagent:control-key"' in module_script.text
    assert 'event.data?.type === "espagent:control-state"' in module_script.text
    assert modules.started is True
    assert modules.closed is True


@pytest.mark.anyio
async def test_light_strip_api_exposes_capabilities_and_invokes_runtime() -> None:
    deps = make_deps()
    deps.modules = FakeModuleRegistry()

    async with make_client(deps) as client:
        described = await client.get("/api/light-strip")
        controlled = await client.post(
            "/api/light-strip/actions/set_state",
            json={"payload": {"power": True, "brightness": 40, "color": "#ff6600"}},
        )
        rejected = await client.post(
            "/api/light-strip/actions/delete_everything",
            json={"payload": {}},
        )

    description = described.json()
    assert description["api_version"] == 1
    assert description["module_id"] == "builtin.ws2812b"
    assert description["module"]["state"]["online"] is True
    assert description["actions"]["set_effect"]["fields"]["preset_id"].startswith("effect preset")
    assert description["actions"]["flash_firmware"]["risk"] == "administrative"
    assert controlled.json()["result"] == {
        "online": True,
        "accepted": "set_state",
        "power": True,
        "brightness": 40,
        "color": "#ff6600",
    }
    assert rejected.status_code == 400


def test_module_display_profile_applies_indexed_scene_override() -> None:
    from espagent_demo.server import _apply_module_display_profile

    deps = make_deps()
    deps.state.active_scene = "desktop"
    deps.state.active_module_id = "module.mirror"

    _apply_module_display_profile(
        deps,
        {
            "transport_mode": "indexed",
            "indexed_bpp": 8,
            "jpeg_quality": 82,
            "jpeg_subsampling": "444",
        },
    )

    assert deps.state.link.scene_transport_override == "indexed"
    assert deps.state.link.indexed_bpp == 8
    assert len(deps.state.link.indexed_protected_colors) == 256
    assert deps.streamer.transport_mode == "indexed"
    assert deps.streamer.indexed_bpp == 8
    assert len(deps.streamer.indexed_protected_colors) == 256
    assert deps.streamer.mjpeg_quality == 82
    assert deps.streamer.mjpeg_subsampling == "444"


@pytest.mark.anyio
async def test_screen_route_serves_the_standalone_web_display_ui() -> None:
    deps = make_deps()

    async with make_client(deps) as client:
        response = await client.get("/screen/")
        css = await client.get("/screen/screen.css")
        script = await client.get("/screen/screen.js")
        first_theme = await client.get("/screen/themes/first/theme.js")
        second_theme = await client.get("/screen/themes/second/theme.js")
        first_theme_css = await client.get("/screen/themes/first/theme.css")
        second_theme_css = await client.get("/screen/themes/second/theme.css")
        font_css = await client.get("/screen/fonts/fonts.css")
        pixel_font = await client.get("/screen/fonts/ark-pixel/ark-pixel-10px-zh-cn.woff2")

    assert response.status_code == 200
    assert 'id="display"' in response.text
    assert css.status_code == 200
    assert "--paper: #f8fafc" in css.text
    assert script.status_code == 200
    assert "window.renderDisplay" in script.text
    assert first_theme.status_code == 200
    assert "registry.first" in first_theme.text
    assert "f1-quota-unit" in first_theme.text
    assert "% 剩余" not in first_theme.text
    assert second_theme.status_code == 200
    assert "registry.second" in second_theme.text
    assert "f2-quota-unit" in second_theme.text
    assert "REMAINING" not in second_theme.text
    assert ".f1-quota-unit" in first_theme_css.text
    assert ".f1-quota-unit { position: relative; z-index: 2; margin-bottom: 3px;" in first_theme_css.text
    assert ".f2-quota-unit" in second_theme_css.text
    assert ".f1-fps-row > span" in first_theme_css.text
    assert ".f1-metric > span" in first_theme_css.text
    assert "color-temperature-segments" in first_theme.text
    assert "scheduleReplyAutoScroll" in first_theme.text
    assert "node.scrollHeight - node.clientHeight" in first_theme.text
    assert "replyScrollFinishing" in first_theme.text
    assert "remoteScrollDelta" in first_theme.text
    assert "remoteScrollDelta" in second_theme.text
    assert "candidateSignature" in first_theme.text
    assert "candidateSignature" in second_theme.text
    assert "node.scrollLeft" in first_theme.text
    assert "node.scrollLeft" in second_theme.text
    assert "overflow-y: hidden" in first_theme_css.text
    assert ".f1-segment.is-selected::after" in first_theme_css.text
    assert "is-motion-ready" in script.text
    assert '[data-motion="on"] #color-temperature-segments.is-motion-ready' in first_theme_css.text
    assert '[data-motion="on"] #brightness-segments.is-motion-ready' not in first_theme_css.text
    assert ".f2-metric > span" in second_theme_css.text
    assert ".f2-brightness-reading > span" in second_theme_css.text
    assert font_css.status_code == 200
    assert "Ark Pixel 10px SC" in font_css.text
    assert '[data-theme="first"][data-font-chinese="auto"]' in font_css.text
    assert '--font-chinese: "Microsoft YaHei"' in font_css.text
    assert '[data-theme="first"][data-font-latin="auto"]' in font_css.text
    assert '--font-latin: "Microsoft YaHei"' in font_css.text
    assert '[data-theme="first"][data-font-numeric="auto"]' in font_css.text
    assert '--font-numeric: "Ark Pixel 10px SC"' in font_css.text
    assert pixel_font.status_code == 200
    assert len(pixel_font.content) > 80_000
