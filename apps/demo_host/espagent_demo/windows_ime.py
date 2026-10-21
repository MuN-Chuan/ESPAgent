from __future__ import annotations

import ctypes
import logging
import os
import threading
import time
from ctypes import wintypes
from dataclasses import asdict, dataclass, field
from typing import Callable, Protocol


logger = logging.getLogger(__name__)

WM_APP_ACTIVATE = 0x8001
WM_APP_DEACTIVATE = 0x8002
WM_APP_DISMISS_EMPTY = 0x8003
WM_APP_MOVE_SELECTION = 0x8004
WM_APP_ENTER_SHORT = 0x8005
WM_APP_VOICE_HOLD = 0x8006
WM_APP_APPLY_VOICE_RESULT = 0x8007
WM_APP_VOICE_RELEASE = 0x8008
WM_CHAR = 0x0102
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105
WM_IME_STARTCOMPOSITION = 0x010D
WM_IME_ENDCOMPOSITION = 0x010E
WM_IME_COMPOSITION = 0x010F
WM_IME_NOTIFY = 0x0282
WM_QUIT = 0x0012
GCS_COMPREADSTR = 0x0001
GCS_COMPSTR = 0x0008
GCS_RESULTSTR = 0x0800
NI_COMPOSITIONSTR = 0x0015
CPS_CANCEL = 0x0004
VK_BACK = 0x08
VK_TAB = 0x09
VK_RETURN = 0x0D
VK_SHIFT = 0x10
VK_CONTROL = 0x11
VK_MENU = 0x12
VK_ESCAPE = 0x1B
VK_SPACE = 0x20
VK_LEFT = 0x25
VK_UP = 0x26
VK_RIGHT = 0x27
VK_DOWN = 0x28
VK_DELETE = 0x2E
VK_LWIN = 0x5B
VK_RWIN = 0x5C
WH_KEYBOARD_LL = 13
HC_ACTION = 0
LLKHF_EXTENDED = 0x01
WS_POPUP = 0x80000000
WS_VISIBLE = 0x10000000
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_LAYERED = 0x00080000
SW_HIDE = 0
SW_SHOW = 5
KLF_ACTIVATE = 0x00000001
LWA_ALPHA = 0x00000002
HWND_TOPMOST = -1
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_SHOWWINDOW = 0x0040


@dataclass(frozen=True)
class ImeSnapshot:
    active: bool
    available: bool = True
    method: str = "Microsoft Pinyin"
    draft: str = ""
    caret: int = 0
    preedit: str = ""
    candidates: list[str] = field(default_factory=list)
    candidate_index: int = 0
    candidate_page: int = 0
    error: str | None = None
    voice_active: bool = False

    def payload(self) -> dict[str, object]:
        return asdict(self)


class ImeHostClient(Protocol):
    def set_hermes_input(self, snapshot: ImeSnapshot) -> None: ...

    def submit_hermes_input(self, prompt: str) -> None: ...

    def start_hermes_voice_input(self) -> None: ...

    def stop_hermes_voice_input(self) -> None: ...

    def prepare_hermes_voice_input(self) -> None: ...


class PinyinCandidateEngine:
    """Small offline candidate fallback for TSF input methods without IMM candidates."""

    def __init__(self) -> None:
        self._params = None
        self._dag = None
        self._syllables: set[str] = set()
        try:
            from Pinyin2Hanzi import DefaultDagParams, all_pinyin, dag

            self._params = DefaultDagParams()
            self._dag = dag
            self._syllables = set(all_pinyin())
        except Exception:
            logger.exception("Pinyin candidate fallback is unavailable")

    @property
    def available(self) -> bool:
        return self._params is not None and self._dag is not None

    def candidates(self, preedit: str, limit: int = 32) -> list[str]:
        if not self.available:
            return []
        syllables = self.segment(preedit)
        if not syllables:
            return []
        try:
            results = self._dag(self._params, syllables, path_num=limit, log=True)
        except (KeyError, ValueError):
            return []
        values: list[str] = []
        for result in results:
            candidate = "".join(result.path)
            if candidate and candidate not in values:
                values.append(candidate)
        return values[:limit]

    def segment(self, preedit: str) -> tuple[str, ...]:
        chunks = [value.lower() for value in preedit.replace(" ", "'").split("'") if value]
        if not chunks or any(not value.isascii() or not value.isalpha() for value in chunks):
            return ()
        syllables = getattr(self, "_syllables", set())
        if not syllables:
            return tuple(chunks)
        result: list[str] = []
        for chunk in chunks:
            segmented = self._segment_run(chunk)
            if not segmented:
                return ()
            result.extend(segmented)
        return tuple(result)

    def _segment_run(self, value: str) -> tuple[str, ...]:
        memo: dict[int, tuple[str, ...] | None] = {len(value): ()}

        def visit(start: int) -> tuple[str, ...] | None:
            if start in memo:
                return memo[start]
            best: tuple[str, ...] | None = None
            for end in range(len(value), start, -1):
                syllable = value[start:end]
                if syllable not in self._syllables:
                    continue
                suffix = visit(end)
                if suffix is None:
                    continue
                candidate = (syllable, *suffix)
                if best is None or len(candidate) < len(best):
                    best = candidate
            memo[start] = best
            return best

        return visit(0) or ()


