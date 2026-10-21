import json
import threading

import httpx
import pytest

from espagent_demo.ha_adapter import HomeAssistantLightAdapter, _is_local_home_assistant_url


class FakeWebSocket:
    def __init__(self, messages: list[dict[str, object]]) -> None:
        self.messages = [json.dumps(message) for message in messages]
        self.sent: list[dict[str, object]] = []
        self.closed = False

    def settimeout(self, timeout: float) -> None:
        self.timeout = timeout

    def recv(self) -> str:
        return self.messages.pop(0)

    def send(self, payload: str) -> None:
        self.sent.append(json.loads(payload))

    def close(self) -> None:
        self.closed = True


def test_state_maps_brightness_to_percent():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"entity_id": "light.living_room", "state": "on", "attributes": {"friendly_name": "客厅主灯", "brightness": 102}})

    adapter = HomeAssistantLightAdapter("http://ha.local", "token", "light.living_room", transport=httpx.MockTransport(handler))
    state = adapter.get_state()
    assert state.name == "客厅主灯"
    assert state.on is True
    assert state.brightness == 40


def test_state_maps_color_temperature_kelvin_and_supported_range():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "entity_id": "light.living_room",
                "state": "on",
                "attributes": {
                    "color_temp_kelvin": 4000,
                    "min_color_temp_kelvin": 2200,
                    "max_color_temp_kelvin": 6000,
                },
            },
        )

    adapter = HomeAssistantLightAdapter("http://ha.local", "token", "light.living_room", transport=httpx.MockTransport(handler))
    state = adapter.get_state()

    assert state.color_temp_kelvin == 4000
    assert state.min_color_temp_kelvin == 2200
    assert state.max_color_temp_kelvin == 6000


def test_state_maps_legacy_mired_color_temperature():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "entity_id": "light.living_room",
                "state": "on",
                "attributes": {"color_temp": 250, "min_mireds": 153, "max_mireds": 500},
            },
        )

    adapter = HomeAssistantLightAdapter("http://ha.local", "token", "light.living_room", transport=httpx.MockTransport(handler))
    state = adapter.get_state()

    assert state.color_temp_kelvin == 4000
    assert state.min_color_temp_kelvin == 2000
    assert state.max_color_temp_kelvin == 6536


def test_empty_token_returns_offline_state():
    adapter = HomeAssistantLightAdapter("http://ha.local", "", "light.living_room")
    state = adapter.get_state()
    assert state.online is False
    assert state.error == "HOME_ASSISTANT_TOKEN is empty"


@pytest.mark.parametrize(
    ("status_code", "expected"),
    [
        (401, "authentication failed"),
        (403, "authentication failed"),
        (404, "entity not found: light.living_room"),
    ],
)
def test_state_reports_actionable_http_error(status_code, expected):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, request=request)

    adapter = HomeAssistantLightAdapter(
        "http://ha.local",
        "token",
        "light.living_room",
        transport=httpx.MockTransport(handler),
    )

    state = adapter.get_state()

    assert state.online is False
    assert expected in str(state.error)


def test_state_clamps_brightness_to_percent_bounds():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"entity_id": "light.living_room", "state": "on", "attributes": {"brightness": 999}})

    adapter = HomeAssistantLightAdapter("http://ha.local", "token", "light.living_room", transport=httpx.MockTransport(handler))
    state = adapter.get_state()
    assert state.brightness == 100


def test_set_light_turns_on_with_auth_and_clamped_brightness():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={})

    adapter = HomeAssistantLightAdapter("http://ha.local", "token", "light.living_room", transport=httpx.MockTransport(handler))
    adapter.set_light(True, brightness=150)

    request = requests[0]
    assert request.method == "POST"
    assert request.url.path == "/api/services/light/turn_on"
    assert request.headers["authorization"] == "Bearer token"
    assert json.loads(request.content) == {"entity_id": "light.living_room", "brightness_pct": 100}


def test_set_light_turns_on_with_color_temperature_kelvin():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={})

    adapter = HomeAssistantLightAdapter("http://ha.local", "token", "light.living_room", transport=httpx.MockTransport(handler))
    adapter.set_light(True, brightness=40, color_temp_kelvin=4200)

    assert json.loads(requests[0].content) == {
        "entity_id": "light.living_room",
        "brightness_pct": 40,
        "color_temp_kelvin": 4200,
    }


def test_set_light_turns_off_without_brightness():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={})

    adapter = HomeAssistantLightAdapter("http://ha.local", "token", "light.living_room", transport=httpx.MockTransport(handler))
    adapter.set_light(False, brightness=80)

    request = requests[0]
    assert request.method == "POST"
    assert request.url.path == "/api/services/light/turn_off"
    assert json.loads(request.content) == {"entity_id": "light.living_room"}


