import json
import queue
from types import SimpleNamespace

import pytest

from espagent_demo.hermes_adapter import DemoHermesAdapter, HermesEvent, WslHermesAdapter


class QueueStream:
    def __init__(self) -> None:
        self.lines: queue.Queue[str | None] = queue.Queue()

    def put(self, message: dict[str, object]) -> None:
        self.lines.put(json.dumps(message, ensure_ascii=False) + "\n")

    def close(self) -> None:
        self.lines.put(None)

    def __iter__(self):
        return self

    def __next__(self) -> str:
        line = self.lines.get(timeout=2)
        if line is None:
            raise StopIteration
        return line


class FakeStdin:
    def __init__(self, process: "FakeProcess") -> None:
        self.process = process
        self.writes: list[dict[str, object]] = []

    def write(self, raw: str) -> int:
        message = json.loads(raw)
        self.writes.append(message)
        request_id = message["id"]
        method = message["method"]
        if method == "session.create":
            self.process.session_count += 1
            self.process.stdout.put(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {"session_id": f"session-{self.process.session_count}"},
                }
            )
        elif method == "prompt.submit":
            self.process.stdout.put(
                {"jsonrpc": "2.0", "id": request_id, "result": {"status": "streaming"}}
            )
            for event_type, payload in self.process.turn_events:
                self.process.stdout.put(
                    {
                        "jsonrpc": "2.0",
                        "method": "event",
                        "params": {"type": event_type, "session_id": "session-1", "payload": payload},
                    }
                )
        elif method == "session.interrupt":
            self.process.stdout.put(
                {"jsonrpc": "2.0", "id": request_id, "result": {"status": "interrupted"}}
            )
        elif method == "voice.toggle":
            action = message["params"].get("action")
            enabled = action == "on" or (action == "status" and self.process.voice_enabled)
            if action == "off":
                enabled = False
            self.process.voice_enabled = enabled
            self.process.stdout.put(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "enabled": enabled,
                        "available": True,
                        "audio_available": True,
                        "stt_available": True,
                        "tts": False,
                    },
                }
            )
        elif method == "voice.record":
            action = message["params"].get("action")
            self.process.stdout.put(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {"status": "recording" if action == "start" else "stopped"},
                }
            )
        elif method == "espagent.voice.transcribe":
            if self.process.stt_respond:
                self.process.stdout.put(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "result": {"transcript": "native voice", "filtered": False},
                    }
                )
        elif method == "espagent.voice.prewarm":
            self.process.stdout.put(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {"success": True, "provider": "local"},
                }
            )
        return len(raw)

    def flush(self) -> None:
        return None

    def close(self) -> None:
        self.process.running = False
        self.process.stdout.close()
        self.process.stderr.close()


class FakeProcess:
    def __init__(self, turn_events: list[tuple[str, dict[str, object]]]) -> None:
        self.running = True
        self.session_count = 0
        self.stdout = QueueStream()
        self.stderr = QueueStream()
        self.stdin = FakeStdin(self)
        self.turn_events = turn_events
        self.voice_enabled = False
        self.stt_respond = True
        self.stdout.put(
            {"jsonrpc": "2.0", "method": "event", "params": {"type": "gateway.ready", "payload": {}}}
        )

    def poll(self):
        return None if self.running else 0

    def wait(self, timeout=None):
        self.running = False
        return 0

    def terminate(self) -> None:
        self.running = False

    def kill(self) -> None:
        self.running = False


def test_demo_hermes_streams_reply_and_reports_capability():
    adapter = DemoHermesAdapter()
    events: list[HermesEvent] = []

    reply = adapter.ask("打开客厅灯", on_event=events.append)

    assert "打开客厅灯" in reply
    assert [event.type for event in events] == ["message.start", "message.delta", "message.complete"]
    assert adapter.health() == {"ok": True, "service": "hermes", "mode": "demo", "streaming": True}


@pytest.mark.parametrize("command", ["", "   \t"])
def test_wsl_hermes_rejects_empty_command(command):
    adapter = WslHermesAdapter("Ubuntu-22.04", command)

    with pytest.raises(RuntimeError, match="HERMES_COMMAND is empty"):
        adapter.ask("hello")


