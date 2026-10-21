from __future__ import annotations

from espagent_demo.global_hotkeys import (
    ACTIVE_HOTKEYS,
    HOTKEY_DOWN,
    HOTKEY_ESCAPE,
    HOTKEY_LEFT,
    HOTKEY_NAV_LEFT,
    HOTKEY_NAV_RIGHT,
    HOTKEY_RIGHT,
    HOTKEY_TOGGLE,
    HOTKEY_UP,
    HOTKEY_VOICE,
    MOD_ALT,
    MOD_CONTROL,
    MOD_SHIFT,
    MOD_WIN,
    SHORTCUT_KEYS,
    ControlSnapshot,
    HostControlClient,
    HotkeySettings,
    GlobalHotkeyController,
    shortcut_virtual_key,
    shortcut_virtual_keys,
)
from espagent_demo.windows_ime import ImeSnapshot


class RecordingHostControlClient(HostControlClient):
    def __init__(self) -> None:
        super().__init__("http://test")
        self.calls: list[tuple[str, dict[str, object] | None]] = []
        self.active_scene = "link"

    def _request(self, path: str, payload: dict[str, object] | None = None) -> dict[str, object]:
        self.calls.append((path, payload))
        if path == "/api/state":
            return {"active_scene": self.active_scene}
        if path == "/api/modules":
            return {
                "modules": [
                    {"id": "module.quota", "scene_key": "quota", "lifecycle": "running"},
                    {"id": "module.mirror", "scene_key": "mirror", "lifecycle": "running"},
                ]
            }
        return {}


def test_host_control_client_cycles_module_scenes_without_module_id_special_cases() -> None:
    client = RecordingHostControlClient()

    assert client.cycle_scene(1) == "quota"
    assert client.calls[-1] == ("/api/modules/module.quota/activate", {"target": "esp32"})


class FakeUser32:
    def __init__(self, fail_id: int | None = None) -> None:
        self.fail_id = fail_id
        self.registered: dict[int, tuple[int, int]] = {}
        self.unregistered: list[int] = []
        self.pressed: set[int] = set()

    def RegisterHotKey(self, _window: object, hotkey_id: int, modifiers: int, virtual_key: int) -> int:
        if hotkey_id == self.fail_id:
            return 0
        self.registered[hotkey_id] = (modifiers, virtual_key)
        return 1

    def UnregisterHotKey(self, _window: object, hotkey_id: int) -> int:
        self.registered.pop(hotkey_id, None)
        self.unregistered.append(hotkey_id)
        return 1

    def GetAsyncKeyState(self, virtual_key: int) -> int:
        return 0x8000 if virtual_key in self.pressed else 0


class FakeControlClient:
    def __init__(self) -> None:
        self.enabled = False
        self.set_calls: list[dict[str, bool]] = []
        self.scene_directions: list[int] = []
        self.scroll_deltas: list[int] = []
        self.settings = HotkeySettings()
        self.shortcut_calls: list[HotkeySettings] = []
        self.active_scene = "link"
        self.input_states: list[ImeSnapshot] = []
        self.input_submissions: list[str] = []
        self.input_voice_revision = 0
        self.input_draft = ""
        self.input_caret = 0
        self.esp_online = True
        self.voice_starts = 0
        self.voice_stops = 0
        self.voice_prepares = 0
        self.hermes_activations = 0
        self.voice_start_error: Exception | None = None

    def control_snapshot(self) -> ControlSnapshot:
        return ControlSnapshot(
            self.enabled,
            self.settings,
            self.active_scene,
            self.input_voice_revision,
            self.input_draft,
            self.input_caret,
            self.esp_online,
        )

    def control_enabled(self) -> bool:
        return self.enabled

    def set_control(
        self,
        *,
        enabled: bool | None = None,
        global_hotkeys_available: bool | None = None,
    ) -> dict[str, object]:
        call: dict[str, bool] = {}
        if enabled is not None:
            self.enabled = enabled
            call["enabled"] = enabled
        if global_hotkeys_available is not None:
            call["global_hotkeys_available"] = global_hotkeys_available
        self.set_calls.append(call)
        return {"enabled": self.enabled}

    def set_shortcuts(self, settings: HotkeySettings) -> dict[str, object]:
        self.settings = settings
        self.shortcut_calls.append(settings)
        return {"shortcuts": settings}

    def cycle_scene(self, direction: int) -> str:
        self.scene_directions.append(direction)
        self.active_scene = "hermes" if direction < 0 else "codex"
        return self.active_scene

    def scroll(self, delta: int) -> None:
        self.scroll_deltas.append(delta)

    def set_hermes_input(self, snapshot: ImeSnapshot) -> None:
        self.input_states.append(snapshot)

    def submit_hermes_input(self, prompt: str) -> None:
        self.input_submissions.append(prompt)

    def start_hermes_voice_input(self) -> None:
        self.voice_starts += 1
        if self.voice_start_error is not None:
            raise self.voice_start_error

    def stop_hermes_voice_input(self) -> None:
        self.voice_stops += 1

    def prepare_hermes_voice_input(self) -> None:
        self.voice_prepares += 1

    def activate_hermes(self) -> str:
        self.hermes_activations += 1
        self.active_scene = "hermes"
        return self.active_scene


