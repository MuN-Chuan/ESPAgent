from __future__ import annotations

import ctypes
import json
import logging
import os
import re
import threading
import time
import urllib.request
from ctypes import wintypes
from dataclasses import dataclass
from typing import Protocol

from espagent_demo.windows_ime import ImeSnapshot


logger = logging.getLogger(__name__)

WM_HOTKEY = 0x0312
PM_REMOVE = 0x0001
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000

HOTKEY_TOGGLE = 1
HOTKEY_LEFT = 2
HOTKEY_RIGHT = 3
HOTKEY_UP = 4
HOTKEY_DOWN = 5
HOTKEY_ESCAPE = 6
HOTKEY_NAV_LEFT = 7
HOTKEY_NAV_RIGHT = 8
HOTKEY_VOICE = 9

VK_LEFT = 0x25
VK_UP = 0x26
VK_RIGHT = 0x27
VK_DOWN = 0x28
VK_ESCAPE = 0x1B
VK_OEM_3 = 0xC0
VK_F9 = 0x78
VK_F10 = 0x79
VK_F11 = 0x7A
VK_F12 = 0x7B
VK_CONTROL = 0x11
VK_SHIFT = 0x10
VK_MENU = 0x12
VK_LWIN = 0x5B
VK_RWIN = 0x5C

ACTIVE_HOTKEYS = {
    HOTKEY_LEFT: VK_LEFT,
    HOTKEY_RIGHT: VK_RIGHT,
    HOTKEY_UP: VK_UP,
    HOTKEY_DOWN: VK_DOWN,
    HOTKEY_ESCAPE: VK_ESCAPE,
    HOTKEY_NAV_LEFT: VK_LEFT,
    HOTKEY_NAV_RIGHT: VK_RIGHT,
}
CORE_SCENE_ORDER = ("hermes", "lights", "link")
SHORTCUT_KEYS = {
    "backquote": VK_OEM_3,
    "minus": 0xBD,
    "equal": 0xBB,
    "bracket_left": 0xDB,
    "bracket_right": 0xDD,
    "backslash": 0xDC,
    "semicolon": 0xBA,
    "quote": 0xDE,
    "comma": 0xBC,
    "period": 0xBE,
    "slash": 0xBF,
    "space": 0x20,
    "tab": 0x09,
    "enter": 0x0D,
    "escape": VK_ESCAPE,
    "backspace": 0x08,
    "delete": 0x2E,
    "insert": 0x2D,
    "home": 0x24,
    "end": 0x23,
    "page_up": 0x21,
    "page_down": 0x22,
    "arrow_left": VK_LEFT,
    "arrow_up": VK_UP,
    "arrow_right": VK_RIGHT,
    "arrow_down": VK_DOWN,
    "caps_lock": 0x14,
    "num_lock": 0x90,
    "scroll_lock": 0x91,
    "pause": 0x13,
    "print_screen": 0x2C,
    "numpad_add": 0x6B,
    "numpad_subtract": 0x6D,
    "numpad_multiply": 0x6A,
    "numpad_divide": 0x6F,
    "numpad_decimal": 0x6E,
}
SHORTCUT_KEYS.update({chr(code).lower(): code for code in range(ord("A"), ord("Z") + 1)})
SHORTCUT_KEYS.update({str(value): ord(str(value)) for value in range(10)})
SHORTCUT_KEYS.update({f"f{value}": 0x6F + value for value in range(1, 25)})
SHORTCUT_KEYS.update({f"numpad{value}": 0x60 + value for value in range(10)})
TOGGLE_KEYS = SHORTCUT_KEYS
ENCODED_SHORTCUT_KEY = re.compile(r"^vk:(\d{1,3}):([A-Za-z0-9]+)$")
SHORTCUT_CHORD_PREFIX = "chord:"
MAX_SHORTCUT_CHORD_KEYS = 12
MODIFIERS = {
    "none": 0,
    "alt": MOD_ALT,
    "ctrl": MOD_CONTROL,
    "shift": MOD_SHIFT,
    "win": MOD_WIN,
    "ctrl_alt": MOD_CONTROL | MOD_ALT,
    "shift_alt": MOD_SHIFT | MOD_ALT,
    "ctrl_shift": MOD_CONTROL | MOD_SHIFT,
    "alt_win": MOD_ALT | MOD_WIN,
    "ctrl_win": MOD_CONTROL | MOD_WIN,
    "shift_win": MOD_SHIFT | MOD_WIN,
    "ctrl_shift_alt": MOD_CONTROL | MOD_SHIFT | MOD_ALT,
    "ctrl_alt_win": MOD_CONTROL | MOD_ALT | MOD_WIN,
    "shift_alt_win": MOD_SHIFT | MOD_ALT | MOD_WIN,
    "ctrl_shift_win": MOD_CONTROL | MOD_SHIFT | MOD_WIN,
    "ctrl_shift_alt_win": MOD_CONTROL | MOD_SHIFT | MOD_ALT | MOD_WIN,
}


