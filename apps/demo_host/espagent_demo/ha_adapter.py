from __future__ import annotations

import ipaddress
import json
import threading
from collections.abc import Callable
import httpx
from time import time
from urllib.parse import urlsplit, urlunsplit

import websocket

from espagent_demo.state import LightState


class HomeAssistantLightAdapter:
    def __init__(self, base_url: str, token: str, entity_id: str, transport: httpx.BaseTransport | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.entity_id = entity_id
        self._bypass_proxy = _is_local_home_assistant_url(self.base_url)
        self.client = httpx.Client(
            transport=transport,
            timeout=5.0,
            trust_env=not self._bypass_proxy,
        )
        self._event_stream_lock = threading.Lock()
        self._event_stream: websocket.WebSocket | None = None
        self.event_stream_connected = False

    def health(self) -> dict[str, object]:
        state = self.get_state()
        return {
            "ok": state.online,
            "service": "home_assistant",
            "entity_id": self.entity_id,
            "reason": state.error,
            "event_stream_connected": self.event_stream_connected,
        }

    def get_state(self) -> LightState:
        if not self.token:
            return LightState(entity_id=self.entity_id, online=False, error="HOME_ASSISTANT_TOKEN is empty")
        try:
            response = self.client.get(
                f"{self.base_url}/api/states/{self.entity_id}",
                headers={"Authorization": f"Bearer {self.token}"},
            )
            response.raise_for_status()
            data = response.json()
            return self._map_state(data)
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status in {401, 403}:
                reason = f"Home Assistant authentication failed (HTTP {status})"
            elif status == 404:
                reason = f"Home Assistant entity not found: {self.entity_id}"
            else:
                reason = f"Home Assistant HTTP error: {status}"
            return LightState(
                entity_id=self.entity_id,
                online=False,
                last_synced_at=time(),
                error=reason,
            )
        except Exception as exc:
            return LightState(
                entity_id=self.entity_id,
                online=False,
                last_synced_at=time(),
                error=f"Home Assistant error: {exc.__class__.__name__}",
            )

    def set_light(
        self,
        on: bool,
        brightness: int | None = None,
        color_temp_kelvin: int | None = None,
    ) -> None:
        if not self.token:
            raise RuntimeError("HOME_ASSISTANT_TOKEN is empty")
        service = "turn_on" if on else "turn_off"
        payload: dict[str, object] = {"entity_id": self.entity_id}
        if brightness is not None and on:
            payload["brightness_pct"] = max(0, min(100, brightness))
        if color_temp_kelvin is not None and on:
            payload["color_temp_kelvin"] = max(1000, min(10000, color_temp_kelvin))
        response = self.client.post(
            f"{self.base_url}/api/services/light/{service}",
            headers={"Authorization": f"Bearer {self.token}"},
            json=payload,
        )
        response.raise_for_status()

    def subscribe_state_changes(
        self,
        stop_event: threading.Event,
        on_state: Callable[[LightState], object],
    ) -> None:
        """Block while subscribed to this entity's state_changed events."""
        if not self.token:
            raise RuntimeError("HOME_ASSISTANT_TOKEN is empty")

        connection_options: dict[str, object] = {
            "timeout": 10.0,
            "suppress_origin": True,
        }
        if self._bypass_proxy:
            connection_options["http_no_proxy"] = ["*"]
        connection = websocket.create_connection(self._websocket_url(), **connection_options)
        connection.settimeout(1.0)
        with self._event_stream_lock:
            self._event_stream = connection
        try:
            self._expect_type(connection, "auth_required")
            connection.send(json.dumps({"type": "auth", "access_token": self.token}))
            auth = self._receive_json(connection)
            if auth.get("type") != "auth_ok":
                raise RuntimeError(f"Home Assistant WebSocket authentication failed: {auth.get('type', 'unknown')}")

            subscription_id = 1
            connection.send(
                json.dumps(
                    {
                        "id": subscription_id,
                        "type": "subscribe_events",
                        "event_type": "state_changed",
                    }
                )
            )
            result = self._receive_json(connection)
            if result.get("type") != "result" or result.get("id") != subscription_id or not result.get("success"):
                raise RuntimeError("Home Assistant rejected the state_changed subscription")
            self.event_stream_connected = True

            while not stop_event.is_set():
                try:
                    message = self._receive_json(connection)
                except websocket.WebSocketTimeoutException:
                    continue
                if message.get("type") != "event" or message.get("id") != subscription_id:
                    continue
                event = message.get("event", {})
                data = event.get("data", {})
                if event.get("event_type") != "state_changed" or data.get("entity_id") != self.entity_id:
                    continue
                new_state = data.get("new_state")
                if new_state is None:
                    on_state(
                        LightState(
                            entity_id=self.entity_id,
                            online=False,
                            last_synced_at=time(),
                            error="Home Assistant entity was removed",
                        )
                    )
                else:
                    on_state(self._map_state(new_state))
        finally:
            self.event_stream_connected = False
            with self._event_stream_lock:
                if self._event_stream is connection:
                    self._event_stream = None
            connection.close()

    def close_event_stream(self) -> None:
        with self._event_stream_lock:
            connection = self._event_stream
        if connection is not None:
            connection.close()

    def _websocket_url(self) -> str:
        parsed = urlsplit(self.base_url)
        scheme = "wss" if parsed.scheme == "https" else "ws"
        path = f"{parsed.path.rstrip('/')}/api/websocket"
        return urlunsplit((scheme, parsed.netloc, path, "", ""))

    def _map_state(self, data: dict[str, object]) -> LightState:
        attributes = data.get("attributes", {})
        if not isinstance(attributes, dict):
            attributes = {}
        brightness_raw = int(attributes.get("brightness", 0) or 0)
        brightness = max(0, min(100, round(brightness_raw / 255 * 100)))
        color_temp_kelvin = self._positive_int(attributes.get("color_temp_kelvin"))
        if color_temp_kelvin is None:
            color_temp_kelvin = self._kelvin_from_mired(attributes.get("color_temp"))

        min_color_temp_kelvin = self._positive_int(attributes.get("min_color_temp_kelvin"))
        if min_color_temp_kelvin is None:
            min_color_temp_kelvin = self._kelvin_from_mired(attributes.get("max_mireds")) or 2000
        max_color_temp_kelvin = self._positive_int(attributes.get("max_color_temp_kelvin"))
        if max_color_temp_kelvin is None:
            max_color_temp_kelvin = self._kelvin_from_mired(attributes.get("min_mireds")) or 6500
        if min_color_temp_kelvin > max_color_temp_kelvin:
            min_color_temp_kelvin, max_color_temp_kelvin = max_color_temp_kelvin, min_color_temp_kelvin
        if color_temp_kelvin is not None:
            color_temp_kelvin = max(min_color_temp_kelvin, min(max_color_temp_kelvin, color_temp_kelvin))
        raw_state = data.get("state")
        online = raw_state not in {None, "unknown", "unavailable"}
        return LightState(
            entity_id=self.entity_id,
            name=str(attributes.get("friendly_name", self.entity_id)),
            online=online,
            on=raw_state == "on",
            brightness=brightness,
            color_temp_kelvin=color_temp_kelvin,
            min_color_temp_kelvin=min_color_temp_kelvin,
            max_color_temp_kelvin=max_color_temp_kelvin,
            last_synced_at=time(),
            error=None if online else f"Home Assistant entity is {raw_state or 'missing'}",
        )

    @staticmethod
    def _positive_int(value: object) -> int | None:
        try:
            converted = round(float(value))
        except (TypeError, ValueError):
            return None
        return converted if converted > 0 else None

    @classmethod
    def _kelvin_from_mired(cls, value: object) -> int | None:
        mired = cls._positive_int(value)
        return round(1_000_000 / mired) if mired is not None else None

    @staticmethod
    def _receive_json(connection: websocket.WebSocket) -> dict[str, object]:
        payload = connection.recv()
        if not isinstance(payload, str):
            raise RuntimeError("Home Assistant WebSocket returned a non-text message")
        message = json.loads(payload)
        if not isinstance(message, dict):
            raise RuntimeError("Home Assistant WebSocket returned an invalid message")
        return message

    def _expect_type(self, connection: websocket.WebSocket, expected: str) -> None:
        message = self._receive_json(connection)
        if message.get("type") != expected:
            raise RuntimeError(f"Expected Home Assistant WebSocket message {expected}")

    def close(self) -> None:
        self.close_event_stream()
        self.client.close()

    def __enter__(self) -> HomeAssistantLightAdapter:
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self.close()


def _is_local_home_assistant_url(base_url: str) -> bool:
    hostname = (urlsplit(base_url).hostname or "").strip().lower()
    if hostname == "localhost" or hostname.endswith(".local"):
        return True
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return False
    return address.is_private or address.is_loopback or address.is_link_local
