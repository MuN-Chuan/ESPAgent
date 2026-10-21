from __future__ import annotations

import json
import os
import threading
import time
from copy import deepcopy
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit, urlunsplit

import httpx

from espagent_demo.firmware_flash import FirmwareFlashService
from espagent_demo.improv_serial import list_serial_ports, provision_wifi


DEFAULT_ENDPOINT = "http://wled.local"
ESP01_FIRMWARE_PACKAGE = Path(__file__).resolve().parents[4] / "firmware" / "releases" / "esp01s_wled"
SUPERMINI_FIRMWARE_PACKAGE = (
    Path(__file__).resolve().parents[4]
    / "firmware"
    / "releases"
    / "esp32s3_supermini_wled"
)
ESP01_BOARD_LED_GPIO = 2
ESP01_STRIP_DATA_GPIO = 0
DEFAULT_FIRMWARE_TARGET = "esp01s"
FIRMWARE_TARGETS: tuple[dict[str, object], ...] = (
    {
        "id": "esp01s",
        "name": "ESP-01S",
        "description": "ESP8266 · 1 MB Flash",
        "data_gpio": ESP01_STRIP_DATA_GPIO,
        "hardware_note": (
            "灯带数据线连接 GPIO0。ESPAgent 会把 WLED 默认的 GPIO2 灯带总线"
            "迁移到 GPIO0，并释放 GPIO2，使板载蓝灯保持熄灭。"
        ),
    },
    {
        "id": "esp32s3_supermini",
        "name": "ESP32-S3 SuperMini",
        "description": "ESP32-S3 · 4 MB QSPI",
        "data_gpio": None,
        "hardware_note": (
            "首次启动后在 WLED 中确认灯带数据 GPIO。ESPAgent 不会修改"
            "ESP32-S3 的灯带引脚；该固件仅用于灯条控制，不支持显示功能。"
        ),
    },
)
EFFECT_PRESETS: tuple[dict[str, object], ...] = (
    {
        "id": "breathe",
        "name": "呼吸",
        "effect_names": ("Breathe",),
        "description": "主色在明暗之间平缓呼吸",
        "preview": ("#16213e", "#4f8cff", "#dce9ff"),
    },
    {
        "id": "colorloop",
        "name": "色彩循环",
        "effect_names": ("Colorloop",),
        "description": "整条灯带连续循环彩虹色相",
        "preview": ("#ff375f", "#ffd60a", "#30d158"),
    },
    {
        "id": "rainbow",
        "name": "彩虹",
        "effect_names": ("Rainbow",),
        "description": "彩虹色沿灯带连续铺展",
        "preview": ("#ff453a", "#64d2ff", "#bf5af2"),
    },
    {
        "id": "chase",
        "name": "追逐",
        "effect_names": ("Chase",),
        "description": "亮点沿灯带方向持续追逐",
        "preview": ("#0b0d12", "#ffffff", "#315bea"),
    },
    {
        "id": "running",
        "name": "流动波",
        "effect_names": ("Running",),
        "description": "主色与背景色形成流动波纹",
        "preview": ("#0a84ff", "#101318", "#5ac8fa"),
    },
    {
        "id": "twinklefox",
        "name": "星光",
        "effect_names": ("Twinklefox", "Twinkle"),
        "description": "随机灯珠缓慢闪烁和淡出",
        "preview": ("#13151a", "#ffd60a", "#ffffff"),
    },
    {
        "id": "fire_2012",
        "name": "火焰",
        "effect_names": ("Fire 2012",),
        "description": "红黄火焰随机升腾闪动",
        "preview": ("#4a0800", "#ff3b30", "#ffd60a"),
    },
    {
        "id": "meteor",
        "name": "流星",
        "effect_names": ("Meteor Smooth", "Meteor"),
        "description": "亮点拖着渐隐尾迹掠过灯带",
        "preview": ("#0b1020", "#315bea", "#ffffff"),
    },
    {
        "id": "pacifica",
        "name": "海浪",
        "effect_names": ("Pacifica",),
        "description": "蓝绿色海浪缓慢叠加流动",
        "preview": ("#002b45", "#00a6a6", "#7dd3fc"),
    },
    {
        "id": "candle",
        "name": "烛光",
        "effect_names": ("Candle", "Fire Flicker"),
        "description": "暖色烛火自然随机闪动",
        "preview": ("#4a1d00", "#ff9500", "#ffe0a3"),
    },
)


