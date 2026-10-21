from __future__ import annotations

import hashlib
import importlib.metadata
import importlib.util
import json
import os
import re
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol, TextIO


KNOWN_USB_SERIAL_VIDS = frozenset({0x0403, 0x10C4, 0x1A86, 0x303A})
ESPRESSIF_VID = 0x303A
PORT_SCAN_INTERVAL_SECONDS = 1.5
MAX_LOG_LINES = 120
MANIFEST_SCHEMA_VERSION = 1
DISPLAY_GPIO_DEFAULTS = {
    "scl": 12,
    "sda": 11,
    "cs": 10,
    "dc": 8,
    "rst": 7,
    "bl": 6,
}
DISPLAY_GPIO_KEYS = tuple(DISPLAY_GPIO_DEFAULTS)
DISPLAY_GPIO_RESERVED = frozenset(range(26, 38)) | {43, 44}
DISPLAY_GPIO_INPUT_ONLY = frozenset({46})


class FirmwareFlashUnavailable(RuntimeError):
    pass


class FirmwareFlashBusy(RuntimeError):
    pass


class WifiProvisioningInvalid(ValueError):
    pass


class DisplayGpioInvalid(ValueError):
    pass


class ProcessLike(Protocol):
    stdout: TextIO | None

    def wait(self, timeout: float | None = None) -> int: ...

    def poll(self) -> int | None: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...


class SerialLike(Protocol):
    def write(self, data: bytes) -> int: ...

    def readline(self) -> bytes: ...

    def reset_input_buffer(self) -> None: ...

    def close(self) -> None: ...


@dataclass(frozen=True)
class FlashRuntime:
    values: dict[str, str]
    esptool_command: tuple[str, ...]
    esptool_version: str
    pyserial_version: str


@dataclass(frozen=True)
class FirmwareImage:
    address: int
    path: Path
    size: int
    sha256: str


@dataclass(frozen=True)
class FirmwarePackage:
    root: Path
    product: str
    firmware: str
    version: str
    created_at: str
    target: str
    chip: str
    baud: int
    before: str
    after: str
    flash_arguments: tuple[str, ...]
    images: tuple[FirmwareImage, ...]

    @property
    def total_size(self) -> int:
        return sum(image.size for image in self.images)

    @property
    def build_time(self) -> float | None:
        timestamps = [image.path.stat().st_mtime for image in self.images if image.path.is_file()]
        return max(timestamps) if timestamps else None


@dataclass(frozen=True)
class SerialPort:
    device: str
    description: str
    vid: int | None = None
    pid: int | None = None
    manufacturer: str | None = None
    product: str | None = None

    @property
    def score(self) -> int:
        description = " ".join(
            value for value in (self.description, self.manufacturer, self.product) if value
        ).lower()
        if self.vid == ESPRESSIF_VID:
            return 100
        if self.vid in KNOWN_USB_SERIAL_VIDS:
            return 80
        if any(token in description for token in ("esp32", "espressif", "usb serial", "usb-serial", "cp210", "ch340")):
            return 60
        return 0

    def payload(self) -> dict[str, object]:
        return {
            "device": self.device,
            "description": self.description or "USB 串口设备",
            "vid": self.vid,
            "pid": self.pid,
            "native_usb": self.vid == ESPRESSIF_VID,
        }


RuntimeLoader = Callable[[], FlashRuntime]
PackageLoader = Callable[[], FirmwarePackage]
PortScanner = Callable[[FlashRuntime], list[SerialPort]]
ProcessFactory = Callable[..., ProcessLike]
SerialFactory = Callable[[str, int, float], SerialLike]


