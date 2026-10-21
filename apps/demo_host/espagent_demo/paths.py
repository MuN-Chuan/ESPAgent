from __future__ import annotations

import os
import sys
from pathlib import Path


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def bundle_root() -> Path:
    if is_frozen():
        return Path(getattr(sys, "_MEIPASS")).resolve()
    return Path(__file__).resolve().parents[3]


def app_resource_dir() -> Path:
    if is_frozen():
        return bundle_root() / "apps" / "demo_host"
    return Path(__file__).resolve().parent.parent


def project_resource_dir() -> Path:
    return bundle_root()


def user_data_dir() -> Path:
    override = os.environ.get("ESPAGENT_DATA_DIR")
    if override:
        return Path(override).expanduser().resolve()
    base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / ".config")
    return base / "ESPAgent"


def installation_env_file() -> Path | None:
    if not is_frozen():
        return None
    return Path(sys.executable).resolve().parent / ".env.local"


def legacy_env_file_candidates() -> tuple[Path, ...]:
    override = os.environ.get("ESPAGENT_LEGACY_ENV_FILE")
    candidates: list[Path] = []
    if override:
        candidates.append(Path(override).expanduser())

    home = Path.home()
    relative = Path("Codex") / "ESPAgent" / "apps" / "demo_host" / ".env.local"
    candidates.extend(
        (
            home / "Documents" / relative,
            home / "OneDrive" / "Documents" / relative,
            Path.cwd() / "apps" / "demo_host" / ".env.local",
        )
    )
    return tuple(dict.fromkeys(path.resolve() for path in candidates))
