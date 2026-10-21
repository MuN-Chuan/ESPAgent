import pytest

from espagent_demo.state import DemoState, LightState, indexed_palette_capacity, resize_indexed_color_slots


def test_display_tone_defaults_to_disabled_neutral_values() -> None:
    tone = DemoState().display_tone

    assert tone.enabled is False
    assert tone.preset == "off"
    assert tone.contrast_percent == 100
    assert tone.brightness_percent == 100
    assert tone.color_percent == 100
    assert tone.sharpness_percent == 100
    assert tone.binary_threshold == 0


def test_panel_gamma_defaults_to_lighter_curve() -> None:
    assert DemoState().link.panel_gamma == "1.8"


@pytest.mark.parametrize("bpp, capacity", [(1, 2), (2, 4), (4, 16), (8, 256)])
def test_indexed_palette_capacity_matches_pixel_format(bpp: int, capacity: int) -> None:
    assert indexed_palette_capacity(bpp) == capacity


def test_indexed_color_slots_resize_without_reordering_existing_slots() -> None:
    assert resize_indexed_color_slots(["#111111", None, "#333333", "#444444"], 1) == ["#111111", None]
    expanded = resize_indexed_color_slots(["#111111", None], 4)
    assert len(expanded) == 16
    assert expanded[:2] == ["#111111", None]
    assert all(color is None for color in expanded[2:])


def test_snapshot_is_independent_from_live_state() -> None:
    state = DemoState()
    with state.locked():
        state.active_scene = "lights"
        state.light = LightState(entity_id="light.test", online=True, on=True, brightness=40)

    snapshot = state.snapshot()
    snapshot.active_scene = "hermes"
    snapshot.light.brightness = 10

    assert state.active_scene == "lights"
    assert state.light.brightness == 40


def test_nested_state_updates_can_reenter_state_lock() -> None:
    state = DemoState()

    with state.locked():
        state.mark_status_seen("192.168.1.8")
        state.update_link_counters(
            rx_packets=10,
            accepted_packets=9,
            rejected_packets=1,
            draw_failed_packets=0,
        )

    snapshot = state.snapshot()
    assert snapshot.link.esp32_online is True
    assert snapshot.link.esp32_ip == "192.168.1.8"
    assert snapshot.link.rx_packets == 10
    assert snapshot.link.accepted_packets == 9
    assert snapshot.link.rejected_packets == 1