class BoardLedPinConflict(RuntimeError):
    pass


class Ws2812bRuntime:
    """Non-blocking WLED controller for supported WS2812B controller boards."""

    def __init__(
        self,
        *,
        on_state: Callable[[dict[str, object]], None],
        client: httpx.Client | None = None,
        config_path: Path | None = None,
        poll_seconds: float = 5.0,
        command_interval: float = 0.4,
        firmware_flasher: FirmwareFlashService | None = None,
        firmware_flashers: dict[str, FirmwareFlashService] | None = None,
        serial_port_provider: Callable[[], list[dict[str, object]]] = list_serial_ports,
        wifi_provisioner: Callable[..., str | None] = provision_wifi,
    ) -> None:
        self._on_state = on_state
        self._client = client or httpx.Client(
            timeout=httpx.Timeout(6.0, connect=1.5),
            trust_env=False,
        )
        self._owns_client = client is None
        self._config_path = config_path or _default_config_path()
        self._poll_seconds = max(0.25, poll_seconds)
        self._command_interval = max(0.0, command_interval)
        self._last_command_at = 0.0
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._provision_thread: threading.Thread | None = None
        if firmware_flashers is not None:
            self._firmware_flashers = dict(firmware_flashers)
        elif firmware_flasher is not None:
            self._firmware_flashers = {DEFAULT_FIRMWARE_TARGET: firmware_flasher}
        else:
            self._firmware_flashers = {
                "esp01s": FirmwareFlashService(ESP01_FIRMWARE_PACKAGE),
                "esp32s3_supermini": FirmwareFlashService(SUPERMINI_FIRMWARE_PACKAGE),
            }
        if not self._firmware_flashers:
            raise ValueError("at least one WS2812B firmware flasher is required")
        self._serial_port_provider = serial_port_provider
        self._wifi_provisioner = wifi_provisioner
        self._post_flash_power_off_pending = False
        self._board_led_configured = False
        self._pending: dict[str, object] = {}
        self._endpoint = _load_endpoint(self._config_path)
        configured_target = _load_firmware_target(self._config_path)
        self._firmware_target = (
            configured_target
            if configured_target in self._firmware_flashers
            else next(iter(self._firmware_flashers))
        )
        self._effect_names: tuple[str, ...] = ()
        self._effect_ids: dict[str, int] = {}
        self._effect_catalog_endpoint: str | None = None
        self._state: dict[str, object] = {
            "status": "stopped",
            "online": False,
            "endpoint": self._endpoint,
            "device_name": "WS2812B 灯条",
            "power": False,
            "brightness": 100,
            "color": "#ffffff",
            "firmware": None,
            "architecture": None,
            "ip": None,
            "led_count": None,
            "signal": None,
            "command_pending": False,
            "connection_failures": 0,
            "updated_at": None,
            "error": None,
            "serial_ports": self._serial_port_provider(),
            "provisioning": False,
            "provision_status": None,
            "provision_error": None,
            "provision_port": None,
            "provision_ssid": None,
            "provision_ip": None,
            "board_led_status": "pending",
            "board_led_error": None,
            "effect_catalog_status": "pending",
            "effect_catalog_error": None,
            "effect_presets": _effect_preset_states({}, catalog_ready=False),
            "active_effect_id": 0,
            "active_effect_name": "Solid",
            "active_effect_preset": "solid",
            "firmware_target": self._firmware_target,
            "firmware_targets": [
                dict(target)
                for target in FIRMWARE_TARGETS
                if target["id"] in self._firmware_flashers
            ],
        }

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._update(status="connecting", error=None)
        self._thread = threading.Thread(target=self._run, name="ws2812b-wled", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=4)
        self._thread = None
        if self._provision_thread is not None:
            self._provision_thread.join(timeout=1)
        self._provision_thread = None
        closed_flashers: set[int] = set()
        for flasher in self._firmware_flashers.values():
            if id(flasher) in closed_flashers:
                continue
            closed_flashers.add(id(flasher))
            flasher.close()
        if self._owns_client:
            self._client.close()
        self._update(status="stopped", online=False, command_pending=False)

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            state = deepcopy(self._state)
        state["flash"] = self._active_flasher().snapshot()
        return state

    def invoke(self, action: str, payload: dict[str, object]) -> dict[str, object]:
        if action == "refresh":
            self._update(serial_ports=self._serial_port_provider())
            self._active_flasher().snapshot()
            self._wake.set()
            return self.snapshot()
        if action == "set_firmware_target":
            target = str(payload.get("target") or "").strip()
            if target not in self._firmware_flashers:
                raise ValueError("unsupported WS2812B firmware target")
            if self._any_flasher_running():
                raise RuntimeError("固件正在烧录，无法切换控制器型号")
            with self._lock:
                self._firmware_target = target
                self._state["firmware_target"] = target
            _save_config(self._config_path, self._endpoint, self._firmware_target)
            self._publish()
            return self.snapshot()
        if action == "flash_firmware":
            serial_ports = self._serial_port_provider()
            requested_port = str(payload.get("port") or "").strip()
            preferred_port = (
                _select_serial_port(requested_port, serial_ports)
                if serial_ports
                else requested_port or None
            )
            self._update(serial_ports=serial_ports)
            result = self._active_flasher().start_flash(preferred_port)
            self._post_flash_power_off_pending = True
            self._wake.set()
            state = self.snapshot()
            state["flash"] = result
            return state
        if action == "provision_wifi":
            if self._provision_thread is not None and self._provision_thread.is_alive():
                raise RuntimeError("WLED 控制器正在配网")
            ssid = str(payload.get("ssid") or "")
            password = str(payload.get("password") or "")
            _validate_wifi_credentials(ssid, password)
            serial_ports = self._serial_port_provider()
            port = _select_serial_port(str(payload.get("port") or ""), serial_ports)
            self._update(
                serial_ports=serial_ports,
                provisioning=True,
                provision_status=f"正在通过 {port} 写入网络",
                provision_port=port,
                provision_ssid=ssid,
                provision_ip=None,
                provision_error=None,
                error=None,
            )
            self._provision_thread = threading.Thread(
                target=self._provision,
                args=(port, ssid, password),
                name="ws2812b-improv",
                daemon=True,
            )
            self._provision_thread.start()
            return self.snapshot()
        if action == "configure":
            endpoint = _normalize_endpoint(str(payload.get("endpoint") or DEFAULT_ENDPOINT))
            with self._lock:
                self._endpoint = endpoint
                self._effect_names = ()
                self._effect_ids = {}
                self._effect_catalog_endpoint = None
                self._state.update(
                    endpoint=endpoint,
                    status="connecting",
                    online=False,
                    error=None,
                    effect_catalog_status="pending",
                    effect_catalog_error=None,
                    effect_presets=_effect_preset_states({}, catalog_ready=False),
                    active_effect_name="Solid",
                    active_effect_preset="solid",
                )
            _save_config(self._config_path, endpoint, self._firmware_target)
            self._publish()
            self._wake.set()
            return self.snapshot()
        if action == "set_power":
            power = payload.get("power")
            if not isinstance(power, bool):
                raise ValueError("power must be a boolean")
            self._queue_command({"on": power}, power=power)
            return self.snapshot()
        if action == "set_brightness":
            brightness = _bounded_int(payload.get("brightness"), 0, 100, "brightness")
            self._queue_command({"bri": _percent_to_byte(brightness)}, brightness=brightness)
            return self.snapshot()
        if action == "set_led_count":
            led_count = _bounded_int(payload.get("led_count"), 1, 1200, "led_count")
            self._set_led_count(led_count)
            return self.snapshot()
        if action == "set_color":
            color = _normalize_color(payload.get("color"))
            red, green, blue = _hex_to_rgb(color)
            self._queue_command(
                {"seg": [{"id": 0, "col": [[red, green, blue]]}]},
                color=color,
            )
            return self.snapshot()
        if action == "set_effect":
            preset_id = str(payload.get("preset_id") or "").strip()
            if preset_id == "solid":
                effect_id = 0
                effect_name = self._effect_name(effect_id) or "Solid"
            else:
                preset = _find_effect_preset(preset_id)
                if preset is None:
                    raise ValueError("unknown effect preset")
                if self._effect_catalog_endpoint != self._endpoint:
                    self._load_effect_catalog()
                with self._lock:
                    effect_id = self._effect_ids.get(preset_id)
                if effect_id is None:
                    raise RuntimeError(f"当前 WLED 固件不支持“{preset['name']}”特效")
                effect_name = self._effect_name(effect_id) or str(preset["effect_names"][0])
            self._queue_command(
                {"on": True, "seg": [{"id": 0, "fx": effect_id, "fxdef": True}]},
                power=True,
                active_effect_id=effect_id,
                active_effect_name=effect_name,
                active_effect_preset=preset_id,
            )
            return self.snapshot()
        if action == "set_state":
            updates: dict[str, object] = {}
            command: dict[str, object] = {}
            if "power" in payload:
                if not isinstance(payload["power"], bool):
                    raise ValueError("power must be a boolean")
                updates["power"] = payload["power"]
                command["on"] = payload["power"]
            if "brightness" in payload:
                brightness = _bounded_int(payload["brightness"], 0, 100, "brightness")
                updates["brightness"] = brightness
                command["bri"] = _percent_to_byte(brightness)
            if "color" in payload:
                color = _normalize_color(payload["color"])
                updates["color"] = color
                command["seg"] = [{"id": 0, "col": [list(_hex_to_rgb(color))]}]
            if not command:
                raise ValueError("set_state requires power, brightness, or color")
            self._queue_command(command, **updates)
            return self.snapshot()
        raise ValueError(f"unsupported WS2812B action: {action}")

    def _queue_command(self, command: dict[str, object], **optimistic: object) -> None:
        with self._lock:
            self._pending = _merge_wled_commands(self._pending, command)
            self._state.update(optimistic)
            self._state["command_pending"] = True
            self._state["error"] = None
        self._publish()
        self._wake.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            if self._any_flasher_running():
                self._wake.wait(0.5)
                self._wake.clear()
                continue
            try:
                if self._has_pending():
                    remaining = self._command_interval - (time.monotonic() - self._last_command_at)
                    if remaining > 0 and self._stop.wait(remaining):
                        break
                    command = self._take_pending()
                    self._post_state(command)
                    self._last_command_at = time.monotonic()
                else:
                    self._read_state()
            except Exception as exc:
                self._record_failure(exc)
            self._wake.wait(self._poll_seconds)
            self._wake.clear()

    def _provision(self, port: str, ssid: str, password: str) -> None:
        try:
            endpoint = self._wifi_provisioner(port, ssid, password)
            if self._stop.is_set():
                return
            if endpoint:
                endpoint = _normalize_endpoint(endpoint)
                with self._lock:
                    self._endpoint = endpoint
                    self._state["endpoint"] = endpoint
                _save_config(self._config_path, endpoint, self._firmware_target)
            self._update(
                provisioning=False,
                provision_status=f"已连接 {ssid}，正在连接设备",
                provision_port=port,
                provision_ssid=ssid,
                provision_ip=urlsplit(endpoint).hostname if endpoint else None,
                provision_error=None,
                status="connecting",
                error=None,
            )
            self._wake.set()
        except Exception as exc:
            if self._stop.is_set():
                return
            error = _safe_error(exc)
            self._update(
                provisioning=False,
                provision_status=f"{port} 配网失败：{error}",
                provision_error=error,
                provision_port=port,
                provision_ssid=ssid,
                provision_ip=None,
                error=error,
            )

    def _take_pending(self) -> dict[str, object]:
        with self._lock:
            command = self._pending
            self._pending = {}
            return command

    def _has_pending(self) -> bool:
        with self._lock:
            return bool(self._pending)

    def _post_state(self, command: dict[str, object]) -> None:
        try:
            response = self._client.post(f"{self._endpoint}/json/state", json=command)
            response.raise_for_status()
            try:
                payload = response.json()
            except ValueError:
                payload = {}
            if isinstance(payload, dict) and any(key in payload for key in ("state", "on", "bri", "seg")):
                self._apply_device_state(payload)
            else:
                self._update(
                    status="online",
                    online=True,
                    connection_failures=0,
                    command_pending=bool(self._pending),
                    error=None,
                    updated_at=_utc_timestamp(),
                )
        except Exception:
            with self._lock:
                self._pending = _merge_wled_commands(command, self._pending)
            raise

    def _read_state(self) -> None:
        response = self._client.get(f"{self._endpoint}/json/si")
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError("WLED returned an invalid JSON response")
        self._apply_device_state(payload)
        if self._effect_catalog_endpoint != self._endpoint:
            try:
                self._load_effect_catalog()
            except Exception as exc:
                self._update(
                    effect_catalog_status="error",
                    effect_catalog_error=_safe_error(exc),
                )
        if not self._board_led_configured:
            try:
                self._configure_board_led_default()
            except BoardLedPinConflict as exc:
                self._board_led_configured = True
                self._update(board_led_status="conflict", board_led_error=str(exc))

    def _read_config(self) -> dict[str, object]:
        response = self._client.get(f"{self._endpoint}/json/cfg")
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError("WLED returned an invalid configuration")
        return payload

    def _load_effect_catalog(self) -> None:
        response = self._client.get(f"{self._endpoint}/json/eff")
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list) or not payload:
            raise RuntimeError("WLED returned an invalid effect list")
        effect_names = tuple(str(name) for name in payload)
        effect_ids = _resolve_effect_ids(effect_names)
        with self._lock:
            self._effect_names = effect_names
            self._effect_ids = effect_ids
            self._effect_catalog_endpoint = self._endpoint
            active_effect_id = int(self._state.get("active_effect_id") or 0)
        self._update(
            effect_catalog_status="ready",
            effect_catalog_error=None,
            effect_presets=_effect_preset_states(effect_ids, catalog_ready=True),
            active_effect_name=self._effect_name(active_effect_id) or f"FX {active_effect_id}",
            active_effect_preset=_preset_id_for_effect(active_effect_id, effect_ids),
        )

    def _effect_name(self, effect_id: int) -> str | None:
        with self._lock:
            if 0 <= effect_id < len(self._effect_names):
                name = self._effect_names[effect_id]
                if name not in {"-", "RSVD"}:
                    return name
        return None

    def _configure_board_led_default(self) -> None:
        config = self._read_config()
        with self._lock:
            architecture = str(self._state.get("architecture") or "").casefold()
        migrate_esp01_pins = "esp8266" in architecture
        hardware = config.get("hw") if isinstance(config.get("hw"), dict) else {}
        led = hardware.get("led") if isinstance(hardware.get("led"), dict) else {}
        buses = led.get("ins") if isinstance(led.get("ins"), list) else []
        relay = hardware.get("relay") if isinstance(hardware.get("relay"), dict) else {}
        buttons = hardware.get("btn") if isinstance(hardware.get("btn"), dict) else {}
        button_inputs = buttons.get("ins") if isinstance(buttons.get("ins"), list) else []
        default = config.get("def") if isinstance(config.get("def"), dict) else {}
        migrated_buses = deepcopy(buses)
        migrate_board_led_bus = migrate_esp01_pins and any(
            isinstance(bus, dict) and bus.get("pin") == [ESP01_BOARD_LED_GPIO]
            for bus in migrated_buses
        )
        if migrate_board_led_bus:
            used_pins = {
                int(pin)
                for bus in migrated_buses
                if isinstance(bus, dict) and isinstance(bus.get("pin"), list)
                for pin in bus["pin"]
                if isinstance(pin, (int, float)) and int(pin) != ESP01_BOARD_LED_GPIO
            }
            if ESP01_STRIP_DATA_GPIO in used_pins:
                raise BoardLedPinConflict(
                    f"GPIO{ESP01_STRIP_DATA_GPIO} 已被另一条灯带总线占用，无法迁移 GPIO2"
                )
            for bus in migrated_buses:
                if isinstance(bus, dict) and bus.get("pin") == [ESP01_BOARD_LED_GPIO]:
                    bus["pin"] = [ESP01_STRIP_DATA_GPIO]

        migrated_buttons = deepcopy(button_inputs)
        disable_default_button = False
        if migrate_board_led_bus:
            for button in migrated_buttons:
                if isinstance(button, dict) and button.get("pin") == [ESP01_STRIP_DATA_GPIO]:
                    button["type"] = 0
                    button["pin"] = [-1]
                    disable_default_button = True

        update: dict[str, object] = {}
        hardware_update: dict[str, object] = {}
        if migrate_board_led_bus:
            led_update: dict[str, object] = {"ins": migrated_buses}
            if isinstance(led.get("total"), (int, float)):
                led_update["total"] = int(led["total"])
            hardware_update["led"] = led_update
        if disable_default_button:
            hardware_update["btn"] = {"ins": migrated_buttons}
        if migrate_esp01_pins and relay.get("pin") != -1:
            hardware_update["relay"] = {"pin": -1}
        if hardware_update:
            update["hw"] = hardware_update
        if default.get("on") is not False:
            update["def"] = {"on": False}
        if update:
            response = self._client.post(
                f"{self._endpoint}/json/cfg",
                json=update,
            )
            response.raise_for_status()
        self._board_led_configured = True
        self._update(board_led_status="off", board_led_error=None)

    def _active_flasher(self) -> FirmwareFlashService:
        with self._lock:
            return self._firmware_flashers[self._firmware_target]

    def _any_flasher_running(self) -> bool:
        return any(flasher.running for flasher in self._firmware_flashers.values())

    def _set_led_count(self, led_count: int) -> None:
        config = self._read_config()
        hardware = config.get("hw") if isinstance(config.get("hw"), dict) else {}
        led = hardware.get("led") if isinstance(hardware.get("led"), dict) else {}
        buses = led.get("ins") if isinstance(led.get("ins"), list) else []
        if len(buses) != 1 or not isinstance(buses[0], dict):
            raise RuntimeError("灯珠数量设置仅支持单路 WLED 灯带")
        bus = deepcopy(buses[0])
        bus["len"] = led_count
        response = self._client.post(
            f"{self._endpoint}/json/cfg",
            json={"hw": {"led": {"total": led_count, "ins": [bus]}}},
        )
        response.raise_for_status()
        self._update(led_count=led_count, status="connecting", online=False, error=None)
        self._wake.set()

    def _apply_device_state(self, payload: dict[str, object]) -> None:
        state = payload.get("state") if isinstance(payload.get("state"), dict) else payload
        info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
        segments = state.get("seg") if isinstance(state, dict) else None
        segment = segments[0] if isinstance(segments, list) and segments and isinstance(segments[0], dict) else {}
        colors = segment.get("col") if isinstance(segment, dict) else None
        primary = colors[0] if isinstance(colors, list) and colors and isinstance(colors[0], list) else None
        leds = info.get("leds") if isinstance(info.get("leds"), dict) else {}
        wifi = info.get("wifi") if isinstance(info.get("wifi"), dict) else {}
        updates: dict[str, object] = {
            "status": "online",
            "online": True,
            "command_pending": bool(self._pending),
            "connection_failures": 0,
            "error": None,
            "updated_at": _utc_timestamp(),
        }
        if info:
            updates.update(
                device_name=info.get("name") or "WS2812B 灯条",
                firmware=info.get("ver"),
                architecture=info.get("arch"),
                ip=info.get("ip"),
                led_count=leds.get("count"),
                signal=wifi.get("signal"),
            )
        if isinstance(state, dict):
            if isinstance(state.get("on"), bool):
                updates["power"] = state["on"]
            if isinstance(state.get("bri"), (int, float)):
                updates["brightness"] = _byte_to_percent(int(state["bri"]))
        if isinstance(primary, list) and len(primary) >= 3:
            updates["color"] = _rgb_to_hex(primary[:3])
        if isinstance(segment.get("fx"), (int, float)):
            effect_id = int(segment["fx"])
            with self._lock:
                effect_ids = dict(self._effect_ids)
            updates.update(
                active_effect_id=effect_id,
                active_effect_name=self._effect_name(effect_id) or f"FX {effect_id}",
                active_effect_preset=_preset_id_for_effect(effect_id, effect_ids),
            )
        self._update(**updates)
        if self._post_flash_power_off_pending:
            self._post_flash_power_off_pending = False
            self._queue_command({"on": False}, power=False)

    def _record_failure(self, exc: Exception) -> None:
        with self._lock:
            failures = int(self._state.get("connection_failures") or 0) + 1
            was_online = bool(self._state.get("online"))
        confirmed_offline = not was_online or failures >= 3
        self._update(
            status="offline" if confirmed_offline else "online",
            online=not confirmed_offline,
            connection_failures=failures,
            command_pending=bool(self._pending),
            error=_safe_error(exc) if confirmed_offline else None,
        )

    def _update(self, **updates: object) -> None:
        with self._lock:
            self._state.update(updates)
        self._publish()

    def _publish(self) -> None:
        self._on_state(self.snapshot())