def test_set_light_empty_token_raises():
    adapter = HomeAssistantLightAdapter("http://ha.local", "", "light.living_room")

    with pytest.raises(RuntimeError, match="HOME_ASSISTANT_TOKEN is empty"):
        adapter.set_light(True)


def test_websocket_authenticates_subscribes_and_maps_matching_event(monkeypatch):
    socket = FakeWebSocket(
        [
            {"type": "auth_required", "ha_version": "2026.7.1"},
            {"type": "auth_ok", "ha_version": "2026.7.1"},
            {"id": 1, "type": "result", "success": True, "result": None},
            {
                "id": 1,
                "type": "event",
                "event": {
                    "event_type": "state_changed",
                    "data": {
                        "entity_id": "light.living_room",
                        "new_state": {
                            "entity_id": "light.living_room",
                            "state": "on",
                            "attributes": {"friendly_name": "Living Room", "brightness": 128},
                        },
                    },
                },
            },
        ]
    )
    created: list[tuple[str, dict[str, object]]] = []

    def create_connection(url: str, **kwargs: object) -> FakeWebSocket:
        created.append((url, kwargs))
        return socket

    monkeypatch.setattr("espagent_demo.ha_adapter.websocket.create_connection", create_connection)
    adapter = HomeAssistantLightAdapter("https://ha.local/base/", "token", "light.living_room")
    stop = threading.Event()
    states = []

    def receive(state):
        states.append(state)
        stop.set()

    adapter.subscribe_state_changes(stop, receive)

    assert created[0][0] == "wss://ha.local/base/api/websocket"
    assert created[0][1]["http_no_proxy"] == ["*"]
    assert socket.sent == [
        {"type": "auth", "access_token": "token"},
        {"id": 1, "type": "subscribe_events", "event_type": "state_changed"},
    ]
    assert states[0].on is True
    assert states[0].brightness == 50
    assert socket.closed is True
    assert adapter.event_stream_connected is False


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://127.0.0.1:8123", True),
        ("http://192.168.1.20:8123", True),
        ("http://homeassistant.local:8123", True),
        ("https://ha.example.com", False),
        ("http://3.25.151.221:8123", False),
    ],
)
def test_home_assistant_proxy_bypass_only_applies_to_local_targets(url, expected):
    assert _is_local_home_assistant_url(url) is expected


def test_public_websocket_keeps_system_proxy_available(monkeypatch):
    socket = FakeWebSocket(
        [
            {"type": "auth_required"},
            {"type": "auth_ok"},
            {"id": 1, "type": "result", "success": True},
        ]
    )
    created: list[dict[str, object]] = []

    def create_connection(url: str, **kwargs: object) -> FakeWebSocket:
        created.append(kwargs)
        return socket

    monkeypatch.setattr("espagent_demo.ha_adapter.websocket.create_connection", create_connection)
    adapter = HomeAssistantLightAdapter("https://ha.example.com", "token", "light.living_room")
    stop = threading.Event()
    stop.set()

    adapter.subscribe_state_changes(stop, lambda state: None)

    assert "http_no_proxy" not in created[0]


def test_websocket_ignores_events_for_other_entities(monkeypatch):
    socket = FakeWebSocket(
        [
            {"type": "auth_required"},
            {"type": "auth_ok"},
            {"id": 1, "type": "result", "success": True},
            {
                "id": 1,
                "type": "event",
                "event": {
                    "event_type": "state_changed",
                    "data": {"entity_id": "light.other", "new_state": {"state": "on", "attributes": {}}},
                },
            },
        ]
    )
    stop = threading.Event()
    original_recv = socket.recv

    def recv_then_stop() -> str:
        payload = original_recv()
        if not socket.messages:
            stop.set()
        return payload

    socket.recv = recv_then_stop
    monkeypatch.setattr("espagent_demo.ha_adapter.websocket.create_connection", lambda *args, **kwargs: socket)
    adapter = HomeAssistantLightAdapter("http://ha.local", "token", "light.living_room")
    states = []

    adapter.subscribe_state_changes(stop, states.append)

    assert states == []


def test_close_closes_client():
    adapter = HomeAssistantLightAdapter("http://ha.local", "token", "light.living_room")

    adapter.close()

    assert adapter.client.is_closed


def test_context_manager_closes_client_on_exit():
    with HomeAssistantLightAdapter("http://ha.local", "token", "light.living_room") as adapter:
        assert adapter.client.is_closed is False

    assert adapter.client.is_closed