class FakeImeController:
    def __init__(self) -> None:
        self.started = 0
        self.active_calls: list[bool] = []
        self.dismiss_calls = 0
        self.selection_moves: list[int] = []
        self.candidate_selection_active = False
        self.voice_results: list[tuple[str, int]] = []
        self.stopped = 0

    def start(self) -> bool:
        self.started += 1
        return True

    def set_active(self, active: bool) -> None:
        self.active_calls.append(active)

    def dismiss_if_empty(self) -> None:
        self.dismiss_calls += 1

    def move_selection(self, delta: int) -> bool:
        if not self.candidate_selection_active:
            return False
        self.selection_moves.append(delta)
        return True

    def apply_voice_result(self, draft: str, caret: int) -> None:
        self.voice_results.append((draft, caret))

    def stop(self) -> None:
        self.stopped += 1


def make_controller(
    *,
    fail_id: int | None = None,
) -> tuple[GlobalHotkeyController, FakeControlClient, FakeUser32]:
    client = FakeControlClient()
    user32 = FakeUser32(fail_id=fail_id)
    controller = GlobalHotkeyController(client, user32=user32, repeat_interval=0)
    controller._esp_online = client.esp_online
    return controller, client, user32


def test_toggle_temporarily_registers_navigation_and_uses_same_key_to_exit() -> None:
    controller, client, user32 = make_controller()

    controller._handle_hotkey(HOTKEY_TOGGLE)

    assert client.enabled is True
    assert set(user32.registered) == {
        HOTKEY_LEFT,
        HOTKEY_RIGHT,
    }
    assert user32.registered[HOTKEY_LEFT][0] == 0
    assert user32.registered[HOTKEY_RIGHT][0] == 0
    assert controller._scroll_prefix_active is True

    controller._handle_hotkey(HOTKEY_LEFT)
    controller._handle_hotkey(HOTKEY_RIGHT)
    controller._handle_hotkey(HOTKEY_UP)
    controller._handle_hotkey(HOTKEY_DOWN)

    assert client.scene_directions == [-1, 1]
    assert client.scroll_deltas == [-36, 36]

    controller._handle_hotkey(HOTKEY_TOGGLE)

    assert client.enabled is False
    assert user32.registered == {}
    assert set(ACTIVE_HOTKEYS).issubset(set(user32.unregistered))


def test_failed_arrow_registration_keeps_control_disabled_and_releases_partial_set() -> None:
    controller, client, user32 = make_controller(fail_id=HOTKEY_RIGHT)

    controller._handle_hotkey(HOTKEY_TOGGLE)

    assert client.enabled is False
    assert controller._active_registered is False
    assert HOTKEY_LEFT in user32.unregistered
    assert user32.registered == {}


def test_host_state_changes_register_and_release_active_hotkeys() -> None:
    controller, client, user32 = make_controller()
    client.enabled = True

    controller._sync_from_host()
    assert controller._active_registered is True
    assert set(user32.registered) == {
        HOTKEY_TOGGLE,
        HOTKEY_VOICE,
        HOTKEY_LEFT,
        HOTKEY_RIGHT,
    }

    client.enabled = False
    controller._sync_from_host()
    assert controller._active_registered is False
    assert set(user32.registered) == {HOTKEY_TOGGLE, HOTKEY_VOICE}