def test_wsl_gateway_starts_hidden_session_and_streams_events(monkeypatch):
    captured = {}
    process = FakeProcess(
        [
            ("message.start", {}),
            ("tool.start", {"name": "terminal", "context": "Running probe"}),
            ("tool.complete", {"name": "terminal", "summary": "Finished probe"}),
            ("message.delta", {"text": "probe "}),
            ("message.delta", {"text": "complete"}),
            ("message.complete", {"text": "probe complete", "status": "complete"}),
        ]
    )

    def fake_popen(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return process

    monkeypatch.setattr("espagent_demo.hermes_adapter.subprocess.Popen", fake_popen)
    adapter = WslHermesAdapter(
        "ESPHermes",
        "/home/mun/.hermes/hermes-agent/venv/bin/hermes -z",
        timeout_seconds=5,
    )
    events: list[HermesEvent] = []

    adapter.start()
    reply = adapter.ask("run probe", on_event=events.append)

    assert reply == "probe complete"
    assert [event.type for event in events] == [
        "message.start",
        "tool.start",
        "tool.complete",
        "message.delta",
        "message.delta",
        "message.complete",
    ]
    assert captured["argv"][:6] == ["wsl.exe", "-d", "ESPHermes", "--exec", "bash", "-lc"]
    assert "venv/bin/python -u -m espagent_demo.hermes_gateway_bridge" in captured["argv"][6]
    assert "espagent-light-strip" in captured["argv"][6]
    assert "ESPAGENT_API_URL=http://127.0.0.1:8787" in captured["argv"][6]
    assert captured["kwargs"]["creationflags"] != 0
    assert adapter.health()["transport"] == "stdio-jsonrpc"
    assert adapter.health()["ok"] is True
    assert [write["method"] for write in process.stdin.writes] == ["session.create", "prompt.submit"]

    assert adapter.interrupt() == "interrupted"

    assert adapter.new_session() == "session-2"
    assert adapter.health()["session_id"] == "session-2"
    assert [write["method"] for write in process.stdin.writes] == [
        "session.create",
        "prompt.submit",
        "session.interrupt",
        "session.create",
    ]

    adapter.close()
    assert process.running is False


def test_wsl_gateway_rejects_empty_final_reply(monkeypatch):
    process = FakeProcess([("message.complete", {"text": "  ", "status": "complete"})])
    monkeypatch.setattr("espagent_demo.hermes_adapter.subprocess.Popen", lambda *args, **kwargs: process)
    adapter = WslHermesAdapter("ESPHermes", "/opt/hermes/venv/bin/hermes -z", timeout_seconds=2)

    with pytest.raises(RuntimeError, match="empty reply"):
        adapter.ask("hello")

    adapter.close()


def test_wsl_gateway_routes_voice_events_separately_and_controls_recording(monkeypatch):
    process = FakeProcess([])
    monkeypatch.setattr("espagent_demo.hermes_adapter.subprocess.Popen", lambda *args, **kwargs: process)
    adapter = WslHermesAdapter("ESPHermes", "/opt/hermes/venv/bin/hermes -z", timeout_seconds=2)

    adapter.start()
    assert adapter.voice_status()["available"] is True
    assert adapter.enable_voice()["enabled"] is True
    assert adapter.start_voice_recording() == {"status": "recording"}
    process.stdout.put(
        {
            "jsonrpc": "2.0",
            "method": "event",
            "params": {
                "type": "voice.transcript",
                "session_id": "session-1",
                "payload": {"text": "打开客厅灯"},
            },
        }
    )

    event = adapter.wait_voice_event(timeout=1)
    assert event == HermesEvent(
        "voice.transcript",
        {"text": "打开客厅灯", "session_id": "session-1"},
    )
    assert adapter.stop_voice_recording() == {"status": "stopped"}
    assert [write["method"] for write in process.stdin.writes] == [
        "session.create",
        "voice.toggle",
        "voice.toggle",
        "voice.record",
        "voice.record",
    ]

    adapter.close()


def test_wsl_gateway_transcribes_windows_recording_through_bridge(monkeypatch, tmp_path):
    process = FakeProcess([])
    monkeypatch.setattr("espagent_demo.hermes_adapter.subprocess.Popen", lambda *args, **kwargs: process)
    adapter = WslHermesAdapter(
        "ESPHermes",
        "/opt/hermes/venv/bin/hermes -z",
        timeout_seconds=2,
        stt_timeout_seconds=420,
    )
    recording = tmp_path / "voice.wav"
    recording.write_bytes(b"RIFF")

    assert adapter.transcribe_voice_recording(recording) == {
        "transcript": "native voice",
        "filtered": False,
    }
    request = process.stdin.writes[-1]
    assert request["method"] == "espagent.voice.transcribe"
    assert request["params"]["path"].endswith("/voice.wav")
    adapter.prewarm_voice()
    assert process.stdin.writes[-1]["method"] == "espagent.voice.prewarm"
    adapter.close()


def test_wsl_voice_transcription_timeout_does_not_close_gateway(monkeypatch, tmp_path):
    gateway = FakeProcess([])
    gateway.stt_respond = False
    monkeypatch.setattr("espagent_demo.hermes_adapter.subprocess.Popen", lambda *args, **kwargs: gateway)
    adapter = WslHermesAdapter(
        "ESPHermes",
        "/opt/hermes/venv/bin/hermes -z",
        timeout_seconds=2,
        stt_timeout_seconds=30,
    )
    adapter.stt_timeout_seconds = 0.05
    recording = tmp_path / "voice.wav"
    recording.write_bytes(b"RIFF")
    adapter.start()

    with pytest.raises(TimeoutError, match="espagent.voice.transcribe"):
        adapter.transcribe_voice_recording(recording)

    assert adapter.health()["ok"] is True
    assert adapter.voice_status()["available"] is True
    adapter.close()


def test_wsl_gateway_persists_adjustable_voice_silence_duration(monkeypatch):
    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout="configured", stderr="")

    monkeypatch.setattr("espagent_demo.hermes_adapter.subprocess.run", fake_run)
    adapter = WslHermesAdapter(
        "ESPHermes",
        "/home/mun/.hermes/hermes-agent/venv/bin/hermes -z",
    )

    adapter.set_voice_silence_duration(2.0)
    adapter.set_voice_silence_duration(2.0)
    adapter.set_voice_silence_duration(1.5)

    assert [call[0][-2:] for call in calls] == [
        ["voice.silence_duration", "2"],
        ["voice.silence_duration", "1.5"],
    ]
    assert calls[0][0][:5] == [
        "wsl.exe",
        "-d",
        "ESPHermes",
        "--exec",
        "/home/mun/.hermes/hermes-agent/venv/bin/hermes",
    ]
    assert calls[0][1]["creationflags"] != 0


def test_wsl_gateway_requires_virtualenv_command():
    adapter = WslHermesAdapter("ESPHermes", "hermes -z")

    with pytest.raises(RuntimeError, match="virtualenv executable"):
        adapter.start()
