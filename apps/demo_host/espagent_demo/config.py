import shutil
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from espagent_demo.paths import (
    app_resource_dir,
    installation_env_file,
    is_frozen,
    legacy_env_file_candidates,
    user_data_dir,
)


def migrate_legacy_env_file(target: Path, candidates: tuple[Path, ...]) -> Path:
    if target.is_file():
        return target
    for candidate in candidates:
        if not candidate.is_file() or candidate == target:
            continue
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(candidate, target)
        except OSError:
            continue
        return target
    return target


if is_frozen():
    INSTALL_ENV_FILE = installation_env_file()
    USER_ENV_FILE = migrate_legacy_env_file(
        user_data_dir() / ".env.local",
        legacy_env_file_candidates(),
    )
    APP_ENV_FILE = (
        (USER_ENV_FILE, INSTALL_ENV_FILE)
        if INSTALL_ENV_FILE is not None
        and INSTALL_ENV_FILE.is_file()
        and INSTALL_ENV_FILE != USER_ENV_FILE
        else USER_ENV_FILE
    )
else:
    APP_ENV_FILE = app_resource_dir() / ".env.local"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=APP_ENV_FILE, env_file_encoding="utf-8", extra="ignore")

    host: str = Field(default="0.0.0.0", alias="ESPAGENT_HOST")
    port: int = Field(default=8787, alias="ESPAGENT_PORT")
    esp32_target_ip: str = Field(default="192.168.1.50", alias="ESP32_TARGET_IP")
    esp32_display_port: int = Field(default=39000, alias="ESP32_DISPLAY_PORT")
    esp32_status_port: int = Field(default=39001, alias="ESP32_STATUS_PORT")

    home_assistant_url: str = Field(default="http://127.0.0.1:8123", alias="HOME_ASSISTANT_URL")
    home_assistant_token: str = Field(default="", alias="HOME_ASSISTANT_TOKEN")
    home_assistant_light_entity: str = Field(default="light.demo_light", alias="HOME_ASSISTANT_LIGHT_ENTITY")
    home_assistant_reconnect_max_seconds: float = Field(default=30.0, alias="HOME_ASSISTANT_RECONNECT_MAX_SECONDS")

    hermes_mode: str = Field(default="demo", alias="HERMES_MODE")
    hermes_wsl_distro: str = Field(default="ESPHermes", alias="HERMES_WSL_DISTRO")
    hermes_command: str = Field(default="", alias="HERMES_COMMAND")
    hermes_timeout_seconds: float = Field(default=180.0, alias="HERMES_TIMEOUT_SECONDS")
    hermes_stt_language: str = Field(default="zh", alias="HERMES_STT_LANGUAGE")
    hermes_stt_timeout_seconds: float = Field(default=600.0, alias="HERMES_STT_TIMEOUT_SECONDS")
    hermes_voice_silence_seconds: float = Field(
        default=2.0,
        alias="HERMES_VOICE_SILENCE_SECONDS",
    )

    fishaudio_enabled: bool = Field(default=False, alias="FISHAUDIO_ENABLED")
    fishaudio_api_key: str = Field(default="", alias="FISHAUDIO_API_KEY")
    fishaudio_voice_id: str = Field(default="", alias="FISHAUDIO_VOICE_ID")
    fishaudio_realtime_url: str = Field(
        default="wss://realtime.fishaudio.org/v1/tts/live",
        alias="FISHAUDIO_REALTIME_URL",
    )
    fishaudio_sample_rate: int = Field(default=44100, alias="FISHAUDIO_SAMPLE_RATE")
    fishaudio_speed: float = Field(default=1.0, alias="FISHAUDIO_SPEED")
    fishaudio_volume: float = Field(default=1.0, alias="FISHAUDIO_VOLUME")
    fishaudio_language: str = Field(default="zh", alias="FISHAUDIO_LANGUAGE")
    fishaudio_chunk_length: int = Field(default=200, alias="FISHAUDIO_CHUNK_LENGTH")
    fishaudio_latency: str = Field(default="balanced", alias="FISHAUDIO_LATENCY")