class QueuedImePublisher:
    """Coalesces rapid composition updates before sending them to the local Host."""

    def __init__(self, client: ImeHostClient) -> None:
        self.client = client
        self._condition = threading.Condition()
        self._latest: ImeSnapshot | None = None
        self._submissions: list[str] = []
        self._voice_starts = 0
        self._voice_stops = 0
        self._voice_prepares = 0
        self._stopped = False
        self._thread = threading.Thread(target=self._run, name="espagent-ime-publisher", daemon=True)
        self._thread.start()

    def publish(self, snapshot: ImeSnapshot) -> None:
        with self._condition:
            self._latest = snapshot
            self._condition.notify()

    def submit(self, prompt: str) -> None:
        with self._condition:
            self._submissions.append(prompt)
            self._condition.notify()

    def start_voice(self) -> None:
        with self._condition:
            self._voice_starts += 1
            self._condition.notify()

    def stop_voice(self) -> None:
        with self._condition:
            self._voice_stops += 1
            self._condition.notify()

    def prepare_voice(self) -> None:
        with self._condition:
            self._voice_prepares += 1
            self._condition.notify()

    def stop(self) -> None:
        with self._condition:
            self._stopped = True
            self._condition.notify()
        self._thread.join(timeout=2.0)

    def _run(self) -> None:
        while True:
            with self._condition:
                self._condition.wait_for(
                    lambda: (
                        self._stopped
                        or self._latest is not None
                        or bool(self._submissions)
                        or self._voice_starts > 0
                        or self._voice_stops > 0
                        or self._voice_prepares > 0
                    )
                )
                if (
                    self._stopped
                    and self._latest is None
                    and not self._submissions
                    and self._voice_starts == 0
                    and self._voice_stops == 0
                    and self._voice_prepares == 0
                ):
                    return
                snapshot = self._latest
                self._latest = None
                submissions = self._submissions
                self._submissions = []
                voice_starts = self._voice_starts
                self._voice_starts = 0
                voice_stops = self._voice_stops
                self._voice_stops = 0
                voice_prepares = self._voice_prepares
                self._voice_prepares = 0
            if snapshot is not None:
                try:
                    self.client.set_hermes_input(snapshot)
                except Exception:
                    logger.debug("Unable to publish Microsoft IME state", exc_info=True)
            for prompt in submissions:
                try:
                    self.client.submit_hermes_input(prompt)
                except Exception:
                    logger.exception("Unable to submit Hermes keyboard input")
            for _ in range(voice_prepares):
                try:
                    self.client.prepare_hermes_voice_input()
                except Exception:
                    logger.debug("Unable to prepare Hermes voice input", exc_info=True)
            for _ in range(voice_starts):
                try:
                    self.client.start_hermes_voice_input()
                except Exception:
                    logger.exception("Unable to start Hermes voice input")
            for _ in range(voice_stops):
                try:
                    self.client.stop_hermes_voice_input()
                except Exception:
                    logger.exception("Unable to stop Hermes voice input")


class WindowsHermesInputController:
    def __init__(self, client: ImeHostClient) -> None:
        self._publisher = QueuedImePublisher(client)
        self._bridge = WindowsImeBridge(
            self._publisher.publish,
            self._publisher.submit,
            self._publisher.start_voice,
            self._publisher.stop_voice,
        )

    def start(self) -> bool:
        return self._bridge.start()

    def set_active(self, active: bool) -> None:
        if active:
            self._publisher.prepare_voice()
        self._bridge.set_active(active)

    def dismiss_if_empty(self) -> None:
        self._bridge.dismiss_if_empty()

    def move_selection(self, delta: int) -> bool:
        return self._bridge.move_selection(delta)

    def apply_voice_result(self, draft: str, caret: int) -> None:
        self._bridge.apply_voice_result(draft, caret)

    def stop(self) -> None:
        self._bridge.stop()
        self._publisher.stop()


