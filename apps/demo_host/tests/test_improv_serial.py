from __future__ import annotations

from espagent_demo.improv_serial import (
    HEADER,
    RPC_CURRENT_STATE,
    RPC_WIFI_SETTINGS,
    TYPE_RPC_COMMAND,
    TYPE_RPC_RESULT,
    TYPE_STATE,
    ImprovParser,
    build_rpc_packet,
    parse_rpc_result,
    provision_wifi,
)


def _device_packet(packet_type: int, data: bytes) -> bytes:
    packet = HEADER + bytes([1, packet_type, len(data)]) + data
    return packet + bytes([sum(packet) & 0xFF])


def test_build_wifi_rpc_packet_uses_improv_checksums() -> None:
    body = bytes([4]) + b"wifi" + bytes([4]) + b"pass"
    packet = build_rpc_packet(0x01, body)

    assert packet[:6] == b"IMPROV"
    assert packet[7] == 0x03
    assert packet[9:11] == bytes([0x01, len(body)])
    assert packet[-1] == sum(packet[:-1]) & 0xFF


def test_parser_handles_fragmented_rpc_result() -> None:
    url = b"http://192.168.1.9"
    strings = bytes([len(url)]) + url
    packet = _device_packet(TYPE_RPC_RESULT, bytes([0x01, len(strings)]) + strings)
    parser = ImprovParser()

    assert parser.feed(packet[:8]) == []
    parsed = parser.feed(packet[8:])
    command, values = parse_rpc_result(parsed[0].data)

    assert command == 0x01
    assert values == ["http://192.168.1.9"]


class ScriptedSerial:
    def __init__(self) -> None:
        self.dtr = True
        self.rts = True
        self.writes: list[bytes] = []
        self.responses: list[bytes] = []
        self.current_state_queries = 0

    def __enter__(self) -> "ScriptedSerial":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def reset_input_buffer(self) -> None:
        self.responses.clear()

    def write(self, data: bytes) -> int:
        self.writes.append(data)
        command = data[9] if len(data) > 10 and data[7] == TYPE_RPC_COMMAND else None
        if command == RPC_CURRENT_STATE:
            self.current_state_queries += 1
            if self.current_state_queries == 2:
                self.responses.append(_device_packet(TYPE_STATE, b"\x03"))
        elif command == RPC_WIFI_SETTINGS:
            url = b"http://192.168.1.23"
            values = bytes([len(url)]) + url
            result = bytes([RPC_WIFI_SETTINGS, len(values)]) + values
            self.responses.extend(
                [
                    _device_packet(TYPE_STATE, b"\x03"),
                    _device_packet(TYPE_RPC_RESULT, result),
                ]
            )
        return len(data)

    def flush(self) -> None:
        return None

    def read(self, _size: int) -> bytes:
        return self.responses.pop(0) if self.responses else b""


def test_provision_wifi_accepts_provisioning_state_and_retries_handshake() -> None:
    connection = ScriptedSerial()

    endpoint = provision_wifi(
        "COM7",
        "Home",
        "secret123",
        timeout=2,
        serial_factory=lambda **_kwargs: connection,
    )

    assert endpoint == "http://192.168.1.23"
    assert connection.current_state_queries == 2
    assert connection.dtr is False
    assert connection.rts is False
    assert any(packet[9] == RPC_WIFI_SETTINGS for packet in connection.writes)
