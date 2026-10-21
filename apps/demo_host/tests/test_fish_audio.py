from __future__ import annotations

from typing import Callable

import msgpack

from espagent_demo.fish_audio import (
    FishAudioConfig,
    FishAudioRealtimeSession,
    HermesSpeechController,
    SpeakableReplyBuffer,
)
from espagent_demo.hermes_adapter import HermesEvent
from espagent_demo.state import DemoState


class FakePlayer:
    def __init__(self) -> None:
        self.chunks: list[tuple[int, bytes]] = []
        self.finished: list[int] = []
        self.interrupted: list[int | None] = []

    def write(self, stream_id: int, data: bytes) -> None:
        self.chunks.append((stream_id, data))

    def finish(self, stream_id: int) -> None:
        self.finished.append(stream_id)

    def interrupt(self, stream_id: int | None = None) -> None:
        self.interrupted.append(stream_id)


class FakeSession:
    def __init__(
        self,
        stream_id: int,
        on_status: Callable[[str, str | None], object],
        on_usage: Callable[[int, int], object],
        on_queue_depth: Callable[[int], object],
    ) -> None:
        self.stream_id = stream_id
        self.on_status = on_status
        self.on_usage = on_usage
        self.on_queue_depth = on_queue_depth
        self.started = False
        self.sent: list[str] = []
        self.finished = False
        self.interrupted = False

    def start(self) -> None:
        self.started = True

    def send_text(self, text: str) -> None:
        self.sent.append(text)

    def finish(self) -> None:
        self.finished = True

    def interrupt(self) -> None:
        self.interrupted = True

    def close(self) -> None:
        self.interrupted = True


class FakeWebSocket:
    def __init__(self, messages: list[dict[str, object]]) -> None:
        self.messages = [msgpack.packb(message, use_bin_type=True) for message in messages]
        self.sent: list[dict[str, object]] = []
        self.closed = False

    def recv(self) -> bytes:
        return self.messages.pop(0) if self.messages else b""

    def send_binary(self, data: bytes) -> None:
        self.sent.append(msgpack.unpackb(data, raw=False))

    def settimeout(self, timeout: float) -> None:
        self.timeout = timeout

    def close(self) -> None:
        self.closed = True


def configured() -> FishAudioConfig:
    return FishAudioConfig(enabled=True, api_key="test-key", voice_id="test-voice")


def test_reply_buffer_streams_sentences_and_removes_non_speech_markup() -> None:
    buffer = SpeakableReplyBuffer()

    assert buffer.feed("这是第一句实际回复内容。") == ["这是第一句实际回复内容。"]
    assert buffer.feed("请查看[使用说明](https://example.com)。") == ["请查看使用说明。"]
    assert buffer.feed("```python\nprint('绝不能朗读')\n```继续给出实际结论。") == ["继续给出实际结论。"]
    assert buffer.finish() == []


def test_controller_only_speaks_message_delta_and_does_not_repeat_complete_text() -> None:
    state = DemoState()
    sessions: list[FakeSession] = []

    def factory(stream_id, on_status, on_usage, on_queue_depth):
        session = FakeSession(stream_id, on_status, on_usage, on_queue_depth)
        sessions.append(session)
        return session

    controller = HermesSpeechController(state, configured(), session_factory=factory, player=FakePlayer())
    controller.handle_hermes_event(HermesEvent("message.start", {}))
    controller.handle_hermes_event(HermesEvent("thinking.delta", {"text": "这是内部思考，不能朗读。"}))
    controller.handle_hermes_event(HermesEvent("reasoning.delta", {"text": "这是推理过程，不能朗读。"}))
    controller.handle_hermes_event(HermesEvent("tool.start", {"context": "工具进度不能朗读"}))
    controller.handle_hermes_event(HermesEvent("status.update", {"text": "临时状态不能朗读"}))
    first = "这是第一句实际回复内容。"
    second = "这是第二句实际回复内容。"
    controller.handle_hermes_event(HermesEvent("message.delta", {"text": first}))
    controller.handle_hermes_event(HermesEvent("message.delta", {"text": second}))
    controller.handle_hermes_event(HermesEvent("message.complete", {"text": first + second}))

    assert len(sessions) == 1
    assert sessions[0].started is True
    assert sessions[0].sent == [first, second]
    assert sessions[0].finished is True


def test_interrupt_invalidates_old_hermes_events_before_a_new_turn() -> None:
    state = DemoState()
    sessions: list[FakeSession] = []

    def factory(stream_id, on_status, on_usage, on_queue_depth):
        session = FakeSession(stream_id, on_status, on_usage, on_queue_depth)
        sessions.append(session)
        return session

    controller = HermesSpeechController(state, configured(), session_factory=factory, player=FakePlayer())
    controller.handle_hermes_event(HermesEvent("message.start", {}))
    controller.handle_hermes_event(HermesEvent("message.delta", {"text": "旧回复尚未完成"}))
    controller.interrupt()
    controller.handle_hermes_event(HermesEvent("message.delta", {"text": "，这段也不能再播放。"}))
    controller.handle_hermes_event(HermesEvent("message.complete", {"text": "旧回复尚未完成，这段也不能再播放。"}))

    controller.handle_hermes_event(HermesEvent("message.start", {}))
    controller.handle_hermes_event(HermesEvent("message.delta", {"text": "新回复可以正常播放。"}))
    controller.handle_hermes_event(HermesEvent("message.complete", {"text": "新回复可以正常播放。"}))

    assert len(sessions) == 2
    assert sessions[0].interrupted is True
    assert sessions[0].sent == []
    assert sessions[1].sent == ["新回复可以正常播放。"]
    assert state.hermes.audio_stream_id == sessions[1].stream_id


def test_realtime_session_uses_documented_msgpack_event_sequence() -> None:
    player = FakePlayer()
    ws = FakeWebSocket(
        [
            {"event": "authenticated"},
            {"event": "ready", "format": "pcm"},
            {"event": "audio", "data": b"\x00\x01"},
            {"event": "usage", "units": 2, "audio_bytes": 2},
            {"event": "finish", "reason": "completed"},
        ]
    )
    statuses: list[tuple[str, str | None]] = []
    usage: list[tuple[int, int]] = []
    session = FishAudioRealtimeSession(
        configured(),
        player,
        7,
        lambda status, error: statuses.append((status, error)),
        lambda units, audio_bytes: usage.append((units, audio_bytes)),
        lambda depth: None,
        connect_factory=lambda *args, **kwargs: ws,
    )
    session.send_text("这是需要合成的实际回复。")
    session.finish()
    session._run()

    assert [event["event"] for event in ws.sent] == ["start", "text", "flush", "stop"]
    assert ws.sent[0]["request"]["model_id"] == "test-voice"
    assert ws.sent[0]["request"]["format"] == "pcm"
    assert ws.sent[1]["text"] == "这是需要合成的实际回复。"
    assert player.chunks == [(7, b"\x00\x01")]
    assert player.finished == [7]
    assert usage == [(2, 2)]
    assert statuses[-1] == ("ready", None)
