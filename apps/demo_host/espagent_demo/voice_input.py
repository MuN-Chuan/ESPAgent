from __future__ import annotations

import logging
import math
import os
import queue
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from espagent_demo.hermes_adapter import HermesEvent
from espagent_demo.native_audio import WindowsMicrophoneRecorder
from espagent_demo.state import DemoState


logger = logging.getLogger(__name__)


class VoiceHermesAdapter(Protocol):
    def voice_status(self) -> dict[str, object]: ...

    def enable_voice(self, enabled: bool = True) -> dict[str, object]: ...

    def start_voice_recording(self) -> dict[str, object]: ...

    def set_voice_silence_duration(self, seconds: float) -> None: ...

    def stop_voice_recording(self) -> dict[str, object]: ...

    def transcribe_voice_recording(self, path: Path) -> dict[str, object]: ...

    def wait_voice_event(self, timeout: float = 0.5) -> HermesEvent | None: ...


class HermesVoiceController:
    """Turns Hermes microphone events into normal Hermes text turns."""

    def __init__(
        self,
        state: DemoState,
        adapter: VoiceHermesAdapter,
        on_transcript: Callable[[str], object],
        silence_duration_seconds: float = 2.0,
        native_recorder: WindowsMicrophoneRecorder | None = None,
    ) -> None:
        self.state = state
        self.adapter = adapter
        self.on_transcript = on_transcript
        self._closed = threading.Event()
        self._listener: threading.Thread | None = None
        self._worker: threading.Thread | None = None
        self._transcripts: queue.Queue[str | None] = queue.Queue()
        self._control_lock = threading.Lock()
        self._destination_lock = threading.Lock()
        self._transcript_destination = "submit"
        self._draft_recording_started = False
        self._draft_idle_timer: threading.Timer | None = None
        self._draft_idle_lock = threading.Lock()
        self._silence_duration_seconds = _validate_silence_duration(silence_duration_seconds)
        self._native_recorder = native_recorder
        self._native_recording = False
        self._recording_started_at: float | None = None
        self._native_enabled = False
        if (
            self._native_recorder is None
            and os.name == "nt"
            and callable(getattr(adapter, "transcribe_voice_recording", None))
        ):
            self._native_recorder = WindowsMicrophoneRecorder()
        with self.state.locked():
            self.state.hermes.voice_supported = True
            self.state.hermes.voice_silence_seconds = self._silence_duration_seconds

    def start(self) -> None:
        if self._listener is not None and self._listener.is_alive():
            return
        self._closed.clear()
        self._listener = threading.Thread(
            target=self._listen,
            name="hermes-voice-events",
            daemon=True,
        )
        self._worker = threading.Thread(
            target=self._submit_transcripts,
            name="hermes-voice-turns",
            daemon=True,
        )
        self._listener.start()
        self._worker.start()
        prewarm = getattr(self.adapter, "prewarm_voice", None)
        if callable(prewarm):
            threading.Thread(
                target=self._prewarm_voice,
                args=(prewarm,),
                name="hermes-stt-prewarm",
                daemon=True,
            ).start()

    @staticmethod
    def _prewarm_voice(prewarm: Callable[[], object]) -> None:
        started_at = time.monotonic()
        logger.info("voice_stt_prewarm_start")
        try:
            prewarm()
        except Exception:
            logger.exception(
                "voice_stt_prewarm_failed elapsed_ms=%d",
                round((time.monotonic() - started_at) * 1000),
            )
        else:
            logger.info(
                "voice_stt_prewarm_done elapsed_ms=%d",
                round((time.monotonic() - started_at) * 1000),
            )

    def refresh_status(self) -> dict[str, object]:
        try:
            result = self._with_native_capabilities(self.adapter.voice_status())
            self._apply_capabilities(result)
            return self.snapshot()
        except Exception as exc:
            self._set_error(exc)
            raise

    def set_enabled(self, enabled: bool) -> dict[str, object]:
        with self._control_lock:
            try:
                if not enabled and self._native_recorder is not None:
                    self._native_recorder.cancel()
                    self._native_recording = False
                if self._native_recorder is not None:
                    self._native_enabled = enabled
                    result = self._with_native_capabilities(self.adapter.voice_status())
                else:
                    result = self.adapter.enable_voice(enabled)
                    # Hermes' voice.toggle on/off response only contains the
                    # runtime flags. Refresh status after enabling so the UI
                    # also receives the complete capability state.
                    if enabled:
                        result = {
                            **result,
                            **self._with_native_capabilities(self.adapter.voice_status()),
                        }
                self._apply_capabilities(result)
                with self.state.locked():
                    voice = self.state.hermes
                    voice.voice_enabled = bool(result.get("enabled", enabled))
                    if not voice.voice_enabled:
                        voice.voice_status = "disabled"
                    elif voice.voice_available:
                        voice.voice_status = "ready"
                    else:
                        voice.voice_status = "unavailable"
                    voice.voice_error = None
                return self.snapshot()
            except Exception as exc:
                self._set_error(exc)
                raise

    def start_recording(self, *, destination: str = "submit") -> dict[str, object]:
        if destination not in {"submit", "draft"}:
            raise ValueError("voice transcript destination must be submit or draft")
        with self._control_lock:
            try:
                self._cancel_draft_idle_timer()
                with self._destination_lock:
                    self._transcript_destination = destination
                    self._draft_recording_started = False
                if self._native_recorder is None:
                    self.adapter.set_voice_silence_duration(self._silence_duration_seconds)
                if self._native_recorder is not None:
                    voice = self.state.snapshot().hermes
                    status = {
                        "enabled": self._native_enabled,
                        "available": voice.voice_available,
                        "audio_available": voice.voice_audio_available,
                        "stt_available": voice.voice_stt_available,
                        "details": voice.voice_details,
                    }
                    if not voice.voice_stt_available:
                        status = self._with_native_capabilities(self.adapter.voice_status())
                        self._apply_capabilities(status)
                else:
                    status = self._with_native_capabilities(self.adapter.voice_status())
                    self._apply_capabilities(status)
                if not bool(status.get("available")):
                    details = str(status.get("details") or "Hermes microphone is unavailable")
                    raise RuntimeError(details)
                if not bool(status.get("enabled")):
                    if self._native_recorder is not None:
                        self._native_enabled = True
                        status["enabled"] = True
                        self._apply_capabilities(status)
                    else:
                        self._apply_capabilities(self.adapter.enable_voice(True))
                if self._native_recorder is not None:
                    self._native_recorder.start()
                    self._native_recording = True
                    self._recording_started_at = time.monotonic()
                    result = {"status": "recording"}
                else:
                    result = self.adapter.start_voice_recording()
                    self._recording_started_at = time.monotonic()
                logger.info(
                    "voice_record_start destination=%s capture_backend=%s",
                    destination,
                    "windows" if self._native_recorder is not None else "hermes",
                )
                with self._destination_lock:
                    self._draft_recording_started = destination == "draft"
                state = str(result.get("status") or "recording")
                with self.state.locked():
                    voice = self.state.hermes
                    voice.voice_enabled = True
                    voice.voice_status = "listening" if state in {"recording", "busy"} else state
                    voice.voice_error = None
                    voice.voice_transcript = ""
                    if destination == "draft":
                        voice.input_active = True
                        voice.input_voice_active = True
                        voice.input_error = None
                return self.snapshot()
            except Exception as exc:
                self._set_error(exc)
                raise

    def set_silence_duration(self, seconds: float) -> dict[str, object]:
        seconds = _validate_silence_duration(seconds)
        with self._control_lock:
            try:
                self.adapter.set_voice_silence_duration(seconds)
                self._silence_duration_seconds = seconds
                with self.state.locked():
                    self.state.hermes.voice_silence_seconds = seconds
                    self.state.hermes.voice_error = None
                return self.snapshot()
            except Exception as exc:
                self._set_error(exc)
                raise

    def stop_recording(self) -> dict[str, object]:
        with self._control_lock:
            try:
                if self._native_recorder is not None and self._native_recording:
                    path = self._native_recorder.stop()
                    self._native_recording = False
                    stopped_at = time.monotonic()
                    duration_ms = _elapsed_ms(self._recording_started_at, stopped_at)
                    self._recording_started_at = None
                    logger.info(
                        "voice_record_stop capture_backend=windows duration_ms=%d bytes=%d file=%s",
                        duration_ms,
                        _file_size(path),
                        path.name,
                    )
                    with self.state.locked():
                        self.state.hermes.voice_status = "transcribing"
                        self.state.hermes.voice_error = None
                    threading.Thread(
                        target=self._transcribe_native_recording,
                        args=(path, stopped_at),
                        name="hermes-native-stt",
                        daemon=True,
                    ).start()
                else:
                    self.adapter.stop_voice_recording()
                    logger.info(
                        "voice_record_stop capture_backend=hermes duration_ms=%d",
                        _elapsed_ms(self._recording_started_at),
                    )
                    self._recording_started_at = None
                    with self.state.locked():
                        self.state.hermes.voice_status = "transcribing"
                        self.state.hermes.voice_error = None
                return self.snapshot()
            except Exception as exc:
                self._set_error(exc)
                raise

    def snapshot(self) -> dict[str, object]:
        voice = self.state.snapshot().hermes
        return {
            "enabled": voice.voice_enabled,
            "supported": voice.voice_supported,
            "available": voice.voice_available,
            "audio_available": voice.voice_audio_available,
            "stt_available": voice.voice_stt_available,
            "status": voice.voice_status,
            "transcript": voice.voice_transcript,
            "error": voice.voice_error,
            "details": voice.voice_details,
            "silence_duration_seconds": voice.voice_silence_seconds,
        }

    def close(self) -> None:
        self._closed.set()
        self._cancel_draft_idle_timer()
        if self._native_recorder is not None:
            self._native_recorder.cancel()
            self._native_recording = False
        self._transcripts.put(None)
        for thread in (self._listener, self._worker):
            if thread is not None and thread is not threading.current_thread():
                thread.join(timeout=2.0)
        self._listener = None
        self._worker = None

    def _listen(self) -> None:
        while not self._closed.is_set():
            try:
                event = self.adapter.wait_voice_event(timeout=0.5)
            except Exception as exc:
                if not self._closed.is_set():
                    self._set_error(exc)
                continue
            if event is None:
                continue
            self._handle_event(event)

    def _handle_event(self, event: HermesEvent) -> None:
        if event.type == "voice.status":
            status = str(event.payload.get("state") or "idle")
            with self._destination_lock:
                draft_ending = (
                    status == "idle"
                    and self._transcript_destination == "draft"
                    and self._draft_recording_started
                )
            with self.state.locked():
                self.state.hermes.voice_status = (
                    "transcribing" if draft_ending else ("ready" if status == "idle" else status)
                )
                self.state.hermes.voice_error = None
            if draft_ending:
                self._schedule_draft_idle_completion()
            return
        if event.type == "voice.transcript":
            self._cancel_draft_idle_timer()
            transcript = str(event.payload.get("text") or "").strip()
            no_speech = bool(event.payload.get("no_speech_limit"))
            self._deliver_transcript(transcript, no_speech=no_speech)
            return
        if event.type == "error":
            self._set_error(RuntimeError(str(event.payload.get("message") or "Hermes voice error")))

    def _submit_transcripts(self) -> None:
        while not self._closed.is_set():
            transcript = self._transcripts.get()
            if transcript is None:
                return
            with self.state.locked():
                self.state.hermes.voice_status = "submitting"
            try:
                self.on_transcript(transcript)
            except Exception as exc:
                logger.exception("Hermes voice transcript submission failed")
                self._set_error(exc)
            else:
                with self.state.locked():
                    if self.state.hermes.voice_status == "submitting":
                        self.state.hermes.voice_status = "ready"

    def _apply_capabilities(self, result: dict[str, object]) -> None:
        with self.state.locked():
            voice = self.state.hermes
            if "enabled" in result:
                voice.voice_enabled = bool(result.get("enabled"))
            if "available" in result:
                voice.voice_available = bool(result.get("available"))
            if "audio_available" in result:
                voice.voice_audio_available = bool(result.get("audio_available"))
            if "stt_available" in result:
                voice.voice_stt_available = bool(result.get("stt_available"))
            if "details" in result:
                voice.voice_details = str(result.get("details") or "")
            voice.voice_error = None

    def _with_native_capabilities(self, result: dict[str, object]) -> dict[str, object]:
        recorder = self._native_recorder
        if recorder is None:
            return result
        probe = recorder.probe()
        native_available = bool(probe.get("available"))
        stt_available = bool(result.get("stt_available"))
        original_details = str(result.get("details") or "")
        stt_details = "\n".join(
            line
            for line in original_details.splitlines()
            if line.startswith("STT provider:")
        )
        device = str(probe.get("device") or "Windows default microphone")
        audio_details = (
            f"Audio capture: OK (Windows: {device})"
            if native_available
            else str(probe.get("details") or "Windows microphone is unavailable")
        )
        return {
            **result,
            "enabled": self._native_enabled,
            "available": native_available and stt_available,
            "audio_available": native_available,
            "details": "\n".join(part for part in (audio_details, stt_details) if part),
            "capture_backend": "windows",
        }

    def _transcribe_native_recording(
        self,
        path: Path,
        queued_at: float | None = None,
    ) -> None:
        started_at = time.monotonic()
        logger.info(
            "voice_stt_start queue_ms=%d bytes=%d file=%s",
            _elapsed_ms(queued_at, started_at),
            _file_size(path),
            path.name,
        )
        try:
            transcribe = getattr(self.adapter, "transcribe_voice_recording")
            result = transcribe(path)
            transcript = str(result.get("transcript") or "").strip()
            logger.info(
                "voice_stt_done elapsed_ms=%d provider=%s chars=%d filtered=%s",
                round((time.monotonic() - started_at) * 1000),
                str(result.get("provider") or "unknown"),
                len(transcript),
                bool(result.get("filtered")) or not transcript,
            )
            self._deliver_transcript(
                transcript,
                no_speech=bool(result.get("filtered")) or not transcript,
            )
        except Exception as exc:
            logger.exception(
                "voice_stt_failed elapsed_ms=%d error_type=%s",
                round((time.monotonic() - started_at) * 1000),
                exc.__class__.__name__,
            )
            self._set_error(exc)
        finally:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                logger.warning("Could not remove temporary voice recording: %s", path)

    def _deliver_transcript(self, transcript: str, *, no_speech: bool = False) -> None:
        self._cancel_draft_idle_timer()
        with self._destination_lock:
            destination = self._transcript_destination
            self._transcript_destination = "submit"
            self._draft_recording_started = False
        draft_destination = destination in {"draft", "draft_waiting"}
        with self.state.locked():
            voice = self.state.hermes
            voice.voice_status = "no_speech" if no_speech or not transcript else "recognized"
            voice.voice_transcript = transcript
            voice.voice_error = None
            if draft_destination:
                if transcript:
                    caret = max(0, min(voice.input_caret, len(voice.input_draft)))
                    voice.input_draft = (
                        voice.input_draft[:caret] + transcript + voice.input_draft[caret:]
                    )[:240]
                    voice.input_caret = min(caret + len(transcript), len(voice.input_draft))
                voice.input_active = True
                voice.input_voice_active = False
                voice.input_voice_revision += 1
        if transcript and not draft_destination:
            self._transcripts.put(transcript)

    def _set_error(self, exc: Exception) -> None:
        if self._native_recorder is not None and self._native_recording:
            self._native_recorder.cancel()
            self._native_recording = False
        self._recording_started_at = None
        self._cancel_draft_idle_timer()
        with self._destination_lock:
            destination = self._transcript_destination
            self._transcript_destination = "submit"
            self._draft_recording_started = False
        with self.state.locked():
            voice = self.state.hermes
            voice.voice_status = "failed"
            voice.voice_error = _voice_error_message(exc)
            if destination in {"draft", "draft_waiting"}:
                voice.input_active = True
                voice.input_voice_active = False
                voice.input_error = "语音输入失败"
                voice.input_voice_revision += 1

    def _schedule_draft_idle_completion(self) -> None:
        self._cancel_draft_idle_timer()
        timer = threading.Timer(0.75, self._complete_draft_without_transcript)
        timer.daemon = True
        with self._draft_idle_lock:
            self._draft_idle_timer = timer
        timer.start()

    def _cancel_draft_idle_timer(self) -> None:
        with self._draft_idle_lock:
            timer = self._draft_idle_timer
            self._draft_idle_timer = None
        if timer is not None and timer is not threading.current_thread():
            timer.cancel()

    def _complete_draft_without_transcript(self) -> None:
        with self._draft_idle_lock:
            self._draft_idle_timer = None
        with self._destination_lock:
            if self._transcript_destination != "draft" or not self._draft_recording_started:
                return
            self._transcript_destination = "draft_waiting"
            self._draft_recording_started = False
        with self.state.locked():
            voice = self.state.hermes
            voice.voice_status = "no_speech"
            voice.voice_transcript = ""
            voice.input_active = True
            voice.input_voice_active = False
            voice.input_voice_revision += 1


def _validate_silence_duration(seconds: float) -> float:
    value = float(seconds)
    if not math.isfinite(value) or not 0.5 <= value <= 10.0:
        raise ValueError("voice silence duration must be between 0.5 and 10 seconds")
    return value


def _elapsed_ms(started_at: float | None, ended_at: float | None = None) -> int:
    if started_at is None:
        return 0
    end = time.monotonic() if ended_at is None else ended_at
    return max(0, round((end - started_at) * 1000))


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return -1


def _voice_error_message(exc: Exception) -> str:
    message = " ".join(str(exc).split())
    if not message:
        message = exc.__class__.__name__
    return f"Voice input failed: {message[:500]}"
