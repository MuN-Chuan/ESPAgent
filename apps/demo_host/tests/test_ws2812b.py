from __future__ import annotations

import time
from pathlib import Path

import httpx
import pytest

from espagent_demo.ws2812b import Ws2812bRuntime


class FakeFirmwareFlasher:
    def __init__(self) -> None:
        self.running = False
        self.started = False
        self.closed = False
        self.preferred_device: str | None = None

    def snapshot(self) -> dict[str, object]:
        return {
            "status": "idle",
            "progress": 0,
            "message": "ready",
            "running": self.running,
            "can_flash": True,
            "firmware_version": "16.0.0",
            "device": {"device": "COM7"},
            "logs": [],
        }

    def start_flash(self, preferred_device: str | None = None) -> dict[str, object]:
        self.started = True
        self.preferred_device = preferred_device
        return self.snapshot()

    def close(self) -> None:
        self.closed = True


def _wait_for(predicate, timeout: float = 1.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition was not reached")


def test_runtime_reads_wled_state_and_coalesces_controls(tmp_path: Path) -> None:
    commands: list[dict[str, object]] = []
    device_state: dict[str, object] = {
        "on": True,
        "bri": 128,
        "seg": [{"col": [[12, 34, 56]]}],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            command = __import__("json").loads(request.content)
            commands.append(command)
            device_state.update({key: value for key, value in command.items() if key in {"on", "bri", "seg"}})
            return httpx.Response(200, json=device_state)
        return httpx.Response(
            200,
            json={
                "state": device_state,
                "info": {
                    "name": "Desk strip",
                    "ver": "0.16.0",
                    "arch": "esp8266",
                    "ip": "192.168.1.9",
                    "leds": {"count": 60},
                    "wifi": {"signal": 84},
                },
            },
        )

    runtime = Ws2812bRuntime(
        on_state=lambda _state: None,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        config_path=tmp_path / "ws2812b.json",
        poll_seconds=0.02,
        command_interval=0.01,
        firmware_flasher=FakeFirmwareFlasher(),
    )
    runtime.start()
    try:
        _wait_for(lambda: runtime.snapshot()["online"] is True)
        state = runtime.snapshot()
        assert state["device_name"] == "Desk strip"
        assert state["brightness"] == 50
        assert state["color"] == "#0c2238"

        runtime.invoke("set_brightness", {"brightness": 40})
        runtime.invoke("set_color", {"color": "#ff8800"})
        _wait_for(lambda: bool(commands))
        _wait_for(lambda: runtime.snapshot()["command_pending"] is False)
        merged = {}
        for command in commands:
            merged.update(command)
        assert merged["bri"] == 102
        assert merged["seg"] == [{"id": 0, "col": [[255, 136, 0]]}]
    finally:
        runtime.close()


def test_configure_persists_normalized_endpoint(tmp_path: Path) -> None:
    config_path = tmp_path / "ws2812b.json"
    runtime = Ws2812bRuntime(
        on_state=lambda _state: None,
        client=httpx.Client(transport=httpx.MockTransport(lambda _request: httpx.Response(503))),
        config_path=config_path,
        firmware_flasher=FakeFirmwareFlasher(),
    )

    state = runtime.invoke("configure", {"endpoint": "192.168.1.42/"})

    assert state["endpoint"] == "http://192.168.1.42"
    assert "http://192.168.1.42" in config_path.read_text(encoding="utf-8")


def test_transient_failures_do_not_immediately_mark_online_device_offline(tmp_path: Path) -> None:
    runtime = Ws2812bRuntime(
        on_state=lambda _state: None,
        client=httpx.Client(transport=httpx.MockTransport(lambda _request: httpx.Response(200))),
        config_path=tmp_path / "ws2812b.json",
        firmware_flasher=FakeFirmwareFlasher(),
    )
    runtime._apply_device_state({"state": {"on": True, "bri": 255}})

    runtime._record_failure(httpx.ReadTimeout("slow response"))
    runtime._record_failure(httpx.ReadTimeout("slow response"))

    state = runtime.snapshot()
    assert state["online"] is True
    assert state["status"] == "online"
    assert state["connection_failures"] == 2
    assert state["error"] is None

    runtime._record_failure(httpx.ReadTimeout("slow response"))

    state = runtime.snapshot()
    assert state["online"] is False
    assert state["status"] == "offline"
    assert state["connection_failures"] == 3
    assert state["error"] == "WLED 控制器响应超时"


@pytest.mark.parametrize("value", ["red", "#12345", "#gg0000"])
def test_rejects_invalid_colors(tmp_path: Path, value: str) -> None:
    runtime = Ws2812bRuntime(
        on_state=lambda _state: None,
        client=httpx.Client(transport=httpx.MockTransport(lambda _request: httpx.Response(200))),
        config_path=tmp_path / "ws2812b.json",
        firmware_flasher=FakeFirmwareFlasher(),
    )

    with pytest.raises(ValueError, match="RRGGBB"):
        runtime.invoke("set_color", {"color": value})


def test_firmware_flash_action_reuses_flasher_and_requests_power_off(tmp_path: Path) -> None:
    flasher = FakeFirmwareFlasher()
    runtime = Ws2812bRuntime(
        on_state=lambda _state: None,
        client=httpx.Client(transport=httpx.MockTransport(lambda _request: httpx.Response(200))),
        config_path=tmp_path / "ws2812b.json",
        firmware_flasher=flasher,
        serial_port_provider=lambda: [],
    )

    state = runtime.invoke("flash_firmware", {})
    assert flasher.started is True
    assert state["flash"]["firmware_version"] == "16.0.0"

    queued: list[dict[str, object]] = []
    runtime._queue_command = lambda command, **_updates: queued.append(command)  # type: ignore[method-assign]
    runtime._apply_device_state({"state": {"on": True, "bri": 255}})
    assert queued == [{"on": False}]

    runtime.close()
    assert flasher.closed is True


def test_firmware_target_selects_package_port_and_persists(tmp_path: Path) -> None:
    config_path = tmp_path / "ws2812b.json"
    esp01_flasher = FakeFirmwareFlasher()
    supermini_flasher = FakeFirmwareFlasher()
    runtime = Ws2812bRuntime(
        on_state=lambda _state: None,
        client=httpx.Client(transport=httpx.MockTransport(lambda _request: httpx.Response(200))),
        config_path=config_path,
        firmware_flashers={
            "esp01s": esp01_flasher,
            "esp32s3_supermini": supermini_flasher,
        },
        serial_port_provider=lambda: [
            {"device": "COM8", "description": "ESPAgent display"},
            {"device": "COM12", "description": "WLED controller"},
        ],
    )

    selected = runtime.invoke("set_firmware_target", {"target": "esp32s3_supermini"})
    flashed = runtime.invoke("flash_firmware", {"port": "COM12"})

    assert selected["firmware_target"] == "esp32s3_supermini"
    assert flashed["firmware_target"] == "esp32s3_supermini"
    assert supermini_flasher.started is True
    assert supermini_flasher.preferred_device == "COM12"
    assert esp01_flasher.started is False
    assert __import__("json").loads(config_path.read_text(encoding="utf-8")) == {
        "endpoint": "http://wled.local",
        "firmware_target": "esp32s3_supermini",
    }

    runtime.close()
    assert esp01_flasher.closed is True
    assert supermini_flasher.closed is True

    restored = Ws2812bRuntime(
        on_state=lambda _state: None,
        client=httpx.Client(transport=httpx.MockTransport(lambda _request: httpx.Response(200))),
        config_path=config_path,
        firmware_flashers={
            "esp01s": FakeFirmwareFlasher(),
            "esp32s3_supermini": FakeFirmwareFlasher(),
        },
    )
    assert restored.snapshot()["firmware_target"] == "esp32s3_supermini"
    restored.close()


def test_firmware_flash_requires_port_choice_when_multiple_devices_are_connected(
    tmp_path: Path,
) -> None:
    runtime = Ws2812bRuntime(
        on_state=lambda _state: None,
        client=httpx.Client(transport=httpx.MockTransport(lambda _request: httpx.Response(200))),
        config_path=tmp_path / "ws2812b.json",
        firmware_flasher=FakeFirmwareFlasher(),
        serial_port_provider=lambda: [
            {"device": "COM8", "description": "ESPAgent display"},
            {"device": "COM12", "description": "WLED controller"},
        ],
    )

    with pytest.raises(RuntimeError, match="多个串口"):
        runtime.invoke("flash_firmware", {})

    runtime.close()


def test_board_led_defaults_off_without_claiming_gpio2(tmp_path: Path) -> None:
    config_posts: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/json/cfg" and request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "hw": {
                        "led": {"total": 60, "ins": [{"pin": [0], "len": 60}]},
                        "btn": {"ins": [{"type": 0, "pin": [-1]}]},
                        "relay": {"pin": 2, "rev": True},
                    },
                    "def": {"on": True},
                },
            )
        if request.url.path == "/json/cfg":
            config_posts.append(__import__("json").loads(request.content))
            return httpx.Response(200, json={"success": True})
        return httpx.Response(
            200,
            json={
                "state": {"on": False},
                "info": {"arch": "esp8266", "leds": {"count": 60}},
            },
        )

    runtime = Ws2812bRuntime(
        on_state=lambda _state: None,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        config_path=tmp_path / "ws2812b.json",
        poll_seconds=0.02,
        firmware_flasher=FakeFirmwareFlasher(),
    )
    runtime.start()
    try:
        _wait_for(lambda: bool(config_posts))
        assert config_posts == [
            {"hw": {"relay": {"pin": -1}}, "def": {"on": False}}
        ]
    finally:
        runtime.close()


