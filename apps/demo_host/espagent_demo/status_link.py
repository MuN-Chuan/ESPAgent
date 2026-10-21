from __future__ import annotations

import ipaddress
import logging
import socket
import threading
import time
from collections.abc import Callable
from typing import Protocol

from espagent_demo.protocol import DISCOVERY_MESSAGE
from espagent_demo.state import DemoState

logger = logging.getLogger(__name__)


class DatagramSocket(Protocol):
    def bind(self, address: tuple[str, int]) -> object:
        ...

    def settimeout(self, timeout: float) -> object:
        ...

    def setsockopt(self, level: int, option: int, value: int) -> object:
        ...

    def sendto(self, payload: bytes, address: tuple[str, int]) -> object:
        ...

    def recvfrom(self, bufsize: int) -> tuple[bytes, tuple[str, int]]:
        ...

    def close(self) -> object:
        ...


class StatusReceiver:
    def __init__(
        self,
        port: int,
        state: DemoState,
        sock: DatagramSocket | None = None,
        nack_handler: Callable[[int, list[int]], object] | None = None,
        displayed_handler: Callable[[int], object] | None = None,
        transport_handler: Callable[[int, int], object] | None = None,
        target_handler: Callable[[str], object] | None = None,
        offline_timeout_seconds: float = 2.0,
        discovery_port: int = 39000,
        discovery_interval_seconds: float = 1.0,
        discovery_targets_provider: Callable[[], list[str]] | None = None,
    ) -> None:
        self.port = port
        self.state = state
        self.sock = sock
        self.nack_handler = nack_handler
        self.displayed_handler = displayed_handler
        self.transport_handler = transport_handler
        self.target_handler = target_handler
        self.offline_timeout_seconds = offline_timeout_seconds
        self.discovery_port = discovery_port
        self.discovery_interval_seconds = discovery_interval_seconds
        self.discovery_targets_provider = discovery_targets_provider or _local_directed_broadcasts
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._last_discovery_at = 0.0
        self._last_displayed_frame_id: int | None = None
        self._display_window_started: float | None = None
        self._display_window_frames = 0

    def start(self) -> None:
        if self._thread is not None:
            return
        if self.sock is None:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        self.sock.bind(("0.0.0.0", self.port))
        self.sock.settimeout(0.2)
        self._thread = threading.Thread(target=self._run, name="esp32-status", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        if self.sock is not None:
            self.sock.close()
        if self._thread is not None:
            self._thread.join(timeout=1.0)

    def _run(self) -> None:
        assert self.sock is not None
        self._send_discovery(force=True)
        while not self._stop.is_set():
            try:
                data, address = self.sock.recvfrom(768)
            except socket.timeout:
                self.state.expire_status(self.offline_timeout_seconds)
                self._send_discovery()
                continue
            except OSError:
                if not self._stop.is_set():
                    logger.exception("ESP32 status socket failed")
                return
            self._handle_status(data, address[0])

    def _send_discovery(self, *, force: bool = False) -> None:
        if self.sock is None:
            return
        now = time.monotonic()
        if not force and now - self._last_discovery_at < self.discovery_interval_seconds:
            return
        sent = False
        targets = ["255.255.255.255", *self.discovery_targets_provider()]
        for target in dict.fromkeys(targets):
            try:
                self.sock.sendto(DISCOVERY_MESSAGE, (target, self.discovery_port))
                sent = True
            except OSError:
                if not self._stop.is_set():
                    logger.debug("ESP32 discovery broadcast to %s failed", target, exc_info=True)
        if sent:
            self._last_discovery_at = now

    def _handle_status(self, data: bytes, ip: str) -> None:
        text = data.decode("ascii", errors="ignore").strip()
        if not text.startswith("ESPAGENT_STATUS "):
            return
        values = _parse_status_values(text)
        self.state.mark_status_seen(ip)
        if self.target_handler is not None:
            self.target_handler(ip)
        self.state.update_link_counters(
            rx_packets=values.get("rx", self.state.link.rx_packets),
            accepted_packets=values.get("accepted", self.state.link.accepted_packets),
            rejected_packets=values.get("rejected", self.state.link.rejected_packets),
            draw_failed_packets=values.get("draw_failed", self.state.link.draw_failed_packets),
            frame_end_packets=values.get("frame_end"),
            flushed_frames=values.get("flushed"),
            stale_frame_end_packets=values.get("stale_end"),
        )
        displayed_frame_id = values.get("displayed")
        transport_names = {0: "udp", 1: "mjpeg", 2: "indexed"}
        if "mode" in values:
            self.state.link.esp_transport_mode = transport_names.get(values["mode"], "unknown")
        if "mode_epoch" in values:
            self.state.link.esp_transport_epoch = values["mode_epoch"]
        if self.transport_handler is not None and "mode" in values and "mode_epoch" in values:
            self.transport_handler(values["mode"], values["mode_epoch"])
        esp_fps_milli = values.get("lcd_fps_milli")
        if esp_fps_milli is not None:
            self.state.link.display_fps = esp_fps_milli / 1000.0
            self.state.link.perf_window_ms = values.get("perf_ms", 0)
            self.state.link.perf_frames = values.get("perf_frames", 0)
            self.state.link.decode_avg_us = values.get("decode_avg_us", 0)
            self.state.link.decode_max_us = values.get("decode_max_us", 0)
            self.state.link.lcd_avg_us = values.get("lcd_avg_us", 0)
            self.state.link.lcd_max_us = values.get("lcd_max_us", 0)
            self.state.link.frame_avg_us = values.get("frame_avg_us", 0)
            self.state.link.frame_max_us = values.get("frame_max_us", 0)
            self.state.link.jpeg_queued_frames = values.get("jpeg_queued", 0)
            self.state.link.jpeg_incomplete_frames = values.get("jpeg_incomplete", 0)
            self.state.link.jpeg_dropped_frames = values.get("jpeg_dropped", 0)
            self.state.link.jpeg_worker_frame_id = values.get("worker_frame", 0)
            self.state.link.jpeg_worker_stage = values.get("worker_stage", 0)
            self.state.link.indexed_queued_frames = values.get("indexed_queued", 0)
            self.state.link.indexed_incomplete_frames = values.get("indexed_incomplete", 0)
            self.state.link.indexed_dropped_frames = values.get("indexed_dropped", 0)
            self.state.link.indexed_no_buffer_frames = values.get("indexed_no_buffer", 0)
            self.state.link.indexed_queue_depth = values.get("indexed_queue", 0)
            self.state.link.indexed_worker_frame_id = values.get("indexed_worker_frame", 0)
            self.state.link.indexed_worker_stage = values.get("indexed_worker_stage", 0)
        if displayed_frame_id is not None and displayed_frame_id != self._last_displayed_frame_id:
            now = time.perf_counter()
            if esp_fps_milli is None:
                if self._display_window_started is None:
                    self._display_window_started = now
                    self._display_window_frames = 1
                else:
                    self._display_window_frames += 1
                    elapsed = now - self._display_window_started
                    if elapsed >= 2.0:
                        measured_fps = (self._display_window_frames - 1) / elapsed
                        previous_fps = self.state.link.display_fps
                        self.state.link.display_fps = measured_fps if previous_fps <= 0 else previous_fps * 0.65 + measured_fps * 0.35
                        self._display_window_started = now
                        self._display_window_frames = 1
            self._last_displayed_frame_id = displayed_frame_id
            if self.displayed_handler is not None:
                self.displayed_handler(displayed_frame_id)
            self.state.link.displayed_frame_id = displayed_frame_id
        if self.nack_handler is not None and "nack_frame" in values:
            raw_values = _parse_raw_status_values(text)
            missing_ids = _parse_tile_ids(raw_values.get("nack_tiles", ""))
            if not missing_ids:
                missing_ids = _parse_tile_ids(raw_values.get("nack_chunks", ""))
            if missing_ids:
                self.nack_handler(values["nack_frame"], missing_ids)


def _parse_status_values(text: str) -> dict[str, int]:
    values: dict[str, int] = {}
    for item in text.split()[1:]:
        if "=" not in item:
            continue
        key, raw_value = item.split("=", 1)
        try:
            values[key] = int(raw_value)
        except ValueError:
            continue
    return values


def _parse_raw_status_values(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for item in text.split()[1:]:
        if "=" not in item:
            continue
        key, raw_value = item.split("=", 1)
        values[key] = raw_value
    return values


def _parse_tile_ids(raw_value: str) -> list[int]:
    tile_ids: list[int] = []
    for item in raw_value.split(","):
        if not item:
            continue
        try:
            tile_ids.append(int(item))
        except ValueError:
            continue
    return tile_ids


def _local_directed_broadcasts() -> list[str]:
    """Return /24 LAN broadcasts so VPN/TUN default routes cannot swallow discovery."""
    try:
        addresses = socket.gethostbyname_ex(socket.gethostname())[2]
    except OSError:
        return []

    broadcasts: list[str] = []
    for raw_address in addresses:
        try:
            address = ipaddress.ip_address(raw_address)
        except ValueError:
            continue
        if (
            not isinstance(address, ipaddress.IPv4Address)
            or address.is_loopback
            or address.is_link_local
            or address.is_multicast
            or address.is_unspecified
        ):
            continue
        broadcast = str(ipaddress.ip_network(f"{address}/24", strict=False).broadcast_address)
        if broadcast not in broadcasts:
            broadcasts.append(broadcast)
    return broadcasts