def test_offline_device_disables_control_and_unregisters_all_hotkeys() -> None:
    controller, client, user32 = make_controller()
    client.enabled = True
    controller._esp_online = True
    controller._register_core_hotkeys()
    controller._sync_from_host()
    assert controller._active_registered is True

    client.esp_online = False
    controller._sync_from_host()

    assert client.enabled is False
    assert controller._toggle_registered is False
    assert controller._active_registered is False
    assert user32.registered == {}

    controller._handle_hotkey(HOTKEY_TOGGLE)
    assert client.enabled is False


def test_online_device_registers_toggle_after_offline_startup() -> None:
    controller, client, user32 = make_controller()
    client.esp_online = False

    controller._sync_from_host()
    assert HOTKEY_TOGGLE not in user32.registered
    assert HOTKEY_VOICE not in user32.registered

    client.esp_online = True
    controller._sync_from_host()

    assert user32.registered[HOTKEY_TOGGLE]
    assert user32.registered[HOTKEY_VOICE]


def test_updated_shortcuts_are_re_registered_while_control_is_active() -> None:
    controller, client, user32 = make_controller()
    client.enabled = True
    controller._sync_from_host()
    client.settings = HotkeySettings(
        toggle_modifier="ctrl_alt",
        toggle_key="f10",
        navigation_mode="custom",
        navigation_prefix="chord:vk:16:Shift",
        scroll_mode="none",
    )

    controller._sync_from_host()

    assert controller._settings == client.settings
    assert user32.registered[HOTKEY_TOGGLE][0] & (MOD_CONTROL | MOD_ALT) == MOD_CONTROL | MOD_ALT
    assert HOTKEY_LEFT not in user32.registered
    assert HOTKEY_RIGHT not in user32.registered
    assert controller._navigation_prefix_active is True
    assert user32.registered[HOTKEY_UP][0] == 0
    assert user32.registered[HOTKEY_DOWN][0] == 0


def test_custom_direction_prefixes_trigger_navigation_and_scrolling() -> None:
    controller, client, user32 = make_controller()
    client.settings = HotkeySettings(
        navigation_mode="custom",
        navigation_prefix="chord:vk:17:Control,vk:16:Shift",
        scroll_mode="custom",
        scroll_prefix="chord:vk:18:Alt,vk:16:Shift",
    )
    client.enabled = True
    controller._sync_from_host()

    assert HOTKEY_LEFT not in user32.registered
    assert HOTKEY_UP not in user32.registered

    user32.pressed.update({17, 16, 0x25})
    controller._poll_direction_prefixes()
    assert client.scene_directions == [-1]

    user32.pressed.clear()
    user32.pressed.update({18, 16, 0x28})
    controller._poll_direction_prefixes()
    assert client.scroll_deltas == [36]


def test_disabled_direction_controls_do_not_register_or_poll() -> None:
    controller, client, user32 = make_controller()
    client.settings = HotkeySettings(
        navigation_mode="disabled",
        scroll_mode="disabled",
    )
    client.enabled = True
    controller._sync_from_host()

    assert not {HOTKEY_LEFT, HOTKEY_RIGHT, HOTKEY_UP, HOTKEY_DOWN} & set(user32.registered)
    user32.pressed.update({0x25, 0x26})
    controller._poll_direction_prefixes()
    assert client.scene_directions == []
    assert client.scroll_deltas == []


def test_failed_shortcut_update_is_rolled_back_in_host_state() -> None:
    controller, client, user32 = make_controller()
    controller._register_core_hotkeys()
    user32.fail_id = HOTKEY_TOGGLE
    client.settings = HotkeySettings(toggle_modifier="ctrl_alt", toggle_key="f10")

    controller._sync_from_host()

    assert client.shortcut_calls == [HotkeySettings()]
    assert client.settings == HotkeySettings()