def test_esp32s3_does_not_apply_esp01_gpio_migration(tmp_path: Path) -> None:
    config_posts: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "hw": {
                        "led": {"total": 60, "ins": [{"pin": [2], "len": 60}]},
                        "btn": {"ins": [{"type": 2, "pin": [0]}]},
                        "relay": {"pin": -1},
                    },
                    "def": {"on": False},
                },
            )
        config_posts.append(__import__("json").loads(request.content))
        return httpx.Response(200, json={"success": True})

    runtime = Ws2812bRuntime(
        on_state=lambda _state: None,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        config_path=tmp_path / "ws2812b.json",
        firmware_flashers={
            "esp01s": FakeFirmwareFlasher(),
            "esp32s3_supermini": FakeFirmwareFlasher(),
        },
    )
    runtime._apply_device_state(
        {
            "state": {"on": False},
            "info": {"arch": "ESP32-S3", "leds": {"count": 60}},
        }
    )

    runtime._configure_board_led_default()

    assert config_posts == []
    assert runtime.snapshot()["board_led_status"] == "off"
    runtime.close()


def test_led_count_preserves_single_bus_configuration(tmp_path: Path) -> None:
    config_posts: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                json={"hw": {"led": {"total": 60, "ins": [{"pin": [3], "len": 60, "type": 22, "order": 1}]}}},
            )
        config_posts.append(__import__("json").loads(request.content))
        return httpx.Response(200, json={"success": True})

    runtime = Ws2812bRuntime(
        on_state=lambda _state: None,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        config_path=tmp_path / "ws2812b.json",
        firmware_flasher=FakeFirmwareFlasher(),
    )

    state = runtime.invoke("set_led_count", {"led_count": 144})

    assert state["led_count"] == 144
    assert config_posts == [
        {"hw": {"led": {"total": 144, "ins": [{"pin": [3], "len": 144, "type": 22, "order": 1}]}}}
    ]


