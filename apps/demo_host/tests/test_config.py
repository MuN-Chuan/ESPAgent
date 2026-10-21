from espagent_demo.config import APP_ENV_FILE, Settings, migrate_legacy_env_file
from espagent_demo.paths import installation_env_file, legacy_env_file_candidates


SETTING_ALIASES = [
    "ESPAGENT_HOST",
    "ESPAGENT_PORT",
    "ESP32_TARGET_IP",
    "ESP32_DISPLAY_PORT",
    "ESP32_STATUS_PORT",
    "HOME_ASSISTANT_URL",
    "HOME_ASSISTANT_TOKEN",
    "HOME_ASSISTANT_LIGHT_ENTITY",
    "HOME_ASSISTANT_RECONNECT_MAX_SECONDS",
    "HERMES_MODE",
    "HERMES_WSL_DISTRO",
    "HERMES_COMMAND",
    "HERMES_TIMEOUT_SECONDS",
    "HERMES_STT_LANGUAGE",
    "HERMES_STT_TIMEOUT_SECONDS",
    "HERMES_VOICE_SILENCE_SECONDS",
]


def test_defaults_are_demo_safe(monkeypatch, tmp_path):
    for alias in SETTING_ALIASES:
        monkeypatch.delenv(alias, raising=False)

    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env.local").write_text("HOME_ASSISTANT_TOKEN=local-secret\n", encoding="utf-8")

    settings = Settings(_env_file=None)
    assert settings.hermes_mode == "demo"
    assert settings.home_assistant_token == ""
    assert settings.home_assistant_reconnect_max_seconds == 30.0
    assert settings.esp32_display_port == 39000
    assert settings.hermes_timeout_seconds == 180.0
    assert settings.hermes_stt_language == "zh"
    assert settings.hermes_stt_timeout_seconds == 600.0
    assert settings.hermes_voice_silence_seconds == 2.0


def test_env_file_is_resolved_from_demo_host_directory():
    assert APP_ENV_FILE.parent.name == "demo_host"
    assert APP_ENV_FILE.name == ".env.local"


def test_migrate_legacy_env_file_copies_first_existing_candidate(tmp_path):
    missing = tmp_path / "missing.env"
    source = tmp_path / "legacy" / ".env.local"
    target = tmp_path / "data" / ".env.local"
    source.parent.mkdir()
    source.write_text("HERMES_MODE=real\n", encoding="utf-8")

    result = migrate_legacy_env_file(target, (missing, source))

    assert result == target
    assert target.read_text(encoding="utf-8") == "HERMES_MODE=real\n"


def test_migrate_legacy_env_file_preserves_existing_target(tmp_path):
    source = tmp_path / "legacy.env"
    target = tmp_path / ".env.local"
    source.write_text("HERMES_MODE=real\n", encoding="utf-8")
    target.write_text("HERMES_MODE=demo\n", encoding="utf-8")

    migrate_legacy_env_file(target, (source,))

    assert target.read_text(encoding="utf-8") == "HERMES_MODE=demo\n"


def test_frozen_install_directory_env_is_resolved_from_executable(monkeypatch, tmp_path):
    executable = tmp_path / "ESPAgent.exe"
    install_env = tmp_path / ".env.local"
    install_env.write_text("HERMES_MODE=real\n", encoding="utf-8")
    monkeypatch.setattr("espagent_demo.paths.sys.frozen", True, raising=False)
    monkeypatch.setattr("espagent_demo.paths.sys.executable", str(executable))
    monkeypatch.delenv("ESPAGENT_LEGACY_ENV_FILE", raising=False)

    assert installation_env_file() == install_env
    assert install_env not in legacy_env_file_candidates()


def test_portable_env_overrides_without_removing_user_config(tmp_path):
    user_env = tmp_path / "user.env"
    portable_env = tmp_path / "portable.env"
    user_env.write_text(
        "HOME_ASSISTANT_URL=http://ha.local:8123\n"
        "HOME_ASSISTANT_TOKEN=ha-secret\n"
        "HERMES_MODE=demo\n",
        encoding="utf-8",
    )
    portable_env.write_text(
        "HERMES_MODE=real\n"
        "HERMES_COMMAND=/opt/hermes/venv/bin/hermes -z\n",
        encoding="utf-8",
    )

    settings = Settings(_env_file=(user_env, portable_env))

    assert settings.home_assistant_url == "http://ha.local:8123"
    assert settings.home_assistant_token == "ha-secret"
    assert settings.hermes_mode == "real"
    assert settings.hermes_command == "/opt/hermes/venv/bin/hermes -z"