def test_arbitrary_key_and_modifier_combination_registers_globally() -> None:
    controller, client, user32 = make_controller()
    client.settings = HotkeySettings(
        toggle_modifier="ctrl_shift_alt_win",
        toggle_key="vk:179:MediaPlayPause",
        voice_modifier="shift",
        voice_key="vk:226:IntlBackslash",
    )

    controller._sync_from_host()

    assert user32.registered[HOTKEY_TOGGLE] == (
        MOD_CONTROL | MOD_SHIFT | MOD_ALT | MOD_WIN | 0x4000,
        179,
    )
    assert user32.registered[HOTKEY_VOICE] == (
        MOD_SHIFT | 0x4000,
        226,
    )
    assert shortcut_virtual_key("vk:179:MediaPlayPause") == 179


def test_multi_key_toggle_chord_is_polled_once_per_press() -> None:
    controller, client, user32 = make_controller()
    client.settings = HotkeySettings(
        toggle_modifier="none",
        toggle_key="chord:vk:17:ControlLeft,vk:75:KeyK",
    )
    controller._sync_from_host()

    assert HOTKEY_TOGGLE not in user32.registered
    assert shortcut_virtual_keys(client.settings.toggle_key) == (17, 75)

    user32.pressed.add(17)
    controller._poll_chord_hotkeys()
    assert client.enabled is False

    user32.pressed.add(75)
    controller._poll_chord_hotkeys()
    controller._poll_chord_hotkeys()
    assert client.enabled is True

    user32.pressed.clear()
    controller._poll_chord_hotkeys()
    user32.pressed.update({17, 75})
    controller._poll_chord_hotkeys()
    assert client.enabled is False


def test_multi_key_voice_chord_stops_when_any_key_is_released() -> None:
    controller, client, user32 = make_controller()
    client.settings = HotkeySettings(
        voice_modifier="none",
        voice_key="chord:vk:17:ControlLeft,vk:18:AltLeft,vk:86:KeyV",
    )
    controller._sync_from_host()
    user32.pressed.update({17, 18, 86})

    controller._poll_chord_hotkeys()

    assert client.voice_starts == 1
    assert client.active_scene == "hermes"
    assert controller._voice_active is True

    user32.pressed.remove(86)
    controller._poll_chord_hotkeys()
    if controller._voice_active and not controller._voice_binding_pressed():
        controller._finish_voice_hotkey()

    assert client.voice_stops == 1
    assert controller._voice_active is False


def test_shortcut_chord_validation_rejects_duplicates_and_too_many_keys() -> None:
    assert shortcut_virtual_keys("chord:vk:65:KeyA,vk:65:KeyA") is None
    oversized = "chord:" + ",".join(
        f"vk:{65 + index}:Key{chr(65 + index)}"
        for index in range(13)
    )
    assert shortcut_virtual_keys(oversized) is None


def test_custom_exit_shortcut_registers_only_while_control_is_active() -> None:
    controller, client, user32 = make_controller()
    client.settings = HotkeySettings(
        exit_mode="custom",
        exit_modifier="ctrl",
        exit_key="vk:179:MediaPlayPause",
    )
    controller._sync_from_host()

    assert HOTKEY_ESCAPE not in user32.registered

    controller._handle_hotkey(HOTKEY_TOGGLE)

    assert user32.registered[HOTKEY_ESCAPE] == (MOD_CONTROL, 179)
    controller._handle_hotkey(HOTKEY_ESCAPE)
    assert client.enabled is False
    assert HOTKEY_ESCAPE not in user32.registered


def test_disabled_exit_shortcut_does_not_register_an_extra_hotkey() -> None:
    controller, client, user32 = make_controller()
    client.settings = HotkeySettings(exit_mode="disabled")
    controller._sync_from_host()

    controller._handle_hotkey(HOTKEY_TOGGLE)

    assert client.enabled is True
    assert HOTKEY_ESCAPE not in user32.registered


def test_voice_hotkey_enables_control_switches_to_hermes_and_stops_on_release() -> None:
    controller, client, user32 = make_controller()
    controller._register_core_hotkeys()
    user32.pressed.update({
        SHORTCUT_KEYS[client.settings.voice_key],
        0x11,
        0x12,
    })

    controller._handle_hotkey(HOTKEY_VOICE)

    assert client.enabled is True
    assert client.active_scene == "hermes"
    assert client.hermes_activations == 1
    assert client.voice_prepares == 1
    assert client.voice_starts == 1
    assert controller._voice_active is True
    assert controller._voice_binding_pressed() is True

    user32.pressed.remove(SHORTCUT_KEYS[client.settings.voice_key])
    assert controller._voice_binding_pressed() is False
    controller._finish_voice_hotkey()

    assert client.voice_stops == 1
    assert controller._voice_active is False
    assert client.enabled is True