def _normalize_endpoint(value: str) -> str:
    value = value.strip()
    if not value:
        value = DEFAULT_ENDPOINT
    if "://" not in value:
        value = f"http://{value}"
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("endpoint must be a valid HTTP address or LAN host name")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("endpoint must not include credentials, query, or fragment")
    path = parsed.path.rstrip("/")
    if path:
        raise ValueError("endpoint must not include an API path")
    return urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))


def _validate_wifi_credentials(ssid: str, password: str) -> None:
    ssid_length = len(ssid.encode("utf-8"))
    password_length = len(password.encode("utf-8"))
    if not 1 <= ssid_length <= 32:
        raise ValueError("SSID 长度必须为 1 至 32 字节")
    if password_length > 64:
        raise ValueError("Wi-Fi 密码不能超过 64 字节")


def _select_serial_port(
    requested_port: str,
    serial_ports: list[dict[str, object]],
) -> str:
    requested_port = requested_port.strip()
    available = [
        str(port.get("device") or "").strip()
        for port in serial_ports
        if str(port.get("device") or "").strip()
    ]
    if requested_port:
        if requested_port not in available:
            raise ValueError(f"串口 {requested_port} 当前不可用")
        return requested_port
    if len(available) == 1:
        return available[0]
    if not available:
        raise RuntimeError("未检测到 WLED 控制器串口，请先通过 USB 连接设备")
    raise RuntimeError("检测到多个串口，请选择 WLED 控制器对应端口")


