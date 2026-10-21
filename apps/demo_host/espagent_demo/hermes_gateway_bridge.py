from __future__ import annotations

import os
import json
import sys
import tempfile
import time
import wave
from pathlib import Path

from tui_gateway import server


server._LONG_HANDLERS = frozenset(
    {
        *server._LONG_HANDLERS,
        "espagent.voice.transcribe",
        "espagent.voice.prewarm",
    }
)


@server.method("espagent.voice.transcribe")
def transcribe_voice(rid: object, params: dict) -> dict:
    started_at = time.monotonic()
    raw_path = str(params.get("path") or "")
    path = Path(raw_path)
    if not raw_path or not path.is_absolute() or not raw_path.startswith("/mnt/"):
        return server._err(rid, 5400, "audio path must be an absolute Windows mount path")
    try:
        if not path.exists():
            return server._err(rid, 5401, "audio recording does not exist")
        resolved = path.resolve(strict=True)
        if not resolved.is_file():
            return server._err(rid, 5401, "audio recording does not exist")
        _stt_log("transcribe_start", bytes=resolved.stat().st_size, file=resolved.name)
        from tools.voice_mode import transcribe_recording

        result = transcribe_recording(str(resolved))
        if not bool(result.get("success")):
            _stt_log(
                "transcribe_failed",
                elapsed_ms=round((time.monotonic() - started_at) * 1000),
                provider=result.get("provider") or "unknown",
                error_type="provider_error",
            )
            message = str(result.get("error") or "speech recognition failed")
            return server._err(rid, 5402, message)
        transcript = str(result.get("transcript") or "").strip()
        _stt_log(
            "transcribe_done",
            elapsed_ms=round((time.monotonic() - started_at) * 1000),
            provider=result.get("provider") or "unknown",
            chars=len(transcript),
            filtered=bool(result.get("filtered")),
        )
        return server._ok(
            rid,
            {
                "transcript": transcript,
                "filtered": bool(result.get("filtered")),
                "provider": str(result.get("provider") or "unknown"),
            },
        )
    except Exception as exc:
        _stt_log(
            "transcribe_failed",
            elapsed_ms=round((time.monotonic() - started_at) * 1000),
            error_type=exc.__class__.__name__,
        )
        return server._err(rid, 5403, str(exc) or exc.__class__.__name__)


@server.method("espagent.voice.prewarm")
def prewarm_voice(rid: object, params: dict) -> dict:
    started_at = time.monotonic()
    _stt_log("prewarm_start")
    try:
        result = warmup_transcriber()
        _stt_log(
            "prewarm_done",
            elapsed_ms=round((time.monotonic() - started_at) * 1000),
            provider=result.get("provider") or "unknown",
            skipped=bool(result.get("skipped")),
        )
        return server._ok(rid, result)
    except Exception as exc:
        _stt_log(
            "prewarm_failed",
            elapsed_ms=round((time.monotonic() - started_at) * 1000),
            error_type=exc.__class__.__name__,
        )
        return server._err(rid, 5404, str(exc) or exc.__class__.__name__)


def transcribe_voice_file(path: Path) -> dict[str, object]:
    if not path.is_absolute() or not str(path).startswith("/mnt/"):
        raise ValueError("audio path must be an absolute Windows mount path")
    resolved = path.resolve(strict=True)
    if not resolved.is_file():
        raise FileNotFoundError("audio recording does not exist")
    from tools.voice_mode import transcribe_recording

    result = transcribe_recording(str(resolved))
    if not bool(result.get("success")):
        raise RuntimeError(str(result.get("error") or "speech recognition failed"))
    return {
        "success": True,
        "transcript": str(result.get("transcript") or "").strip(),
        "filtered": bool(result.get("filtered")),
    }


def transcribe_cli(raw_path: str) -> int:
    try:
        result = transcribe_voice_file(Path(raw_path))
    except Exception as exc:
        result = {"success": False, "error": str(exc) or exc.__class__.__name__}
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0 if bool(result.get("success")) else 1


def warmup_transcriber() -> dict[str, object]:
    from tools import transcription_tools
    from tools.voice_mode import transcribe_recording

    config = transcription_tools._load_stt_config()
    provider = transcription_tools._get_provider(config)
    if provider != "local":
        return {"success": True, "provider": provider, "skipped": True}

    path = Path(tempfile.gettempdir()) / "espagent-stt-warmup.wav"
    try:
        with wave.open(str(path), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(16000)
            output.writeframes(b"\0\0" * 1600)
        result = transcribe_recording(str(path))
        if not bool(result.get("success")):
            raise RuntimeError(str(result.get("error") or "STT warmup failed"))
        return {"success": True, "provider": result.get("provider")}
    finally:
        path.unlink(missing_ok=True)


def _stt_log(event: str, **fields: object) -> None:
    details = " ".join(f"{key}={value}" for key, value in fields.items())
    suffix = f" {details}" if details else ""
    print(f"ESPAGENT_STT {event}{suffix}", file=sys.stderr, flush=True)


def main() -> None:
    if len(sys.argv) == 3 and sys.argv[1] == "--transcribe":
        raise SystemExit(transcribe_cli(sys.argv[2]))
    os.environ.setdefault("HERMES_VOICE_DEBUG", "1")
    from tui_gateway.entry import main as gateway_main

    gateway_main()


if __name__ == "__main__":
    main()
