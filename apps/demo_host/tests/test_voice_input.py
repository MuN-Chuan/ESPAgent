import logging
import queue
import time
from pathlib import Path

from espagent_demo.hermes_adapter import HermesEvent
from espagent_demo.state import DemoState
from espagent_demo.voice_input import HermesVoiceController


class FakeVoiceAdapter:
    def __init__(self) -> None:
        self.enabled = False
        self.events: queue.Queue[HermesEvent] = queue.Queue()
        self.starts = 0
        self.stops = 0
        self.status_calls = 0
        self.enable_calls: list[bool] = []
        self.silence_durations: list[float] = []

    def voice_status(self) -> dict[str, object]:
        self.status_calls += 1
        return {
            "enabled": self.enabled,
            "available": True,
            "audio_available": True,
            "stt_available": True,
            "details": "ready",
        }

    def enable_voice(self, enabled: bool = True) -> dict[str, object]:
        self.enable_calls.append(enabled)
        self.enabled = enabled
        # Match Hermes: voice.toggle on/off does not return capabilities.
        return {"enabled": enabled, "record_key": "ctrl+b", "tts": False}

    def start_voice_recording(self) -> dict[str, object]:
        self.starts += 1
        return {"status": "recording"}

    def set_voice_silence_duration(self, seconds: float) -> None:
        self.silence_durations.append(seconds)

    def stop_voice_recording(self) -> dict[str, object]:
        self.stops += 1
        return {"status": "stopped"}

    def wait_voice_event(self, timeout: float = 0.5) -> HermesEvent | None:
        try:
            return self.events.get(timeout=timeout)
        except queue.Empty:
            return None


class FakeNativeRecorder:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.started = False
        self.cancelled = False

    def probe(self) -> dict[str, object]:
        return {"available": True, "device": "Test microphone"}

    def start(self) -> None:
        self.started = True

    def stop(self) -> Path:
        self.started = False
        self.path.write_bytes(b"RIFF")
        return self.path

    def cancel(self) -> None:
        self.started = False
        self.cancelled = True


class FakeNativeVoiceAdapter(FakeVoiceAdapter):
    def transcribe_voice_recording(self, path: Path) -> dict[str, object]:
        assert path.read_bytes() == b"RIFF"
        return {"transcript": "Windows microphone transcript", "filtered": False}


