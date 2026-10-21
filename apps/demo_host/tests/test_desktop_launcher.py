from __future__ import annotations

import os
import sys

import start_espagent_app


ROOT = start_espagent_app.ROOT


class FakeWindow:
    def __init__(self, app_ready: bool = True) -> None:
        self.app_ready = app_ready
        self.urls: list[str] = []
        self.html: list[str] = []

    def load_url(self, url: str) -> None:
        self.urls.append(url)

    def evaluate_js(self, script: str) -> bool:
        assert ".app-shell" in script
        return self.app_ready

    def get_current_url(self) -> str:
        return self.urls[-1]

    def load_html(self, content: str) -> None:
        self.html.append(content)


def test_configure_localhost_bypass_preserves_existing_proxy_exclusions(monkeypatch) -> None:
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.setenv("NO_PROXY", "example.test")
    monkeypatch.setenv("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS", "--disable-gpu")

    start_espagent_app.configure_localhost_bypass()

    assert os.environ["NO_PROXY"] == "example.test,127.0.0.1,localhost"
    assert os.environ["no_proxy"].endswith("127.0.0.1,localhost")
    assert os.environ["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] == (
        "--disable-gpu --proxy-bypass-list=127.0.0.1;localhost"
    )


def test_frozen_launcher_uses_default_log_when_started_without_arguments(
    monkeypatch,
    tmp_path,
) -> None:
    original_stdout = sys.stdout
    original_stderr = sys.stderr
    stream = None
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))

    try:
        start_espagent_app.configure_logging(None)
        stream = sys.stderr
        assert stream is not None
        assert sys.stdout is stream
        assert stream.isatty() is False
        stream.write("direct executable startup\n")
        stream.flush()
    finally:
        sys.stdout = original_stdout
        sys.stderr = original_stderr
        if stream is not None:
            stream.close()

    log_file = tmp_path / "ESPAgent" / "logs" / "espagent_app.log"
    assert "direct executable startup" in log_file.read_text(encoding="utf-8")


def test_load_desktop_ui_navigates_after_backend_is_ready(monkeypatch) -> None:
    window = FakeWindow()
    monkeypatch.setattr(start_espagent_app, "is_espagent_host", lambda _: True)

    start_espagent_app.load_desktop_ui(window, "http://127.0.0.1:8787/app/")

    assert window.urls == ["http://127.0.0.1:8787/app/?desktop_attempt=1"]
    assert window.html == []


def test_load_desktop_ui_shows_error_instead_of_blank_page(monkeypatch) -> None:
    window = FakeWindow(app_ready=False)
    monkeypatch.setattr(start_espagent_app, "is_espagent_host", lambda _: True)
    monkeypatch.setattr(start_espagent_app.time, "sleep", lambda _: None)

    start_espagent_app.load_desktop_ui(window, "http://127.0.0.1:8787/app/")

    assert len(window.urls) == 3
    assert len(window.html) == 1
    assert "ESPAgent 启动失败" in window.html[0]
    assert "应用界面未初始化" in window.html[0]


def test_desktop_setup_installs_application_owned_flash_runtime() -> None:
    setup_script = (ROOT / "start_demo_host.cmd").read_text(encoding="utf-8")
    project = (ROOT / "apps" / "demo_host" / "pyproject.toml").read_text(encoding="utf-8")

    assert "webview, esptool, serial" in setup_script
    assert "serial, dxcam" in setup_script
    assert '"esptool>=4.11,<5"' in project
    assert '"pyserial>=3.5,<4"' in project


def test_native_launcher_starts_visible_diagnostic_console() -> None:
    source = (ROOT / "tools" / "windows_launcher" / "Program.cs").read_text(encoding="utf-8")
    build_script = (ROOT / "tools" / "windows_launcher" / "build_launcher.ps1").read_text(encoding="utf-8")
    command_script = (ROOT / "start_espagent_app.cmd").read_text(encoding="utf-8")
    vbs_script = (ROOT / "start_espagent_app.vbs").read_text(encoding="utf-8")

    assert "CreateNoWindow = false" in source
    assert "ProcessWindowStyle.Normal" in source
    assert "start_espagent_app.cmd" in source
    assert "--console" not in source
    assert ':parse_arguments' in command_script
    assert 'if /i "%~1"=="--console"' in command_script
    assert '"%~dp0start_espagent_app.py" %ESPAGENT_APP_ARGS%' in command_script
    assert "start_demo_host.cmd" not in command_script
    assert 'if not exist "%DEMO_PROJECT%\\pyproject.toml" goto :incomplete_install' in command_script
    assert "Copy the complete ESPAgent project directory" in command_script
    assert 'set "VENV_ROOT_MARKER=%~dp0apps\\demo_host\\.venv\\.espagent_root"' in command_script
    assert "pathlib.Path(os.environ['DEMO_PROJECT'])/'.venv'" in command_script
    assert "The application environment was copied from another directory" in command_script
    assert 'py -3 -m venv --clear "%DEMO_PROJECT%\\.venv"' in command_script
    assert '>"%VENV_ROOT_MARKER%" echo %DEMO_PROJECT%' in command_script
    assert 'pip install -r "%REQUIREMENTS_FILE%"' in command_script
    assert "pip install -e" not in command_script
    assert 'Path.Combine(root, "apps", "demo_host", "pyproject.toml")' in source
    assert "Desktop application exited with code" in command_script
    assert "pause" in command_script
    assert "shell.Run appCommand, 1, False" in vbs_script
    assert "/target:winexe" in build_script
    assert '"ESPAgent.exe"' in build_script
