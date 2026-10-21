from __future__ import annotations

import base64
import json
import logging
import os
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.request
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict
from io import BytesIO
from pathlib import Path
from typing import Callable, Protocol

from PIL import Image, ImageChops, ImageFilter
from websocket import WebSocket, create_connection

from espagent_demo.display_geometry import display_geometry
from espagent_demo.paths import app_resource_dir
from espagent_demo.renderer import DisplayRenderer, INK, PAPER, _apply_display_tone
from espagent_demo.state import DemoState

logger = logging.getLogger(__name__)
WIDTH = 240
HEIGHT = 320
WEB_RENDER_PIPELINE_DEPTH = 1
SCENE_TRANSITION_SECONDS = 0.55
WEB_SCENES = frozenset({"hermes", "lights", "link", "codex"})
WEB_SCENE_MIN_INTERVALS = {
    "hermes": 0.0,
    "lights": 0.0,
    "link": 0.0,
    "codex": 0.0,
}
WEB_SCENE_MOTION_SECONDS = {
    "hermes": 3.0,
    "lights": SCENE_TRANSITION_SECONDS,
    "link": SCENE_TRANSITION_SECONDS,
    "codex": SCENE_TRANSITION_SECONDS,
}
WEB_SCENE_IDLE_KEEPALIVE_SECONDS = 1.0
WEB_LINK_BROWSER_CAPTURE_INTERVAL = 1.0 / 10.0
NEUTRAL_IMAGE_CHROMA = 32
MIN_ACCENT_BLOCK_SIZE = 5
DISPLAY_UI_PATH = app_resource_dir() / "display_ui" / "index.html"
CHROME_CANDIDATES = (
    Path("C:/Program Files/Google/Chrome/Application/chrome.exe"),
    Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"),
    Path("/usr/bin/google-chrome"),
    Path("/usr/bin/chromium"),
    Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
)


class BrowserPage(Protocol):
    def render(self, state: dict[str, object]) -> Image.Image:
        ...

    def close(self) -> None:
        ...


class ModuleDisplayScenes(Protocol):
    def scene_renderer(self, module_id: str | None, scene_key: str) -> str | None:
        ...

    def sync_display_scene(self, module_id: str | None, scene_key: str, width: int, height: int) -> None:
        ...

    def display_frame(self, module_id: str | None, scene_key: str) -> Image.Image | None:
        ...

    def display_frame_pending(self, module_id: str | None, scene_key: str) -> bool | None:
        ...

    def deactivate_display_scenes(self) -> None:
        ...