def test_gpio2_strip_is_migrated_to_gpio0_and_default_button_is_disabled(tmp_path: Path) -> None:
    config_posts: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/json/cfg" and request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "hw": {
                        "led": {
                            "total": 30,
                            "ins": [{"pin": [2], "len": 30, "type": 22, "order": 1}],
                        },
                        "btn": {
                            "ins": [{"type": 2, "pin": [0], "macros": [0, 0, 0]}],
                        },
                        "relay": {"pin": -1},
                    },
                    "def": {"on": False},
                },
            )
        if request.url.path == "/json/cfg":
            config_posts.append(__import__("json").loads(request.content))
            return httpx.Response(200, json={"success": True})
        return httpx.Response(
            200,
            json={
                "state": {"on": False},
                "info": {"arch": "esp8266", "leds": {"count": 30}},
            },
        )

    runtime = Ws2812bRuntime(
        on_state=lambda _state: None,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        config_path=tmp_path / "ws2812b.json",
        poll_seconds=0.02,
        firmware_flasher=FakeFirmwareFlasher(),
    )
    runtime.start()
    try:
        _wait_for(lambda: bool(config_posts))
        state = runtime.snapshot()
        assert state["online"] is True
        assert state["board_led_status"] == "off"
        assert state["board_led_error"] is None
        assert config_posts == [
            {
                "hw": {
                    "led": {
                        "total": 30,
                        "ins": [{"pin": [0], "len": 30, "type": 22, "order": 1}],
                    },
                    "btn": {
                        "ins": [{"type": 0, "pin": [-1], "macros": [0, 0, 0]}],
                    },
                },
            }
        ]
    finally:
        runtime.close()