def test_voice_hotkey_stops_when_required_modifier_is_released() -> None:
    controller, client, user32 = make_controller()
    controller._register_core_hotkeys()
    user32.pressed.update({
        SHORTCUT_KEYS[client.settings.voice_key],
        0x11,
        0x12,
    })
    controller._handle_hotkey(HOTKEY_VOICE)

    user32.pressed.remove(0x12)

    assert controller._voice_binding_pressed() is False
    controller._finish_voice_hotkey()
    assert client.voice_stops == 1


def test_voice_hotkey_cleans_up_when_start_request_fails() -> None:
    controller, client, _user32 = make_controller()
    client.voice_start_error = RuntimeError("voice start response failed")

    controller._handle_hotkey(HOTKEY_VOICE)

    assert client.voice_starts == 1
    assert client.voice_stops == 1
    assert controller._voice_active is False


def test_hermes_scene_reserves_alt_navigation_and_activates_system_ime() -> None:
    client = FakeControlClient()
    client.active_scene = "hermes"
    user32 = FakeUser32()
    ime = FakeImeController()
    controller = GlobalHotkeyController(
        client,
        user32=user32,
        ime_controller=ime,
        repeat_interval=0,
    )
    controller._esp_online = True
    ime.candidate_selection_active = True

    controller._handle_hotkey(HOTKEY_TOGGLE)

    assert client.enabled is True
    assert user32.registered[HOTKEY_LEFT][0] == 0
    assert user32.registered[HOTKEY_RIGHT][0] == 0
    assert user32.registered[HOTKEY_NAV_LEFT][0] == MOD_ALT
    assert user32.registered[HOTKEY_NAV_RIGHT][0] == MOD_ALT
    assert HOTKEY_ESCAPE not in user32.registered
    assert ime.active_calls == [True]

    controller._handle_hotkey(HOTKEY_RIGHT)
    assert ime.selection_moves == [1]
    assert client.active_scene == "hermes"

    controller._handle_hotkey(HOTKEY_NAV_RIGHT)

    assert client.active_scene == "codex"
    assert ime.active_calls == [True, False]
    assert HOTKEY_ESCAPE not in user32.registered


def test_hermes_scene_arrows_navigate_when_candidate_selection_is_inactive() -> None:
    client = FakeControlClient()
    client.active_scene = "hermes"
    user32 = FakeUser32()
    ime = FakeImeController()
    controller = GlobalHotkeyController(
        client,
        user32=user32,
        ime_controller=ime,
        repeat_interval=0,
    )
    controller._esp_online = True

    controller._handle_hotkey(HOTKEY_TOGGLE)
    controller._handle_hotkey(HOTKEY_RIGHT)

    assert ime.selection_moves == []
    assert client.scene_directions == [1]
    assert client.active_scene == "codex"


def test_hermes_scroll_dismisses_empty_input_before_scrolling() -> None:
    client = FakeControlClient()
    client.active_scene = "hermes"
    ime = FakeImeController()
    controller = GlobalHotkeyController(
        client,
        user32=FakeUser32(),
        ime_controller=ime,
        repeat_interval=0,
    )
    controller._esp_online = True

    controller._handle_hotkey(HOTKEY_TOGGLE)
    controller._handle_hotkey(HOTKEY_UP)
    controller._handle_hotkey(HOTKEY_DOWN)

    assert ime.dismiss_calls == 2
    assert client.scroll_deltas == [-36, 36]


def test_voice_result_revision_updates_hidden_ime_draft() -> None:
    client = FakeControlClient()
    client.enabled = True
    client.active_scene = "hermes"
    ime = FakeImeController()
    controller = GlobalHotkeyController(
        client,
        user32=FakeUser32(),
        ime_controller=ime,
        repeat_interval=0,
    )

    controller._sync_from_host()
    client.input_voice_revision = 1
    client.input_draft = "recognized text"
    client.input_caret = 15
    controller._sync_from_host()

    assert ime.voice_results == [("recognized text", 15)]
