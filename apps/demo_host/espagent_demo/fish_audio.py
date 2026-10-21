from __future__ import annotations

import logging
import queue
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import uuid4

import msgpack
import websocket

from espagent_demo.hermes_adapter import HermesEvent
from espagent_demo.state import DemoState


logger = logging.getLogger(__name__)
FISHAUDIO_SUBPROTOCOL = "realtime.tts.msgpack.v1"


@dataclass(frozen=True)
class FishAudioConfig:
    enabled: bool
    api_key: str
    voice_id: str
    realtime_url: str = "wss://realtime.fishaudio.org/v1/tts/live"
    sample_rate: int = 44100
    speed: float = 1.0
    volume: float = 1.0
    language: str = "zh"
    chunk_length: int = 200
    latency: str = "balanced"
    connect_timeout_seconds: float = 20.0
    finish_timeout_seconds: float = 120.0

    @property
    def configured(self) -> bool:
        return self.enabled and bool(self.api_key.strip()) and bool(self.voice_id.strip())


class SpeakableReplyBuffer:
    """Incrementally extracts speakable sentences from assistant reply deltas."""

    _strong_boundaries = frozenset("。！？!?；;\n")
    _soft_boundaries = frozenset("，,：:")
    _markdown_link = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
    _url = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
    _html = re.compile(r"<[^>]+>")
    _line_prefix = re.compile(r"(?m)^\s{0,3}(?:#{1,6}\s+|[-+*]\s+|\d+[.)]\s+|>\s*)")
    _whitespace = re.compile(r"\s+")

    def __init__(self, *, min_chars: int = 8, max_chars: int = 120) -> None:
        self.min_chars = max(1, min_chars)
        self.max_chars = max(self.min_chars, max_chars)
        self._buffer = ""
        self._in_code_fence = False
        self._in_inline_code = False
        self._pending_backticks = 0

    def feed(self, delta: str) -> list[str]:
        for char in delta:
            if char == "`":
                self._pending_backticks += 1
                continue
            self._flush_backticks()
            if not self._in_code_fence and not self._in_inline_code:
                self._buffer += char
        return self._extract(force=False)

    def finish(self) -> list[str]:
        self._pending_backticks = 0
        return self._extract(force=True)

    def reset(self) -> None:
        self._buffer = ""
        self._in_code_fence = False
        self._in_inline_code = False
        self._pending_backticks = 0

    def _flush_backticks(self) -> None:
        ticks = self._pending_backticks
        self._pending_backticks = 0
        while ticks >= 3:
            self._in_code_fence = not self._in_code_fence
            self._in_inline_code = False
            ticks -= 3
        if ticks == 1 and not self._in_code_fence:
            self._in_inline_code = not self._in_inline_code

    def _extract(self, *, force: bool) -> list[str]:
        sentences: list[str] = []
        consumed = 0
        for index, char in enumerate(self._buffer):
            if not self._is_boundary(index, char):
                continue
            candidate = self._clean(self._buffer[consumed : index + 1])
            if len(candidate) >= self.min_chars:
                sentences.append(candidate)
                consumed = index + 1
        self._buffer = self._buffer[consumed:]

        while len(self._clean(self._buffer)) > self.max_chars:
            cut = self._find_soft_cut(self._buffer)
            candidate = self._clean(self._buffer[:cut])
            self._buffer = self._buffer[cut:]
            if candidate:
                sentences.append(candidate)

        if force:
            candidate = self._clean(self._buffer)
            self._buffer = ""
            if candidate:
                sentences.append(candidate)
        return sentences

    def _is_boundary(self, index: int, char: str) -> bool:
        if char in self._strong_boundaries:
            return True
        if char != ".":
            return False
        prefix = self._buffer[:index]
        if re.search(r"https?://[^\s)]*$", prefix, re.IGNORECASE):
            return False
        previous = self._buffer[index - 1] if index else ""
        following = self._buffer[index + 1] if index + 1 < len(self._buffer) else ""
        return not (previous.isdigit() and following.isdigit())

    def _find_soft_cut(self, text: str) -> int:
        limit = min(len(text), self.max_chars)
        floor = max(self.min_chars, limit // 2)
        for index in range(limit - 1, floor - 1, -1):
            if text[index] in self._soft_boundaries or text[index].isspace():
                return index + 1
        return limit

    @classmethod
    def _clean(cls, text: str) -> str:
        text = cls._markdown_link.sub(lambda match: match.group(1), text)
        text = cls._url.sub("", text)
        text = cls._html.sub("", text)
        text = cls._line_prefix.sub("", text)
        text = text.replace("|", " ")
        text = re.sub(r"[*_~#]", "", text)
        return cls._whitespace.sub(" ", text).strip()


class PcmPlayer(Protocol):
    def write(self, stream_id: int, data: bytes) -> None:
        ...

    def finish(self, stream_id: int) -> None:
        ...

    def interrupt(self, stream_id: int | None = None) -> None:
        ...


class SoundDevicePcmPlayer:
    """Streams signed 16-bit mono PCM to the current Windows output device."""

    def __init__(self, sample_rate: int = 44100) -> None:
        self.sample_rate = sample_rate
        self._lock = threading.Lock()
        self._stream: Any | None = None
        self._stream_id: int | None = None
        self._carry = b""

    def write(self, stream_id: int, data: bytes) -> None:
        if not data:
            return
        with self._lock:
            if self._stream_id not in {None, stream_id}:
                return
            if self._stream is None:
                import sounddevice as sd

                self._stream = sd.RawOutputStream(
                    samplerate=self.sample_rate,
                    channels=1,
                    dtype="int16",
                    latency="low",
                )
                self._stream.start()
                self._stream_id = stream_id
            payload = self._carry + bytes(data)
            even_length = len(payload) - len(payload) % 2
            self._carry = payload[even_length:]
            payload = payload[:even_length]
            stream = self._stream
        if not payload or stream is None:
            return
        try:
            stream.write(payload)
        except Exception:
            with self._lock:
                still_current = self._stream is stream and self._stream_id == stream_id
            if still_current:
                raise

    def finish(self, stream_id: int) -> None:
        stream = self._detach(stream_id)
        if stream is None:
            return
        try:
            stream.stop()
        finally:
            stream.close()

    def interrupt(self, stream_id: int | None = None) -> None:
        stream = self._detach(stream_id)
        if stream is None:
            return
        try:
            stream.abort()
        except Exception:
            logger.debug("Audio stream abort failed", exc_info=True)
        finally:
            try:
                stream.close()
            except Exception:
                logger.debug("Audio stream close failed", exc_info=True)

    def _detach(self, stream_id: int | None) -> Any | None:
        with self._lock:
            if stream_id is not None and self._stream_id not in {None, stream_id}:
                return None
            stream = self._stream
            self._stream = None
            self._stream_id = None
            self._carry = b""
            return stream


StatusCallback = Callable[[str, str | None], object]
UsageCallback = Callable[[int, int], object]
QueueDepthCallback = Callable[[int], object]


class SpeechSession(Protocol):
    def start(self) -> None:
        ...

    def send_text(self, text: str) -> None:
        ...

    def finish(self) -> None:
        ...

    def interrupt(self) -> None:
        ...

    def close(self) -> None:
        ...


class FishAudioRealtimeSession:
    def __init__(
        self,
        config: FishAudioConfig,
        player: PcmPlayer,
        stream_id: int,
        on_status: StatusCallback,
        on_usage: UsageCallback,
        on_queue_depth: QueueDepthCallback,
        *,
        connect_factory: Callable[..., Any] = websocket.create_connection,
    ) -> None:
        self.config = config
        self.player = player
        self.stream_id = stream_id
        self.on_status = on_status
        self.on_usage = on_usage
        self.on_queue_depth = on_queue_depth
        self._connect_factory = connect_factory
        self._commands: queue.Queue[tuple[str, str | None]] = queue.Queue()
        self._cancelled = threading.Event()
        self._thread: threading.Thread | None = None
        self._ws_lock = threading.Lock()
        self._ws: Any | None = None

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run,
            name=f"fishaudio-{self.stream_id}",
            daemon=True,
        )
        self._thread.start()

    def send_text(self, text: str) -> None:
        if text and not self._cancelled.is_set():
            self._commands.put(("text", text))
            self.on_queue_depth(self._commands.qsize())

    def finish(self) -> None:
        if not self._cancelled.is_set():
            self._commands.put(("finish", None))
            self.on_queue_depth(self._commands.qsize())

    def interrupt(self) -> None:
        if self._cancelled.is_set():
            return
        self._cancelled.set()
        self._commands.put(("interrupt", None))
        self.player.interrupt(self.stream_id)
        with self._ws_lock:
            ws = self._ws
        if ws is not None:
            try:
                ws.close()
            except Exception:
                logger.debug("Fish Audio WebSocket close failed", exc_info=True)

    def close(self) -> None:
        self.interrupt()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)

    def _run(self) -> None:
        completed = False
        try:
            self.on_status("pending", None)
            ws = self._connect_factory(
                self.config.realtime_url,
                header=[f"Authorization: Bearer {self.config.api_key}"],
                subprotocols=[FISHAUDIO_SUBPROTOCOL],
                suppress_origin=True,
                timeout=self.config.connect_timeout_seconds,
            )
            with self._ws_lock:
                self._ws = ws
            self._wait_for(ws, "authenticated")
            request_id = f"espagent-{uuid4().hex}"
            self._send(
                ws,
                {
                    "event": "start",
                    "request": {
                        "model_id": self.config.voice_id,
                        "format": "pcm",
                        "speed": self.config.speed,
                        "volume": self.config.volume,
                        "language": self.config.language,
                        "chunk_length": self.config.chunk_length,
                        "latency": self.config.latency,
                    },
                    "request_id": request_id,
                },
            )
            self._wait_for(ws, "ready")
            ws.settimeout(0.1)
            finish_sent_at: float | None = None
            playing = False

            while not self._cancelled.is_set():
                while True:
                    try:
                        command, value = self._commands.get_nowait()
                    except queue.Empty:
                        break
                    self.on_queue_depth(self._commands.qsize())
                    if command == "interrupt":
                        return
                    if command == "text" and value:
                        self.on_status("generating", None)
                        self._send(ws, {"event": "text", "text": value, "request_id": request_id})
                        self._send(ws, {"event": "flush", "request_id": request_id})
                    elif command == "finish" and finish_sent_at is None:
                        self._send(ws, {"event": "stop", "request_id": request_id})
                        finish_sent_at = time.monotonic()

                if finish_sent_at is not None and time.monotonic() - finish_sent_at > self.config.finish_timeout_seconds:
                    raise TimeoutError("Fish Audio did not finish the realtime stream")

                try:
                    message = self._receive(ws)
                except websocket.WebSocketTimeoutException:
                    continue
                if message is None:
                    if self._cancelled.is_set():
                        return
                    raise ConnectionError("Fish Audio realtime stream closed unexpectedly")
                event = str(message.get("event") or "")
                if event == "audio":
                    audio = message.get("data")
                    if isinstance(audio, bytes):
                        self.player.write(self.stream_id, audio)
                        if not playing:
                            playing = True
                            self.on_status("playing", None)
                elif event == "usage":
                    self.on_usage(int(message.get("units") or 0), int(message.get("audio_bytes") or 0))
                elif event == "warning":
                    logger.warning("Fish Audio warning: %s", message.get("code") or "unknown")
                elif event == "error":
                    raise RuntimeError(f"Fish Audio error: {message.get('code') or 'unknown'}")
                elif event == "finish":
                    completed = True
                    self.player.finish(self.stream_id)
                    self.on_status("ready", None)
                    return
        except Exception as exc:
            self.player.interrupt(self.stream_id)
            if not self._cancelled.is_set():
                logger.exception("Fish Audio realtime session failed")
                self.on_status("failed", exc.__class__.__name__)
        finally:
            with self._ws_lock:
                ws = self._ws
                self._ws = None
            if ws is not None:
                try:
                    ws.close()
                except Exception:
                    logger.debug("Fish Audio WebSocket final close failed", exc_info=True)
            if self._cancelled.is_set() and not completed:
                self.on_status("interrupted", None)
            self.on_queue_depth(0)

    def _wait_for(self, ws: Any, expected: str) -> dict[str, Any]:
        deadline = time.monotonic() + self.config.connect_timeout_seconds
        while not self._cancelled.is_set():
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Fish Audio did not send {expected}")
            message = self._receive(ws)
            if message is None:
                raise ConnectionError("Fish Audio realtime stream closed during setup")
            event = str(message.get("event") or "")
            if event == "error":
                raise RuntimeError(f"Fish Audio error: {message.get('code') or 'unknown'}")
            if event == expected:
                return message
        raise InterruptedError("Fish Audio setup interrupted")

    @staticmethod
    def _send(ws: Any, payload: dict[str, object]) -> None:
        ws.send_binary(msgpack.packb(payload, use_bin_type=True))

    @staticmethod
    def _receive(ws: Any) -> dict[str, Any] | None:
        raw = ws.recv()
        if raw in {None, b"", ""}:
            return None
        if not isinstance(raw, bytes):
            raise TypeError("Fish Audio sent a non-binary WebSocket frame")
        message = msgpack.unpackb(raw, raw=False)
        if not isinstance(message, dict):
            raise TypeError("Fish Audio sent an invalid MessagePack event")
        return message


