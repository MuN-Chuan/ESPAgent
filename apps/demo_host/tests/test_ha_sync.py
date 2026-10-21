from espagent_demo.ha_sync import HomeAssistantStateWorker
from espagent_demo.state import DemoState, LightState


class FakeLightReader:
    def __init__(self, states: list[LightState]) -> None:
        self.states = states
        self.calls = 0

    def get_state(self) -> LightState:
        state = self.states[min(self.calls, len(self.states) - 1)]
        self.calls += 1
        return state


def test_sync_replaces_complete_light_state_and_notifies_on_visible_change() -> None:
    state = DemoState()
    changes: list[bool] = []
    worker = HomeAssistantStateWorker(
        FakeLightReader([LightState(entity_id="light.real", name="Desk", online=True, on=True, brightness=40)]),
        state,
        on_change=lambda: changes.append(True),
    )

    assert worker.sync_once() is True
    assert state.light.entity_id == "light.real"
    assert state.light.name == "Desk"
    assert state.light.on is True
    assert state.light.brightness == 40
    assert changes == [True]


def test_timestamp_only_change_does_not_request_display_refresh() -> None:
    first = LightState(
        entity_id="light.real",
        name="Desk",
        online=True,
        on=True,
        brightness=40,
        last_synced_at=1.0,
    )
    second = LightState(
        entity_id="light.real",
        name="Desk",
        online=True,
        on=True,
        brightness=40,
        last_synced_at=2.0,
    )
    state = DemoState(light=first)
    changes: list[bool] = []
    worker = HomeAssistantStateWorker(
        FakeLightReader([second]),
        state,
        on_change=lambda: changes.append(True),
    )

    assert worker.sync_once() is False
    assert state.light.last_synced_at == 2.0
    assert changes == []


def test_event_replaces_state_and_requests_display_refresh() -> None:
    state = DemoState(light=LightState(entity_id="light.real", online=True, on=False, brightness=0))
    changes: list[bool] = []
    worker = HomeAssistantStateWorker(
        FakeLightReader([state.light]),
        state,
        on_change=lambda: changes.append(True),
    )

    changed = worker._apply_state(
        LightState(entity_id="light.real", name="Desk", online=True, on=True, brightness=70)
    )

    assert changed is True
    assert state.light.on is True
    assert state.light.brightness == 70
    assert changes == [True]


def test_color_temperature_change_requests_display_refresh() -> None:
    state = DemoState(
        light=LightState(
            entity_id="light.real",
            online=True,
            on=True,
            brightness=70,
            color_temp_kelvin=3000,
        )
    )
    changes: list[bool] = []
    worker = HomeAssistantStateWorker(
        FakeLightReader([state.light]),
        state,
        on_change=lambda: changes.append(True),
    )

    changed = worker._apply_state(
        LightState(
            entity_id="light.real",
            online=True,
            on=True,
            brightness=70,
            color_temp_kelvin=5000,
        )
    )

    assert changed is True
    assert state.light.color_temp_kelvin == 5000
    assert changes == [True]
