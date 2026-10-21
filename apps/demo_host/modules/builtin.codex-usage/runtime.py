from __future__ import annotations

import json
import os
import queue
import shlex
import shutil
import subprocess
import threading
import time
from copy import deepcopy
from typing import Callable


class CodexAppServerError(RuntimeError):
    def __init__(self, message: str, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


class CodexUsageRuntime:
    def __init__(
        self,
        *,
        on_state: Callable[[dict[str, object]], None],
        command: str | None = None,
        refresh_seconds: float = 60.0,
        process_factory: Callable[[list[str]], subprocess.Popen[str]] | None = None,
    ) -> None:
        self._on_state = on_state
        self._command = command or os.environ.get("ESPAGENT_CODEX_COMMAND") or _find_codex_command()
        self._refresh_seconds = max(5.0, refresh_seconds)
        self._process_factory = process_factory or _spawn_app_server
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._refresh = threading.Event()
        self._thread: threading.Thread | None = None
        self._reader_thread: threading.Thread | None = None
        self._process: subprocess.Popen[str] | None = None
        self._pending: dict[int, queue.Queue[dict[str, object]]] = {}
        self._request_id = 0
        self._state: dict[str, object] = {
            "status": "stopped",
            "provider": "codex_app_server",
            "command": self._command,
            "plan_type": None,
            "limit_id": None,
            "limit_name": None,
            "primary": None,
            "secondary": None,
            "quota": None,
            "credits": None,
            "updated_at": None,
            "error": None,
        }

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="codex-usage-module", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        self._refresh.set()
        self._terminate_process()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._thread = None

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return deepcopy(self._state)

    def invoke(self, action: str, payload: dict[str, object]) -> dict[str, object]:
        if action == "refresh":
            self._refresh.set()
            return {"accepted": True}
        del payload
        raise ValueError(f"unsupported Codex usage action: {action}")

    def _run(self) -> None:
        if not self._command:
            self._update(status="codex_not_found", error="Codex CLI was not found")
            return
        while not self._stop.is_set():
            try:
                self._connect()
                self._read_rate_limits()
                while not self._stop.is_set():
                    self._refresh.wait(self._refresh_seconds)
                    self._refresh.clear()
                    if self._stop.is_set():
                        break
                    self._read_rate_limits()
            except Exception as exc:
                self._update(status="error", error=_safe_error(exc))
            finally:
                self._terminate_process()
            if not self._stop.wait(10):
                self._update(status="connecting", error=None)

    def _connect(self) -> None:
        command = shlex.split(self._command, posix=os.name != "nt")
        if not command:
            raise RuntimeError("Codex command is empty")
        self._update(status="connecting", error=None)
        self._process = self._process_factory([*command, "app-server"])
        self._reader_thread = threading.Thread(target=self._read_stdout, name="codex-usage-reader", daemon=True)
        self._reader_thread.start()
        self._request(
            "initialize",
            {
                "clientInfo": {"name": "espagent-codex-usage", "version": "0.1.0"},
                "capabilities": {"experimentalApi": True},
            },
        )

    def _read_rate_limits(self) -> None:
        try:
            response = self._request("account/rateLimits/read", None)
        except CodexAppServerError as exc:
            if "authentication required" in str(exc).lower():
                self._update(status="auth_required", error="请先运行 codex login 完成 CLI 登录")
                return
            compatibility_result = normalize_embedded_usage_response(str(exc))
            if compatibility_result is not None:
                self._replace_state(compatibility_result)
                return
            raise
        normalized = normalize_rate_limits(response)
        self._replace_state(normalized)

    def _request(self, method: str, params: object, timeout: float = 8.0) -> dict[str, object]:
        process = self._process
        if process is None or process.stdin is None:
            raise RuntimeError("Codex app-server is not connected")
        with self._lock:
            self._request_id += 1
            request_id = self._request_id
            response_queue: queue.Queue[dict[str, object]] = queue.Queue(maxsize=1)
            self._pending[request_id] = response_queue
        try:
            packet = {"id": request_id, "method": method, "params": params}
            process.stdin.write(json.dumps(packet, ensure_ascii=False) + "\n")
            process.stdin.flush()
            response = response_queue.get(timeout=timeout)
        except queue.Empty as exc:
            raise TimeoutError(f"Codex app-server request timed out: {method}") from exc
        finally:
            with self._lock:
                self._pending.pop(request_id, None)
        error = response.get("error")
        if isinstance(error, dict):
            raise CodexAppServerError(str(error.get("message") or "Codex app-server request failed"), error.get("code"))
        result = response.get("result")
        return result if isinstance(result, dict) else {}

    def _read_stdout(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            return
        for line in process.stdout:
            try:
                packet = json.loads(line)
            except (TypeError, json.JSONDecodeError):
                continue
            request_id = packet.get("id")
            if isinstance(request_id, int):
                with self._lock:
                    target = self._pending.get(request_id)
                if target is not None:
                    target.put(packet)
                continue
            if packet.get("method") == "account/rateLimits/updated":
                params = packet.get("params") or {}
                snapshot = params.get("rateLimits") if isinstance(params, dict) else None
                if isinstance(snapshot, dict):
                    self._replace_state(normalize_rate_limit_snapshot(snapshot))

    def _replace_state(self, normalized: dict[str, object]) -> None:
        with self._lock:
            self._state.update(normalized)
            self._state["status"] = "ready"
            self._state["updated_at"] = time.time()
            self._state["error"] = None
            snapshot = deepcopy(self._state)
        self._on_state(snapshot)

    def _update(self, **patch: object) -> None:
        with self._lock:
            self._state.update(patch)
            snapshot = deepcopy(self._state)
        self._on_state(snapshot)

    def _terminate_process(self) -> None:
        process = self._process
        self._process = None
        if process is None:
            return
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
        with self._lock:
            self._pending.clear()


def normalize_rate_limits(payload: dict[str, object]) -> dict[str, object]:
    buckets = payload.get("rateLimitsByLimitId")
    snapshot = None
    if isinstance(buckets, dict):
        candidate = buckets.get("codex")
        if isinstance(candidate, dict):
            snapshot = candidate
        elif buckets:
            first = next(iter(buckets.values()))
            if isinstance(first, dict):
                snapshot = first
    if snapshot is None:
        candidate = payload.get("rateLimits")
        if isinstance(candidate, dict):
            snapshot = candidate
    return normalize_rate_limit_snapshot(snapshot or {})


def normalize_rate_limit_snapshot(snapshot: dict[str, object]) -> dict[str, object]:
    primary = _normalize_window(snapshot.get("primary"))
    secondary = _normalize_window(snapshot.get("secondary"))
    return {
        "plan_type": snapshot.get("planType"),
        "limit_id": snapshot.get("limitId"),
        "limit_name": snapshot.get("limitName"),
        "primary": primary,
        "secondary": secondary,
        "quota": _select_quota(primary, secondary),
        "credits": snapshot.get("credits") if isinstance(snapshot.get("credits"), dict) else None,
    }


def normalize_embedded_usage_response(message: str) -> dict[str, object] | None:
    """Recover usage from app-server errors caused by a newer plan enum.

    Codex CLI may reject a valid server payload before converting it to the
    stable app-server schema. The error includes that JSON payload after
    ``body=``; parse only the documented usage fields and never retain account
    identity fields from the response.
    """
    marker = "body="
    if marker not in message:
        return None
    try:
        payload, _end = json.JSONDecoder().raw_decode(message.split(marker, 1)[1].lstrip())
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    rate_limit = payload.get("rate_limit")
    if not isinstance(rate_limit, dict):
        return None
    primary = _normalize_usage_window(rate_limit.get("primary_window"))
    secondary = _normalize_usage_window(rate_limit.get("secondary_window"))
    return {
        "plan_type": payload.get("plan_type"),
        "limit_id": "codex",
        "limit_name": "Codex",
        "primary": primary,
        "secondary": secondary,
        "quota": _select_quota(primary, secondary),
        "credits": _normalize_usage_credits(payload.get("credits")),
    }


def _normalize_window(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    used = max(0, min(100, int(value.get("usedPercent") or 0)))
    return {
        "used_percent": used,
        "remaining_percent": 100 - used,
        "resets_at": value.get("resetsAt"),
        "window_duration_minutes": value.get("windowDurationMins"),
    }


def _normalize_usage_window(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    used = max(0, min(100, int(value.get("used_percent") or 0)))
    seconds = value.get("limit_window_seconds")
    return {
        "used_percent": used,
        "remaining_percent": 100 - used,
        "resets_at": value.get("reset_at"),
        "window_duration_minutes": int(seconds) // 60 if isinstance(seconds, (int, float)) else None,
    }


def _normalize_usage_credits(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    return {
        "balance": value.get("balance"),
        "unlimited": bool(value.get("unlimited")),
        "has_credits": bool(value.get("has_credits")),
    }


def _select_quota(*windows: dict[str, object] | None) -> dict[str, object] | None:
    available = [window for window in windows if window is not None]
    if not available:
        return None
    return max(available, key=lambda item: int(item.get("window_duration_minutes") or 0))


def _safe_error(exc: Exception) -> str:
    message = str(exc)
    if "body=" in message or "backend-api/wham/usage" in message:
        return "Codex CLI 无法解析当前账户额度响应，请升级 Codex CLI"
    if isinstance(exc, TimeoutError):
        return "Codex app-server 响应超时"
    return f"{exc.__class__.__name__}: {message[:240]}"


def _find_codex_command() -> str | None:
    candidates = ["codex.cmd", "codex"] if os.name == "nt" else ["codex"]
    return next((path for item in candidates if (path := shutil.which(item))), None)


def _spawn_app_server(command: list[str]) -> subprocess.Popen[str]:
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    return subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        bufsize=1,
        creationflags=creationflags,
    )
