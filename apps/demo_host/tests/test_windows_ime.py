from __future__ import annotations

import time
from types import SimpleNamespace

from espagent_demo.windows_ime import (
    GCS_RESULTSTR,
    VK_BACK,
    VK_CONTROL,
    VK_LEFT,
    VK_MENU,
    VK_RIGHT,
    WM_APP_MOVE_SELECTION,
    WM_APP_APPLY_VOICE_RESULT,
    WM_APP_ENTER_SHORT,
    WM_APP_VOICE_HOLD,
    WM_APP_VOICE_RELEASE,
    WM_CHAR,
    WM_IME_COMPOSITION,
    WM_KEYDOWN,
    PinyinCandidateEngine,
    WindowsImeBridge,
)


def test_candidate_engine_uses_segmented_preedit_and_deduplicates_results() -> None:
    calls: list[tuple[str, ...]] = []
    engine = object.__new__(PinyinCandidateEngine)
    engine._params = object()

    def fake_dag(_params: object, syllables: tuple[str, ...], **_kwargs: object):
        calls.append(syllables)
        return [
            SimpleNamespace(path=["你", "好"]),
            SimpleNamespace(path=["你", "号"]),
            SimpleNamespace(path=["你", "好"]),
        ]

    engine._dag = fake_dag

    assert engine.candidates("Ni'hao", limit=5) == ["你好", "你号"]
    assert calls == [("ni", "hao")]


def test_candidate_engine_ignores_invalid_syllables() -> None:
    engine = object.__new__(PinyinCandidateEngine)
    engine._params = object()
    engine._dag = lambda *_args, **_kwargs: []

    assert engine.candidates("ni'好") == []


def test_candidate_engine_segments_continuous_pinyin() -> None:
    calls: list[tuple[str, ...]] = []
    engine = object.__new__(PinyinCandidateEngine)
    engine._params = object()
    engine._syllables = {"ni", "hao"}

    def fake_dag(_params: object, syllables: tuple[str, ...], **_kwargs: object):
        calls.append(syllables)
        return [SimpleNamespace(path=["你", "好"])]

    engine._dag = fake_dag

    assert engine.segment("nihao") == ("ni", "hao")
    assert engine.candidates("nihao") == ["你好"]
    assert calls == [("ni", "hao")]


def test_candidate_engine_requests_extended_candidate_list_by_default() -> None:
    requested: list[int] = []
    engine = object.__new__(PinyinCandidateEngine)
    engine._params = object()
    engine._syllables = {"ni"}

    def fake_dag(_params: object, _syllables: tuple[str, ...], **kwargs: object):
        requested.append(int(kwargs["path_num"]))
        return [SimpleNamespace(path=[f"候选{index}"]) for index in range(40)]

    engine._dag = fake_dag

    assert len(engine.candidates("ni")) == 32
    assert requested == [32]


def test_input_stays_hidden_until_typing_and_hides_after_submit() -> None:
    snapshots = []
    submissions: list[str] = []
    bridge = WindowsImeBridge(snapshots.append, submissions.append)
    bridge._active = True

    bridge._publish()
    bridge._insert_text("测试")
    bridge._publish()
    bridge._submit()

    assert [snapshot.active for snapshot in snapshots] == [False, True, False]
    assert [snapshot.caret for snapshot in snapshots] == [0, 2, 0]
    assert submissions == ["测试"]
    assert snapshots[-1].draft == ""


def test_voice_input_opens_editor_then_short_enter_submits_result() -> None:
    snapshots = []
    submissions: list[str] = []
    voice_starts: list[bool] = []
    voice_stops: list[bool] = []
    bridge = WindowsImeBridge(
        snapshots.append,
        submissions.append,
        lambda: voice_starts.append(True),
        lambda: voice_stops.append(True),
    )
    bridge._active = True

    bridge._begin_voice_input()

    assert voice_starts == [True]
    assert snapshots[-1].active is True
    assert snapshots[-1].voice_active is True
    assert bridge._window_proc(0, WM_APP_VOICE_RELEASE, 0, 0) == 0
    assert voice_stops == [True]
    assert bridge._window_proc(0, WM_APP_ENTER_SHORT, 0, 0) == 0
    assert submissions == []

    with bridge._voice_result_lock:
        bridge._pending_voice_result = ("voice result", 12)
    assert bridge._window_proc(0, WM_APP_APPLY_VOICE_RESULT, 0, 0) == 0
    assert snapshots[-1].draft == "voice result"
    assert snapshots[-1].voice_active is False

    assert bridge._window_proc(0, WM_APP_ENTER_SHORT, 0, 0) == 0
    assert submissions == ["voice result"]


def test_deactivating_screen_control_stops_active_voice_recording() -> None:
    voice_stops: list[bool] = []
    bridge = WindowsImeBridge(
        lambda _snapshot: None,
        lambda _prompt: None,
        lambda: None,
        lambda: voice_stops.append(True),
    )
    bridge._active = True
    bridge._voice_active = True
    bridge._hwnd = 1
    bridge._imm32 = SimpleNamespace(ImmGetContext=lambda _hwnd: 0)
    bridge._user32 = SimpleNamespace(
        ShowWindow=lambda *_args: None,
        IsWindow=lambda _hwnd: False,
    )

    bridge._deactivate_window()

    assert bridge._voice_active is False
    assert voice_stops == [True]