class FakePrewarmVoiceAdapter(FakeVoiceAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.prewarmed = False

    def prewarm_voice(self) -> None:
        self.prewarmed = True


def test_voice_controller_enables_records_and_submits_final_transcript() -> None:
    state = DemoState()
    adapter = FakeVoiceAdapter()
    prompts: list[str] = []
    controller = HermesVoiceController(state, adapter, prompts.append)
    controller.start()

    assert controller.start_recording()["status"] == "listening"
    assert adapter.enabled is True
    assert adapter.starts == 1
    assert adapter.silence_durations == [2.0]
    adapter.events.put(HermesEvent("voice.status", {"state": "transcribing"}))
    adapter.events.put(HermesEvent("voice.transcript", {"text": "  今天天气怎么样  "}))

    for _ in range(100):
        if prompts:
            break
        time.sleep(0.01)

    snapshot = controller.snapshot()
    assert prompts == ["今天天气怎么样"]
    assert snapshot["transcript"] == "今天天气怎么样"
    assert snapshot["error"] is None
    controller.close()


def test_voice_controller_prewarms_stt_in_background() -> None:
    state = DemoState()
    adapter = FakePrewarmVoiceAdapter()
    controller = HermesVoiceController(state, adapter, lambda _prompt: None)

    controller.start()

    for _ in range(100):
        if adapter.prewarmed:
            break
        time.sleep(0.01)

    assert adapter.prewarmed is True
    controller.close()


def test_voice_controller_uses_windows_capture_and_hermes_stt_for_draft(
    tmp_path,
    caplog,
) -> None:
    caplog.set_level(logging.INFO)
    state = DemoState()
    adapter = FakeNativeVoiceAdapter()
    recorder = FakeNativeRecorder(tmp_path / "voice.wav")
    controller = HermesVoiceController(
        state,
        adapter,
        lambda _prompt: None,
        native_recorder=recorder,
    )
    controller.start()

    status = controller.refresh_status()
    assert status["available"] is True
    assert "Windows: Test microphone" in str(status["details"])
    assert adapter.status_calls == 1
    controller.start_recording(destination="draft")
    assert recorder.started is True
    assert adapter.status_calls == 1
    assert adapter.starts == 0
    assert adapter.enable_calls == []
    controller.stop_recording()

    for _ in range(100):
        if state.hermes.input_voice_revision:
            break
        time.sleep(0.01)

    assert state.hermes.input_draft == "Windows microphone transcript"
    assert state.hermes.voice_status == "recognized"
    assert not recorder.path.exists()
    messages = [record.message for record in caplog.records]
    assert any("voice_record_start destination=draft capture_backend=windows" in item for item in messages)
    assert any("voice_record_stop capture_backend=windows" in item for item in messages)
    assert any("voice_stt_start" in item for item in messages)
    assert any("voice_stt_done" in item for item in messages)
    controller.close()


def test_native_voice_enable_does_not_toggle_wsl_microphone(tmp_path) -> None:
    state = DemoState()
    adapter = FakeNativeVoiceAdapter()
    recorder = FakeNativeRecorder(tmp_path / "voice.wav")
    controller = HermesVoiceController(
        state,
        adapter,
        lambda _prompt: None,
        native_recorder=recorder,
    )

    enabled = controller.set_enabled(True)
    disabled = controller.set_enabled(False)

    assert enabled["enabled"] is True
    assert enabled["available"] is True
    assert disabled["enabled"] is False
    assert adapter.enable_calls == []


def test_voice_controller_refreshes_capabilities_after_enabling() -> None:
    state = DemoState()
    adapter = FakeVoiceAdapter()
    controller = HermesVoiceController(state, adapter, lambda _prompt: None)

    snapshot = controller.set_enabled(True)

    assert adapter.status_calls == 1
    assert snapshot["enabled"] is True
    assert snapshot["available"] is True
    assert snapshot["audio_available"] is True
    assert snapshot["stt_available"] is True
    assert snapshot["status"] == "ready"


def test_voice_controller_updates_adjustable_silence_duration() -> None:
    state = DemoState()
    adapter = FakeVoiceAdapter()
    controller = HermesVoiceController(state, adapter, lambda _prompt: None)

    snapshot = controller.set_silence_duration(1.5)

    assert adapter.silence_durations == [1.5]
    assert snapshot["silence_duration_seconds"] == 1.5
    assert state.hermes.voice_silence_seconds == 1.5


def test_voice_controller_rejects_out_of_range_silence_duration() -> None:
    state = DemoState()
    adapter = FakeVoiceAdapter()
    controller = HermesVoiceController(state, adapter, lambda _prompt: None)

    for seconds in (0.49, 10.01, float("nan")):
        try:
            controller.set_silence_duration(seconds)
        except ValueError:
            pass
        else:
            raise AssertionError(f"expected ValueError for {seconds}")


def test_voice_controller_does_not_submit_empty_transcript() -> None:
    state = DemoState()
    adapter = FakeVoiceAdapter()
    prompts: list[str] = []
    controller = HermesVoiceController(state, adapter, prompts.append)
    controller.start()
    adapter.events.put(HermesEvent("voice.transcript", {"no_speech_limit": True}))

    for _ in range(100):
        if controller.snapshot()["status"] == "no_speech":
            break
        time.sleep(0.01)

    assert prompts == []
    assert controller.snapshot()["status"] == "no_speech"
    controller.close()


def test_voice_controller_returns_draft_transcript_without_submitting() -> None:
    state = DemoState()
    state.hermes.input_draft = "AB"
    state.hermes.input_caret = 1
    adapter = FakeVoiceAdapter()
    prompts: list[str] = []
    controller = HermesVoiceController(state, adapter, prompts.append)
    controller.start()

    controller.start_recording(destination="draft")
    adapter.events.put(HermesEvent("voice.transcript", {"text": "voice"}))

    for _ in range(100):
        if state.hermes.input_voice_revision:
            break
        time.sleep(0.01)

    assert prompts == []
    assert state.hermes.input_draft == "AvoiceB"
    assert state.hermes.input_caret == 6
    assert state.hermes.input_active is True
    assert state.hermes.input_voice_active is False
    assert state.hermes.input_voice_revision == 1
    controller.close()


def test_draft_voice_idle_without_transcript_closes_recording_state() -> None:
    state = DemoState()
    adapter = FakeVoiceAdapter()
    prompts: list[str] = []
    controller = HermesVoiceController(state, adapter, prompts.append)
    controller.start()

    controller.start_recording(destination="draft")
    adapter.events.put(HermesEvent("voice.status", {"state": "idle"}))

    for _ in range(150):
        if state.hermes.input_voice_revision:
            break
        time.sleep(0.01)

    assert state.hermes.voice_status == "no_speech"
    assert state.hermes.input_active is True
    assert state.hermes.input_voice_active is False
    assert state.hermes.input_voice_revision == 1

    adapter.events.put(HermesEvent("voice.transcript", {"text": "late result"}))
    for _ in range(100):
        if state.hermes.input_voice_revision == 2:
            break
        time.sleep(0.01)

    assert prompts == []
    assert state.hermes.input_draft == "late result"
    assert state.hermes.input_voice_revision == 2
    controller.close()
