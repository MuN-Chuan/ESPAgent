from __future__ import annotations

import argparse
import ctypes
import html
import json
import logging
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path


if getattr(sys, "frozen", False):
    ROOT = Path(sys.executable).resolve().parent
    RESOURCE_ROOT = Path(getattr(sys, "_MEIPASS")).resolve()
else:
    ROOT = Path(__file__).resolve().parent
    RESOURCE_ROOT = ROOT
APP_DIR = RESOURCE_ROOT / "apps" / "demo_host"
LOADING_HTML = """<!doctype html>
<html lang="zh-CN"><meta charset="utf-8"><title>ESPAgent</title>
<style>
html,body{height:100%;margin:0;background:#f4f5f2;color:#171a20;font-family:"Microsoft YaHei UI",sans-serif}
body{display:grid;place-items:center}.boot{width:360px;border-top:3px solid #171a20;padding:24px 4px}
small{font:11px Consolas,monospace;color:#5a6068}h1{margin:12px 0 8px;font-size:26px}p{margin:0;color:#5a6068;font-size:13px}
</style><body><main class="boot"><small>ESP32-S3 COMPANION</small><h1>ESPAgent 正在启动</h1><p>正在连接本机服务...</p></main></body></html>"""


def main() -> None:
    parser = argparse.ArgumentParser(description="Start the ESPAgent desktop application.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--status-port", type=int, default=int(os.environ.get("ESP32_STATUS_PORT", "39001")))
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--reuse-host", action="store_true", help="Reuse an already running ESPAgent backend.")
    parser.add_argument("--log-file", type=Path)
    args = parser.parse_args()

    configure_logging(args.log_file)
    configure_localhost_bypass()
    set_windows_app_id()
    sys.path.insert(0, str(APP_DIR))
    from espagent_demo.version import APP_VERSION

    logging.getLogger(__name__).info(
        "app_start version=%s frozen=%s pid=%d",
        APP_VERSION,
        bool(getattr(sys, "frozen", False)),
        os.getpid(),
    )

    base_url = f"http://{args.host}:{args.port}"
    server = None
    server_thread = None
    if not (args.reuse_host and is_espagent_host(base_url)):
        from start_demo_host import kill_port_owners, set_high_priority

        kill_port_owners("tcp", args.port)
        kill_port_owners("udp", args.status_port)
        set_high_priority()
        server, server_thread = start_server(args.host, args.port)
        wait_for_host(base_url, timeout=30.0)

    import webview

    window = webview.create_window(
        "ESPAgent",
        html=LOADING_HTML,
        width=1280,
        height=820,
        min_size=(920, 650),
        background_color="#f4f5f2",
        text_select=True,
    )
    hotkeys = start_global_hotkeys(base_url)
    try:
        webview.start(
            load_desktop_ui,
            args=(window, f"{base_url}/app/"),
            gui="edgechromium",
            debug=args.debug,
            private_mode=True,
        )
    finally:
        if hotkeys is not None:
            hotkeys.stop()
        if server is not None:
            server.should_exit = True
        if server_thread is not None:
            server_thread.join(timeout=10)
        del window


def load_desktop_ui(window, target_url: str) -> None:
    last_error: Exception | None = None
    for attempt in range(1, 4):
        try:
            if not is_espagent_host(target_url.removesuffix("/app/")):
                raise RuntimeError("本机服务尚未就绪")
            separator = "&" if "?" in target_url else "?"
            window.load_url(f"{target_url}{separator}desktop_attempt={attempt}")
            app_ready = window.evaluate_js("Boolean(document.querySelector('.app-shell'))")
            if app_ready:
                print(f"ESPAgent desktop UI loaded: {window.get_current_url()}")
                return
            raise RuntimeError("页面已响应，但应用界面未初始化")
        except Exception as exc:
            last_error = exc
            print(f"Desktop UI load attempt {attempt} failed: {exc}", file=sys.stderr)
            time.sleep(0.5)

    message = html.escape(str(last_error or "未知错误"))
    window.load_html(
        LOADING_HTML.replace(
            "ESPAgent 正在启动",
            "ESPAgent 启动失败",
        ).replace(
            "正在连接本机服务...",
            f"无法加载本机界面：{message}。请查看 logs/espagent_app.log。",
        )
    )


def start_server(host: str, port: int):
    import uvicorn

    config = uvicorn.Config(
        "espagent_demo.server:app",
        host=host,
        port=port,
        access_log=False,
        log_level="info",
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="espagent-host", daemon=True)
    thread.start()
    return server, thread


def start_global_hotkeys(base_url: str):
    if os.name != "nt":
        return None
    from espagent_demo.global_hotkeys import GlobalHotkeyController, HostControlClient
    from espagent_demo.windows_ime import WindowsHermesInputController

    client = HostControlClient(base_url)
    controller = GlobalHotkeyController(
        client,
        ime_controller=WindowsHermesInputController(client),
    )
    if controller.start():
        return controller
    return None


def is_espagent_host(base_url: str) -> bool:
    try:
        with urllib.request.urlopen(f"{base_url}/api/health", timeout=0.6) as response:
            if response.status != 200:
                return False
            payload = json.loads(response.read().decode("utf-8"))
            return isinstance(payload, dict) and "services" in payload
    except (OSError, ValueError, urllib.error.URLError):
        return False


def wait_for_host(base_url: str, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if is_espagent_host(base_url):
            return
        time.sleep(0.1)
    raise RuntimeError(f"ESPAgent backend did not start within {timeout:.0f} seconds")


def configure_logging(log_file: Path | None) -> None:
    if log_file is None:
        if not getattr(sys, "frozen", False) and sys.stdout is not None and sys.stderr is not None:
            os.environ["ESPAGENT_LOG_FILE"] = "console"
            logging.basicConfig(
                level=logging.INFO,
                format="%(asctime)s %(levelname)s [%(threadName)s] %(name)s: %(message)s",
                force=True,
            )
            return
        local_app_data = Path(os.environ.get("LOCALAPPDATA") or Path.home() / ".config")
        log_file = local_app_data / "ESPAgent" / "logs" / "espagent_app.log"
    log_file = log_file.expanduser().resolve()
    os.environ["ESPAGENT_LOG_FILE"] = str(log_file)
    try:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        stream = log_file.open("a", encoding="utf-8", buffering=1)
    except OSError:
        stream = open(os.devnull, "w", encoding="utf-8")
    sys.stdout = stream
    sys.stderr = stream
    logging.basicConfig(
        stream=stream,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(threadName)s] %(name)s: %(message)s",
        force=True,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)


def configure_localhost_bypass() -> None:
    local_hosts = "127.0.0.1,localhost"
    for key in ("NO_PROXY", "no_proxy"):
        current = os.environ.get(key, "")
        values = [value.strip() for value in current.split(",") if value.strip()]
        for host in local_hosts.split(","):
            if host not in values:
                values.append(host)
        os.environ[key] = ",".join(values)

    browser_args = os.environ.get("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS", "")
    bypass_argument = "--proxy-bypass-list=127.0.0.1;localhost"
    if bypass_argument not in browser_args:
        os.environ["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = f"{browser_args} {bypass_argument}".strip()


def set_windows_app_id() -> None:
    if os.name != "nt":
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("ESPAgent.CompanionDesk")
    except (AttributeError, OSError):
        pass


if __name__ == "__main__":
    main()
