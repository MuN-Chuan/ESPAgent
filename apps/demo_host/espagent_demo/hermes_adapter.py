from __future__ import annotations

import json
import logging
import os
import queue
import shlex
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from espagent_demo.paths import app_resource_dir


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class HermesEvent:
    type: str
    payload: dict[str, object]


HermesEventHandler = Callable[[HermesEvent], object]


class DemoHermesAdapter:
    def __init__(self) -> None:
        self._session_counter = 1

    def health(self) -> dict[str, object]:
        return {"ok": True, "service": "hermes", "mode": "demo", "streaming": True}

    def ask(self, prompt: str, on_event: HermesEventHandler | None = None) -> str:
        reply = f"Hermes demo: 已收到“{prompt}”。如果这是灯具指令，状态会同步到小屏。"
        if on_event is not None:
            on_event(HermesEvent("message.start", {}))
            on_event(HermesEvent("message.delta", {"text": reply}))
            on_event(HermesEvent("message.complete", {"text": reply, "status": "complete"}))
        return reply

    def close(self) -> None:
        return None

    def new_session(self) -> str:
        self._session_counter += 1
        return f"demo-{self._session_counter}"

    def interrupt(self) -> str:
        return "interrupted"

    def voice_status(self) -> dict[str, object]:
        return {
            "enabled": False,
            "available": False,
            "audio_available": False,
            "stt_available": False,
            "tts": False,
            "details": "Hermes demo mode does not capture a microphone",
        }


