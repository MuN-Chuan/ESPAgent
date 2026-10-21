from __future__ import annotations

import re
import threading
import time
from copy import deepcopy
from dataclasses import dataclass
from typing import Callable, Protocol

import numpy as np
from PIL import Image, ImageDraw, ImageGrab

WIDTH = 240
HEIGHT = 320


@dataclass(frozen=True)
class CaptureOutput:
    id: str
    name: str
    device_index: int
    output_index: int
    width: int
    height: int
    primary: bool = False

    def snapshot(self) -> dict[str, object]:
        return {
            "id": self.id,
            "name": self.name,
            "device_index": self.device_index,
            "output_index": self.output_index,
            "width": self.width,
            "height": self.height,
            "primary": self.primary,
        }


class CaptureSession(Protocol):
    def grab(self) -> object | None:
        ...

    def close(self) -> None:
        ...


class CaptureBackend(Protocol):
    name: str

    def outputs(self) -> list[CaptureOutput]:
        ...

    def open(self, output: CaptureOutput, *, cursor: bool) -> CaptureSession:
        ...


class DesktopMirrorRuntime:
    """On-demand desktop capture module with a single latest-frame slot."""

    def __init__(
        self,
        *,
        on_state: Callable[[dict[str, object]], None],
        backend: CaptureBackend | None = None,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self._on_state = on_state
        self._backend = backend or _default_backend()
        self._clock = clock
        self._lock = threading.RLock()
        self._capture_stop = threading.Event()
        self._capture_thread: threading.Thread | None = None
        self._scene_active = False
        self._desired_active = False
        self._outputs: list[CaptureOutput] = []
        self._output_size = (WIDTH, HEIGHT)
        self._frame = _status_frame("DESKTOP MIRROR", "READY")
        self._frame_generation = 0
        self._consumed_generation = -1
        self._state: dict[str, object] = {
            "status": "stopped",
            "backend": self._backend.name,
            "capture_active": False,
            "scene_active": False,
            "monitor_id": "",
            "monitors": [],
            "target_fps": 0,
            "capture_fps": 0.0,
            "frames_captured": 0,
            "transport_mode": "mjpeg",
            "jpeg_quality": 80,
            "jpeg_subsampling": "420",
            "indexed_bpp": 8,
            "cursor": False,
            "fit_mode": "contain",
            "last_frame_at": None,
            "error": None,
        }

    def start(self) -> None:
        try:
            outputs = self._backend.outputs()
            if not outputs:
                raise RuntimeError("no desktop outputs were found")
            with self._lock:
                self._outputs = outputs
                self._state["monitors"] = [item.snapshot() for item in outputs]
                selected = next((item for item in outputs if item.primary), outputs[0])
                self._state["monitor_id"] = selected.id
                self._state["status"] = "ready"
                self._state["error"] = None
        except Exception as exc:
            with self._lock:
                self._state["status"] = "unavailable"
                self._state["error"] = f"{exc.__class__.__name__}: {exc}"
        self._publish()

    def close(self) -> None:
        with self._lock:
            self._desired_active = False
            self._scene_active = False
        self._stop_capture("stopped")

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return deepcopy(self._state)

    def invoke(self, action: str, payload: dict[str, object]) -> dict[str, object]:
        if action == "configure":
            self._configure(payload)
        elif action == "start":
            self._configure(payload)
            with self._lock:
                if self._state["status"] == "unavailable":
                    raise RuntimeError(str(self._state["error"] or "desktop capture is unavailable"))
                self._desired_active = True
            self._sync_capture_lifecycle()
        elif action == "stop":
            with self._lock:
                self._desired_active = False
            self._stop_capture("ready")
            self._replace_frame(_status_frame("DESKTOP MIRROR", "PAUSED"))
        elif action == "refresh_outputs":
            self._refresh_outputs()
        else:
            raise ValueError(f"unsupported desktop mirror action: {action}")
        self._publish()
        return self.snapshot()

    def set_scene_active(self, active: bool) -> None:
        with self._lock:
            changed = self._scene_active != bool(active)
            self._scene_active = bool(active)
            self._state["scene_active"] = self._scene_active
        if changed:
            self._sync_capture_lifecycle()
            self._publish()

    def set_output_size(self, width: int, height: int) -> None:
        if (width, height) not in {(240, 320), (320, 240)}:
            raise ValueError("desktop mirror output must be 240x320 or 320x240")
        with self._lock:
            self._output_size = (width, height)

    def has_pending_frame(self) -> bool:
        with self._lock:
            return self._frame_generation != self._consumed_generation

    def current_frame(self) -> Image.Image:
        with self._lock:
            self._consumed_generation = self._frame_generation
            return self._frame

    def display_transport_profile(self, scene_key: str) -> dict[str, object]:
        if scene_key != "desktop":
            return {}
        with self._lock:
            return {
                "transport_mode": self._state["transport_mode"],
                "jpeg_quality": self._state["jpeg_quality"],
                "jpeg_subsampling": self._state["jpeg_subsampling"],
                "indexed_bpp": self._state["indexed_bpp"],
            }

    def _configure(self, payload: dict[str, object]) -> None:
        restart = False
        with self._lock:
            if "monitor_id" in payload:
                monitor_id = str(payload["monitor_id"])
                if monitor_id not in {item.id for item in self._outputs}:
                    raise ValueError("unknown monitor_id")
                restart = restart or monitor_id != self._state["monitor_id"]
                self._state["monitor_id"] = monitor_id
            if "target_fps" in payload:
                target_fps = int(payload["target_fps"])
                if not 0 <= target_fps <= 240:
                    raise ValueError("target_fps must be between 0 and 240")
                self._state["target_fps"] = target_fps
            if "transport_mode" in payload:
                transport_mode = str(payload["transport_mode"])
                if transport_mode not in {"mjpeg", "indexed"}:
                    raise ValueError("transport_mode must be mjpeg or indexed")
                self._state["transport_mode"] = transport_mode
            if "jpeg_quality" in payload:
                quality = int(payload["jpeg_quality"])
                if not 30 <= quality <= 95:
                    raise ValueError("jpeg_quality must be between 30 and 95")
                self._state["jpeg_quality"] = quality
            if "jpeg_subsampling" in payload:
                subsampling = str(payload["jpeg_subsampling"])
                if subsampling not in {"420", "422", "444"}:
                    raise ValueError("jpeg_subsampling must be 420, 422, or 444")
                self._state["jpeg_subsampling"] = subsampling
            if "indexed_bpp" in payload:
                indexed_bpp = int(payload["indexed_bpp"])
                if indexed_bpp not in {1, 2, 4, 8}:
                    raise ValueError("indexed_bpp must be 1, 2, 4, or 8")
                self._state["indexed_bpp"] = indexed_bpp
            if "cursor" in payload:
                cursor = bool(payload["cursor"])
                restart = restart or cursor != self._state["cursor"]
                self._state["cursor"] = cursor
        if restart:
            self._stop_capture("ready")
            self._sync_capture_lifecycle()

    def _refresh_outputs(self) -> None:
        outputs = self._backend.outputs()
        if not outputs:
            raise RuntimeError("no desktop outputs were found")
        with self._lock:
            selected = str(self._state["monitor_id"])
            self._outputs = outputs
            self._state["monitors"] = [item.snapshot() for item in outputs]
            if selected not in {item.id for item in outputs}:
                self._state["monitor_id"] = next((item.id for item in outputs if item.primary), outputs[0].id)

    def _sync_capture_lifecycle(self) -> None:
        with self._lock:
            should_capture = self._desired_active and self._scene_active
        if should_capture:
            self._ensure_capture_started()
        else:
            self._stop_capture("paused" if self._desired_active else "ready")

    def _ensure_capture_started(self) -> None:
        with self._lock:
            if self._capture_thread is not None and self._capture_thread.is_alive():
                return
            self._capture_stop.clear()
            self._state["status"] = "starting"
            self._state["error"] = None
            thread = threading.Thread(target=self._capture_loop, name="desktop-mirror-capture", daemon=True)
            self._capture_thread = thread
            thread.start()

    def _stop_capture(self, status: str) -> None:
        with self._lock:
            thread = self._capture_thread
            self._capture_stop.set()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        with self._lock:
            if self._capture_thread is thread and (thread is None or not thread.is_alive()):
                self._capture_thread = None
            self._state["capture_active"] = False
            if self._state["status"] != "unavailable":
                self._state["status"] = status
            self._state["capture_fps"] = 0.0

    def _capture_loop(self) -> None:
        session: CaptureSession | None = None
        window_started = self._clock()
        window_frames = 0
        last_publish = window_started
        try:
            with self._lock:
                output = self._selected_output_locked()
                cursor = bool(self._state["cursor"])
            session = self._backend.open(output, cursor=cursor)
            with self._lock:
                self._state["status"] = "capturing"
                self._state["capture_active"] = True
            self._publish()
            while not self._capture_stop.is_set():
                started = self._clock()
                frame = session.grab()
                if frame is not None:
                    with self._lock:
                        output_size = self._output_size
                    image = fit_desktop_frame(frame, size=output_size)
                    self._replace_frame(image)
                    window_frames += 1
                    with self._lock:
                        self._state["frames_captured"] = int(self._state["frames_captured"]) + 1
                        self._state["last_frame_at"] = time.time()
                now = self._clock()
                if now - window_started >= 1.0:
                    with self._lock:
                        self._state["capture_fps"] = window_frames / max(0.001, now - window_started)
                    window_started = now
                    window_frames = 0
                if now - last_publish >= 0.5:
                    self._publish()
                    last_publish = now
                with self._lock:
                    target_fps = int(self._state["target_fps"])
                if target_fps > 0:
                    interval = 1.0 / target_fps
                    self._capture_stop.wait(max(0.0, interval - (self._clock() - started)))
                elif frame is None:
                    # Unlimited mode follows the capture/display pipeline but still
                    # yields when DXGI has no new desktop frame.
                    self._capture_stop.wait(0.001)
        except Exception as exc:
            with self._lock:
                self._desired_active = False
                self._state["status"] = "error"
                self._state["error"] = f"{exc.__class__.__name__}: {exc}"
            self._replace_frame(_status_frame("DESKTOP MIRROR", "CAPTURE ERROR"))
        finally:
            if session is not None:
                session.close()
            with self._lock:
                self._state["capture_active"] = False
                if self._capture_thread is threading.current_thread():
                    self._capture_thread = None
                if self._state["status"] not in {"error", "unavailable"}:
                    self._state["status"] = "paused" if self._desired_active else "ready"
            self._publish()

    def _selected_output_locked(self) -> CaptureOutput:
        selected = str(self._state["monitor_id"])
        return next((item for item in self._outputs if item.id == selected), self._outputs[0])

    def _replace_frame(self, frame: Image.Image) -> None:
        with self._lock:
            self._frame = frame
            self._frame_generation += 1

    def _publish(self) -> None:
        self._on_state(self.snapshot())


class _DxcamSession:
    def __init__(self, camera: object) -> None:
        self._camera = camera

    def grab(self) -> object | None:
        try:
            return self._camera.grab(new_frame_only=True)
        except TypeError:
            return self._camera.grab()

    def close(self) -> None:
        release = getattr(self._camera, "release", None)
        if callable(release):
            release()


class DxcamCaptureBackend:
    name = "dxgi"
    _OUTPUT_PATTERN = re.compile(
        r"Device\[(?P<device>\d+)\].*?Output\[(?P<output>\d+)\].*?"
        r"Res:\((?P<width>\d+),\s*(?P<height>\d+)\).*?Primary:(?P<primary>True|False)",
        re.IGNORECASE,
    )

    def __init__(self) -> None:
        import dxcam

        self._dxcam = dxcam

    def outputs(self) -> list[CaptureOutput]:
        outputs: list[CaptureOutput] = []
        for line in str(self._dxcam.output_info()).splitlines():
            match = self._OUTPUT_PATTERN.search(line)
            if match is None:
                continue
            device = int(match.group("device"))
            output = int(match.group("output"))
            width = int(match.group("width"))
            height = int(match.group("height"))
            primary = match.group("primary").lower() == "true"
            outputs.append(
                CaptureOutput(
                    id=f"{device}:{output}",
                    name=f"显示器 {len(outputs) + 1} · {width}×{height}",
                    device_index=device,
                    output_index=output,
                    width=width,
                    height=height,
                    primary=primary,
                )
            )
        return outputs

    def open(self, output: CaptureOutput, *, cursor: bool) -> CaptureSession:
        kwargs: dict[str, object] = {
            "device_idx": output.device_index,
            "output_idx": output.output_index,
            "output_color": "BGRA",
        }
        if cursor:
            kwargs["backend"] = "winrt"
        try:
            camera = self._dxcam.create(**kwargs)
        except Exception:
            if not cursor:
                raise
            kwargs.pop("backend", None)
            camera = self._dxcam.create(**kwargs)
        return _DxcamSession(camera)


class _PillowSession:
    def grab(self) -> Image.Image:
        return ImageGrab.grab(all_screens=False).convert("RGB")

    def close(self) -> None:
        return None


class PillowCaptureBackend:
    name = "pillow-fallback"

    def outputs(self) -> list[CaptureOutput]:
        with ImageGrab.grab(all_screens=False) as image:
            width, height = image.size
        return [CaptureOutput("0:0", f"主显示器 · {width}×{height}", 0, 0, width, height, True)]

    def open(self, output: CaptureOutput, *, cursor: bool) -> CaptureSession:
        del output, cursor
        return _PillowSession()


def fit_desktop_frame(
    frame: object,
    *,
    size: tuple[int, int] = (WIDTH, HEIGHT),
    background: tuple[int, int, int] = (0, 0, 0),
) -> Image.Image:
    width, height = size
    if isinstance(frame, Image.Image):
        source = frame.convert("RGB")
        scale = min(width / source.width, height / source.height)
        target = (max(1, round(source.width * scale)), max(1, round(source.height * scale)))
        resized = source.resize(target, Image.Resampling.BILINEAR)
    else:
        array = np.asarray(frame)
        if array.ndim != 3 or array.shape[2] not in {3, 4}:
            raise ValueError("desktop frame must be an RGB, BGR, RGBA, or BGRA image")
        import cv2

        source_height, source_width = array.shape[:2]
        scale = min(width / source_width, height / source_height)
        target = (max(1, round(source_width * scale)), max(1, round(source_height * scale)))
        reduced = cv2.resize(array, target, interpolation=cv2.INTER_AREA)
        conversion = cv2.COLOR_BGRA2RGB if array.shape[2] == 4 else cv2.COLOR_BGR2RGB
        resized = Image.fromarray(cv2.cvtColor(reduced, conversion), mode="RGB")
    canvas = Image.new("RGB", (width, height), background)
    canvas.paste(resized, ((width - target[0]) // 2, (height - target[1]) // 2))
    return canvas


def _default_backend() -> CaptureBackend:
    try:
        return DxcamCaptureBackend()
    except (ImportError, OSError):
        return PillowCaptureBackend()


def _status_frame(title: str, status: str) -> Image.Image:
    image = Image.new("RGB", (WIDTH, HEIGHT), (10, 13, 18))
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, WIDTH - 1, HEIGHT - 1), outline=(67, 74, 85))
    draw.text((16, 18), title, fill=(235, 238, 242))
    draw.text((16, 44), status, fill=(132, 141, 153))
    return image