def is_supported_shortcut_key(value: str) -> bool:
    return shortcut_virtual_keys(value) is not None


def shortcut_virtual_key(value: str) -> int | None:
    keys = shortcut_virtual_keys(value)
    return keys[0] if keys is not None and len(keys) == 1 else None


def shortcut_virtual_keys(value: str) -> tuple[int, ...] | None:
    if value.startswith(SHORTCUT_CHORD_PREFIX):
        encoded_keys = value[len(SHORTCUT_CHORD_PREFIX):].split(",")
        if not 1 <= len(encoded_keys) <= MAX_SHORTCUT_CHORD_KEYS:
            return None
        virtual_keys: list[int] = []
        for encoded_key in encoded_keys:
            virtual_key = _single_shortcut_virtual_key(encoded_key)
            if virtual_key is None or virtual_key in virtual_keys:
                return None
            virtual_keys.append(virtual_key)
        return tuple(virtual_keys)
    virtual_key = _single_shortcut_virtual_key(value)
    return None if virtual_key is None else (virtual_key,)


def _single_shortcut_virtual_key(value: str) -> int | None:
    if value in SHORTCUT_KEYS:
        return SHORTCUT_KEYS[value]
    match = ENCODED_SHORTCUT_KEY.fullmatch(value)
    if match is None:
        return None
    virtual_key = int(match.group(1))
    return virtual_key if 1 <= virtual_key <= 0xFF else None


def shortcut_is_chord(value: str) -> bool:
    return value.startswith(SHORTCUT_CHORD_PREFIX)


def is_supported_shortcut_modifier(value: str) -> bool:
    return value in MODIFIERS


@dataclass(frozen=True)
class HotkeySettings:
    toggle_modifier: str = "alt"
    toggle_key: str = "backquote"
    navigation_modifier: str = "none"
    scroll_modifier: str = "alt"
    navigation_mode: str = "none"
    navigation_prefix: str = "chord:vk:18:Alt"
    scroll_mode: str = "custom"
    scroll_prefix: str = "chord:vk:18:Alt"
    voice_modifier: str = "ctrl_alt"
    voice_key: str = "v"
    exit_mode: str = "same"
    exit_modifier: str = "alt"
    exit_key: str = "backquote"

    @classmethod
    def from_display_ui(cls, value: object) -> "HotkeySettings":
        display_ui = value if isinstance(value, dict) else {}
        settings = cls(
            toggle_modifier=str(display_ui.get("shortcut_toggle_modifier") or "alt"),
            toggle_key=str(display_ui.get("shortcut_toggle_key") or "backquote"),
            navigation_modifier=str(display_ui.get("shortcut_navigation_modifier") or "none"),
            scroll_modifier=str(display_ui.get("shortcut_scroll_modifier") or "alt"),
            navigation_mode=str(display_ui.get("shortcut_navigation_mode") or "none"),
            navigation_prefix=str(display_ui.get("shortcut_navigation_prefix") or "chord:vk:18:Alt"),
            scroll_mode=str(display_ui.get("shortcut_scroll_mode") or "custom"),
            scroll_prefix=str(display_ui.get("shortcut_scroll_prefix") or "chord:vk:18:Alt"),
            voice_modifier=str(display_ui.get("shortcut_voice_modifier") or "ctrl_alt"),
            voice_key=str(display_ui.get("shortcut_voice_key") or "v"),
            exit_mode=str(display_ui.get("shortcut_exit_mode") or "same"),
            exit_modifier=str(display_ui.get("shortcut_exit_modifier") or "alt"),
            exit_key=str(display_ui.get("shortcut_exit_key") or "backquote"),
        )
        if not is_supported_shortcut_modifier(settings.toggle_modifier):
            return cls()
        if not is_supported_shortcut_key(settings.toggle_key):
            return cls()
        if not is_supported_shortcut_modifier(settings.navigation_modifier):
            return cls()
        if not is_supported_shortcut_modifier(settings.scroll_modifier):
            return cls()
        if settings.navigation_mode not in {"none", "custom", "disabled"}:
            return cls()
        if not is_supported_shortcut_key(settings.navigation_prefix):
            return cls()
        if settings.scroll_mode not in {"none", "custom", "disabled"}:
            return cls()
        if not is_supported_shortcut_key(settings.scroll_prefix):
            return cls()
        if not is_supported_shortcut_modifier(settings.voice_modifier):
            return cls()
        if not is_supported_shortcut_key(settings.voice_key):
            return cls()
        if settings.exit_mode not in {"same", "custom", "disabled"}:
            return cls()
        if not is_supported_shortcut_modifier(settings.exit_modifier):
            return cls()
        if not is_supported_shortcut_key(settings.exit_key):
            return cls()
        return settings