class WslHermesAdapter:
    """Persistent client for Hermes' native stdio JSON-RPC gateway."""

    def __init__(
        self,
        distro: str,
        command: str,
        timeout_seconds: float = 180.0,
        stt_language: str = "zh",
        stt_timeout_seconds: float = 600.0,
    ) -> None:
        self.distro = distro
        self.command = command
        self.timeout_seconds = timeout_seconds
        self.stt_language = stt_language.strip()
        self.stt_timeout_seconds = max(30.0, float(stt_timeout_seconds))
        self._process: subprocess.Popen[str] | None = None
        self._session_id: str | None = None
        self._ready = threading.Event()
        self._stopped = threading.Event()
        self._events: queue.Queue[HermesEvent] = queue.Queue()
        self._voice_events: queue.Queue[HermesEvent] = queue.Queue()
        self._pending: dict[str, queue.Queue[dict[str, Any]]] = {}
        self._pending_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._start_lock = threading.Lock()
        self._turn_lock = threading.Lock()
        self._request_counter = 0
        self._reader_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._stderr_tail: deque[str] = deque(maxlen=20)
        self._last_error: str | None = None
        self._voice_silence_seconds: float | None = None
        self._stt_request_lock = threading.Lock()

    def start(self) -> None:
        with self._start_lock:
            if self._is_running() and self._ready.is_set() and self._session_id:
                return
            self._close_process()
            gateway_script = self._gateway_script()
            self._ready.clear()
            self._stopped.clear()
            self._last_error = None
            self._session_id = None
            self._process = subprocess.Popen(
                ["wsl.exe", "-d", self.distro, "--exec", "bash", "-lc", gateway_script],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                **_hidden_subprocess_options(),
            )
            process = self._process
            self._reader_thread = threading.Thread(
                target=self._read_stdout,
                args=(process,),
                name="hermes-gateway-reader",
                daemon=True,
            )
            self._stderr_thread = threading.Thread(
                target=self._read_stderr,
                args=(process,),
                name="hermes-gateway-stderr",
                daemon=True,
            )
            self._reader_thread.start()
            self._stderr_thread.start()

            if not self._ready.wait(timeout=20.0):
                self._close_process()
                raise RuntimeError("Hermes gateway did not become ready")
            result = self._request(
                "session.create",
                {
                    "source": "tool",
                    "close_on_disconnect": True,
                    "title": "ESPAgent",
                },
                timeout=30.0,
            )
            session_id = str(result.get("session_id") or "")
            if not session_id:
                self._close_process()
                raise RuntimeError("Hermes gateway did not create a session")
            self._session_id = session_id

    def health(self) -> dict[str, object]:
        running = self._is_running()
        return {
            "ok": running and self._ready.is_set() and bool(self._session_id),
            "service": "hermes",
            "mode": "gateway",
            "transport": "stdio-jsonrpc",
            "distro": self.distro,
            "streaming": True,
            "tool_progress": True,
            "session_id": self._session_id,
            "reason": self._last_error,
        }

    def ask(self, prompt: str, on_event: HermesEventHandler | None = None) -> str:
        if not self.command.strip():
            raise RuntimeError("HERMES_COMMAND is empty")
        with self._turn_lock:
            if not self._is_running() or not self._session_id:
                self.start()
            self._drain_events()
            session_id = self._session_id
            if not session_id:
                raise RuntimeError("Hermes gateway session is unavailable")
            self._request(
                "prompt.submit",
                {"session_id": session_id, "text": prompt},
                timeout=30.0,
            )

            deadline = time.monotonic() + self.timeout_seconds
            final_reply = ""
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Hermes gateway response timed out")
                try:
                    event = self._events.get(timeout=remaining)
                except queue.Empty as exc:
                    raise TimeoutError("Hermes gateway response timed out") from exc
                if event.payload.get("session_id") not in {None, session_id}:
                    continue
                if on_event is not None:
                    on_event(event)
                deadline = time.monotonic() + self.timeout_seconds
                if event.type == "error":
                    message = str(event.payload.get("message") or "Hermes gateway error")
                    raise RuntimeError(message)
                if event.type == "message.complete":
                    final_reply = str(event.payload.get("text") or "").strip()
                    status = str(event.payload.get("status") or "complete")
                    if status == "error":
                        raise RuntimeError(final_reply or "Hermes gateway turn failed")
                    break

            if not final_reply:
                raise RuntimeError("Hermes returned an empty reply")
            return final_reply

    def new_session(self) -> str:
        with self._turn_lock:
            if not self._is_running():
                self.start()
            self._drain_events()
            result = self._request(
                "session.create",
                {
                    "source": "tool",
                    "close_on_disconnect": True,
                    "title": "ESPAgent",
                },
                timeout=30.0,
            )
            session_id = str(result.get("session_id") or "")
            if not session_id:
                raise RuntimeError("Hermes gateway did not create a session")
            self._session_id = session_id
            return session_id

    def interrupt(self) -> str:
        """Interrupt the active turn without waiting for the turn lock."""
        session_id = self._session_id
        if not self._is_running() or not session_id:
            return "idle"
        result = self._request(
            "session.interrupt",
            {"session_id": session_id},
            timeout=10.0,
        )
        return str(result.get("status") or "interrupted")

    def voice_status(self) -> dict[str, object]:
        self._ensure_started()
        return self._request(
            "voice.toggle",
            {"action": "status"},
            timeout=max(60.0, self.timeout_seconds),
        )

    def enable_voice(self, enabled: bool = True) -> dict[str, object]:
        self._ensure_started()
        result = self._request(
            "voice.toggle",
            {"action": "on" if enabled else "off"},
            timeout=30.0,
        )
        # Fish Audio owns speech output. Keep Hermes' built-in TTS disabled
        # even when a previous caller enabled it in this gateway process.
        if enabled and bool(result.get("tts")):
            result = self._request("voice.toggle", {"action": "tts"}, timeout=30.0)
        return result

    def start_voice_recording(self) -> dict[str, object]:
        self._ensure_started()
        if not self._session_id:
            raise RuntimeError("Hermes gateway session is unavailable")
        return self._request(
            "voice.record",
            {"action": "start", "session_id": self._session_id},
            timeout=30.0,
        )

    def transcribe_voice_recording(self, path: Path) -> dict[str, object]:
        queued_at = time.monotonic()
        logger.info(
            "hermes_stt_queue operation=transcribe bytes=%d file=%s",
            _file_size(path),
            path.name,
        )
        with self._stt_request_lock:
            started_at = time.monotonic()
            logger.info(
                "hermes_stt_request_start operation=transcribe queue_ms=%d timeout_seconds=%.1f language=%s",
                round((started_at - queued_at) * 1000),
                self.stt_timeout_seconds,
                self.stt_language or "auto",
            )
            try:
                self._ensure_started()
                result = self._request(
                    "espagent.voice.transcribe",
                    {"path": str(_as_wsl_mount_path(path))},
                    timeout=self.stt_timeout_seconds,
                )
            except Exception as exc:
                logger.exception(
                    "hermes_stt_request_failed operation=transcribe elapsed_ms=%d error_type=%s",
                    round((time.monotonic() - started_at) * 1000),
                    exc.__class__.__name__,
                )
                raise
            logger.info(
                "hermes_stt_request_done operation=transcribe elapsed_ms=%d provider=%s chars=%d filtered=%s",
                round((time.monotonic() - started_at) * 1000),
                str(result.get("provider") or "unknown"),
                len(str(result.get("transcript") or "")),
                bool(result.get("filtered")),
            )
            return result

    def prewarm_voice(self) -> None:
        queued_at = time.monotonic()
        logger.info("hermes_stt_queue operation=prewarm")
        with self._stt_request_lock:
            started_at = time.monotonic()
            logger.info(
                "hermes_stt_request_start operation=prewarm queue_ms=%d timeout_seconds=%.1f",
                round((started_at - queued_at) * 1000),
                self.stt_timeout_seconds,
            )
            try:
                self._ensure_started()
                result = self._request(
                    "espagent.voice.prewarm",
                    {},
                    timeout=self.stt_timeout_seconds,
                )
            except Exception as exc:
                logger.exception(
                    "hermes_stt_request_failed operation=prewarm elapsed_ms=%d error_type=%s",
                    round((time.monotonic() - started_at) * 1000),
                    exc.__class__.__name__,
                )
                raise
            logger.info(
                "hermes_stt_request_done operation=prewarm elapsed_ms=%d provider=%s skipped=%s",
                round((time.monotonic() - started_at) * 1000),
                str(result.get("provider") or "unknown"),
                bool(result.get("skipped")),
            )

    def set_voice_silence_duration(self, seconds: float) -> None:
        seconds = float(seconds)
        if self._voice_silence_seconds == seconds:
            return
        executable = self._hermes_executable()
        value = format(seconds, ".3f").rstrip("0").rstrip(".")
        result = subprocess.run(
            [
                "wsl.exe",
                "-d",
                self.distro,
                "--exec",
                str(executable),
                "config",
                "set",
                "voice.silence_duration",
                value,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30.0,
            check=False,
            **_hidden_subprocess_options(),
        )
        if result.returncode != 0:
            raise RuntimeError("Hermes voice silence configuration failed")
        self._voice_silence_seconds = seconds

    def stop_voice_recording(self) -> dict[str, object]:
        self._ensure_started()
        return self._request(
            "voice.record",
            {"action": "stop", "session_id": self._session_id},
            timeout=30.0,
        )

    def wait_voice_event(self, timeout: float = 0.5) -> HermesEvent | None:
        try:
            return self._voice_events.get(timeout=timeout)
        except queue.Empty:
            return None

    def close(self) -> None:
        with self._start_lock:
            self._close_process()

    def _gateway_script(self) -> str:
        executable = self._hermes_executable()
        root = executable.parents[2]
        python = root / "venv" / "bin" / "python"
        app_source = _as_wsl_mount_path(app_resource_dir())
        skill_source = _as_wsl_mount_path(
            app_resource_dir() / "hermes_skills" / "espagent-light-strip"
        )
        skill_target = "$HOME/.hermes/skills/smart-home/espagent-light-strip"
        return (
            f"if [ -d {shlex.quote(str(skill_source))} ]; then "
            f"mkdir -p {skill_target}; "
            f"cp -R {shlex.quote(str(skill_source))}/. {skill_target}/; fi; "
            "export ESPAGENT_API_URL=http://127.0.0.1:8787; "
            f"export HERMES_LOCAL_STT_LANGUAGE={shlex.quote(self.stt_language)}; "
            f"export PYTHONPATH={shlex.quote(str(root))}:{shlex.quote(str(app_source))}"
            "${PYTHONPATH:+:$PYTHONPATH}; "
            f"exec {shlex.quote(str(python))} -u -m espagent_demo.hermes_gateway_bridge"
        )

    def _hermes_executable(self) -> PurePosixPath:
        parts = shlex.split(self.command)
        if not parts:
            raise RuntimeError("HERMES_COMMAND is empty")
        executable = PurePosixPath(parts[0])
        if not executable.is_absolute() or executable.parent.name != "bin" or executable.parent.parent.name != "venv":
            raise RuntimeError("HERMES_COMMAND must use the Hermes virtualenv executable")
        return executable

    def _request(self, method: str, params: dict[str, object], *, timeout: float) -> dict[str, Any]:
        process = self._process
        if process is None or process.stdin is None or process.poll() is not None:
            raise RuntimeError("Hermes gateway is not running")
        with self._pending_lock:
            self._request_counter += 1
            request_id = f"esp-{self._request_counter}"
            response_queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)
            self._pending[request_id] = response_queue
        request = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        try:
            with self._write_lock:
                process.stdin.write(json.dumps(request, ensure_ascii=False) + "\n")
                process.stdin.flush()
            response = response_queue.get(timeout=timeout)
        except queue.Empty as exc:
            raise TimeoutError(f"Hermes gateway request timed out: {method}") from exc
        finally:
            with self._pending_lock:
                self._pending.pop(request_id, None)
        if error := response.get("error"):
            message = error.get("message") if isinstance(error, dict) else str(error)
            raise RuntimeError(f"Hermes gateway rejected {method}: {message}")
        result = response.get("result", {})
        return result if isinstance(result, dict) else {}

    def _read_stdout(self, process: subprocess.Popen[str]) -> None:
        stream = process.stdout
        if stream is None:
            return
        try:
            for raw_line in stream:
                try:
                    message = json.loads(raw_line)
                except (json.JSONDecodeError, TypeError):
                    continue
                request_id = message.get("id")
                if request_id is not None:
                    with self._pending_lock:
                        response_queue = self._pending.get(str(request_id))
                    if response_queue is not None:
                        response_queue.put(message)
                    continue
                if message.get("method") != "event":
                    continue
                params = message.get("params", {})
                if not isinstance(params, dict):
                    continue
                event_type = str(params.get("type") or "")
                payload = params.get("payload", {})
                if not isinstance(payload, dict):
                    payload = {}
                payload = dict(payload)
                if params.get("session_id"):
                    payload["session_id"] = params["session_id"]
                if event_type == "gateway.ready":
                    self._ready.set()
                elif event_type in {"voice.status", "voice.transcript"}:
                    self._voice_events.put(HermesEvent(event_type, payload))
                else:
                    self._events.put(HermesEvent(event_type, payload))
        finally:
            if not self._stopped.is_set():
                self._last_error = "gateway_exited"
                self._events.put(HermesEvent("error", {"message": "Hermes gateway exited"}))
                self._voice_events.put(HermesEvent("error", {"message": "Hermes gateway exited"}))
            self._ready.clear()

    def _read_stderr(self, process: subprocess.Popen[str]) -> None:
        stream = process.stderr
        if stream is None:
            return
        for raw_line in stream:
            line = raw_line.strip()
            if line:
                self._stderr_tail.append(line)
                if line.startswith("ESPAGENT_STT "):
                    logger.info("hermes_gateway_%s", line.removeprefix("ESPAGENT_STT "))

    def _drain_events(self) -> None:
        while True:
            try:
                self._events.get_nowait()
            except queue.Empty:
                return

    def _is_running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def _ensure_started(self) -> None:
        if not self._is_running() or not self._session_id:
            self.start()

    def _close_process(self) -> None:
        process = self._process
        self._stopped.set()
        self._ready.clear()
        self._session_id = None
        if process is None:
            return
        if process.stdin is not None:
            try:
                process.stdin.close()
            except OSError:
                pass
        try:
            process.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                process.kill()
        self._process = None
        self._reader_thread = None
        self._stderr_thread = None


def _hidden_subprocess_options() -> dict[str, object]:
    if os.name != "nt":
        return {}
    startup_info = subprocess.STARTUPINFO()
    startup_info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup_info.wShowWindow = subprocess.SW_HIDE
    return {
        "creationflags": subprocess.CREATE_NO_WINDOW,
        "startupinfo": startup_info,
    }


def _as_wsl_mount_path(path: Path) -> PurePosixPath:
    resolved = path.resolve()
    drive = resolved.drive.rstrip(":").lower()
    if not drive:
        raise ValueError("Hermes skill source must be on a Windows drive")
    relative_parts = resolved.parts[1:]
    return PurePosixPath("/mnt", drive, *relative_parts)


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return -1