SessionFactory = Callable[[int, StatusCallback, UsageCallback, QueueDepthCallback], SpeechSession]


class HermesSpeechController:
    """Routes only actual Hermes assistant reply events into Fish Audio."""

    def __init__(
        self,
        state: DemoState,
        config: FishAudioConfig,
        *,
        session_factory: SessionFactory | None = None,
        player: PcmPlayer | None = None,
    ) -> None:
        self.state = state
        self.config = config
        self.enabled = config.configured
        self._lock = threading.RLock()
        self._stream_id = 0
        self._session: SpeechSession | None = None
        self._buffer = SpeakableReplyBuffer()
        self._raw_reply = ""
        self._accepting_events = False
        self._player = player or SoundDevicePcmPlayer(config.sample_rate)
        self._session_factory = session_factory or self._create_session
        with self.state.locked():
            self.state.hermes.audio_status = "ready" if self.enabled else "disabled"
            if config.enabled and not self.enabled:
                self.state.hermes.audio_error = "missing_configuration"

    def handle_hermes_event(self, event: HermesEvent) -> None:
        if not self.enabled:
            return
        if event.type == "message.start":
            self.start_turn()
            return
        if event.type == "message.delta":
            delta = str(event.payload.get("text") or "")
            if not delta:
                return
            with self._lock:
                if not self._accepting_events or self._session is None:
                    return
                self._raw_reply += delta
                sentences = self._buffer.feed(delta)
                session = self._session
            if session is not None:
                for sentence in sentences:
                    session.send_text(sentence)
            return
        if event.type == "message.complete":
            final_text = str(event.payload.get("text") or "")
            with self._lock:
                if not self._accepting_events or self._session is None:
                    return
                if not self._raw_reply:
                    sentences = self._buffer.feed(final_text)
                elif final_text.startswith(self._raw_reply):
                    sentences = self._buffer.feed(final_text[len(self._raw_reply) :])
                else:
                    sentences = []
                sentences.extend(self._buffer.finish())
                session = self._session
                self._accepting_events = False
            if session is not None:
                for sentence in sentences:
                    session.send_text(sentence)
                session.finish()
            return
        if event.type == "error":
            self.interrupt()

    def start_turn(self) -> int:
        self.interrupt(set_status=False)
        with self._lock:
            return self._start_turn_locked()

    def interrupt(self, *, set_status: bool = True) -> None:
        with self._lock:
            session = self._session
            self._session = None
            self._stream_id += 1
            stream_id = self._stream_id
            self._buffer.reset()
            self._raw_reply = ""
            self._accepting_events = False
        if session is not None:
            session.interrupt()
        else:
            self._player.interrupt()
        with self.state.locked():
            self.state.hermes.audio_stream_id = stream_id
            self.state.hermes.audio_queue_depth = 0
            if set_status:
                self.state.hermes.audio_status = "interrupted" if self.enabled else "disabled"

    def close(self) -> None:
        with self._lock:
            session = self._session
            self._session = None
            self._stream_id += 1
            self._accepting_events = False
        if session is not None:
            session.close()
        self._player.interrupt()
        with self.state.locked():
            self.state.hermes.audio_status = "disabled"
            self.state.hermes.audio_queue_depth = 0

    def health(self) -> dict[str, object]:
        snapshot = self.state.snapshot().hermes
        return {
            "ok": self.enabled and snapshot.audio_status != "failed",
            "service": "fish_audio",
            "mode": "realtime-websocket" if self.enabled else "disabled",
            "voice_id": self.config.voice_id or None,
            "status": snapshot.audio_status,
            "stream_id": snapshot.audio_stream_id,
            "queue_depth": snapshot.audio_queue_depth,
            "reason": snapshot.audio_error,
        }

    def _start_turn_locked(self) -> int:
        self._stream_id += 1
        stream_id = self._stream_id
        self._buffer.reset()
        self._raw_reply = ""
        self._accepting_events = True
        session = self._session_factory(
            stream_id,
            lambda status, error: self._on_status(stream_id, status, error),
            lambda units, audio_bytes: self._on_usage(stream_id, units, audio_bytes),
            lambda depth: self._on_queue_depth(stream_id, depth),
        )
        self._session = session
        with self.state.locked():
            hermes = self.state.hermes
            hermes.audio_stream_id = stream_id
            hermes.audio_status = "pending"
            hermes.audio_queue_depth = 0
            hermes.audio_units = 0
            hermes.audio_bytes = 0
            hermes.audio_error = None
        session.start()
        return stream_id

    def _create_session(
        self,
        stream_id: int,
        on_status: StatusCallback,
        on_usage: UsageCallback,
        on_queue_depth: QueueDepthCallback,
    ) -> SpeechSession:
        return FishAudioRealtimeSession(
            self.config,
            self._player,
            stream_id,
            on_status,
            on_usage,
            on_queue_depth,
        )

    def _on_status(self, stream_id: int, status: str, error: str | None) -> None:
        with self._lock:
            if stream_id != self._stream_id:
                return
        with self.state.locked():
            self.state.hermes.audio_status = status
            self.state.hermes.audio_error = error

    def _on_usage(self, stream_id: int, units: int, audio_bytes: int) -> None:
        with self._lock:
            if stream_id != self._stream_id:
                return
        with self.state.locked():
            self.state.hermes.audio_units += units
            self.state.hermes.audio_bytes += audio_bytes

    def _on_queue_depth(self, stream_id: int, depth: int) -> None:
        with self._lock:
            if stream_id != self._stream_id:
                return
        with self.state.locked():
            self.state.hermes.audio_queue_depth = depth