class WebDisplayRenderer:
    """Render product scenes with HTML/CSS in a persistent headless browser.

    Calibration patterns and video stay on the Pillow path because they are
    pixel sources rather than UI. The resulting browser frame still enters the
    existing tone, MJPEG and RGB565 transport pipeline unchanged.
    """

    def __init__(
        self,
        video_source: object | None = None,
        module_scenes: ModuleDisplayScenes | None = None,
        *,
        page_factory: Callable[[], BrowserPage] | None = None,
        fallback_renderer: DisplayRenderer | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.fallback_renderer = fallback_renderer or DisplayRenderer(video_source=video_source)
        self.module_scenes = module_scenes
        self._page_factory = page_factory or ChromeDisplayPage
        self._pages: list[BrowserPage] = []
        self._lock = threading.RLock()
        self._browser_failed = False
        self._executor = ThreadPoolExecutor(max_workers=WEB_RENDER_PIPELINE_DEPTH, thread_name_prefix="display-browser")
        self._pending_frames: deque[tuple[BrowserPage, Future[Image.Image]]] = deque()
        self._pending_scene: str | None = None
        self._pending_orientation: str | None = None
        self._transition_until = 0.0
        self._clock = clock
        self._last_render_signature: str | None = None
        self._last_render_at = float("-inf")
        self._motion_until = 0.0
        self._last_prepared_frame: Image.Image | None = None
        self._last_browser_frame_at = float("-inf")

    def render(self, state: DemoState) -> Image.Image:
        scene_renderer = self._sync_module_scene(state)
        if scene_renderer == "frame_source" and self.module_scenes is not None:
            image = self.module_scenes.display_frame(state.active_module_id, state.active_scene)
            if image is not None:
                self._discard_pending_frame()
                return image
            return self.fallback_renderer.render(state)
        if scene_renderer == "fallback":
            self._discard_pending_frame()
            return self.fallback_renderer.render(state)
        if state.active_scene not in WEB_SCENES and scene_renderer != "host_web":
            self._discard_pending_frame()
            return self.fallback_renderer.render(state)

        with self._lock:
            if self._browser_failed:
                return self.fallback_renderer.render(state)
            try:
                if not self._pages:
                    self._pages.append(self._page_factory())
                render_started_at = self._clock()
                orientation = display_geometry(state.display_ui.orientation).orientation
                if (
                    state.active_scene == "link"
                    and self._pending_scene == "link"
                    and self._pending_orientation == orientation
                    and self._transition_until <= render_started_at
                    and self._last_prepared_frame is not None
                    and render_started_at - self._last_browser_frame_at < WEB_LINK_BROWSER_CAPTURE_INTERVAL
                ):
                    self._record_web_render_locked(state, started_at=render_started_at)
                    return self._last_prepared_frame
                browser_state = asdict(state)
                if self._pending_scene is None or self._pending_orientation != orientation:
                    self._discard_pending_frame_locked()
                    self._ensure_pipeline_pages_locked()
                    sync_state = _without_display_motion(browser_state)
                    image = self._pages[0].render(sync_state).convert("RGB")
                    for page in self._pages[1:]:
                        page.render(sync_state)
                    self._pending_scene = state.active_scene
                    self._pending_orientation = orientation
                    self._transition_until = 0.0
                    self._prime_pipeline_locked(browser_state)
                elif self._pending_scene != state.active_scene:
                    self._discard_pending_frame_locked()
                    image = self._pages[0].render(browser_state).convert("RGB")
                    self._pending_scene = state.active_scene
                    self._pending_orientation = orientation
                    if state.display_ui.motion_enabled:
                        self._transition_until = self._clock() + SCENE_TRANSITION_SECONDS
                        self._prime_pipeline_locked(browser_state)
                    else:
                        self._transition_until = 0.0
                        sync_state = _without_display_motion(browser_state)
                        for page in self._pages[1:]:
                            page.render(sync_state)
                        self._prime_pipeline_locked(browser_state)
                elif self._transition_until > self._clock():
                    # The single page preserves a monotonic animation clock while
                    # its next screenshot overlaps encoding and UDP of this frame.
                    image = self._next_pipeline_frame_locked(browser_state)
                elif self._transition_until > 0:
                    self._drain_pipeline_locked()
                    image = self._pages[0].render(browser_state).convert("RGB")
                    sync_state = _without_display_motion(browser_state)
                    for page in self._pages[1:]:
                        page.render(sync_state)
                    self._transition_until = 0.0
                    self._prime_pipeline_locked(browser_state)
                else:
                    image = self._next_pipeline_frame_locked(browser_state)
                prepared = _prepare_web_frame(image, state)
                self._last_prepared_frame = prepared
                self._last_browser_frame_at = self._clock()
                self._record_web_render_locked(state, started_at=render_started_at)
                return prepared
            except Exception:
                self._browser_failed = True
                logger.exception("Web display renderer failed; using Pillow fallback")
                return self.fallback_renderer.render(state)

    def should_render(self, state: DemoState) -> bool:
        scene_renderer = self._sync_module_scene(state)
        if scene_renderer == "frame_source" and self.module_scenes is not None:
            pending = self.module_scenes.display_frame_pending(state.active_module_id, state.active_scene)
            return True if pending is None else pending
        if scene_renderer == "fallback":
            return True
        if state.active_scene not in WEB_SCENES and scene_renderer != "host_web":
            return True
        with self._lock:
            orientation = display_geometry(state.display_ui.orientation).orientation
            if (
                self._browser_failed
                or self._pending_scene != state.active_scene
                or self._pending_orientation != orientation
            ):
                return True
            now = self._clock()
            minimum_interval = WEB_SCENE_MIN_INTERVALS.get(state.active_scene, 0.0)
            if now - self._last_render_at < minimum_interval:
                return False
            signature = _web_scene_signature(state)
            continuous_motion = state.active_scene == "hermes" and (
                state.hermes.streaming
                or state.hermes.input_active
                or state.hermes.input_voice_active
            )
            return (
                signature != self._last_render_signature
                or now < self._motion_until
                or continuous_motion
                or now - self._last_render_at >= WEB_SCENE_IDLE_KEEPALIVE_SECONDS
            )

    def close(self) -> None:
        with self._lock:
            if self.module_scenes is not None:
                self.module_scenes.deactivate_display_scenes()
            self._discard_pending_frame_locked()
            closed: set[int] = set()
            for page in self._pages:
                if id(page) not in closed:
                    page.close()
                    closed.add(id(page))
            self._pages.clear()
            video_source = getattr(self.fallback_renderer, "video_source", None)
            close_video = getattr(video_source, "close", None)
            if callable(close_video):
                close_video()
        self._executor.shutdown(wait=True, cancel_futures=True)

    def _sync_module_scene(self, state: DemoState) -> str | None:
        if self.module_scenes is None:
            return None
        geometry = display_geometry(state.display_ui.orientation)
        self.module_scenes.sync_display_scene(
            state.active_module_id,
            state.active_scene,
            geometry.width,
            geometry.height,
        )
        return self.module_scenes.scene_renderer(state.active_module_id, state.active_scene)

    def _discard_pending_frame(self) -> None:
        with self._lock:
            self._discard_pending_frame_locked()

    def _discard_pending_frame_locked(self) -> None:
        self._drain_pipeline_locked()
        self._pending_scene = None
        self._pending_orientation = None
        self._transition_until = 0.0
        self._last_prepared_frame = None
        self._last_browser_frame_at = float("-inf")

    def _drain_pipeline_locked(self) -> None:
        while self._pending_frames:
            _page, pending = self._pending_frames.popleft()
            pending.cancel()
            if not pending.cancelled():
                pending.result()

    def _ensure_pipeline_pages_locked(self) -> None:
        while len(self._pages) < WEB_RENDER_PIPELINE_DEPTH:
            self._pages.append(self._page_factory())

    def _prime_pipeline_locked(self, browser_state: dict[str, object]) -> None:
        self._ensure_pipeline_pages_locked()
        for page in self._pages:
            self._pending_frames.append((page, self._executor.submit(page.render, browser_state)))

    def _next_pipeline_frame_locked(self, browser_state: dict[str, object]) -> Image.Image:
        page, pending = self._pending_frames.popleft()
        image = pending.result().convert("RGB")
        self._pending_frames.append((page, self._executor.submit(page.render, browser_state)))
        return image

    def _record_web_render_locked(self, state: DemoState, *, started_at: float) -> None:
        signature = _web_scene_signature(state)
        now = self._clock()
        if signature != self._last_render_signature and state.display_ui.motion_enabled:
            duration = (
                SCENE_TRANSITION_SECONDS
                if self._last_render_signature is None
                else WEB_SCENE_MOTION_SECONDS.get(state.active_scene, SCENE_TRANSITION_SECONDS)
            )
            self._motion_until = max(
                self._motion_until,
                now + duration,
            )
        self._last_render_signature = signature
        self._last_render_at = started_at


def _web_scene_signature(state: DemoState) -> str:
    common: dict[str, object] = {
        "active_scene": state.active_scene,
        "active_module_id": state.active_module_id,
        "display_ui": asdict(state.display_ui),
        "display_tone": asdict(state.display_tone),
        "transport_mode": state.link.scene_transport_override or state.link.transport_mode,
    }
    if state.active_scene == "hermes":
        common["hermes"] = asdict(state.hermes)
        common["esp32_online"] = state.link.esp32_online
    elif state.active_scene == "lights":
        common["light"] = asdict(state.light)
    elif state.active_scene == "link":
        common["link"] = {
            "esp32_online": state.link.esp32_online,
            "esp32_ip": state.link.esp32_ip,
            "display_fps": round(state.link.display_fps, 1),
            "frame_id": state.link.frame_id,
            "dirty_tiles": state.link.dirty_tiles,
            "rx_packets": state.link.rx_packets,
            "accepted_packets": state.link.accepted_packets,
            "rejected_packets": state.link.rejected_packets,
            "draw_failed_packets": state.link.draw_failed_packets,
        }
    elif state.active_module_id is not None:
        common["module"] = state.modules.get(state.active_module_id, {})
    return json.dumps(common, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class ChromeDisplayPage:
    def __init__(self, *, chrome_path: Path | None = None, page_path: Path = DISPLAY_UI_PATH) -> None:
        self.chrome_path = chrome_path or _find_chrome()
        self.page_path = page_path.resolve()
        if not self.page_path.exists():
            raise FileNotFoundError(f"Display UI not found: {self.page_path}")
        self._profile_dir = Path(tempfile.mkdtemp(prefix="espagent-display-"))
        self._process: subprocess.Popen[bytes] | None = None
        self._socket: WebSocket | None = None
        self._job_handle: int | None = None
        self._next_command_id = 0
        self._width = WIDTH
        self._height = HEIGHT
        try:
            self._start()
        except Exception:
            self.close()
            raise

    def render(self, state: dict[str, object]) -> Image.Image:
        display_ui = state.get("display_ui")
        orientation = display_ui.get("orientation") if isinstance(display_ui, dict) else None
        geometry = display_geometry(orientation)
        self._set_geometry(geometry.width, geometry.height)
        state_json = json.dumps(state, ensure_ascii=False, separators=(",", ":"))
        # DOM updates are synchronous and captureScreenshot forces layout and paint.
        # Fonts and the first paint are awaited once during startup instead of
        # imposing two display-vsync waits on every transmitted frame.
        self._command(
            "Runtime.evaluate",
            {"expression": f"window.renderDisplay({state_json})", "returnByValue": True},
        )
        result = self._command(
            "Page.captureScreenshot",
            {
                "format": "png",
                "fromSurface": True,
                "captureBeyondViewport": False,
                "optimizeForSpeed": True,
            },
        )
        png = base64.b64decode(result["data"])
        with Image.open(BytesIO(png)) as image:
            return image.convert("RGB").copy()

    def close(self) -> None:
        if self._socket is not None:
            try:
                self._socket.close()
            except Exception:
                logger.debug("Failed to close Chrome DevTools socket", exc_info=True)
            self._socket = None
        if self._job_handle is not None:
            _close_windows_handle(self._job_handle)
            self._job_handle = None
        if self._process is not None and self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait(timeout=2)
        self._process = None
        shutil.rmtree(self._profile_dir, ignore_errors=True)

    def _start(self) -> None:
        flags = [
            str(self.chrome_path),
            "--headless=new",
            "--remote-debugging-port=0",
            "--remote-allow-origins=*",
            f"--user-data-dir={self._profile_dir}",
            "--window-size=320,320",
            "--window-position=-32000,-32000",
            "--force-device-scale-factor=1",
            "--hide-scrollbars",
            "--no-first-run",
            "--no-default-browser-check",
            "--noerrdialogs",
            "--disable-background-networking",
            "--disable-background-mode",
            "--disable-background-timer-throttling",
            "--disable-component-update",
            "--disable-default-apps",
            "--disable-extensions",
            "--disable-renderer-backgrounding",
            "--disable-sync",
            "--metrics-recording-only",
            "--no-service-autorun",
            "--renderer-process-limit=1",
            self.page_path.as_uri(),
        ]
        self._process = subprocess.Popen(
            flags,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **_hidden_process_options(),
        )
        self._job_handle = _assign_kill_on_close_job(self._process)
        port = self._wait_for_debug_port()
        endpoint = self._wait_for_page_endpoint(port)
        self._socket = create_connection(endpoint, timeout=5, http_proxy_host=None)
        self._command("Page.enable")
        self._command("Runtime.enable")
        self._command(
            "Emulation.setDeviceMetricsOverride",
            {
                "width": WIDTH,
                "height": HEIGHT,
                "deviceScaleFactor": 1,
                "mobile": False,
                "screenWidth": WIDTH,
                "screenHeight": HEIGHT,
            },
        )
        self._wait_for_ui_ready()
        _hide_native_windows(self._process.pid)

    def _set_geometry(self, width: int, height: int) -> None:
        if (width, height) == (self._width, self._height):
            return
        self._command(
            "Emulation.setDeviceMetricsOverride",
            {
                "width": width,
                "height": height,
                "deviceScaleFactor": 1,
                "mobile": False,
                "screenWidth": width,
                "screenHeight": height,
            },
        )
        self._width = width
        self._height = height

    def _wait_for_debug_port(self) -> int:
        port_file = self._profile_dir / "DevToolsActivePort"
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if self._process is not None and self._process.poll() is not None:
                raise RuntimeError("Chrome exited before DevTools became ready")
            if port_file.exists():
                try:
                    lines = port_file.read_text(encoding="utf-8").splitlines()
                    if lines:
                        return int(lines[0])
                except (OSError, ValueError):
                    pass
            time.sleep(0.05)
        raise TimeoutError("Timed out waiting for Chrome DevTools port")

    def _wait_for_page_endpoint(self, port: int) -> str:
        deadline = time.monotonic() + 10
        url = f"http://127.0.0.1:{port}/json/list"
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(url, timeout=1) as response:
                    targets = json.load(response)
                for target in targets:
                    if target.get("type") == "page" and target.get("webSocketDebuggerUrl"):
                        return str(target["webSocketDebuggerUrl"])
            except (OSError, ValueError):
                pass
            time.sleep(0.05)
        raise TimeoutError("Timed out waiting for Chrome display page")

    def _wait_for_ui_ready(self) -> None:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            result = self._command(
                "Runtime.evaluate",
                {"expression": "Boolean(window.renderDisplay)", "returnByValue": True},
            )
            if result.get("result", {}).get("value") is True:
                self._command(
                    "Runtime.evaluate",
                    {
                        "expression": (
                            "(async()=>{"
                            "await document.fonts.ready;"
                            "await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));"
                            "return true;"
                            "})()"
                        ),
                        "awaitPromise": True,
                        "returnByValue": True,
                    },
                )
                return
            time.sleep(0.05)
        raise TimeoutError("Display UI JavaScript did not become ready")

    def _command(self, method: str, params: dict[str, object] | None = None) -> dict[str, object]:
        if self._socket is None:
            raise RuntimeError("Chrome DevTools socket is not connected")
        self._next_command_id += 1
        command_id = self._next_command_id
        self._socket.send(json.dumps({"id": command_id, "method": method, "params": params or {}}))
        while True:
            message = json.loads(self._socket.recv())
            if message.get("id") != command_id:
                continue
            if "error" in message:
                raise RuntimeError(f"Chrome DevTools {method} failed: {message['error']}")
            return dict(message.get("result", {}))


def _find_chrome() -> Path:
    configured = os.environ.get("ESPAGENT_CHROME_PATH")
    if configured:
        path = Path(configured)
        if path.exists():
            return path
        raise FileNotFoundError(f"ESPAGENT_CHROME_PATH does not exist: {path}")
    for path in CHROME_CANDIDATES:
        if path.exists():
            return path
    raise FileNotFoundError("Chrome or Edge is required for Web display rendering")


def _hidden_process_options() -> dict[str, object]:
    if os.name != "nt":
        return {}
    startup_info = subprocess.STARTUPINFO()
    startup_info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup_info.wShowWindow = subprocess.SW_HIDE
    return {
        "creationflags": subprocess.CREATE_NO_WINDOW,
        "startupinfo": startup_info,
    }


def _assign_kill_on_close_job(process: subprocess.Popen[bytes]) -> int | None:
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes

    class JobObjectBasicLimitInformation(ctypes.Structure):
        _fields_ = [
            ("per_process_user_time_limit", ctypes.c_longlong),
            ("per_job_user_time_limit", ctypes.c_longlong),
            ("limit_flags", wintypes.DWORD),
            ("minimum_working_set_size", ctypes.c_size_t),
            ("maximum_working_set_size", ctypes.c_size_t),
            ("active_process_limit", wintypes.DWORD),
            ("affinity", ctypes.c_size_t),
            ("priority_class", wintypes.DWORD),
            ("scheduling_class", wintypes.DWORD),
        ]

    class IoCounters(ctypes.Structure):
        _fields_ = [
            ("read_operation_count", ctypes.c_ulonglong),
            ("write_operation_count", ctypes.c_ulonglong),
            ("other_operation_count", ctypes.c_ulonglong),
            ("read_transfer_count", ctypes.c_ulonglong),
            ("write_transfer_count", ctypes.c_ulonglong),
            ("other_transfer_count", ctypes.c_ulonglong),
        ]

    class JobObjectExtendedLimitInformation(ctypes.Structure):
        _fields_ = [
            ("basic_limit_information", JobObjectBasicLimitInformation),
            ("io_info", IoCounters),
            ("process_memory_limit", ctypes.c_size_t),
            ("job_memory_limit", ctypes.c_size_t),
            ("peak_process_memory_used", ctypes.c_size_t),
            ("peak_job_memory_used", ctypes.c_size_t),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        logger.warning("Failed to create Chromium cleanup job: %d", ctypes.get_last_error())
        return None
    info = JobObjectExtendedLimitInformation()
    info.basic_limit_information.limit_flags = 0x00002000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not kernel32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
        logger.warning("Failed to configure Chromium cleanup job: %d", ctypes.get_last_error())
        kernel32.CloseHandle(job)
        return None
    process_handle = wintypes.HANDLE(process._handle)
    if not kernel32.AssignProcessToJobObject(job, process_handle):
        logger.warning("Failed to assign Chromium cleanup job: %d", ctypes.get_last_error())
        kernel32.CloseHandle(job)
        return None
    return int(job)


def _close_windows_handle(handle: int) -> None:
    if os.name != "nt":
        return
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle(wintypes.HANDLE(handle))


def _hide_native_windows(process_id: int) -> None:
    """Keep Chromium's hidden Aura surface out of the Windows DWM scene.

    Some Chrome/Windows combinations composite a headless window even though
    WS_VISIBLE is clear. Moving every top-level window owned by the browser
    off-screen prevents the 240x320 rounded rectangle from leaking onto the
    desktop while DevTools screenshots continue to render fromSurface.
    """
    if os.name != "nt":
        return
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    target_pid = int(process_id)

    def hide_if_owned(window: int, _lparam: int) -> bool:
        owner_pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(window, ctypes.byref(owner_pid))
        if owner_pid.value == target_pid:
            user32.ShowWindow(window, 0)  # SW_HIDE
            user32.SetWindowPos(
                window,
                1,  # HWND_BOTTOM
                -32000,
                -32000,
                0,
                0,
                0x0001 | 0x0010 | 0x0200,  # NOSIZE | NOACTIVATE | NOOWNERZORDER
            )
        return True

    user32.EnumWindows(callback_type(hide_if_owned), 0)


def _quantize_duotone(image: Image.Image, threshold: int = 160) -> Image.Image:
    mask = image.convert("L").point(lambda pixel: 255 if pixel < threshold else 0)
    paper = Image.new("RGB", image.size, PAPER)
    ink = Image.new("RGB", image.size, INK)
    paper.paste(ink, mask=mask)
    return paper


def _prepare_web_frame(image: Image.Image, state: DemoState) -> Image.Image:
    # Indexed frames keep their adaptive palette for genuinely colored UIs.
    # Neutral UIs share MJPEG's raster and tone path so glyph coverage does not
    # change when the transport mode changes.
    if state.link.transport_mode == "indexed" and not _is_neutral_image(image):
        return image
    if state.display_ui.grayscale_antialiasing:
        image = _preserve_grayscale_antialiasing(image)
    else:
        image = _quantize_duotone(image)
    return _apply_display_tone(image, state)


def _is_neutral_image(image: Image.Image) -> bool:
    red, green, blue = image.convert("RGB").split()
    chroma = ImageChops.lighter(
        ImageChops.lighter(ImageChops.difference(red, green), ImageChops.difference(red, blue)),
        ImageChops.difference(green, blue),
    )
    if chroma.getextrema()[1] <= NEUTRAL_IMAGE_CHROMA:
        return True
    if min(image.size) < MIN_ACCENT_BLOCK_SIZE:
        return False
    chromatic_pixels = chroma.point(lambda value: 255 if value > NEUTRAL_IMAGE_CHROMA else 0)
    substantial_accents = chromatic_pixels.filter(ImageFilter.MinFilter(MIN_ACCENT_BLOCK_SIZE))
    return substantial_accents.getbbox() is None


def _without_display_motion(state: dict[str, object]) -> dict[str, object]:
    synchronized = dict(state)
    display_ui = dict(state.get("display_ui") or {})
    display_ui["motion_enabled"] = False
    synchronized["display_ui"] = display_ui
    return synchronized


def _preserve_grayscale_antialiasing(image: Image.Image) -> Image.Image:
    """Map browser coverage to the neutral Paper-to-Ink color ramp.

    Converting to luminance first deliberately removes RGB subpixel fringes while
    retaining the intermediate coverage levels used by grayscale antialiasing.
    """
    grayscale = image.convert("L")
    paper_luma = round(0.299 * PAPER[0] + 0.587 * PAPER[1] + 0.114 * PAPER[2])
    ink_luma = round(0.299 * INK[0] + 0.587 * INK[1] + 0.114 * INK[2])
    luma_range = max(1, paper_luma - ink_luma)

    def channel_lut(paper_channel: int, ink_channel: int) -> list[int]:
        values: list[int] = []
        for luminance in range(256):
            coverage = max(0.0, min(1.0, (paper_luma - luminance) / luma_range))
            values.append(round(paper_channel + coverage * (ink_channel - paper_channel)))
        return values

    return Image.merge(
        "RGB",
        tuple(grayscale.point(channel_lut(paper, ink)) for paper, ink in zip(PAPER, INK, strict=True)),
    )