def test_effect_presets_resolve_from_device_catalog_and_preserve_color(tmp_path: Path) -> None:
    commands: list[dict[str, object]] = []
    effects = ["Solid", "Blink", "Breathe", "RSVD", "Colorloop", "Meteor Smooth", "Pacifica"]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/json/eff":
            return httpx.Response(200, json=effects)
        if request.method == "POST":
            commands.append(__import__("json").loads(request.content))
            return httpx.Response(200, json={"success": True})
        return httpx.Response(200, json={})

    runtime = Ws2812bRuntime(
        on_state=lambda _state: None,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        config_path=tmp_path / "ws2812b.json",
        firmware_flasher=FakeFirmwareFlasher(),
    )

    runtime._load_effect_catalog()
    state = runtime.invoke("set_effect", {"preset_id": "meteor"})
    runtime.invoke("set_color", {"color": "#123456"})

    assert state["active_effect_id"] == 5
    assert state["active_effect_name"] == "Meteor Smooth"
    assert state["active_effect_preset"] == "meteor"
    presets = {preset["id"]: preset for preset in state["effect_presets"]}
    assert presets["breathe"]["supported"] is True
    assert presets["meteor"]["effect_id"] == 5
    assert presets["rainbow"]["supported"] is False
    assert runtime._take_pending() == {
        "on": True,
        "seg": [{"id": 0, "fx": 5, "fxdef": True, "col": [[18, 52, 86]]}],
    }
    assert commands == []


def test_unsupported_effect_preset_is_rejected(tmp_path: Path) -> None:
    runtime = Ws2812bRuntime(
        on_state=lambda _state: None,
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json=["Solid", "Breathe"])
                if request.url.path == "/json/eff"
                else httpx.Response(200, json={})
            )
        ),
        config_path=tmp_path / "ws2812b.json",
        firmware_flasher=FakeFirmwareFlasher(),
    )

    runtime._load_effect_catalog()

    with pytest.raises(RuntimeError, match="不支持"):
        runtime.invoke("set_effect", {"preset_id": "pacifica"})


def test_wifi_provisioning_auto_selects_only_port_and_updates_endpoint(tmp_path: Path) -> None:
    calls: list[tuple[str, str, str]] = []
    ports = [{"device": "COM12", "description": "USB-SERIAL CH340", "vid": 6790, "pid": 29987}]

    def provisioner(port: str, ssid: str, password: str) -> str:
        calls.append((port, ssid, password))
        return "http://192.168.1.23"

    runtime = Ws2812bRuntime(
        on_state=lambda _state: None,
        client=httpx.Client(transport=httpx.MockTransport(lambda _request: httpx.Response(200))),
        config_path=tmp_path / "ws2812b.json",
        firmware_flasher=FakeFirmwareFlasher(),
        serial_port_provider=lambda: ports,
        wifi_provisioner=provisioner,
    )

    state = runtime.invoke(
        "provision_wifi",
        {"port": "", "ssid": "Home WiFi", "password": "secret123"},
    )
    assert state["provisioning"] is True
    assert state["provision_port"] == "COM12"
    _wait_for(lambda: runtime.snapshot()["provisioning"] is False)

    state = runtime.snapshot()
    assert calls == [("COM12", "Home WiFi", "secret123")]
    assert state["endpoint"] == "http://192.168.1.23"
    assert state["provision_status"] == "已连接 Home WiFi，正在连接设备"
    assert state["provision_ssid"] == "Home WiFi"
    assert state["provision_ip"] == "192.168.1.23"
    assert state["provision_error"] is None
    assert "secret123" not in str(state)


def test_wifi_provisioning_requires_port_choice_when_multiple_are_present(tmp_path: Path) -> None:
    runtime = Ws2812bRuntime(
        on_state=lambda _state: None,
        client=httpx.Client(transport=httpx.MockTransport(lambda _request: httpx.Response(200))),
        config_path=tmp_path / "ws2812b.json",
        firmware_flasher=FakeFirmwareFlasher(),
        serial_port_provider=lambda: [
            {"device": "COM3", "description": "USB Serial"},
            {"device": "COM8", "description": "USB Serial"},
        ],
    )

    with pytest.raises(RuntimeError, match="多个串口"):
        runtime.invoke("provision_wifi", {"ssid": "Home", "password": ""})


@pytest.mark.parametrize(
    ("ssid", "password", "message"),
    [
        ("", "", "SSID"),
        ("网" * 11, "", "SSID"),
        ("Home", "x" * 65, "密码"),
    ],
)
def test_wifi_provisioning_validates_credentials_before_opening_serial(
    tmp_path: Path,
    ssid: str,
    password: str,
    message: str,
) -> None:
    runtime = Ws2812bRuntime(
        on_state=lambda _state: None,
        client=httpx.Client(transport=httpx.MockTransport(lambda _request: httpx.Response(200))),
        config_path=tmp_path / "ws2812b.json",
        firmware_flasher=FakeFirmwareFlasher(),
        serial_port_provider=lambda: [{"device": "COM12"}],
    )

    with pytest.raises(ValueError, match=message):
        runtime.invoke("provision_wifi", {"ssid": ssid, "password": password})
