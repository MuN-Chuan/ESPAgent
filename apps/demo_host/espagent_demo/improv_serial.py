from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

import serial
from serial.tools import list_ports


HEADER = b"IMPROV"
VERSION = 1
TYPE_STATE = 0x01
TYPE_ERROR = 0x02
TYPE_RPC_COMMAND = 0x03
TYPE_RPC_RESULT = 0x04
RPC_WIFI_SETTINGS = 0x01
RPC_CURRENT_STATE = 0x02


@dataclass(frozen=True)
class ImprovPacket:
    packet_type: int
    data: bytes


def list_serial_ports() -> list[dict[str, object]]:
    return [
        {
            "device": item.device,
            "description": item.description,
            "vid": item.vid,
            "pid": item.pid,
        }
        for item in sorted(list_ports.comports(), key=lambda port: port.device)
    ]


def provision_wifi(
    port: str,
    ssid: str,
    password: str,
    *,
    timeout: float = 35.0,
    serial_factory: Callable[..., serial.Serial] = serial.Serial,
) -> str | None:
    ssid_bytes = ssid.encode("utf-8")
    password_bytes = password.encode("utf-8")
    if not ssid_bytes or len(ssid_bytes) > 32:
        raise ValueError("SSID 长度必须为 1 至 32 字节")
    if len(password_bytes) > 64:
        raise ValueError("Wi-Fi 密码不能超过 64 字节")
    if not port.strip():
        raise ValueError("请选择 ESP-01S 串口")

    with _open_serial_connection(port, serial_factory) as connection:
        connection.reset_input_buffer()
        ready_deadline = time.monotonic() + min(4.0, timeout)
        parser = ImprovParser()
        ready = False
        next_query_at = 0.0
        while time.monotonic() < ready_deadline:
            now = time.monotonic()
            if now >= next_query_at:
                connection.write(build_rpc_packet(RPC_CURRENT_STATE))
                connection.flush()
                next_query_at = now + 0.5
            for packet in parser.feed(connection.read(256)):
                if packet.packet_type == TYPE_ERROR and packet.data and packet.data[0] != 0:
                    raise RuntimeError(_error_message(packet.data[0]))
                if packet.packet_type == TYPE_STATE and packet.data and packet.data[0] in {0x02, 0x03, 0x04}:
                    ready = True
                    break
            if ready:
                break
        if not ready:
            raise RuntimeError(
                "串口设备未响应 Improv，请确认 ESP-01S 已安装 WLED、串口波特率为 115200 且未被占用"
            )

        body = bytes([len(ssid_bytes)]) + ssid_bytes + bytes([len(password_bytes)]) + password_bytes
        connection.write(build_rpc_packet(RPC_WIFI_SETTINGS, body))
        connection.flush()
        deadline = time.monotonic() + timeout
        provisioned = False
        result_url: str | None = None
        while time.monotonic() < deadline:
            for packet in parser.feed(connection.read(256)):
                if packet.packet_type == TYPE_ERROR and packet.data and packet.data[0] != 0:
                    raise RuntimeError(_error_message(packet.data[0]))
                if packet.packet_type == TYPE_STATE and packet.data:
                    if packet.data[0] == 0x04:
                        provisioned = True
                if packet.packet_type == TYPE_RPC_RESULT:
                    command, values = parse_rpc_result(packet.data)
                    if command == RPC_WIFI_SETTINGS and values:
                        result_url = values[0]
                        provisioned = True
            if provisioned:
                return result_url
        raise TimeoutError("ESP-01S Wi-Fi 配网超时")


def _open_serial_connection(
    port: str,
    serial_factory: Callable[..., serial.Serial],
) -> serial.Serial:
    if serial_factory is serial.Serial:
        connection = serial_factory()
        connection.port = port
        connection.baudrate = 115200
        connection.timeout = 0.2
        connection.write_timeout = 2
        # Set line states before opening so CH340/CP210x adapters do not pulse
        # an ESP8266 into reset or bootloader mode.
        connection.dtr = False
        connection.rts = False
        connection.open()
        return connection
    connection = serial_factory(port=port, baudrate=115200, timeout=0.2, write_timeout=2)
    connection.dtr = False
    connection.rts = False
    return connection


def build_rpc_packet(command: int, data: bytes = b"") -> bytes:
    if len(data) > 253:
        raise ValueError("Improv RPC data is too large")
    rpc = bytes([command, len(data)]) + data
    packet = HEADER + bytes([VERSION, TYPE_RPC_COMMAND, len(rpc)]) + rpc
    return packet + bytes([sum(packet) & 0xFF])


def parse_rpc_result(data: bytes) -> tuple[int, list[str]]:
    if len(data) < 2:
        raise ValueError("invalid Improv RPC result")
    command = data[0]
    payload_length = data[1]
    payload = data[2 : 2 + payload_length]
    if len(payload) != payload_length:
        raise ValueError("truncated Improv RPC result")
    values: list[str] = []
    offset = 0
    while offset < len(payload):
        length = payload[offset]
        offset += 1
        value = payload[offset : offset + length]
        if len(value) != length:
            raise ValueError("truncated Improv RPC string")
        values.append(value.decode("utf-8"))
        offset += length
    return command, values


class ImprovParser:
    def __init__(self) -> None:
        self._buffer = bytearray()

    def feed(self, chunk: bytes) -> list[ImprovPacket]:
        self._buffer.extend(chunk)
        packets: list[ImprovPacket] = []
        while True:
            start = self._buffer.find(HEADER)
            if start < 0:
                if len(self._buffer) > len(HEADER):
                    del self._buffer[: -len(HEADER)]
                break
            if start:
                del self._buffer[:start]
            if len(self._buffer) < 10:
                break
            length = self._buffer[8]
            packet_length = 10 + length
            if len(self._buffer) < packet_length:
                break
            raw = bytes(self._buffer[:packet_length])
            del self._buffer[:packet_length]
            if raw[6] != VERSION or (sum(raw[:-1]) & 0xFF) != raw[-1]:
                continue
            packets.append(ImprovPacket(packet_type=raw[7], data=raw[9:-1]))
        return packets


def _error_message(code: int) -> str:
    return {
        0x01: "ESP-01S 拒绝了无效配网数据",
        0x02: "ESP-01S 不支持该配网命令",
        0x03: "ESP-01S 无法连接指定 Wi-Fi，请检查密码和信号",
        0x05: "设备主机名无效",
    }.get(code, f"ESP-01S 配网失败（错误 {code}）")