def _default_config_path() -> Path:
    override = os.environ.get("ESPAGENT_WS2812B_CONFIG")
    if override:
        return Path(override)
    base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / ".config")
    return base / "ESPAgent" / "ws2812b.json"


def _load_endpoint(path: Path) -> str:
    env_endpoint = os.environ.get("ESPAGENT_WS2812B_ENDPOINT")
    if env_endpoint:
        return _normalize_endpoint(env_endpoint)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return _normalize_endpoint(str(payload.get("endpoint") or DEFAULT_ENDPOINT))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return DEFAULT_ENDPOINT


def _load_firmware_target(path: Path) -> str:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return str(payload.get("firmware_target") or DEFAULT_FIRMWARE_TARGET)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return DEFAULT_FIRMWARE_TARGET


def _save_config(path: Path, endpoint: str, firmware_target: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(
            {
                "endpoint": endpoint,
                "firmware_target": firmware_target,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    temporary.replace(path)


def _merge_wled_commands(older: dict[str, object], newer: dict[str, object]) -> dict[str, object]:
    merged = {**older, **newer}
    older_segments = _segment_commands(older.get("seg"))
    newer_segments = _segment_commands(newer.get("seg"))
    if older_segments and newer_segments:
        segment_ids = list(older_segments)
        segment_ids.extend(segment_id for segment_id in newer_segments if segment_id not in older_segments)
        merged["seg"] = [
            {**older_segments.get(segment_id, {}), **newer_segments.get(segment_id, {})}
            for segment_id in segment_ids
        ]
    return merged


def _segment_commands(value: object) -> dict[int, dict[str, object]]:
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, list):
        return {}
    segments: dict[int, dict[str, object]] = {}
    for raw_segment in value:
        if not isinstance(raw_segment, dict):
            continue
        segment_id = raw_segment.get("id", 0)
        if isinstance(segment_id, bool) or not isinstance(segment_id, (int, float)):
            continue
        segments[int(segment_id)] = dict(raw_segment)
    return segments


def _find_effect_preset(preset_id: str) -> dict[str, object] | None:
    return next((preset for preset in EFFECT_PRESETS if preset["id"] == preset_id), None)


def _normalize_effect_name(value: str) -> str:
    return "".join(character for character in value.casefold() if character.isalnum())


def _resolve_effect_ids(effect_names: tuple[str, ...]) -> dict[str, int]:
    available = {
        _normalize_effect_name(name): index
        for index, name in enumerate(effect_names)
        if name not in {"-", "RSVD"}
    }
    resolved: dict[str, int] = {}
    for preset in EFFECT_PRESETS:
        for effect_name in preset["effect_names"]:
            effect_id = available.get(_normalize_effect_name(str(effect_name)))
            if effect_id is not None:
                resolved[str(preset["id"])] = effect_id
                break
    return resolved


def _effect_preset_states(
    effect_ids: dict[str, int],
    *,
    catalog_ready: bool,
) -> list[dict[str, object]]:
    return [
        {
            "id": preset["id"],
            "name": preset["name"],
            "effect_name": preset["effect_names"][0],
            "description": preset["description"],
            "preview": list(preset["preview"]),
            "effect_id": effect_ids.get(str(preset["id"])),
            "supported": str(preset["id"]) in effect_ids if catalog_ready else None,
        }
        for preset in EFFECT_PRESETS
    ]


def _preset_id_for_effect(effect_id: int, effect_ids: dict[str, int]) -> str | None:
    if effect_id == 0:
        return "solid"
    return next((preset_id for preset_id, resolved_id in effect_ids.items() if resolved_id == effect_id), None)


def _normalize_color(value: object) -> str:
    text = str(value).strip().lower()
    if len(text) != 7 or not text.startswith("#"):
        raise ValueError("color must use #RRGGBB format")
    try:
        int(text[1:], 16)
    except ValueError as exc:
        raise ValueError("color must use #RRGGBB format") from exc
    return text


def _hex_to_rgb(color: str) -> tuple[int, int, int]:
    return tuple(int(color[index : index + 2], 16) for index in (1, 3, 5))


def _rgb_to_hex(values: list[object]) -> str:
    channels = [max(0, min(255, int(value))) for value in values]
    return "#" + "".join(f"{channel:02x}" for channel in channels)


def _bounded_int(value: object, minimum: int, maximum: int, name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be between {minimum} and {maximum}") from exc
    if not minimum <= number <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return number


def _percent_to_byte(value: int) -> int:
    return round(value * 255 / 100)


def _byte_to_percent(value: int) -> int:
    return round(max(0, min(255, value)) * 100 / 255)


def _utc_timestamp() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _safe_error(exc: Exception) -> str:
    if isinstance(exc, httpx.ConnectError):
        return "无法连接 WLED 控制器，请检查设备网络和地址"
    if isinstance(exc, httpx.TimeoutException):
        return "WLED 控制器响应超时"
    if isinstance(exc, httpx.HTTPStatusError):
        return f"WLED 控制器返回 HTTP {exc.response.status_code}"
    return f"{exc.__class__.__name__}: {exc}"