@dataclass(frozen=True)
class ControlSnapshot:
    enabled: bool
    settings: HotkeySettings
    active_scene: str
    input_voice_revision: int = 0
    input_draft: str = ""
    input_caret: int = 0
    esp_online: bool = False


class ControlClient(Protocol):
    def control_snapshot(self) -> ControlSnapshot: ...

    def control_enabled(self) -> bool: ...

    def set_control(
        self,
        *,
        enabled: bool | None = None,
        global_hotkeys_available: bool | None = None,
    ) -> dict[str, object]: ...

    def set_shortcuts(self, settings: HotkeySettings) -> dict[str, object]: ...

    def cycle_scene(self, direction: int) -> str: ...

    def scroll(self, delta: int) -> None: ...

    def set_hermes_input(self, snapshot: ImeSnapshot) -> None: ...

    def submit_hermes_input(self, prompt: str) -> None: ...

    def start_hermes_voice_input(self) -> None: ...

    def stop_hermes_voice_input(self) -> None: ...

    def prepare_hermes_voice_input(self) -> None: ...

    def activate_hermes(self) -> str: ...


class ImeController(Protocol):
    def start(self) -> bool: ...

    def set_active(self, active: bool) -> None: ...

    def dismiss_if_empty(self) -> None: ...

    def move_selection(self, delta: int) -> bool: ...

    def apply_voice_result(self, draft: str, caret: int) -> None: ...

    def stop(self) -> None: ...