class WindowsImeBridge:
    """Hosts the system IME in an off-screen Win32 window and mirrors its UI state."""

    def __init__(
        self,
        publish: Callable[[ImeSnapshot], None],
        submit: Callable[[str], None],
        start_voice: Callable[[], None] | None = None,
        stop_voice: Callable[[], None] | None = None,
        enter_hold_seconds: float = 1.0,
    ) -> None:
        self._publish_callback = publish
        self._submit_callback = submit
        self._start_voice_callback = start_voice or (lambda: None)
        self._stop_voice_callback = stop_voice or (lambda: None)
        self._enter_hold_seconds = enter_hold_seconds
        self._stop_event = threading.Event()
        self._ready_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._thread_id = 0
        self._hwnd = 0
        self._active = False
        self._input_visible = False
        self._available = False
        self._error: str | None = None
        self._draft = ""
        self._caret = 0
        self._preedit = ""
        self._fallback_composition = False
        self._candidates: list[str] = []
        self._candidate_index = 0
        self._candidate_page = 0
        self._voice_active = False
        self._enter_down = False
        self._enter_hold_triggered = False
        self._enter_started_at = 0.0
        self._enter_timer: threading.Timer | None = None
        self._enter_lock = threading.Lock()
        self._pending_voice_result = ("", 0)
        self._voice_result_lock = threading.Lock()
        self._candidate_selection_active = threading.Event()
        self._previous_foreground = 0
        self._candidate_engine = PinyinCandidateEngine()
        self._wndproc = None
        self._keyboard_proc = None
        self._keyboard_hook = 0
        self._user32 = None
        self._imm32 = None
        self._kernel32 = None

    def start(self) -> bool:
        if self._thread is not None:
            return self._available
        if os.name != "nt":
            self._error = "Microsoft IME is only available on Windows"
            self._publish()
            return False
        self._thread = threading.Thread(target=self._run, name="espagent-microsoft-ime", daemon=True)
        self._thread.start()
        self._ready_event.wait(timeout=3.0)
        return self._available

    def set_active(self, active: bool) -> None:
        if self._thread is None:
            self.start()
        if not self._available or not self._hwnd:
            self._active = False
            self._publish()
            return
        message = WM_APP_ACTIVATE if active else WM_APP_DEACTIVATE
        self._user32.PostMessageW(self._hwnd, message, 0, 0)

    def dismiss_if_empty(self) -> None:
        if self._available and self._hwnd:
            self._user32.PostMessageW(self._hwnd, WM_APP_DISMISS_EMPTY, 0, 0)

    def move_selection(self, delta: int) -> bool:
        if not self._candidate_selection_active.is_set() or not self._available or not self._hwnd:
            return False
        self._user32.PostMessageW(self._hwnd, WM_APP_MOVE_SELECTION, 0 if delta < 0 else 1, 0)
        return True

    def apply_voice_result(self, draft: str, caret: int) -> None:
        with self._voice_result_lock:
            self._pending_voice_result = (draft[:240], max(0, caret))
        if self._available and self._hwnd:
            self._user32.PostMessageW(self._hwnd, WM_APP_APPLY_VOICE_RESULT, 0, 0)

    def stop(self) -> None:
        self._cancel_enter_hold()
        self._stop_event.set()
        if self._thread_id and self._user32 is not None:
            self._user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
        if self._thread is not None:
            self._thread.join(timeout=3.0)

    def _run(self) -> None:
        try:
            self._configure_api()
            self._thread_id = int(self._kernel32.GetCurrentThreadId())
            self._create_window()
            self._install_keyboard_hook()
            self._available = bool(self._hwnd)
            self._ready_event.set()
            self._publish()
            message = wintypes.MSG()
            while not self._stop_event.is_set():
                result = self._user32.GetMessageW(ctypes.byref(message), None, 0, 0)
                if result <= 0:
                    break
                candidate_control = self._is_candidate_control_message(message)
                ime_translated = False
                if not candidate_control:
                    ime_translated = self._translate_key_message(message)
                if not candidate_control and not ime_translated:
                    self._user32.TranslateMessage(ctypes.byref(message))
                self._user32.DispatchMessageW(ctypes.byref(message))
        except Exception as exc:
            self._error = f"{exc.__class__.__name__}: {exc}"
            logger.exception("Microsoft IME bridge failed")
            self._ready_event.set()
            self._publish()
        finally:
            self._active = False
            if self._keyboard_hook and self._user32 is not None:
                self._user32.UnhookWindowsHookEx(self._keyboard_hook)
                self._keyboard_hook = 0
            if self._hwnd and self._user32 is not None:
                self._user32.DestroyWindow(self._hwnd)
                self._hwnd = 0
            self._publish()

    def _configure_api(self) -> None:
        self._user32 = ctypes.windll.user32
        self._imm32 = ctypes.windll.imm32
        self._kernel32 = ctypes.windll.kernel32
        lresult = ctypes.c_ssize_t
        wndproc_type = ctypes.WINFUNCTYPE(
            lresult,
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        )
        keyboard_proc_type = ctypes.WINFUNCTYPE(
            lresult,
            ctypes.c_int,
            wintypes.WPARAM,
            wintypes.LPARAM,
        )

        class KeyboardHookData(ctypes.Structure):
            _fields_ = [
                ("vk_code", wintypes.DWORD),
                ("scan_code", wintypes.DWORD),
                ("flags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("extra_info", ctypes.c_size_t),
            ]

        class WndClass(ctypes.Structure):
            _fields_ = [
                ("style", wintypes.UINT),
                ("lpfnWndProc", wndproc_type),
                ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE),
                ("hIcon", wintypes.HANDLE),
                ("hCursor", wintypes.HANDLE),
                ("hbrBackground", wintypes.HANDLE),
                ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR),
            ]

        self._wndclass_type = WndClass
        self._keyboard_hook_data_type = KeyboardHookData
        self._wndproc = wndproc_type(self._window_proc)
        self._keyboard_proc = keyboard_proc_type(self._keyboard_hook_proc)
        self._kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        self._kernel32.GetModuleHandleW.restype = wintypes.HINSTANCE
        self._kernel32.GetCurrentThreadId.argtypes = []
        self._kernel32.GetCurrentThreadId.restype = wintypes.DWORD
        self._user32.RegisterClassW.argtypes = [ctypes.POINTER(WndClass)]
        self._user32.RegisterClassW.restype = wintypes.ATOM
        self._user32.CreateWindowExW.argtypes = [
            wintypes.DWORD,
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            wintypes.DWORD,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.HWND,
            wintypes.HMENU,
            wintypes.HINSTANCE,
            wintypes.LPVOID,
        ]
        self._user32.DefWindowProcW.restype = lresult
        self._user32.DefWindowProcW.argtypes = [
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        ]
        self._user32.CreateWindowExW.restype = wintypes.HWND
        self._user32.GetMessageW.argtypes = [
            ctypes.POINTER(wintypes.MSG),
            wintypes.HWND,
            wintypes.UINT,
            wintypes.UINT,
        ]
        self._user32.GetMessageW.restype = ctypes.c_int
        self._user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
        self._user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
        self._user32.DispatchMessageW.restype = lresult
        self._user32.PostMessageW.argtypes = [
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        ]
        self._user32.PostThreadMessageW.argtypes = [
            wintypes.DWORD,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        ]
        self._user32.SetWindowsHookExW.argtypes = [
            ctypes.c_int,
            keyboard_proc_type,
            wintypes.HINSTANCE,
            wintypes.DWORD,
        ]
        self._user32.SetWindowsHookExW.restype = wintypes.HANDLE
        self._user32.CallNextHookEx.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.WPARAM,
            wintypes.LPARAM,
        ]
        self._user32.CallNextHookEx.restype = lresult
        self._user32.UnhookWindowsHookEx.argtypes = [wintypes.HANDLE]
        self._user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
        self._user32.GetAsyncKeyState.restype = ctypes.c_short
        self._user32.LoadKeyboardLayoutW.argtypes = [wintypes.LPCWSTR, wintypes.UINT]
        self._user32.LoadKeyboardLayoutW.restype = wintypes.HANDLE
        self._user32.GetKeyboardLayout.argtypes = [wintypes.DWORD]
        self._user32.GetKeyboardLayout.restype = wintypes.HANDLE
        self._user32.GetForegroundWindow.argtypes = []
        self._user32.GetForegroundWindow.restype = wintypes.HWND
        self._user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, wintypes.LPVOID]
        self._user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        self._user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
        self._user32.SetWindowPos.argtypes = [
            wintypes.HWND,
            wintypes.HWND,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.UINT,
        ]
        self._user32.SetLayeredWindowAttributes.argtypes = [
            wintypes.HWND,
            wintypes.COLORREF,
            wintypes.BYTE,
            wintypes.DWORD,
        ]
        self._imm32.ImmGetContext.restype = wintypes.HANDLE
        self._imm32.ImmGetContext.argtypes = [wintypes.HWND]
        self._imm32.ImmReleaseContext.argtypes = [wintypes.HWND, wintypes.HANDLE]
        self._imm32.ImmGetCompositionStringW.restype = ctypes.c_long
        self._imm32.ImmGetCompositionStringW.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
        ]
        self._imm32.ImmGetCandidateListW.restype = wintypes.DWORD
        self._imm32.ImmGetCandidateListW.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
        ]
        self._imm32.ImmTranslateMessage.argtypes = [
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        ]
        self._imm32.ImmTranslateMessage.restype = wintypes.BOOL
        self._imm32.ImmProcessKey.argtypes = [
            wintypes.HANDLE,
            wintypes.HANDLE,
            wintypes.UINT,
            wintypes.LPARAM,
            wintypes.DWORD,
        ]
        self._imm32.ImmProcessKey.restype = wintypes.BOOL

    def _install_keyboard_hook(self) -> None:
        instance = self._kernel32.GetModuleHandleW(None)
        self._keyboard_hook = int(
            self._user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._keyboard_proc, instance, 0)
        )
        if not self._keyboard_hook:
            raise ctypes.WinError(ctypes.get_last_error())

    def _keyboard_hook_proc(self, code: int, message: int, data_pointer: int) -> int:
        if code == HC_ACTION and self._active and message in {
            WM_KEYDOWN,
            WM_KEYUP,
            WM_SYSKEYDOWN,
            WM_SYSKEYUP,
        }:
            data = ctypes.cast(
                data_pointer,
                ctypes.POINTER(self._keyboard_hook_data_type),
            ).contents
            virtual_key = int(data.vk_code)
            if self._should_capture_key(virtual_key):
                key_up = message in {WM_KEYUP, WM_SYSKEYUP}
                if virtual_key == VK_RETURN and not self._preedit:
                    self._capture_enter(key_up)
                    return 1
                forwarded_message = WM_KEYUP if key_up else WM_KEYDOWN
                forwarded_lparam = 1 | (int(data.scan_code) << 16)
                if data.flags & LLKHF_EXTENDED:
                    forwarded_lparam |= 1 << 24
                if key_up:
                    forwarded_lparam |= (1 << 30) | (1 << 31)
                self._user32.PostMessageW(
                    self._hwnd,
                    forwarded_message,
                    virtual_key,
                    forwarded_lparam,
                )
                return 1
        return int(self._user32.CallNextHookEx(self._keyboard_hook, code, message, data_pointer))

    def _should_capture_key(self, virtual_key: int) -> bool:
        if virtual_key in {VK_MENU, VK_CONTROL, VK_LWIN, VK_RWIN, VK_LEFT, VK_RIGHT}:
            return False
        if any(
            self._user32.GetAsyncKeyState(modifier) & 0x8000
            for modifier in (VK_MENU, VK_CONTROL, VK_LWIN, VK_RWIN)
        ):
            return False
        if virtual_key in {
            VK_BACK,
            VK_TAB,
            VK_RETURN,
            VK_SHIFT,
            VK_ESCAPE,
            VK_SPACE,
            VK_UP,
            VK_DOWN,
            VK_DELETE,
        }:
            return True
        return (
            0x30 <= virtual_key <= 0x5A
            or 0x60 <= virtual_key <= 0x6F
            or 0xBA <= virtual_key <= 0xDF
        )

    def _create_window(self) -> None:
        instance = self._kernel32.GetModuleHandleW(None)
        class_name = "ESPAgentHermesImeWindow"
        window_class = self._wndclass_type(
            0,
            self._wndproc,
            0,
            0,
            instance,
            None,
            None,
            None,
            None,
            class_name,
        )
        atom = self._user32.RegisterClassW(ctypes.byref(window_class))
        if not atom and ctypes.get_last_error() not in {0, 1410}:
            raise ctypes.WinError(ctypes.get_last_error())
        self._hwnd = int(
            self._user32.CreateWindowExW(
                WS_EX_TOOLWINDOW | WS_EX_LAYERED,
                class_name,
                "",
                WS_POPUP | WS_VISIBLE,
                0,
                0,
                2,
                2,
                None,
                None,
                instance,
                None,
            )
        )
        if not self._hwnd:
            raise ctypes.WinError(ctypes.get_last_error())
        self._user32.SetLayeredWindowAttributes(self._hwnd, 0, 0, LWA_ALPHA)
        self._user32.ShowWindow(self._hwnd, SW_HIDE)

    def _capture_enter(self, key_up: bool) -> None:
        with self._enter_lock:
            if key_up:
                if not self._enter_down:
                    return
                self._enter_down = False
                timer = self._enter_timer
                self._enter_timer = None
                held = self._enter_hold_triggered or (
                    time.monotonic() - self._enter_started_at >= self._enter_hold_seconds
                )
                hold_was_posted = self._enter_hold_triggered
                self._enter_hold_triggered = False
            else:
                if self._enter_down:
                    return
                self._enter_down = True
                self._enter_started_at = time.monotonic()
                self._enter_hold_triggered = False
                timer = threading.Timer(self._enter_hold_seconds, self._enter_hold_elapsed)
                timer.daemon = True
                self._enter_timer = timer
                timer.start()
                return
        if timer is not None:
            timer.cancel()
        if held and self._hwnd:
            if not hold_was_posted:
                self._user32.PostMessageW(self._hwnd, WM_APP_VOICE_HOLD, 0, 0)
            self._user32.PostMessageW(self._hwnd, WM_APP_VOICE_RELEASE, 0, 0)
        elif self._hwnd:
            self._user32.PostMessageW(self._hwnd, WM_APP_ENTER_SHORT, 0, 0)

    def _enter_hold_elapsed(self) -> None:
        with self._enter_lock:
            if not self._enter_down or not self._active:
                return
            self._enter_hold_triggered = True
            if self._hwnd:
                self._user32.PostMessageW(self._hwnd, WM_APP_VOICE_HOLD, 0, 0)

    def _cancel_enter_hold(self) -> None:
        with self._enter_lock:
            timer = self._enter_timer
            self._enter_timer = None
            self._enter_down = False
            self._enter_hold_triggered = False
            self._enter_started_at = 0.0
        if timer is not None:
            timer.cancel()

    def _window_proc(self, hwnd: int, message: int, wparam: int, lparam: int) -> int:
        if message == WM_APP_ACTIVATE:
            self._activate_window()
            return 0
        if message == WM_APP_DEACTIVATE:
            self._deactivate_window()
            return 0
        if message == WM_APP_DISMISS_EMPTY:
            self._dismiss_if_empty()
            return 0
        if message == WM_APP_MOVE_SELECTION:
            if self._preedit and self._candidates:
                self._move_candidate(-1 if wparam == 0 else 1)
            elif not self._preedit:
                self._caret = max(0, min(len(self._draft), self._caret + (-1 if wparam == 0 else 1)))
                self._publish()
            return 0
        if message == WM_APP_ENTER_SHORT:
            if self._voice_active:
                return 0
            if self._preedit and self._candidates:
                self._commit_candidate(self._candidate_index)
            elif not self._preedit:
                self._submit()
            return 0
        if message == WM_APP_VOICE_HOLD:
            self._begin_voice_input()
            return 0
        if message == WM_APP_VOICE_RELEASE:
            self._end_voice_input()
            return 0
        if message == WM_APP_APPLY_VOICE_RESULT:
            self._apply_pending_voice_result()
            return 0
        if message == WM_IME_STARTCOMPOSITION:
            result = int(self._user32.DefWindowProcW(hwnd, message, wparam, lparam))
            self._refresh_ime_state()
            return result
        if message == WM_IME_COMPOSITION:
            if lparam & GCS_RESULTSTR:
                result = self._composition_string(GCS_RESULTSTR)
                if result and not self._accept_fallback_pinyin(result, replace=True):
                    self._insert_text(result)
                self._refresh_ime_state()
                return 0
            result = int(self._user32.DefWindowProcW(hwnd, message, wparam, lparam))
            self._refresh_ime_state()
            return result
        if message == WM_IME_ENDCOMPOSITION:
            result = int(self._user32.DefWindowProcW(hwnd, message, wparam, lparam))
            if not self._fallback_composition:
                self._preedit = ""
                self._candidates = []
                self._candidate_index = 0
                self._candidate_page = 0
            self._publish()
            return result
        if message == WM_IME_NOTIFY:
            result = int(self._user32.DefWindowProcW(hwnd, message, wparam, lparam))
            self._refresh_ime_state()
            return result
        if message == WM_KEYDOWN and self._preedit and self._candidates:
            if wparam in {VK_LEFT, VK_UP}:
                self._move_candidate(-1)
                return 0
            if wparam in {VK_RIGHT, VK_DOWN}:
                self._move_candidate(1)
                return 0
            if wparam == VK_SPACE:
                self._commit_candidate(self._candidate_index)
                return 0
            if wparam == VK_RETURN:
                self._commit_candidate(self._candidate_index)
                return 0
            if 0x31 <= wparam <= 0x39:
                candidate_index = wparam - 0x31
                if candidate_index < len(self._candidates):
                    self._commit_candidate(candidate_index)
                return 0
        if message == WM_KEYDOWN and not self._preedit:
            if wparam == VK_RETURN:
                self._submit()
                return 0
            if wparam == VK_LEFT:
                self._caret = max(0, self._caret - 1)
                self._publish()
                return 0
            if wparam == VK_RIGHT:
                self._caret = min(len(self._draft), self._caret + 1)
                self._publish()
                return 0
            if wparam == VK_DELETE and self._caret < len(self._draft):
                self._draft = self._draft[: self._caret] + self._draft[self._caret + 1 :]
                self._publish()
                return 0
        if message == WM_CHAR and self._active:
            character = chr(wparam)
            if wparam == VK_BACK:
                if self._fallback_composition and self._preedit:
                    self._preedit = self._preedit[:-1]
                    self._refresh_fallback_candidates()
                    self._publish()
                    return 0
                if self._caret:
                    self._draft = self._draft[: self._caret - 1] + self._draft[self._caret :]
                    self._caret -= 1
                    self._publish()
                return 0
            if wparam == VK_RETURN:
                return 0
            if self._accept_fallback_pinyin(character):
                self._publish()
                return 0
            if character >= " " and character != "\x7f":
                self._input_visible = True
                self._insert_text(character)
                self._publish()
                return 0
        return int(self._user32.DefWindowProcW(hwnd, message, wparam, lparam))

    def _is_candidate_control_message(self, message: wintypes.MSG) -> bool:
        return bool(
            message.message == WM_KEYDOWN
            and self._preedit
            and self._candidates
            and (
                message.wParam in {VK_RETURN, VK_LEFT, VK_UP, VK_RIGHT, VK_DOWN, VK_SPACE}
                or 0x31 <= message.wParam <= 0x39
            )
        )

    def _translate_key_message(self, message: wintypes.MSG) -> bool:
        if message.message not in {WM_KEYDOWN, 0x0101}:
            return False
        if message.message == WM_KEYDOWN:
            context = self._imm32.ImmGetContext(self._hwnd)
            if context:
                try:
                    layout = self._user32.GetKeyboardLayout(self._thread_id)
                    if not self._imm32.ImmProcessKey(
                        layout,
                        context,
                        message.wParam,
                        message.lParam,
                        0,
                    ):
                        return False
                finally:
                    self._imm32.ImmReleaseContext(self._hwnd, context)
        return bool(
            self._imm32.ImmTranslateMessage(
                self._hwnd,
                message.message,
                message.wParam,
                message.lParam,
            )
        )

    def _activate_window(self) -> None:
        self._active = True
        self._input_visible = bool(self._draft or self._preedit)
        layout = self._user32.LoadKeyboardLayoutW("00000804", KLF_ACTIVATE)
        if layout:
            self._user32.ActivateKeyboardLayout(layout, 0)
        self._user32.ShowWindow(self._hwnd, SW_SHOW)
        context = self._imm32.ImmGetContext(self._hwnd)
        if context:
            self._imm32.ImmSetOpenStatus(context, True)
            self._imm32.ImmReleaseContext(self._hwnd, context)
        self._refresh_ime_state()

    def _focus_window(self) -> None:
        current_thread = int(self._kernel32.GetCurrentThreadId())
        foreground = self._user32.GetForegroundWindow()
        if foreground and foreground != self._hwnd:
            self._previous_foreground = int(foreground)
        foreground_thread = (
            int(self._user32.GetWindowThreadProcessId(foreground, None)) if foreground else 0
        )
        attached = bool(
            foreground_thread
            and foreground_thread != current_thread
            and self._user32.AttachThreadInput(current_thread, foreground_thread, True)
        )
        try:
            self._user32.SetWindowPos(
                self._hwnd,
                HWND_TOPMOST,
                0,
                0,
                1,
                1,
                SWP_NOSIZE | SWP_NOMOVE | SWP_SHOWWINDOW,
            )
            self._user32.SetActiveWindow(self._hwnd)
            self._user32.BringWindowToTop(self._hwnd)
            self._user32.SetForegroundWindow(self._hwnd)
            self._user32.SetFocus(self._hwnd)
        finally:
            if attached:
                self._user32.AttachThreadInput(current_thread, foreground_thread, False)

    def _deactivate_window(self) -> None:
        self._cancel_enter_hold()
        voice_was_active = self._voice_active
        context = self._imm32.ImmGetContext(self._hwnd)
        if context:
            self._imm32.ImmNotifyIME(context, NI_COMPOSITIONSTR, CPS_CANCEL, 0)
            self._imm32.ImmReleaseContext(self._hwnd, context)
        self._active = False
        self._voice_active = False
        self._input_visible = False
        self._preedit = ""
        self._fallback_composition = False
        self._candidates = []
        self._user32.ShowWindow(self._hwnd, SW_HIDE)
        if self._previous_foreground and self._user32.IsWindow(self._previous_foreground):
            self._user32.SetForegroundWindow(self._previous_foreground)
        self._previous_foreground = 0
        self._publish()
        if voice_was_active:
            self._stop_voice_callback()

    def _submit(self) -> None:
        prompt = self._draft.strip()
        if not prompt:
            return
        self._draft = ""
        self._caret = 0
        self._preedit = ""
        self._fallback_composition = False
        self._candidates = []
        self._input_visible = False
        self._voice_active = False
        self._publish()
        self._submit_callback(prompt)

    def _dismiss_if_empty(self) -> None:
        if self._draft or self._preedit:
            return
        self._input_visible = False
        self._candidates = []
        self._candidate_index = 0
        self._candidate_page = 0
        self._publish()

    def _move_candidate(self, delta: int) -> None:
        if not self._candidates:
            return
        self._candidate_index = (self._candidate_index + delta) % len(self._candidates)
        self._publish()

    def _commit_candidate(self, index: int) -> None:
        if not 0 <= index < len(self._candidates):
            return
        context = self._imm32.ImmGetContext(self._hwnd)
        if context:
            self._imm32.ImmNotifyIME(context, NI_COMPOSITIONSTR, CPS_CANCEL, 0)
            self._imm32.ImmReleaseContext(self._hwnd, context)
        self._insert_text(self._candidates[index])
        self._preedit = ""
        self._fallback_composition = False
        self._candidates = []
        self._candidate_index = 0
        self._candidate_page = 0
        self._publish()

    def _insert_text(self, text: str) -> None:
        self._input_visible = True
        self._draft = self._draft[: self._caret] + text + self._draft[self._caret :]
        self._caret += len(text)

    def _begin_voice_input(self) -> None:
        if not self._active or self._preedit or self._voice_active:
            return
        self._input_visible = True
        self._voice_active = True
        self._candidates = []
        self._candidate_index = 0
        self._candidate_page = 0
        self._publish()
        self._start_voice_callback()

    def _end_voice_input(self) -> None:
        if not self._voice_active:
            return
        self._stop_voice_callback()

    def _apply_pending_voice_result(self) -> None:
        with self._voice_result_lock:
            draft, caret = self._pending_voice_result
        self._draft = draft
        self._caret = max(0, min(caret, len(draft)))
        self._preedit = ""
        self._fallback_composition = False
        self._candidates = []
        self._candidate_index = 0
        self._candidate_page = 0
        self._voice_active = False
        self._input_visible = True
        self._publish()

    def _refresh_ime_state(self) -> None:
        reading = self._composition_string(GCS_COMPREADSTR)
        composition = self._composition_string(GCS_COMPSTR)
        native_preedit = reading or composition
        if native_preedit:
            self._fallback_composition = False
            self._preedit = native_preedit
            self._input_visible = True
        candidates, candidate_index, candidate_page = self._candidate_list()
        if not candidates and self._preedit:
            candidates = self._candidate_engine.candidates(self._preedit)
            candidate_index = min(self._candidate_index, max(0, len(candidates) - 1))
            candidate_page = 0
        self._candidates = candidates
        self._candidate_index = candidate_index
        self._candidate_page = candidate_page
        self._publish()

    def _refresh_fallback_candidates(self) -> None:
        if not self._preedit:
            self._fallback_composition = False
            self._candidates = []
            self._candidate_index = 0
            self._candidate_page = 0
            return
        self._candidates = self._candidate_engine.candidates(self._preedit)
        self._candidate_index = min(self._candidate_index, max(0, len(self._candidates) - 1))
        self._candidate_page = 0

    def _accept_fallback_pinyin(self, text: str, *, replace: bool = False) -> bool:
        normalized = text.lower().replace(" ", "'")
        if not normalized or not normalized.isascii():
            return False
        if any(not (character.isalpha() or character == "'") for character in normalized):
            return False
        self._fallback_composition = True
        self._input_visible = True
        self._preedit = normalized if replace else self._preedit + normalized
        self._refresh_fallback_candidates()
        return True

    def _composition_string(self, index: int) -> str:
        context = self._imm32.ImmGetContext(self._hwnd)
        if not context:
            return ""
        try:
            size = int(self._imm32.ImmGetCompositionStringW(context, index, None, 0))
            if size <= 0:
                return ""
            buffer = ctypes.create_string_buffer(size)
            written = int(self._imm32.ImmGetCompositionStringW(context, index, buffer, size))
            if written <= 0:
                return ""
            return buffer.raw[:written].decode("utf-16-le", errors="replace")
        finally:
            self._imm32.ImmReleaseContext(self._hwnd, context)

    def _candidate_list(self) -> tuple[list[str], int, int]:
        context = self._imm32.ImmGetContext(self._hwnd)
        if not context:
            return [], 0, 0
        try:
            size = int(self._imm32.ImmGetCandidateListW(context, 0, None, 0))
            if size < 28:
                return [], 0, 0
            buffer = ctypes.create_string_buffer(size)
            if not self._imm32.ImmGetCandidateListW(context, 0, buffer, size):
                return [], 0, 0
            raw = buffer.raw
            count = int.from_bytes(raw[8:12], "little")
            selection = int.from_bytes(raw[12:16], "little")
            page_start = int.from_bytes(raw[16:20], "little")
            page_size = int.from_bytes(raw[20:24], "little")
            page_end = min(count, page_start + max(1, page_size), page_start + 9)
            values: list[str] = []
            base = ctypes.addressof(buffer)
            for item_index in range(page_start, page_end):
                offset_position = 24 + item_index * 4
                if offset_position + 4 > size:
                    break
                offset = int.from_bytes(raw[offset_position : offset_position + 4], "little")
                if 0 < offset < size:
                    values.append(ctypes.wstring_at(base + offset))
            relative_selection = max(0, min(selection - page_start, max(0, len(values) - 1)))
            page_number = page_start // max(1, page_size)
            return values, relative_selection, page_number
        finally:
            self._imm32.ImmReleaseContext(self._hwnd, context)

    def _publish(self) -> None:
        if self._active and self._preedit and self._candidates:
            self._candidate_selection_active.set()
        else:
            self._candidate_selection_active.clear()
        self._publish_callback(
            ImeSnapshot(
                active=self._active and self._input_visible,
                available=self._available,
                draft=self._draft,
                caret=self._caret,
                preedit=self._preedit,
                candidates=list(self._candidates),
                candidate_index=self._candidate_index,
                candidate_page=self._candidate_page,
                error=self._error,
                voice_active=self._voice_active,
            )
        )
