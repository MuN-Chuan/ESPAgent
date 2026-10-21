from __future__ import annotations

import hashlib
import json
import sys
from io import StringIO
from pathlib import Path

import pytest

from espagent_demo.firmware_flash import (
    DisplayGpioInvalid,
    FirmwareFlashService,
    FlashRuntime,
    SerialPort,
    WifiProvisioningInvalid,
    build_flash_command,
    load_application_flash_runtime,
    load_firmware_package,
    rank_serial_ports,
    validate_display_gpio,
)


REPO_ROOT = Path(__file__).resolve().parents[3]


class FakeProcess:
    def __init__(self, output: str, return_code: int) -> None:
        self.stdout = StringIO(output)
        self.return_code = return_code
        self.terminated = False

    def wait(self, timeout: float | None = None) -> int:
        return self.return_code

    def poll(self) -> int | None:
        return self.return_code

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.terminated = True


class FakeSerial:
    def __init__(self) -> None:
        self.responses: list[bytes] = []
        self.writes: list[bytes] = []
        self.closed = False

    def write(self, data: bytes) -> int:
        self.writes.append(data)
        if data == b"ESPAGENT_WIFI_HELLO 1\n":
            self.responses.append(b"ESPAGENT_WIFI READY 1 1 6f6c642d77696669 192.168.1.7\r\n")
        elif data.startswith(b"ESPAGENT_WIFI_SET 1 "):
            self.responses.extend([
                b"ESPAGENT_WIFI ACK APPLYING\r\n",
                b"ESPAGENT_WIFI OK 192.168.1.8\r\n",
            ])
        elif data == b"ESPAGENT_DISPLAY_GPIO_GET 1\n":
            self.responses.append(b"ESPAGENT_DISPLAY_GPIO READY 1 12 11 10 8 7 6\r\n")
        elif data.startswith(b"ESPAGENT_DISPLAY_GPIO_SET 1 "):
            self.responses.append(b"ESPAGENT_DISPLAY_GPIO OK RESTARTING\r\n")
        return len(data)

    def readline(self) -> bytes:
        return self.responses.pop(0) if self.responses else b""

    def reset_input_buffer(self) -> None:
        self.responses.clear()

    def close(self) -> None:
        self.closed = True


def fake_runtime() -> FlashRuntime:
    return FlashRuntime(
        values={"PATH": "test", "PYTHONIOENCODING": "utf-8"},
        esptool_command=("app-python.exe", "-m", "esptool"),
        esptool_version="4.11.0",
        pyserial_version="3.5",
    )