class FirmwareFlashService:
    def __init__(
        self,
        package_dir: Path,
        *,
        runtime_loader: RuntimeLoader | None = None,
        package_loader: PackageLoader | None = None,
        port_scanner: PortScanner | None = None,
        process_factory: ProcessFactory | None = None,
        serial_factory: SerialFactory | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.package_dir = package_dir.resolve()
        self._runtime_loader = runtime_loader or load_application_flash_runtime
        self._package_loader = package_loader or (lambda: load_firmware_package(self.package_dir))
        self._port_scanner = port_scanner or scan_serial_ports
        self._process_factory = process_factory or subprocess.Popen
        self._serial_factory = serial_factory or open_serial_port
        self._clock = clock
        self._lock = threading.RLock()
        self._runtime: FlashRuntime | None = None
        self._runtime_error: str | None = None
        self._package: FirmwarePackage | None = None
        self._package_error: str | None = None
        self._ports: list[SerialPort] = []
        self._ports_scanned_at = 0.0
        self._status = "idle"
        self._phase = "ready"
        self._progress = 0
        self._message = "正在检查内置烧录器"
        self._selected_port: SerialPort | None = None
        self._started_at: float | None = None
        self._finished_at: float | None = None
        self._logs: deque[str] = deque(maxlen=MAX_LOG_LINES)
        self._thread: threading.Thread | None = None
        self._process: ProcessLike | None = None
        self._wifi_thread: threading.Thread | None = None
        self._wifi_serial: SerialLike | None = None
        self._wifi_status = "idle"
        self._wifi_message = "连接 USB 后可写入网络"
        self._wifi_ssid: str | None = None
        self._wifi_ip: str | None = None
        self._wifi_connected: bool | None = None
        self._wifi_port: SerialPort | None = None
        self._wifi_started_at: float | None = None
        self._wifi_finished_at: float | None = None
        self._display_gpio_thread: threading.Thread | None = None
        self._display_gpio_serial: SerialLike | None = None
        self._display_gpio_status = "idle"
        self._display_gpio_message = "连接 USB 后可读取或写入屏幕引脚"
        self._display_gpio_values = dict(DISPLAY_GPIO_DEFAULTS)
        self._display_gpio_port: SerialPort | None = None
        self._display_gpio_started_at: float | None = None
        self._display_gpio_finished_at: float | None = None
        self._closed = False

    def snapshot(self, *, refresh_ports: bool = True) -> dict[str, object]:
        runtime = self._get_runtime()
        package = self._get_package()
        if refresh_ports and runtime is not None and not self.running and not self.configuration_running:
            self._refresh_ports(runtime)
        with self._lock:
            running = self._status in {"detecting", "connecting", "flashing", "verifying", "restarting"}
            wifi_running = self.wifi_running
            display_gpio_running = self.display_gpio_running
            configuration_running = wifi_running or display_gpio_running
            ports = [port.payload() for port in self._ports]
            selected = self._selected_port.payload() if self._selected_port is not None else (ports[0] if ports else None)
            runtime_ready = runtime is not None
            package_ready = package is not None
            return {
                "status": self._status,
                "phase": self._phase,
                "progress": self._progress,
                "message": self._message,
                "running": running,
                "can_flash": runtime_ready and package_ready and bool(ports) and not running and not configuration_running,
                "runtime_ready": runtime_ready,
                "runtime_error": self._runtime_error,
                "environment_ready": runtime_ready,
                "environment_error": self._runtime_error,
                "flasher": "esptool",
                "flasher_version": runtime.esptool_version if runtime is not None else None,
                "pyserial_version": runtime.pyserial_version if runtime is not None else None,
                "target": package.target if package is not None else "ESP device",
                "chip": package.chip if package is not None else None,
                "project": package.firmware if package is not None else "espagent_display",
                "project_ready": package_ready,
                "package_ready": package_ready,
                "package_error": self._package_error,
                "firmware": package.firmware if package is not None else "espagent_display",
                "firmware_version": package.version if package is not None else None,
                "release_mode": "prebuilt",
                "build_time": package.build_time if package is not None else None,
                "binary_size": package.total_size if package is not None else None,
                "image_count": len(package.images) if package is not None else 0,
                "ports": ports,
                "device": selected,
                "device_count": len(ports),
                "started_at": self._started_at,
                "finished_at": self._finished_at,
                "logs": list(self._logs),
                "wifi": {
                    "status": self._wifi_status,
                    "message": self._wifi_message,
                    "running": wifi_running,
                    "can_configure": runtime_ready and bool(ports) and not running and not configuration_running,
                    "ssid": self._wifi_ssid,
                    "ip": self._wifi_ip,
                    "connected": self._wifi_connected,
                    "device": self._wifi_port.payload() if self._wifi_port is not None else selected,
                    "started_at": self._wifi_started_at,
                    "finished_at": self._wifi_finished_at,
                },
                "display_gpio": {
                    "status": self._display_gpio_status,
                    "message": self._display_gpio_message,
                    "running": display_gpio_running,
                    "can_configure": runtime_ready and bool(ports) and not running and not configuration_running,
                    "values": dict(self._display_gpio_values),
                    "defaults": dict(DISPLAY_GPIO_DEFAULTS),
                    "device": self._display_gpio_port.payload() if self._display_gpio_port is not None else selected,
                    "started_at": self._display_gpio_started_at,
                    "finished_at": self._display_gpio_finished_at,
                },
            }

    @property
    def running(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    @property
    def wifi_running(self) -> bool:
        with self._lock:
            return self._wifi_thread is not None and self._wifi_thread.is_alive()

    @property
    def display_gpio_running(self) -> bool:
        with self._lock:
            return self._display_gpio_thread is not None and self._display_gpio_thread.is_alive()

    @property
    def configuration_running(self) -> bool:
        return self.wifi_running or self.display_gpio_running

    def start_flash(self, preferred_device: str | None = None) -> dict[str, object]:
        runtime = self._get_runtime()
        if runtime is None:
            raise FirmwareFlashUnavailable(self._runtime_error or "应用内置烧录器不可用")
        package = self._get_package()
        if package is None:
            raise FirmwareFlashUnavailable(self._package_error or "内置固件包不可用")
        ports = self._refresh_ports(runtime, force=True)
        if not ports:
            raise FirmwareFlashUnavailable(f"未发现可用的 {package.target} USB 串口")
        preferred_device = str(preferred_device or "").strip()
        if preferred_device:
            ports = [
                port
                for port in ports
                if port.device.casefold() == preferred_device.casefold()
            ]
            if not ports:
                raise FirmwareFlashUnavailable(f"串口 {preferred_device} 当前不可用")
        with self._lock:
            if self.running or self.configuration_running:
                raise FirmwareFlashBusy("USB 串口正在执行其他任务")
            self._status = "detecting"
            self._phase = "detecting"
            self._progress = 4
            self._message = f"正在确认 {package.target} 端口"
            self._selected_port = None
            self._started_at = self._clock()
            self._finished_at = None
            self._logs.clear()
            self._append_log(f"使用内置固件 {package.version}，无需 ESP-IDF 或手动参数。")
            self._thread = threading.Thread(
                target=self._flash_worker,
                args=(runtime, package, list(ports)),
                name="espagent-firmware-flash",
                daemon=True,
            )
            self._thread.start()
        return self.snapshot(refresh_ports=False)

    def start_wifi_provisioning(self, ssid: str, password: str) -> dict[str, object]:
        ssid_bytes, password_bytes = validate_wifi_credentials(ssid, password)
        runtime = self._get_runtime()
        if runtime is None:
            raise FirmwareFlashUnavailable(self._runtime_error or "应用内置串口组件不可用")
        ports = self._refresh_ports(runtime, force=True)
        if not ports:
            raise FirmwareFlashUnavailable("未发现可用的 ESP32-S3 USB 串口")
        with self._lock:
            if self.running or self.configuration_running:
                raise FirmwareFlashBusy("USB 串口正在执行其他任务")
            self._wifi_status = "detecting"
            self._wifi_message = "正在识别 ESPAgent 设备"
            self._wifi_ssid = ssid
            self._wifi_ip = None
            self._wifi_connected = None
            self._wifi_port = None
            self._wifi_started_at = self._clock()
            self._wifi_finished_at = None
            self._wifi_thread = threading.Thread(
                target=self._wifi_worker,
                args=(list(ports), ssid_bytes, password_bytes),
                name="espagent-wifi-provision",
                daemon=True,
            )
            self._wifi_thread.start()
        return self.snapshot(refresh_ports=False)

    def start_display_gpio_read(self) -> dict[str, object]:
        return self._start_display_gpio_operation(None)

    def start_display_gpio_configuration(self, values: dict[str, int]) -> dict[str, object]:
        return self._start_display_gpio_operation(validate_display_gpio(values))

    def _start_display_gpio_operation(self, values: dict[str, int] | None) -> dict[str, object]:
        runtime = self._get_runtime()
        if runtime is None:
            raise FirmwareFlashUnavailable(self._runtime_error or "应用内置串口组件不可用")
        ports = self._refresh_ports(runtime, force=True)
        if not ports:
            raise FirmwareFlashUnavailable("未发现可用的 ESP32-S3 USB 串口")
        with self._lock:
            if self.running or self.configuration_running:
                raise FirmwareFlashBusy("USB 串口正在执行其他任务")
            self._display_gpio_status = "detecting"
            self._display_gpio_message = "正在识别 ESPAgent 设备"
            if values is not None:
                self._display_gpio_values = dict(values)
            self._display_gpio_port = None
            self._display_gpio_started_at = self._clock()
            self._display_gpio_finished_at = None
            self._display_gpio_thread = threading.Thread(
                target=self._display_gpio_worker,
                args=(list(ports), values),
                name="espagent-display-gpio",
                daemon=True,
            )
            self._display_gpio_thread.start()
        return self.snapshot(refresh_ports=False)

    def wait(self, timeout: float | None = None) -> bool:
        thread = self._thread
        if thread is None:
            return True
        thread.join(timeout=timeout)
        return not thread.is_alive()

    def wait_wifi(self, timeout: float | None = None) -> bool:
        thread = self._wifi_thread
        if thread is None:
            return True
        thread.join(timeout=timeout)
        return not thread.is_alive()

    def wait_display_gpio(self, timeout: float | None = None) -> bool:
        thread = self._display_gpio_thread
        if thread is None:
            return True
        thread.join(timeout=timeout)
        return not thread.is_alive()

    def close(self) -> None:
        with self._lock:
            self._closed = True
            process = self._process
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
        with self._lock:
            serial_connection = self._wifi_serial
        if serial_connection is not None:
            serial_connection.close()
        self.wait(timeout=3)
        self.wait_wifi(timeout=3)
        with self._lock:
            display_gpio_connection = self._display_gpio_serial
        if display_gpio_connection is not None:
            display_gpio_connection.close()
        self.wait_display_gpio(timeout=3)

    def _display_gpio_worker(self, ports: list[SerialPort], values: dict[str, int] | None) -> None:
        try:
            for port in ports:
                if self._closed:
                    return
                with self._lock:
                    self._display_gpio_message = f"正在验证 {port.device}"
                if self._configure_display_gpio_port(port, values):
                    return
            raise FirmwareFlashUnavailable("未找到支持屏幕 GPIO 设置的 ESPAgent 固件")
        except Exception as exc:
            with self._lock:
                self._display_gpio_status = "error"
                self._display_gpio_message = str(exc)
                self._display_gpio_finished_at = self._clock()
        finally:
            with self._lock:
                serial_connection = self._display_gpio_serial
                self._display_gpio_serial = None
            if serial_connection is not None:
                try:
                    serial_connection.close()
                except Exception:
                    pass

    def _configure_display_gpio_port(self, port: SerialPort, values: dict[str, int] | None) -> bool:
        try:
            connection = self._serial_factory(port.device, 115200, 0.2)
        except Exception:
            return False
        with self._lock:
            self._display_gpio_serial = connection
        try:
            try:
                connection.reset_input_buffer()
            except Exception:
                pass
            current = self._wait_for_display_gpio_ready(connection, timeout=7.0)
            if current is None:
                return False
            with self._lock:
                self._display_gpio_port = port
                self._display_gpio_values = dict(current)
            if values is None:
                with self._lock:
                    self._display_gpio_status = "success"
                    self._display_gpio_message = "已读取设备屏幕引脚"
                    self._display_gpio_finished_at = self._clock()
                return True

            with self._lock:
                self._display_gpio_status = "applying"
                self._display_gpio_message = "正在写入屏幕引脚"
                self._display_gpio_values = dict(values)
            arguments = " ".join(str(values[key]) for key in DISPLAY_GPIO_KEYS)
            connection.write(f"ESPAGENT_DISPLAY_GPIO_SET 1 {arguments}\n".encode("ascii"))
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                line = read_display_gpio_protocol_line(connection)
                if line is None:
                    continue
                if line == "ESPAGENT_DISPLAY_GPIO OK RESTARTING":
                    with self._lock:
                        self._display_gpio_status = "success"
                        self._display_gpio_message = "屏幕引脚已保存，设备正在重启"
                        self._display_gpio_finished_at = self._clock()
                    return True
                if line.startswith("ESPAGENT_DISPLAY_GPIO ERR "):
                    code = line.removeprefix("ESPAGENT_DISPLAY_GPIO ERR ").strip()
                    raise FirmwareFlashUnavailable(display_gpio_error_message(code))
            raise FirmwareFlashUnavailable("写入屏幕引脚超时；请重新连接 USB 后读取确认")
        finally:
            with self._lock:
                if self._display_gpio_serial is connection:
                    self._display_gpio_serial = None
            try:
                connection.close()
            except Exception:
                pass

    def _wait_for_display_gpio_ready(self, connection: SerialLike, *, timeout: float) -> dict[str, int] | None:
        deadline = time.monotonic() + timeout
        next_request = 0.0
        while time.monotonic() < deadline:
            now = time.monotonic()
            if now >= next_request:
                connection.write(b"ESPAGENT_DISPLAY_GPIO_GET 1\n")
                next_request = now + 0.8
            line = read_display_gpio_protocol_line(connection)
            if line is None or not line.startswith("ESPAGENT_DISPLAY_GPIO READY 1 "):
                continue
            fields = line.split()
            if len(fields) != 9:
                continue
            try:
                return validate_display_gpio(dict(zip(DISPLAY_GPIO_KEYS, map(int, fields[3:9]))))
            except (ValueError, DisplayGpioInvalid):
                continue
        return None

    def _wifi_worker(
        self,
        ports: list[SerialPort],
        ssid: bytes,
        password: bytes,
    ) -> None:
        try:
            for port in ports:
                if self._closed:
                    return
                with self._lock:
                    self._wifi_message = f"正在验证 {port.device}"
                if self._configure_wifi_port(port, ssid, password):
                    return
            raise FirmwareFlashUnavailable("未找到运行 USB 配网协议的 ESPAgent 固件")
        except Exception as exc:
            with self._lock:
                self._wifi_status = "error"
                self._wifi_message = str(exc)
                self._wifi_connected = False
                self._wifi_finished_at = self._clock()
        finally:
            with self._lock:
                serial_connection = self._wifi_serial
                self._wifi_serial = None
            if serial_connection is not None:
                try:
                    serial_connection.close()
                except Exception:
                    pass

    def _configure_wifi_port(self, port: SerialPort, ssid: bytes, password: bytes) -> bool:
        try:
            connection = self._serial_factory(port.device, 115200, 0.2)
        except Exception:
            return False
        with self._lock:
            self._wifi_serial = connection
        try:
            try:
                connection.reset_input_buffer()
            except Exception:
                pass
            ready = self._wait_for_wifi_ready(connection, timeout=7.0)
            if ready is None:
                return False
            connected, current_ssid, current_ip = ready
            with self._lock:
                self._wifi_port = port
                self._wifi_connected = connected
                self._wifi_ip = current_ip
                self._wifi_status = "applying"
                self._wifi_message = f"正在写入 {self._wifi_ssid}"
                if not self._wifi_ssid:
                    self._wifi_ssid = current_ssid

            ssid_hex = ssid.hex()
            password_hex = password.hex() if password else "-"
            command = f"ESPAGENT_WIFI_SET 1 {ssid_hex} {password_hex}\n".encode("ascii")
            connection.write(command)
            deadline = time.monotonic() + 25.0
            while time.monotonic() < deadline:
                line = read_wifi_protocol_line(connection)
                if line is None:
                    continue
                if line == "ESPAGENT_WIFI ACK APPLYING":
                    with self._lock:
                        self._wifi_status = "connecting"
                        self._wifi_message = "网络已保存，正在连接"
                    continue
                if line.startswith("ESPAGENT_WIFI OK "):
                    ip = line.removeprefix("ESPAGENT_WIFI OK ").strip()
                    with self._lock:
                        self._wifi_status = "success"
                        self._wifi_message = f"已连接 {self._wifi_ssid}"
                        self._wifi_ip = None if ip == "-" else ip
                        self._wifi_connected = True
                        self._wifi_finished_at = self._clock()
                    return True
                if line.startswith("ESPAGENT_WIFI ERR "):
                    code = line.removeprefix("ESPAGENT_WIFI ERR ").strip()
                    raise FirmwareFlashUnavailable(wifi_error_message(code))
            raise FirmwareFlashUnavailable("网络连接超时；凭据已保存，可检查路由器后重试")
        finally:
            with self._lock:
                if self._wifi_serial is connection:
                    self._wifi_serial = None
            try:
                connection.close()
            except Exception:
                pass

    def _wait_for_wifi_ready(
        self,
        connection: SerialLike,
        *,
        timeout: float,
    ) -> tuple[bool, str | None, str | None] | None:
        deadline = time.monotonic() + timeout
        next_hello = 0.0
        while time.monotonic() < deadline:
            now = time.monotonic()
            if now >= next_hello:
                connection.write(b"ESPAGENT_WIFI_HELLO 1\n")
                next_hello = now + 0.8
            line = read_wifi_protocol_line(connection)
            if line is None or not line.startswith("ESPAGENT_WIFI READY 1 "):
                continue
            fields = line.split()
            if len(fields) != 6:
                continue
            connected = fields[3] == "1"
            current_ssid = decode_wifi_hex(fields[4])
            current_ip = None if fields[5] == "-" else fields[5]
            return connected, current_ssid, current_ip
        return None

    def _get_runtime(self) -> FlashRuntime | None:
        with self._lock:
            if self._runtime is not None:
                return self._runtime
            if self._runtime_error is not None:
                return None
        try:
            runtime = self._runtime_loader()
        except Exception as exc:
            with self._lock:
                self._runtime_error = f"内置烧录器初始化失败：{exc}"
                self._message = "应用内置烧录器不可用"
                self._phase = "unavailable"
            return None
        with self._lock:
            self._runtime = runtime
            self._runtime_error = None
        return runtime

    def _get_package(self) -> FirmwarePackage | None:
        with self._lock:
            if self._package is not None:
                return self._package
            if self._package_error is not None:
                return None
        try:
            package = self._package_loader()
        except Exception as exc:
            with self._lock:
                self._package_error = f"固件包校验失败：{exc}"
                self._message = "内置固件包不可用"
                self._phase = "unavailable"
            return None
        with self._lock:
            self._package = package
            self._package_error = None
        return package

    def _refresh_ports(self, runtime: FlashRuntime, *, force: bool = False) -> list[SerialPort]:
        now = self._clock()
        with self._lock:
            if not force and now - self._ports_scanned_at < PORT_SCAN_INTERVAL_SECONDS:
                return list(self._ports)
        try:
            ports = rank_serial_ports(self._port_scanner(runtime))
        except Exception as exc:
            with self._lock:
                self._ports = []
                self._ports_scanned_at = now
                if not self.running and not self.configuration_running:
                    self._message = f"串口扫描失败：{exc}"
            return []
        with self._lock:
            self._ports = ports
            self._ports_scanned_at = now
            if not self.running and not self.configuration_running and self._selected_port is not None:
                connected_devices = {port.device for port in ports}
                if self._selected_port.device not in connected_devices:
                    self._selected_port = None
            if not self.running and not self.configuration_running and self._status not in {"success", "error"}:
                target = self._package.target if self._package is not None else "ESP 设备"
                self._message = f"已发现 {target}，等待烧录" if ports else f"连接 {target} 后即可烧录"
                self._phase = "ready" if ports else "waiting"
        return list(ports)

    def _flash_worker(
        self,
        runtime: FlashRuntime,
        package: FirmwarePackage,
        ports: list[SerialPort],
    ) -> None:
        try:
            selected = self._detect_target(runtime, package, ports)
            if selected is None:
                raise FirmwareFlashUnavailable(f"无法确认 {package.target}；请重新插拔设备后重试")
            with self._lock:
                self._selected_port = selected
                self._status = "connecting"
                self._phase = "connecting"
                self._progress = 28
                self._message = f"正在连接 {package.target}"
            self._append_log(f"已确认 {selected.device} 为 {package.target}。")
            command = build_flash_command(runtime, package, selected.device)
            return_code = self._run_process(command, runtime, publish_output=True)
            if return_code != 0:
                raise RuntimeError(self._last_log() or f"esptool 退出码 {return_code}")
            with self._lock:
                self._status = "success"
                self._phase = "complete"
                self._progress = 100
                self._message = f"烧录完成，{package.target} 正在重启"
                self._finished_at = self._clock()
            self._append_log("固件校验完成，设备已重启。")
        except Exception as exc:
            with self._lock:
                self._status = "error"
                self._phase = "error"
                self._progress = 0
                self._message = str(exc)
                self._finished_at = self._clock()
            self._append_log(f"烧录失败：{exc}")
        finally:
            with self._lock:
                self._process = None

    def _detect_target(
        self,
        runtime: FlashRuntime,
        package: FirmwarePackage,
        ports: list[SerialPort],
    ) -> SerialPort | None:
        for port in ports:
            if self._closed:
                return None
            with self._lock:
                self._message = f"正在验证 {port.device}"
            self._append_log(f"检测 {port.device} · {port.description}")
            command = build_probe_command(runtime, package, port.device)
            if self._run_process(command, runtime, publish_output=False) == 0:
                return port
            self._append_log(f"{port.device} 不是可用的 {package.target}，继续检测。")
        return None

    def _run_process(
        self,
        command: list[str],
        runtime: FlashRuntime,
        *,
        publish_output: bool,
    ) -> int:
        process = self._process_factory(
            command,
            cwd=self.package_dir,
            env=runtime.values,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            **hidden_subprocess_options(),
        )
        with self._lock:
            self._process = process
        if process.stdout is not None:
            for raw_line in process.stdout:
                line = strip_ansi(raw_line).strip()
                if not line:
                    continue
                if publish_output:
                    self._append_log(line)
                    self._update_progress(line)
        return_code = process.wait()
        with self._lock:
            if self._process is process:
                self._process = None
        return return_code

    def _update_progress(self, line: str) -> None:
        lowered = line.lower()
        with self._lock:
            if "serial port" in lowered or "connecting" in lowered:
                self._status = "connecting"
                self._phase = "connecting"
                self._progress = max(self._progress, 28)
                target = self._package.target if self._package is not None else "ESP 设备"
                self._message = f"正在连接 {target}"
            if re.search(r"writing at .*?\(\s*\d+\s*%\)", lowered):
                self._status = "flashing"
                self._phase = "flashing"
                self._progress = max(self._progress, 72)
                self._message = "正在写入内置固件"
            if "hard resetting" in lowered or "leaving" in lowered:
                self._status = "restarting"
                self._phase = "restarting"
                self._progress = max(self._progress, 98)
                target = self._package.target if self._package is not None else "ESP 设备"
                self._message = f"正在重启 {target}"

    def _append_log(self, line: str) -> None:
        with self._lock:
            self._logs.append(line[:600])

    def _last_log(self) -> str:
        with self._lock:
            return self._logs[-1] if self._logs else ""


def validate_wifi_credentials(ssid: str, password: str) -> tuple[bytes, bytes]:
    normalized_ssid = ssid.strip()
    if not normalized_ssid:
        raise WifiProvisioningInvalid("请输入 Wi-Fi 名称")
    if "\x00" in normalized_ssid or "\x00" in password:
        raise WifiProvisioningInvalid("网络名称或密码包含无效字符")
    ssid_bytes = normalized_ssid.encode("utf-8")
    password_bytes = password.encode("utf-8")
    if len(ssid_bytes) > 32:
        raise WifiProvisioningInvalid("Wi-Fi 名称不能超过 32 字节")
    if password_bytes and len(password_bytes) < 8:
        raise WifiProvisioningInvalid("Wi-Fi 密码至少需要 8 字节；开放网络请留空")
    if len(password_bytes) > 63:
        raise WifiProvisioningInvalid("Wi-Fi 密码不能超过 63 字节")
    return ssid_bytes, password_bytes


def validate_display_gpio(values: dict[str, int]) -> dict[str, int]:
    if set(values) != set(DISPLAY_GPIO_KEYS):
        raise DisplayGpioInvalid("屏幕引脚参数不完整")
    normalized: dict[str, int] = {}
    for key in DISPLAY_GPIO_KEYS:
        value = values[key]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > 48:
            raise DisplayGpioInvalid(f"{key.upper()} 必须是 0 到 48 的 GPIO 编号")
        if value in DISPLAY_GPIO_RESERVED:
            raise DisplayGpioInvalid(f"GPIO {value} 已保留给八线 PSRAM/Flash 或 USB 串口")
        if value in DISPLAY_GPIO_INPUT_ONLY:
            raise DisplayGpioInvalid(f"GPIO {value} 不能作为屏幕输出引脚")
        normalized[key] = value
    if len(set(normalized.values())) != len(normalized):
        raise DisplayGpioInvalid("六个屏幕信号不能使用重复 GPIO")
    return normalized


def open_serial_port(device: str, baudrate: int, timeout: float) -> SerialLike:
    try:
        serial_module = importlib.import_module("serial")
    except ImportError as exc:
        raise FirmwareFlashUnavailable("桌面应用缺少 pyserial") from exc
    connection = serial_module.Serial(port=None, baudrate=baudrate, timeout=timeout, write_timeout=2)
    connection.port = device
    connection.dtr = False
    connection.rts = False
    connection.open()
    return connection


def read_wifi_protocol_line(connection: SerialLike) -> str | None:
    raw = connection.readline()
    if not raw:
        return None
    line = strip_ansi(raw.decode("utf-8", errors="replace")).strip()
    marker = line.find("ESPAGENT_WIFI ")
    return line[marker:] if marker >= 0 else None


def read_display_gpio_protocol_line(connection: SerialLike) -> str | None:
    raw = connection.readline()
    if not raw:
        return None
    line = strip_ansi(raw.decode("utf-8", errors="replace")).strip()
    marker = line.find("ESPAGENT_DISPLAY_GPIO ")
    return line[marker:] if marker >= 0 else None


def decode_wifi_hex(value: str) -> str | None:
    if value == "-":
        return None
    try:
        return bytes.fromhex(value).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None


def wifi_error_message(code: str) -> str:
    messages = {
        "INVALID_SSID": "ESP32 拒绝了 Wi-Fi 名称",
        "INVALID_PASSWORD": "ESP32 拒绝了 Wi-Fi 密码",
        "INVALID_COMMAND": "ESP32 不支持当前配网命令",
        "CONNECT_TIMEOUT": "ESP32 连接网络超时，请检查名称、密码和 2.4 GHz 信号",
        "IP_UNAVAILABLE": "ESP32 已连接但未取得 IP 地址",
    }
    if code.startswith("APPLY_"):
        return "ESP32 无法保存网络配置"
    return messages.get(code, f"ESP32 配网失败：{code}")


def display_gpio_error_message(code: str) -> str:
    messages = {
        "INVALID_COMMAND": "ESP32 不支持当前屏幕引脚命令",
        "INVALID_GPIO": "ESP32 拒绝了无效、重复或保留的 GPIO",
    }
    if code.startswith("LOAD_"):
        return "ESP32 无法读取屏幕引脚配置"
    if code.startswith("SAVE_"):
        return "ESP32 无法保存屏幕引脚配置"
    return messages.get(code, f"ESP32 屏幕引脚设置失败：{code}")


def load_application_flash_runtime() -> FlashRuntime:
    missing = [module for module in ("esptool", "serial") if importlib.util.find_spec(module) is None]
    if missing:
        raise FirmwareFlashUnavailable(f"桌面应用缺少烧录组件：{', '.join(missing)}")
    environment = dict(os.environ)
    environment["PYTHONIOENCODING"] = "utf-8"
    environment["PYTHONUNBUFFERED"] = "1"
    executable_override = os.environ.get("ESPAGENT_ESPTOOL_EXECUTABLE")
    if executable_override:
        executable = Path(executable_override).resolve()
        if not executable.is_file():
            raise FirmwareFlashUnavailable("内置 esptool 可执行文件不存在")
        command = (str(executable),)
    else:
        command = (sys.executable, "-m", "esptool")
    return FlashRuntime(
        values=environment,
        esptool_command=command,
        esptool_version=importlib.metadata.version("esptool"),
        pyserial_version=importlib.metadata.version("pyserial"),
    )


def load_firmware_package(package_dir: Path) -> FirmwarePackage:
    root = package_dir.resolve()
    manifest_path = root / "manifest.json"
    if not manifest_path.is_file():
        raise FirmwareFlashUnavailable("缺少 manifest.json")
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FirmwareFlashUnavailable(f"无法读取固件清单：{exc}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        raise FirmwareFlashUnavailable("固件清单版本不受支持")
    flash = payload.get("flash")
    image_payloads = payload.get("images")
    if not isinstance(flash, dict) or not isinstance(image_payloads, list) or not image_payloads:
        raise FirmwareFlashUnavailable("固件清单缺少烧录参数或镜像")
    images: list[FirmwareImage] = []
    seen_addresses: set[int] = set()
    for item in image_payloads:
        if not isinstance(item, dict):
            raise FirmwareFlashUnavailable("固件镜像条目无效")
        try:
            address = int(str(item["address"]), 0)
            relative_path = Path(str(item["file"]))
            expected_size = int(item["size"])
            expected_sha256 = str(item["sha256"]).lower()
        except (KeyError, TypeError, ValueError) as exc:
            raise FirmwareFlashUnavailable("固件镜像参数无效") from exc
        path = (root / relative_path).resolve()
        if relative_path.is_absolute() or not path.is_relative_to(root):
            raise FirmwareFlashUnavailable("固件镜像路径越界")
        if address < 0 or address in seen_addresses:
            raise FirmwareFlashUnavailable("固件镜像地址无效或重复")
        if not path.is_file():
            raise FirmwareFlashUnavailable(f"缺少固件镜像：{relative_path.as_posix()}")
        actual_size = path.stat().st_size
        if actual_size != expected_size:
            raise FirmwareFlashUnavailable(f"固件镜像大小不匹配：{relative_path.as_posix()}")
        actual_sha256 = file_sha256(path)
        if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256) or actual_sha256 != expected_sha256:
            raise FirmwareFlashUnavailable(f"固件镜像校验失败：{relative_path.as_posix()}")
        seen_addresses.add(address)
        images.append(FirmwareImage(address, path, actual_size, actual_sha256))
    arguments = flash.get("arguments", [])
    if not isinstance(arguments, list) or not all(isinstance(value, str) for value in arguments):
        raise FirmwareFlashUnavailable("固件烧录参数无效")
    try:
        baud = int(flash.get("baud", 460800))
    except (TypeError, ValueError) as exc:
        raise FirmwareFlashUnavailable("固件烧录波特率无效") from exc
    if baud < 115200 or baud > 2_000_000:
        raise FirmwareFlashUnavailable("固件烧录波特率超出允许范围")
    chip = str(payload.get("chip") or "").lower()
    if chip not in {"esp32s3", "esp8266"}:
        raise FirmwareFlashUnavailable("内置固件目标不受支持")
    return FirmwarePackage(
        root=root,
        product=str(payload.get("product") or "ESPAgent Display"),
        firmware=str(payload.get("firmware") or "espagent_display"),
        version=str(payload.get("version") or "unknown"),
        created_at=str(payload.get("created_at") or ""),
        target=str(payload.get("target") or "ESP32-S3"),
        chip=chip,
        baud=baud,
        before=str(flash.get("before") or "default_reset"),
        after=str(flash.get("after") or "hard_reset"),
        flash_arguments=tuple(arguments),
        images=tuple(sorted(images, key=lambda image: image.address)),
    )


def build_probe_command(runtime: FlashRuntime, package: FirmwarePackage, port: str) -> list[str]:
    return [
        *runtime.esptool_command,
        "--chip",
        package.chip,
        "--port",
        port,
        "--baud",
        "115200",
        "chip_id",
    ]


def build_flash_command(runtime: FlashRuntime, package: FirmwarePackage, port: str) -> list[str]:
    command = [
        *runtime.esptool_command,
        "--chip",
        package.chip,
        "--port",
        port,
        "--baud",
        str(package.baud),
        "--before",
        package.before,
        "--after",
        package.after,
        "write_flash",
        *package.flash_arguments,
    ]
    for image in package.images:
        command.extend((hex(image.address), str(image.path)))
    return command


def scan_serial_ports(_runtime: FlashRuntime) -> list[SerialPort]:
    try:
        from serial.tools import list_ports
    except ImportError as exc:
        raise FirmwareFlashUnavailable("桌面应用缺少 pyserial") from exc
    return [
        SerialPort(
            device=str(port.device or ""),
            description=str(port.description or ""),
            vid=port.vid,
            pid=port.pid,
            manufacturer=port.manufacturer,
            product=port.product,
        )
        for port in list_ports.comports()
        if re.fullmatch(r"COM\d+", str(port.device or ""), re.IGNORECASE)
    ]


def rank_serial_ports(ports: list[SerialPort]) -> list[SerialPort]:
    candidates = [port for port in ports if port.score > 0]
    return sorted(
        candidates,
        key=lambda port: (-port.score, _port_number(port.device), port.device.lower()),
    )


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(128 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def hidden_subprocess_options() -> dict[str, object]:
    if os.name != "nt":
        return {}
    startup_info = subprocess.STARTUPINFO()
    startup_info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup_info.wShowWindow = subprocess.SW_HIDE
    return {
        "creationflags": subprocess.CREATE_NO_WINDOW,
        "startupinfo": startup_info,
    }


def strip_ansi(value: str) -> str:
    return re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", value)


def _port_number(device: str) -> int:
    match = re.search(r"(\d+)$", device)
    return int(match.group(1)) if match else 1_000_000