class HostControlClient:
    def __init__(self, base_url: str, timeout: float = 0.8) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _request(self, path: str, payload: dict[str, object] | None = None) -> dict[str, object]:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=data,
            method="GET" if payload is None else "POST",
            headers={"Content-Type": "application/json"} if data is not None else {},
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    def state(self) -> dict[str, object]:
        return self._request("/api/state")

    def control_enabled(self) -> bool:
        return self.control_snapshot().enabled

    def control_snapshot(self) -> ControlSnapshot:
        state = self.state()
        display_ui = state.get("display_ui") or {}
        hermes = state.get("hermes") or {}
        link = state.get("link") or {}
        settings = HotkeySettings.from_display_ui(display_ui)
        enabled = bool(display_ui.get("keyboard_control_enabled")) if isinstance(display_ui, dict) else False
        return ControlSnapshot(
            enabled=enabled,
            settings=settings,
            active_scene=str(state.get("active_scene") or "hermes"),
            input_voice_revision=(
                int(hermes.get("input_voice_revision") or 0) if isinstance(hermes, dict) else 0
            ),
            input_draft=str(hermes.get("input_draft") or "") if isinstance(hermes, dict) else "",
            input_caret=int(hermes.get("input_caret") or 0) if isinstance(hermes, dict) else 0,
            esp_online=bool(link.get("esp32_online")) if isinstance(link, dict) else False,
        )

    def set_control(
        self,
        *,
        enabled: bool | None = None,
        global_hotkeys_available: bool | None = None,
    ) -> dict[str, object]:
        payload: dict[str, object] = {}
        if enabled is not None:
            payload["enabled"] = enabled
        if global_hotkeys_available is not None:
            payload["global_hotkeys_available"] = global_hotkeys_available
        return self._request("/api/display/control", payload)

    def set_shortcuts(self, settings: HotkeySettings) -> dict[str, object]:
        return self._request(
            "/api/settings/shortcuts",
            {
                "toggle_modifier": settings.toggle_modifier,
                "toggle_key": settings.toggle_key,
                "navigation_modifier": settings.navigation_modifier,
                "scroll_modifier": settings.scroll_modifier,
                "navigation_mode": settings.navigation_mode,
                "navigation_prefix": settings.navigation_prefix,
                "scroll_mode": settings.scroll_mode,
                "scroll_prefix": settings.scroll_prefix,
                "voice_modifier": settings.voice_modifier,
                "voice_key": settings.voice_key,
                "exit_mode": settings.exit_mode,
                "exit_modifier": settings.exit_modifier,
                "exit_key": settings.exit_key,
            },
        )

    def cycle_scene(self, direction: int) -> str:
        state = self.state()
        current = str(state.get("active_scene") or "")
        targets: list[tuple[str, str | None]] = [(scene, None) for scene in CORE_SCENE_ORDER]
        modules = self._request("/api/modules").get("modules", [])
        if isinstance(modules, list):
            known_scenes = set(CORE_SCENE_ORDER)
            for descriptor in modules:
                if not isinstance(descriptor, dict) or descriptor.get("lifecycle") == "disabled":
                    continue
                scene_key = descriptor.get("scene_key")
                module_id = descriptor.get("id")
                if not isinstance(scene_key, str) or not isinstance(module_id, str) or scene_key in known_scenes:
                    continue
                known_scenes.add(scene_key)
                targets.append((scene_key, module_id))
        scene_order = [scene for scene, _module_id in targets]
        try:
            current_index = scene_order.index(current)
        except ValueError:
            current_index = -1 if direction > 0 else 0
        target, module_id = targets[(current_index + direction) % len(targets)]
        if module_id is not None:
            self._request(f"/api/modules/{module_id}/activate", {"target": "esp32"})
        else:
            self._request(f"/api/scene/{target}", {})
        return target

    def scroll(self, delta: int) -> None:
        self._request("/api/display/scroll", {"delta": delta})

    def set_hermes_input(self, snapshot: ImeSnapshot) -> None:
        self._request("/api/hermes/input/state", snapshot.payload())

    def submit_hermes_input(self, prompt: str) -> None:
        self._request("/api/hermes/input/submit", {"prompt": prompt})

    def start_hermes_voice_input(self) -> None:
        self._request("/api/hermes/input/voice/start", {})

    def stop_hermes_voice_input(self) -> None:
        self._request("/api/hermes/input/voice/stop", {})

    def prepare_hermes_voice_input(self) -> None:
        self._request("/api/voice/enable", {"enabled": True})

    def activate_hermes(self) -> str:
        self._request("/api/scene/hermes", {})
        return "hermes"


