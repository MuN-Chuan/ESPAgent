from __future__ import annotations

import tempfile
import threading
import wave
from pathlib import Path
from typing import Any

import numpy as np


SAMPLE_RATE = 16_000
CHANNELS = 1
MIN_RECORDING_SECONDS = 0.12
MIN_PEAK_LEVEL = 24


class WindowsMicrophoneRecorder:
    """Capture the Windows default microphone without relying on WSL audio."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stream: Any = None
        self._frames: list[np.ndarray] = []
        self._status_error: str | None = None

    def probe(self) -> dict[str, object]:
        try:
            import sounddevice as sd

            device = sd.query_devices(kind="input")
            channels = int(device.get("max_input_channels") or 0)
            if channels < 1:
                raise RuntimeError("Windows default recording device has no input channels")
            return {
                "available": True,
                "device": str(device.get("name") or "Windows default microphone"),
                "sample_rate": SAMPLE_RATE,
            }
        except Exception as exc:
            return {
                "available": False,
                "details": _audio_error_message(exc),
            }

    def start(self) -> None:
        with self._lock:
            if self._stream is not None:
                return
            probe = self.probe()
            if not bool(probe.get("available")):
                raise RuntimeError(str(probe.get("details") or "Windows microphone is unavailable"))

            import sounddevice as sd

            self._frames = []
            self._status_error = None

            def callback(
                indata: np.ndarray,
                _frames: int,
                _time_info: object,
                status: object,
            ) -> None:
                if status and self._status_error is None:
                    self._status_error = str(status)
                self._frames.append(indata.copy())

            try:
                stream = sd.InputStream(
                    samplerate=SAMPLE_RATE,
                    channels=CHANNELS,
                    dtype="int16",
                    callback=callback,
                )
                stream.start()
            except Exception as exc:
                self._frames = []
                raise RuntimeError(_audio_error_message(exc)) from exc
            self._stream = stream

    def stop(self) -> Path:
        with self._lock:
            stream = self._stream
            self._stream = None
        if stream is None:
            raise RuntimeError("Windows microphone recording is not active")
        try:
            stream.stop()
        finally:
            stream.close()

        with self._lock:
            frames = self._frames
            self._frames = []
            status_error = self._status_error
            self._status_error = None
        if not frames:
            detail = f": {status_error}" if status_error else ""
            raise RuntimeError(f"Windows microphone returned no audio samples{detail}")

        audio = np.concatenate(frames, axis=0).reshape(-1).astype(np.int16, copy=False)
        duration = audio.size / SAMPLE_RATE
        peak = int(np.max(np.abs(audio.astype(np.int32)))) if audio.size else 0
        if duration < MIN_RECORDING_SECONDS:
            raise RuntimeError("Recording is too short; hold the voice key while speaking")
        if peak < MIN_PEAK_LEVEL:
            raise RuntimeError(
                "Microphone signal is silent; check the selected Windows input device and privacy permission"
            )

        handle = tempfile.NamedTemporaryFile(
            prefix="espagent_voice_",
            suffix=".wav",
            delete=False,
        )
        handle.close()
        path = Path(handle.name)
        with wave.open(str(path), "wb") as wav:
            wav.setnchannels(CHANNELS)
            wav.setsampwidth(2)
            wav.setframerate(SAMPLE_RATE)
            wav.writeframes(audio.tobytes())
        return path

    def cancel(self) -> None:
        with self._lock:
            stream = self._stream
            self._stream = None
            self._frames = []
            self._status_error = None
        if stream is None:
            return
        try:
            stream.abort()
        finally:
            stream.close()


def _audio_error_message(exc: Exception) -> str:
    message = " ".join(str(exc).split())
    if not message:
        message = exc.__class__.__name__
    lower = message.lower()
    if "device unavailable" in lower or "invalid device" in lower:
        return (
            "Windows microphone is unavailable; select a default input device and allow "
            "desktop apps to access the microphone"
        )
    return f"Windows microphone error: {message}"