def test_enter_hold_and_short_press_post_distinct_commands() -> None:
    posted: list[int] = []
    bridge = WindowsImeBridge(
        lambda _snapshot: None,
        lambda _prompt: None,
        enter_hold_seconds=0.01,
    )
    bridge._active = True
    bridge._hwnd = 1
    bridge._user32 = SimpleNamespace(
        PostMessageW=lambda _hwnd, message, _wparam, _lparam: posted.append(message),
    )

    bridge._capture_enter(False)
    bridge._capture_enter(True)
    assert posted == [WM_APP_ENTER_SHORT]

    posted.clear()
    bridge._capture_enter(False)
    time.sleep(0.03)
    bridge._capture_enter(True)
    assert posted == [WM_APP_VOICE_HOLD, WM_APP_VOICE_RELEASE]


def test_left_and_right_keys_cycle_displayed_candidates() -> None:
    snapshots = []
    bridge = WindowsImeBridge(snapshots.append, lambda _prompt: None)
    bridge._active = True
    bridge._input_visible = True
    bridge._preedit = "ni'hao"
    bridge._candidates = ["你好", "你号", "你豪"]

    assert bridge._window_proc(0, WM_KEYDOWN, VK_RIGHT, 0) == 0
    assert snapshots[-1].candidate_index == 1
    assert bridge._window_proc(0, WM_KEYDOWN, VK_LEFT, 0) == 0
    assert snapshots[-1].candidate_index == 0
    assert bridge._window_proc(0, WM_KEYDOWN, VK_LEFT, 0) == 0
    assert snapshots[-1].candidate_index == 2
    for expected in (0, 1, 2, 0, 1, 2, 0):
        assert bridge._window_proc(0, WM_KEYDOWN, VK_RIGHT, 0) == 0
        assert snapshots[-1].candidate_index == expected


def test_move_selection_only_posts_while_candidates_are_active() -> None:
    posted: list[tuple[int, int]] = []
    bridge = WindowsImeBridge(lambda _snapshot: None, lambda _prompt: None)
    bridge._available = True
    bridge._hwnd = 1
    bridge._user32 = SimpleNamespace(
        PostMessageW=lambda _hwnd, message, wparam, _lparam: posted.append((message, wparam)),
    )

    assert bridge.move_selection(1) is False
    assert posted == []

    bridge._active = True
    bridge._preedit = "ni"
    bridge._candidates = ["\u4f60", "\u5462"]
    bridge._publish()

    assert bridge.move_selection(-1) is True
    assert posted == [(WM_APP_MOVE_SELECTION, 0)]

    bridge._preedit = ""
    bridge._publish()
    assert bridge.move_selection(1) is False
    assert posted == [(WM_APP_MOVE_SELECTION, 0)]


def test_ascii_letters_fall_back_to_pinyin_preedit_and_candidates() -> None:
    snapshots = []
    bridge = WindowsImeBridge(snapshots.append, lambda _prompt: None)
    bridge._active = True
    bridge._candidate_engine = SimpleNamespace(
        candidates=lambda preedit: ["你好", "你号"] if preedit == "nihao" else []
    )

    for character in "nihao":
        assert bridge._window_proc(0, WM_CHAR, ord(character), 0) == 0

    assert snapshots[-1].active is True
    assert snapshots[-1].draft == ""
    assert snapshots[-1].preedit == "nihao"
    assert snapshots[-1].candidates == ["你好", "你号"]


def test_ascii_ime_result_remains_preedit_when_focus_changes() -> None:
    snapshots = []
    bridge = WindowsImeBridge(snapshots.append, lambda _prompt: None)
    bridge._active = True
    bridge._candidate_engine = SimpleNamespace(
        candidates=lambda preedit: ["你好", "你号"] if preedit == "nihao" else []
    )
    bridge._preedit = "ni'hao"
    bridge._composition_string = lambda index: "nihao" if index == GCS_RESULTSTR else ""
    bridge._candidate_list = lambda: ([], 0, 0)

    assert bridge._window_proc(0, WM_IME_COMPOSITION, 0, GCS_RESULTSTR) == 0

    assert snapshots[-1].draft == ""
    assert snapshots[-1].preedit == "nihao"
    assert snapshots[-1].candidates == ["你好", "你号"]


def test_dismiss_only_hides_an_empty_input() -> None:
    snapshots = []
    bridge = WindowsImeBridge(snapshots.append, lambda _prompt: None)
    bridge._active = True
    bridge._input_visible = True

    bridge._dismiss_if_empty()
    assert snapshots[-1].active is False

    bridge._input_visible = True
    bridge._draft = "保留"
    bridge._dismiss_if_empty()
    assert bridge._input_visible is True


def test_move_selection_updates_text_caret_without_preedit() -> None:
    snapshots = []
    bridge = WindowsImeBridge(snapshots.append, lambda _prompt: None)
    bridge._active = True
    bridge._input_visible = True
    bridge._draft = "你好"
    bridge._caret = 2

    assert bridge._window_proc(0, WM_APP_MOVE_SELECTION, 0, 0) == 0
    assert snapshots[-1].caret == 1
    assert bridge._window_proc(0, WM_APP_MOVE_SELECTION, 1, 0) == 0
    assert snapshots[-1].caret == 2


def test_keyboard_hook_capture_keeps_system_shortcuts_and_global_arrows_free() -> None:
    bridge = WindowsImeBridge(lambda _snapshot: None, lambda _prompt: None)
    pressed: set[int] = set()
    bridge._user32 = SimpleNamespace(
        GetAsyncKeyState=lambda key: -32768 if key in pressed else 0,
    )

    assert bridge._should_capture_key(ord("A")) is True
    assert bridge._should_capture_key(VK_BACK) is True
    assert bridge._should_capture_key(VK_LEFT) is False
    assert bridge._should_capture_key(VK_RIGHT) is False
    pressed.add(VK_MENU)
    assert bridge._should_capture_key(ord("A")) is False
    pressed.clear()
    pressed.add(VK_CONTROL)
    assert bridge._should_capture_key(ord("A")) is False