def make_package(tmp_path: Path) -> Path:
    package = tmp_path / "firmware" / "releases" / "esp32_display"
    package.mkdir(parents=True, exist_ok=True)
    image_specs = [
        ("0x0", "bootloader.bin", b"bootloader"),
        ("0x8000", "partition-table.bin", b"partition-table"),
        ("0x10000", "espagent_display.bin", b"application"),
    ]
    images = []
    for address, name, content in image_specs:
        (package / name).write_bytes(content)
        images.append(
            {
                "address": address,
                "file": name,
                "size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        )
    manifest = {
        "schema_version": 1,
        "product": "ESPAgent Display",
        "firmware": "espagent_display",
        "version": "test-1",
        "created_at": "2026-07-19T00:00:00+08:00",
        "target": "ESP32-S3",
        "chip": "esp32s3",
        "flash": {
            "baud": 460800,
            "before": "default_reset",
            "after": "hard_reset",
            "arguments": ["--flash_mode", "dio", "--flash_size", "2MB"],
        },
        "images": images,
    }
    (package / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return package


def test_serial_ports_prefer_native_espressif_then_known_usb_bridges() -> None:
    ports = [
        SerialPort("COM3", "Bluetooth link"),
        SerialPort("COM10", "USB-SERIAL CH340", vid=0x1A86, pid=0x7523),
        SerialPort("COM7", "USB JTAG/serial debug unit", vid=0x303A, pid=0x1001),
    ]

    ranked = rank_serial_ports(ports)

    assert [port.device for port in ranked] == ["COM7", "COM10"]


def test_application_flash_runtime_does_not_require_idf_environment(monkeypatch) -> None:
    monkeypatch.delenv("IDF_PATH", raising=False)
    monkeypatch.delenv("IDF_PYTHON_ENV_PATH", raising=False)
    monkeypatch.delenv("ESPAGENT_IDF_ACTIVATE", raising=False)

    runtime = load_application_flash_runtime()

    assert runtime.esptool_command == (sys.executable, "-m", "esptool")
    assert runtime.esptool_version
    assert runtime.pyserial_version == "3.5"


def test_firmware_package_verifies_images_and_builds_complete_command(tmp_path: Path) -> None:
    package = load_firmware_package(make_package(tmp_path))

    command = build_flash_command(fake_runtime(), package, "COM10")

    assert package.version == "test-1"
    assert package.total_size == len(b"bootloaderpartition-tableapplication")
    assert command[:9] == [
        "app-python.exe",
        "-m",
        "esptool",
        "--chip",
        "esp32s3",
        "--port",
        "COM10",
        "--baud",
        "460800",
    ]
    assert "write_flash" in command
    assert command[-6::2] == ["0x0", "0x8000", "0x10000"]
    assert [Path(value).name for value in command[-5::2]] == [
        "bootloader.bin",
        "partition-table.bin",
        "espagent_display.bin",
    ]


def test_supermini_wled_release_has_complete_blank_chip_layout() -> None:
    package = load_firmware_package(
        REPO_ROOT / "firmware" / "releases" / "esp32s3_supermini_wled"
    )

    command = build_flash_command(fake_runtime(), package, "COM12")

    assert package.version == "16.0.0"
    assert package.target == "ESP32-S3 SuperMini (4MB QSPI)"
    assert package.chip == "esp32s3"
    assert [image.address for image in package.images] == [0x0, 0x8000, 0xE000, 0x10000]
    assert [image.path.name for image in package.images] == [
        "bootloader.bin",
        "partition-table.bin",
        "boot_app0.bin",
        "WLED_16.0.0_ESP32-S3_4M_qspi.bin",
    ]
    assert "--flash_size" in command
    assert command[command.index("--flash_size") + 1] == "4MB"
    assert command[command.index("--chip") + 1] == "esp32s3"


def test_flash_uses_application_runtime_and_prebuilt_images(tmp_path: Path) -> None:
    runtime = fake_runtime()
    commands: list[list[str]] = []
    detected_ports = [SerialPort("COM10", "USB-SERIAL CH340", vid=0x1A86, pid=0x7523)]

    def process_factory(command: list[str], **_kwargs: object) -> FakeProcess:
        commands.append(command)
        if "chip_id" in command:
            return FakeProcess("Chip is ESP32-S3\n", 0)
        return FakeProcess(
            "Serial port COM10\n"
            "Writing at 0x00010000... (52 %)\n"
            "Hash of data verified.\n"
            "Hard resetting via RTS pin...\n",
            0,
        )

    service = FirmwareFlashService(
        make_package(tmp_path),
        runtime_loader=lambda: runtime,
        port_scanner=lambda _runtime: list(detected_ports),
        process_factory=process_factory,
    )

    accepted = service.start_flash()
    assert accepted["running"] is True
    assert service.wait(timeout=2)

    snapshot = service.snapshot()
    assert snapshot["status"] == "success"
    assert snapshot["progress"] == 100
    assert snapshot["device"]["device"] == "COM10"
    assert snapshot["flasher_version"] == "4.11.0"
    assert snapshot["firmware_version"] == "test-1"
    assert snapshot["release_mode"] == "prebuilt"
    assert snapshot["message"] == "烧录完成，ESP32-S3 正在重启"
    assert commands[0][-1] == "chip_id"
    assert "write_flash" in commands[1]
    assert "idf.py" not in " ".join(commands[1]).lower()

    detected_ports.clear()
    service._refresh_ports(runtime, force=True)
    disconnected = service.snapshot(refresh_ports=False)
    assert disconnected["device"] is None
    assert disconnected["can_flash"] is False
    assert disconnected["message"] == "烧录完成，ESP32-S3 正在重启"


def test_flash_honors_preferred_serial_port(tmp_path: Path) -> None:
    runtime = fake_runtime()
    probed: list[str] = []

    def process_factory(command: list[str], **_kwargs: object) -> FakeProcess:
        port = command[command.index("--port") + 1]
        if "chip_id" in command:
            probed.append(port)
            return FakeProcess("Chip is ESP32-S3\n", 0)
        return FakeProcess("Hard resetting via RTS pin...\n", 0)

    service = FirmwareFlashService(
        make_package(tmp_path),
        runtime_loader=lambda: runtime,
        port_scanner=lambda _runtime: [
            SerialPort("COM7", "USB JTAG", vid=0x303A, pid=0x1001),
            SerialPort("COM12", "USB JTAG", vid=0x303A, pid=0x1001),
        ],
        process_factory=process_factory,
    )

    service.start_flash("com12")
    assert service.wait(timeout=2)
    assert probed == ["COM12"]
    assert service.snapshot()["device"]["device"] == "COM12"


def test_partition_write_percentages_do_not_make_flash_progress_jump(tmp_path: Path) -> None:
    service = FirmwareFlashService(
        make_package(tmp_path),
        runtime_loader=fake_runtime,
        port_scanner=lambda _runtime: [],
    )

    service._update_progress("Writing at 0x00008000... (100 %)")
    first = service.snapshot(refresh_ports=False)
    service._update_progress("Writing at 0x00010000... (3 %)")
    second = service.snapshot(refresh_ports=False)

    assert first["progress"] == 72
    assert second["progress"] == 72
    assert second["message"] == "正在写入内置固件"


def test_flash_tries_next_ranked_candidate(tmp_path: Path) -> None:
    runtime = fake_runtime()
    probed: list[str] = []

    def process_factory(command: list[str], **_kwargs: object) -> FakeProcess:
        if "chip_id" in command:
            port = command[command.index("--port") + 1]
            probed.append(port)
            return FakeProcess("probe\n", 0 if port == "COM10" else 2)
        return FakeProcess("Hard resetting via RTS pin...\n", 0)

    service = FirmwareFlashService(
        make_package(tmp_path),
        runtime_loader=lambda: runtime,
        port_scanner=lambda _runtime: [
            SerialPort("COM6", "USB JTAG", vid=0x303A, pid=0x1001),
            SerialPort("COM10", "USB-SERIAL CH340", vid=0x1A86, pid=0x7523),
        ],
        process_factory=process_factory,
    )

    service.start_flash()
    assert service.wait(timeout=2)

    snapshot = service.snapshot(refresh_ports=False)
    assert probed == ["COM6", "COM10"]
    assert snapshot["status"] == "success"
    assert snapshot["device"]["device"] == "COM10"


def test_usb_wifi_provisioning_auto_detects_device_and_hides_password(tmp_path: Path) -> None:
    serial_connection = FakeSerial()
    service = FirmwareFlashService(
        make_package(tmp_path),
        runtime_loader=fake_runtime,
        port_scanner=lambda _runtime: [
            SerialPort("COM10", "USB-SERIAL CH340", vid=0x1A86, pid=0x7523)
        ],
        serial_factory=lambda _device, _baud, _timeout: serial_connection,
    )

    accepted = service.start_wifi_provisioning("测试网络", "secret123")
    assert accepted["wifi"]["status"] in {"detecting", "applying", "connecting", "success"}
    assert service.wait_wifi(timeout=2)

    snapshot = service.snapshot(refresh_ports=False)
    assert snapshot["wifi"]["status"] == "success"
    assert snapshot["wifi"]["ssid"] == "测试网络"
    assert snapshot["wifi"]["ip"] == "192.168.1.8"
    assert snapshot["wifi"]["connected"] is True
    assert "secret123" not in json.dumps(snapshot, ensure_ascii=False)
    set_command = next(value for value in serial_connection.writes if value.startswith(b"ESPAGENT_WIFI_SET"))
    assert b"secret123" not in set_command
    assert "测试网络".encode("utf-8").hex().encode("ascii") in set_command
    assert serial_connection.closed is True


@pytest.mark.parametrize(
    ("ssid", "password", "message"),
    [
        ("", "password", "Wi-Fi 名称"),
        ("wifi", "short", "至少需要 8"),
        ("网" * 11, "password", "32 字节"),
    ],
)
def test_usb_wifi_provisioning_validates_credentials(
    tmp_path: Path,
    ssid: str,
    password: str,
    message: str,
) -> None:
    service = FirmwareFlashService(make_package(tmp_path), runtime_loader=fake_runtime)

    with pytest.raises(WifiProvisioningInvalid, match=message):
        service.start_wifi_provisioning(ssid, password)


def test_display_gpio_reads_then_writes_and_restarts(tmp_path: Path) -> None:
    serial_connection = FakeSerial()
    service = FirmwareFlashService(
        make_package(tmp_path),
        runtime_loader=fake_runtime,
        port_scanner=lambda _runtime: [
            SerialPort("COM10", "USB-SERIAL CH340", vid=0x1A86, pid=0x7523)
        ],
        serial_factory=lambda _device, _baud, _timeout: serial_connection,
    )
    values = {"scl": 18, "sda": 17, "cs": 16, "dc": 15, "rst": 14, "bl": 13}

    accepted = service.start_display_gpio_configuration(values)
    assert accepted["display_gpio"]["status"] in {"detecting", "applying", "success"}
    assert service.wait_display_gpio(timeout=2)

    snapshot = service.snapshot(refresh_ports=False)
    assert snapshot["display_gpio"]["status"] == "success"
    assert snapshot["display_gpio"]["values"] == values
    assert serial_connection.writes[0] == b"ESPAGENT_DISPLAY_GPIO_GET 1\n"
    assert b"ESPAGENT_DISPLAY_GPIO_SET 1 18 17 16 15 14 13\n" in serial_connection.writes
    assert serial_connection.closed is True


def test_display_gpio_read_keeps_device_values(tmp_path: Path) -> None:
    serial_connection = FakeSerial()
    service = FirmwareFlashService(
        make_package(tmp_path),
        runtime_loader=fake_runtime,
        port_scanner=lambda _runtime: [SerialPort("COM10", "USB-SERIAL CH340", vid=0x1A86)],
        serial_factory=lambda _device, _baud, _timeout: serial_connection,
    )

    service.start_display_gpio_read()
    assert service.wait_display_gpio(timeout=2)

    snapshot = service.snapshot(refresh_ports=False)
    assert snapshot["display_gpio"]["status"] == "success"
    assert snapshot["display_gpio"]["values"] == {
        "scl": 12, "sda": 11, "cs": 10, "dc": 8, "rst": 7, "bl": 6,
    }
    assert all(not value.startswith(b"ESPAGENT_DISPLAY_GPIO_SET") for value in serial_connection.writes)


@pytest.mark.parametrize(
    "values",
    [
        {"scl": 12, "sda": 12, "cs": 10, "dc": 8, "rst": 7, "bl": 6},
        {"scl": 26, "sda": 11, "cs": 10, "dc": 8, "rst": 7, "bl": 6},
        {"scl": 49, "sda": 11, "cs": 10, "dc": 8, "rst": 7, "bl": 6},
        {"scl": 46, "sda": 11, "cs": 10, "dc": 8, "rst": 7, "bl": 6},
    ],
)
def test_display_gpio_rejects_duplicate_reserved_and_out_of_range(values: dict[str, int]) -> None:
    with pytest.raises(DisplayGpioInvalid):
        validate_display_gpio(values)


def test_snapshot_disables_flash_when_runtime_package_or_device_is_missing(tmp_path: Path) -> None:
    missing_runtime = FirmwareFlashService(
        make_package(tmp_path / "runtime"),
        runtime_loader=lambda: (_ for _ in ()).throw(RuntimeError("not bundled")),
    )
    missing_package = FirmwareFlashService(
        tmp_path / "missing-package",
        runtime_loader=fake_runtime,
    )
    missing_device = FirmwareFlashService(
        make_package(tmp_path / "device"),
        runtime_loader=fake_runtime,
        port_scanner=lambda _runtime: [],
    )

    runtime_snapshot = missing_runtime.snapshot()
    package_snapshot = missing_package.snapshot()
    device_snapshot = missing_device.snapshot()

    assert runtime_snapshot["runtime_ready"] is False
    assert runtime_snapshot["can_flash"] is False
    assert "not bundled" in runtime_snapshot["runtime_error"]
    assert package_snapshot["package_ready"] is False
    assert "manifest.json" in package_snapshot["package_error"]
    assert device_snapshot["runtime_ready"] is True
    assert device_snapshot["device_count"] == 0
    assert device_snapshot["can_flash"] is False


def test_firmware_package_rejects_corrupted_image(tmp_path: Path) -> None:
    package_dir = make_package(tmp_path)
    (package_dir / "espagent_display.bin").write_bytes(b"corrupted")

    service = FirmwareFlashService(package_dir, runtime_loader=fake_runtime)
    snapshot = service.snapshot()

    assert snapshot["package_ready"] is False
    assert "大小不匹配" in snapshot["package_error"]


def test_repository_release_package_uses_generic_nvs_wifi_firmware() -> None:
    package = load_firmware_package(REPO_ROOT / "firmware" / "releases" / "esp32_display")
    manifest = json.loads((package.root / "manifest.json").read_text(encoding="utf-8"))
    wifi_source = (
        REPO_ROOT / "firmware" / "esp32_display" / "main" / "wifi_connect.c"
    ).read_text(encoding="utf-8")

    assert package.version == "2026.07.23.3"
    assert manifest["provisioning"] == {
        "wifi": "nvs",
        "usb": "uart0+usb_serial_jtag",
        "baud": 115200,
        "erase_nvs": False,
        "display_gpio": "nvs",
    }
    assert "esp_wifi_get_config" in wifi_source
    assert "wifi_set_credentials" in wifi_source
    provisioning_source = (
        REPO_ROOT / "firmware" / "esp32_display" / "main" / "usb_wifi_provisioning.c"
    ).read_text(encoding="utf-8")
    sdkconfig_defaults = (
        REPO_ROOT / "firmware" / "esp32_display" / "sdkconfig.defaults"
    ).read_text(encoding="utf-8")
    assert "usb_serial_jtag_driver_install" in provisioning_source
    assert "usb_serial_jtag_vfs_use_driver" in provisioning_source
    assert "usb_serial_jtag_read_bytes" in provisioning_source
    assert 'open("/dev/secondary"' not in provisioning_source
    assert "CONFIG_ESP_CONSOLE_SECONDARY_USB_SERIAL_JTAG=y" in sdkconfig_defaults
    assert "set it with idf.py menuconfig" not in wifi_source


def test_repository_esp01s_package_uses_esp8266_wled_image() -> None:
    package = load_firmware_package(REPO_ROOT / "firmware" / "releases" / "esp01s_wled")
    command = build_flash_command(fake_runtime(), package, "COM7")

    assert package.target == "ESP-01S"
    assert package.chip == "esp8266"
    assert package.version == "16.0.0"
    assert package.total_size == 887376
    assert command[command.index("--chip") + 1] == "esp8266"
    assert command[-2] == "0x0"
    assert Path(command[-1]).name == "WLED_16.0.0_ESP01.bin"