class GlobalHotkeyController:
    def __init__(
        self,
        client: ControlClient,
        *,
        user32: object | None = None,
        ime_controller: ImeController | None = None,
        poll_interval: float = 0.35,
        repeat_interval: float = 0.08,
    ) -> None:
        self.client = client
        self._user32 = user32
        self._ime_controller = ime_controller
        self.poll_interval = poll_interval
        self.repeat_interval = repeat_interval
        self._stop_event = threading.Event()
        self._ready_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._toggle_registered = False
        self._toggle_native_registered = False
        self._toggle_chord_down = False
        self._voice_registered = False
        self._voice_native_registered = False
        self._voice_chord_down = False
        self._voice_active = False
        self._active_registered = False
        self._navigation_prefix_active = False
        self._scroll_prefix_active = False
        self._direction_prefix_down: dict[int, bool] = {}
        self._exit_chord_active = False
        self._exit_chord_down = False
        self._last_direction_at = 0.0
        self._settings = HotkeySettings()
        self._active_scene = "link"
        self._ime_active = False
        self._input_voice_revision: int | None = None
        self._esp_online = False
        self._runtime_available = False

    def start(self) -> bool:
        if self._thread is not None:
            return self._runtime_available
        if os.name != "nt" and self._user32 is None:
            logger.info("Global hotkeys are only available on Windows")
            return False
        if self._ime_controller is not None:
            self._ime_controller.start()
        self._thread = threading.Thread(target=self._run, name="espagent-global-hotkeys", daemon=True)
        self._thread.start()
        self._ready_event.wait(timeout=2.0)
        return self._runtime_available

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        if self._ime_controller is not None:
            self._ime_controller.stop()

    def _run(self) -> None:
        if self._user32 is None:
            self._user32 = ctypes.windll.user32
        try:
            snapshot = self.client.control_snapshot()
            self._settings = snapshot.settings
            self._active_scene = snapshot.active_scene
            self._input_voice_revision = snapshot.input_voice_revision
            self._esp_online = snapshot.esp_online
        except Exception:
            logger.debug("Unable to load shortcut settings; using defaults", exc_info=True)
        if self._esp_online and not self._register_core_hotkeys():
            logger.error("Unable to register global ESP32 control shortcuts")
            self._ready_event.set()
            return
        try:
            self.client.set_control(enabled=False, global_hotkeys_available=True)
        except Exception:
            logger.exception("Unable to publish global hotkey availability")
            self._unregister_core_hotkeys()
            self._ready_event.set()
            return

        logger.info("Global ESP32 control runtime ready")
        self._runtime_available = True
        self._ready_event.set()
        message = wintypes.MSG()
        next_sync_at = 0.0
        try:
            while not self._stop_event.is_set():
                while self._user32.PeekMessageW(ctypes.byref(message), None, 0, 0, PM_REMOVE):
                    if message.message == WM_HOTKEY:
                        self._handle_hotkey(int(message.wParam))
                self._poll_chord_hotkeys()
                self._poll_direction_prefixes()
                if self._voice_active and not self._voice_binding_pressed():
                    self._finish_voice_hotkey()
                now = time.monotonic()
                if now >= next_sync_at:
                    self._sync_from_host()
                    next_sync_at = now + self.poll_interval
                self._stop_event.wait(0.02)
        finally:
            self._finish_voice_hotkey()
            self._set_ime_active(False)
            self._unregister_active_hotkeys()
            self._unregister_core_hotkeys()
            self._runtime_available = False
            try:
                self.client.set_control(enabled=False, global_hotkeys_available=False)
            except Exception:
                logger.debug("Host stopped before global hotkey state could be cleared", exc_info=True)

    def _sync_from_host(self) -> None:
        try:
            snapshot = self.client.control_snapshot()
            enabled = snapshot.enabled
            settings = snapshot.settings
            self._esp_online = snapshot.esp_online
            if not self._esp_online:
                self._settings = settings
                self._active_scene = snapshot.active_scene
                self._finish_voice_hotkey()
                if enabled:
                    self.client.set_control(enabled=False)
                self._set_ime_active(False)
                if self._active_registered:
                    self._unregister_active_hotkeys()
                self._unregister_core_hotkeys()
                return
            if not self._toggle_registered or not self._voice_registered:
                self._settings = settings
                self._active_scene = snapshot.active_scene
                if not self._register_core_hotkeys():
                    logger.error("Unable to register global ESP32 control shortcuts")
                    return
            input_mode_changed = (snapshot.active_scene == "hermes") != (self._active_scene == "hermes")
            if (settings != self._settings or (enabled and input_mode_changed)) and not self._reconfigure_hotkeys(
                settings,
                enabled,
                snapshot.active_scene,
            ):
                logger.error("Unable to apply updated global shortcut settings")
                self.client.set_shortcuts(self._settings)
                return
            self._active_scene = snapshot.active_scene
            if enabled and not self._active_registered:
                if not self._register_active_hotkeys():
                    self.client.set_control(enabled=False)
            elif not enabled and self._active_registered:
                self._unregister_active_hotkeys()
            self._set_ime_active(enabled and self._active_scene == "hermes")
            if self._input_voice_revision is None:
                self._input_voice_revision = snapshot.input_voice_revision
            elif snapshot.input_voice_revision != self._input_voice_revision:
                self._input_voice_revision = snapshot.input_voice_revision
                if enabled and self._active_scene == "hermes" and self._ime_controller is not None:
                    self._ime_controller.apply_voice_result(
                        snapshot.input_draft,
                        snapshot.input_caret,
                    )
        except Exception:
            logger.debug("Unable to synchronize global control state", exc_info=True)

    def _handle_hotkey(self, hotkey_id: int) -> None:
        try:
            if hotkey_id == HOTKEY_TOGGLE:
                if not self._esp_online:
                    return
                self._set_enabled(not self.client.control_enabled())
                return
            if hotkey_id == HOTKEY_VOICE:
                self._begin_voice_hotkey()
                return
            if hotkey_id == HOTKEY_ESCAPE:
                self._set_enabled(False)
                return
            if not self._active_registered:
                return
            now = time.monotonic()
            if now - self._last_direction_at < self.repeat_interval:
                return
            self._last_direction_at = now
            if hotkey_id == HOTKEY_LEFT:
                candidate_selected = (
                    self._active_scene == "hermes"
                    and self._ime_controller is not None
                    and self._ime_controller.move_selection(-1)
                )
                if not candidate_selected:
                    self._scene_changed(self.client.cycle_scene(-1))
            elif hotkey_id == HOTKEY_RIGHT:
                candidate_selected = (
                    self._active_scene == "hermes"
                    and self._ime_controller is not None
                    and self._ime_controller.move_selection(1)
                )
                if not candidate_selected:
                    self._scene_changed(self.client.cycle_scene(1))
            elif hotkey_id == HOTKEY_NAV_LEFT:
                self._scene_changed(self.client.cycle_scene(-1))
            elif hotkey_id == HOTKEY_NAV_RIGHT:
                self._scene_changed(self.client.cycle_scene(1))
            elif hotkey_id == HOTKEY_UP:
                self._dismiss_empty_input()
                self.client.scroll(-36)
            elif hotkey_id == HOTKEY_DOWN:
                self._dismiss_empty_input()
                self.client.scroll(36)
        except Exception:
            logger.exception("Global ESP32 control command failed")

    def _set_enabled(self, enabled: bool) -> None:
        if enabled and not self._esp_online:
            self.client.set_control(enabled=False)
            return
        if enabled:
            snapshot = self.client.control_snapshot()
            self._active_scene = snapshot.active_scene
            if self._active_registered or self._register_active_hotkeys():
                try:
                    self.client.set_control(enabled=True)
                    self._set_ime_active(self._active_scene == "hermes")
                except Exception:
                    self._unregister_active_hotkeys()
                    raise
            else:
                self.client.set_control(enabled=False)
                logger.error("Unable to reserve global arrow keys; control mode remains disabled")
        else:
            self._finish_voice_hotkey()
            self.client.set_control(enabled=False)
            self._set_ime_active(False)
            self._unregister_active_hotkeys()

    def _register_active_hotkeys(self) -> bool:
        if not self._esp_online:
            self._active_registered = False
            return False
        registered: list[int] = []
        bindings: dict[int, tuple[int, int]] = {}
        if self._active_scene == "hermes":
            bindings[HOTKEY_LEFT] = (0, VK_LEFT)
            bindings[HOTKEY_RIGHT] = (0, VK_RIGHT)
            if self._settings.navigation_mode == "none":
                bindings[HOTKEY_NAV_LEFT] = (MOD_ALT, VK_LEFT)
                bindings[HOTKEY_NAV_RIGHT] = (MOD_ALT, VK_RIGHT)
            elif self._settings.navigation_mode == "custom":
                self._navigation_prefix_active = True
        elif self._settings.navigation_mode == "none":
            bindings[HOTKEY_LEFT] = (0, VK_LEFT)
            bindings[HOTKEY_RIGHT] = (0, VK_RIGHT)
        elif self._settings.navigation_mode == "custom":
            self._navigation_prefix_active = True
        if self._settings.scroll_mode == "none":
            bindings[HOTKEY_UP] = (0, VK_UP)
            bindings[HOTKEY_DOWN] = (0, VK_DOWN)
        elif self._settings.scroll_mode == "custom":
            self._scroll_prefix_active = True
        if self._settings.exit_mode == "custom":
            if shortcut_is_chord(self._settings.exit_key):
                self._exit_chord_active = True
                self._exit_chord_down = False
            else:
                exit_virtual_key = shortcut_virtual_key(self._settings.exit_key)
                if exit_virtual_key is None:
                    self._active_registered = False
                    return False
                bindings[HOTKEY_ESCAPE] = (
                    MODIFIERS[self._settings.exit_modifier],
                    exit_virtual_key,
                )
        for hotkey_id, (modifiers, virtual_key) in bindings.items():
            if not self._user32.RegisterHotKey(None, hotkey_id, modifiers, virtual_key):
                for registered_id in registered:
                    self._user32.UnregisterHotKey(None, registered_id)
                self._active_registered = False
                self._exit_chord_active = False
                self._exit_chord_down = False
                self._navigation_prefix_active = False
                self._scroll_prefix_active = False
                self._direction_prefix_down.clear()
                return False
            registered.append(hotkey_id)
        self._active_registered = True
        return True

    def _register_toggle_hotkey(self) -> bool:
        if shortcut_is_chord(self._settings.toggle_key):
            self._toggle_registered = True
            self._toggle_native_registered = False
            self._toggle_chord_down = False
            return True
        modifiers = MODIFIERS[self._settings.toggle_modifier] | MOD_NOREPEAT
        virtual_key = shortcut_virtual_key(self._settings.toggle_key)
        if virtual_key is None:
            self._toggle_registered = False
            return False
        self._toggle_registered = bool(
            self._user32.RegisterHotKey(None, HOTKEY_TOGGLE, modifiers, virtual_key)
        )
        self._toggle_native_registered = self._toggle_registered
        return self._toggle_registered

    def _register_voice_hotkey(self) -> bool:
        if shortcut_is_chord(self._settings.voice_key):
            self._voice_registered = True
            self._voice_native_registered = False
            self._voice_chord_down = False
            return True
        modifiers = MODIFIERS[self._settings.voice_modifier] | MOD_NOREPEAT
        virtual_key = shortcut_virtual_key(self._settings.voice_key)
        if virtual_key is None:
            self._voice_registered = False
            return False
        self._voice_registered = bool(
            self._user32.RegisterHotKey(None, HOTKEY_VOICE, modifiers, virtual_key)
        )
        self._voice_native_registered = self._voice_registered
        return self._voice_registered

    def _register_core_hotkeys(self) -> bool:
        if self._register_toggle_hotkey() and self._register_voice_hotkey():
            return True
        self._unregister_core_hotkeys()
        return False

    def _unregister_core_hotkeys(self) -> None:
        if self._user32 is None:
            return
        if self._toggle_native_registered:
            self._user32.UnregisterHotKey(None, HOTKEY_TOGGLE)
        if self._voice_native_registered:
            self._user32.UnregisterHotKey(None, HOTKEY_VOICE)
        self._toggle_registered = False
        self._toggle_native_registered = False
        self._toggle_chord_down = False
        self._voice_registered = False
        self._voice_native_registered = False
        self._voice_chord_down = False

    def _reconfigure_hotkeys(
        self,
        settings: HotkeySettings,
        enabled: bool,
        active_scene: str | None = None,
    ) -> bool:
        previous_settings = self._settings
        previous_scene = self._active_scene
        previous_active = self._active_registered
        self._finish_voice_hotkey()
        self._unregister_active_hotkeys()
        self._unregister_core_hotkeys()

        self._settings = settings
        if active_scene is not None:
            self._active_scene = active_scene
        if self._register_core_hotkeys() and (not enabled or self._register_active_hotkeys()):
            return True

        self._unregister_active_hotkeys()
        self._unregister_core_hotkeys()
        self._settings = previous_settings
        self._active_scene = previous_scene
        self._register_core_hotkeys()
        if previous_active:
            self._register_active_hotkeys()
        return False

    def _begin_voice_hotkey(self) -> None:
        if self._voice_active or not self._esp_online:
            return
        if not self.client.control_enabled():
            self._set_enabled(True)
        if not self.client.control_enabled():
            return
        if self._active_scene != "hermes":
            self._scene_changed(self.client.activate_hermes())
        self.client.prepare_hermes_voice_input()
        self._voice_active = True
        try:
            self.client.start_hermes_voice_input()
        except Exception:
            self._finish_voice_hotkey()
            raise

    def _finish_voice_hotkey(self) -> None:
        if not self._voice_active:
            return
        self._voice_active = False
        try:
            self.client.stop_hermes_voice_input()
        except Exception:
            logger.exception("Unable to stop Hermes voice input")

    def _voice_binding_pressed(self) -> bool:
        return self._shortcut_binding_pressed(
            self._settings.voice_modifier,
            self._settings.voice_key,
        )

    def _shortcut_binding_pressed(self, modifier: str, key: str) -> bool:
        if self._user32 is None:
            return False
        virtual_keys = shortcut_virtual_keys(key)
        if virtual_keys is None or not all(self._key_pressed(key) for key in virtual_keys):
            return False
        if shortcut_is_chord(key):
            return True
        modifiers = MODIFIERS[modifier]
        if modifiers & MOD_ALT and not self._key_pressed(VK_MENU):
            return False
        if modifiers & MOD_CONTROL and not self._key_pressed(VK_CONTROL):
            return False
        if modifiers & MOD_SHIFT and not self._key_pressed(VK_SHIFT):
            return False
        if modifiers & MOD_WIN and not (
            self._key_pressed(VK_LWIN) or self._key_pressed(VK_RWIN)
        ):
            return False
        return True

    def _poll_chord_hotkeys(self) -> None:
        if not self._esp_online:
            return
        if shortcut_is_chord(self._settings.toggle_key):
            pressed = self._shortcut_binding_pressed(
                self._settings.toggle_modifier,
                self._settings.toggle_key,
            )
            if pressed and not self._toggle_chord_down:
                self._handle_hotkey(HOTKEY_TOGGLE)
            self._toggle_chord_down = pressed
        if shortcut_is_chord(self._settings.voice_key):
            pressed = self._voice_binding_pressed()
            if pressed and not self._voice_chord_down:
                self._handle_hotkey(HOTKEY_VOICE)
            self._voice_chord_down = pressed
        if (
            self._active_registered
            and self._settings.exit_mode == "custom"
            and self._exit_chord_active
        ):
            pressed = self._shortcut_binding_pressed(
                self._settings.exit_modifier,
                self._settings.exit_key,
            )
            if pressed and not self._exit_chord_down:
                self._handle_hotkey(HOTKEY_ESCAPE)
            self._exit_chord_down = pressed

    def _poll_direction_prefixes(self) -> None:
        if not self._active_registered:
            return
        bindings: list[tuple[int, int, str]] = []
        if self._navigation_prefix_active:
            left_id = HOTKEY_NAV_LEFT if self._active_scene == "hermes" else HOTKEY_LEFT
            right_id = HOTKEY_NAV_RIGHT if self._active_scene == "hermes" else HOTKEY_RIGHT
            bindings.extend((
                (left_id, VK_LEFT, self._settings.navigation_prefix),
                (right_id, VK_RIGHT, self._settings.navigation_prefix),
            ))
        if self._scroll_prefix_active:
            bindings.extend((
                (HOTKEY_UP, VK_UP, self._settings.scroll_prefix),
                (HOTKEY_DOWN, VK_DOWN, self._settings.scroll_prefix),
            ))
        active_ids: set[int] = set()
        for hotkey_id, direction_key, prefix in bindings:
            active_ids.add(hotkey_id)
            prefix_keys = shortcut_virtual_keys(prefix)
            pressed = (
                prefix_keys is not None
                and all(self._key_pressed(key) for key in prefix_keys)
                and self._key_pressed(direction_key)
            )
            if pressed:
                self._handle_hotkey(hotkey_id)
            self._direction_prefix_down[hotkey_id] = pressed
        for hotkey_id in set(self._direction_prefix_down) - active_ids:
            self._direction_prefix_down.pop(hotkey_id, None)

    def _key_pressed(self, virtual_key: int) -> bool:
        return bool(self._user32.GetAsyncKeyState(virtual_key) & 0x8000)

    def _unregister_active_hotkeys(self) -> None:
        if self._user32 is None:
            return
        for hotkey_id in ACTIVE_HOTKEYS:
            self._user32.UnregisterHotKey(None, hotkey_id)
        self._active_registered = False
        self._navigation_prefix_active = False
        self._scroll_prefix_active = False
        self._direction_prefix_down.clear()
        self._exit_chord_active = False
        self._exit_chord_down = False

    def _scene_changed(self, scene: str) -> None:
        previous_input_mode = self._active_scene == "hermes"
        self._active_scene = scene
        input_mode = scene == "hermes"
        if previous_input_mode != input_mode and self._active_registered:
            self._unregister_active_hotkeys()
            if not self._register_active_hotkeys():
                self.client.set_control(enabled=False)
                self._set_ime_active(False)
                return
        self._set_ime_active(input_mode)

    def _set_ime_active(self, active: bool) -> None:
        if active == self._ime_active:
            return
        self._ime_active = active
        if self._ime_controller is not None:
            self._ime_controller.set_active(active)

    def _dismiss_empty_input(self) -> None:
        if self._active_scene == "hermes" and self._ime_controller is not None:
            self._ime_controller.dismiss_if_empty()
