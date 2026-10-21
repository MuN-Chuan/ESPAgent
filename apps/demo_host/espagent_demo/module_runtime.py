from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha1
from importlib import import_module, util
from inspect import signature
import json
from pathlib import Path
import sys
from threading import Lock, RLock
from typing import Callable, Literal, Protocol

import yaml
from pydantic import BaseModel, Field


class NavigationManifest(BaseModel):
    order: int = 100
    icon: str = "module"
    pinned: bool = True


class RuntimeManifest(BaseModel):
    type: str = "python_builtin"
    entry: str


class PageManifest(BaseModel):
    id: str
    path: str
    default: bool = False


class SceneTransportManifest(BaseModel):
    transport_mode: Literal["mjpeg", "indexed"] | None = None
    jpeg_quality: int | None = None
    jpeg_subsampling: Literal["420", "422", "444"] | None = None
    indexed_bpp: Literal[1, 2, 4, 8] | None = None


class SceneManifest(PageManifest):
    scene_key: str
    width: int = 240
    height: int = 320
    renderer: Literal["host_web", "frame_source", "fallback"] = "host_web"
    transport: SceneTransportManifest = Field(default_factory=SceneTransportManifest)


class FrontendManifest(BaseModel):
    app_pages: list[PageManifest] = Field(default_factory=list)
    esp32_scenes: list[SceneManifest] = Field(default_factory=list)


class ModuleManifest(BaseModel):
    id: str
    name: str
    version: str
    description: str = ""
    module_type: str = "builtin"
    enabled: bool = True
    navigation: NavigationManifest = Field(default_factory=NavigationManifest)
    runtime: RuntimeManifest
    frontend: FrontendManifest = Field(default_factory=FrontendManifest)
    permissions: list[str] = Field(default_factory=list)


class ModuleRuntime(Protocol):
    def start(self) -> None:
        ...

    def close(self) -> None:
        ...

    def snapshot(self) -> dict[str, object]:
        ...

    def invoke(self, action: str, payload: dict[str, object]) -> dict[str, object]:
        ...


@dataclass(frozen=True)
class ModuleRuntimeContext:
    module_id: str
    root: Path


@dataclass(frozen=True)
class ModuleDefinition:
    manifest: ModuleManifest
    root: Path

    def default_app_page(self) -> PageManifest | None:
        return _default_entry(self.manifest.frontend.app_pages)

    def default_esp32_scene(self) -> SceneManifest | None:
        return _default_entry(self.manifest.frontend.esp32_scenes)

    def app_route(self) -> str | None:
        page = self.default_app_page()
        return f"/module-assets/{self.manifest.id}/app/" if page is not None else None

    def esp32_route(self) -> str | None:
        scene = self.default_esp32_scene()
        return f"/module-assets/{self.manifest.id}/esp32/" if scene is not None else None


@dataclass
class ModuleRecord:
    definition: ModuleDefinition
    runtime: ModuleRuntime
    enabled: bool = True
    lifecycle: str = "registered"
    error: str | None = None
    needs_reload: bool = False


class ModuleRegistry:
    def __init__(
        self,
        definitions: list[ModuleDefinition] | None = None,
        *,
        on_state: Callable[[str, dict[str, object]], None] | None = None,
        config_path: Path | None = None,
    ) -> None:
        self._lock = RLock()
        self._transition_lock = Lock()
        self._records: dict[str, ModuleRecord] = {}
        self._scenes: dict[str, tuple[ModuleRecord, SceneManifest]] = {}
        self._on_state = on_state or (lambda _module_id, _state: None)
        self._config_path = config_path
        self._enabled_overrides = _load_enabled_overrides(config_path)
        for definition in definitions or []:
            self.register(definition)

    @classmethod
    def discover(
        cls,
        modules_dir: Path,
        *,
        on_state: Callable[[str, dict[str, object]], None] | None = None,
        config_path: Path | None = None,
    ) -> ModuleRegistry:
        return cls(
            discover_module_definitions(modules_dir),
            on_state=on_state,
            config_path=config_path,
        )

    def register(self, definition: ModuleDefinition) -> None:
        module_id = definition.manifest.id
        with self._lock:
            if module_id in self._records:
                raise ValueError(f"duplicate module id: {module_id}")
            for scene in definition.manifest.frontend.esp32_scenes:
                if scene.scene_key in self._scenes:
                    raise ValueError(f"duplicate ESP32 scene key: {scene.scene_key}")
            runtime = self._create_runtime(definition)
            enabled = self._enabled_overrides.get(module_id, definition.manifest.enabled)
            record = ModuleRecord(definition=definition, runtime=runtime, enabled=enabled)
            self._records[module_id] = record
            for scene in definition.manifest.frontend.esp32_scenes:
                self._scenes[scene.scene_key] = (record, scene)
            self._publish(module_id, runtime.snapshot())

    def start_all(self) -> None:
        for record in self._ordered_records():
            if not record.enabled:
                record.lifecycle = "disabled"
                continue
            record.lifecycle = "starting"
            try:
                record.runtime.start()
                record.lifecycle = "running"
            except Exception as exc:
                record.lifecycle = "failed"
                record.error = f"{exc.__class__.__name__}: {exc}"

    def close(self) -> None:
        for record in reversed(self._ordered_records()):
            try:
                record.runtime.close()
            except Exception as exc:
                record.lifecycle = "failed"
                record.error = f"{exc.__class__.__name__}: {exc}"
            else:
                if record.lifecycle != "disabled":
                    record.lifecycle = "stopped"

    def list_modules(self) -> list[dict[str, object]]:
        return [self._descriptor(record) for record in self._ordered_records()]

    def get_state(self, module_id: str) -> dict[str, object]:
        record = self._record(module_id)
        return {
            "module_id": module_id,
            "enabled": record.enabled,
            "lifecycle": record.lifecycle,
            "error": record.error,
            "state": record.runtime.snapshot(),
        }

    def invoke(self, module_id: str, action: str, payload: dict[str, object]) -> dict[str, object]:
        record = self._record(module_id)
        if record.lifecycle not in {"running", "registered"}:
            raise RuntimeError(f"module is not running: {record.lifecycle}")
        return record.runtime.invoke(action, payload)

    def is_enabled(self, module_id: str) -> bool:
        return self._record(module_id).enabled

    def set_enabled(self, module_id: str, enabled: bool) -> dict[str, object]:
        with self._transition_lock:
            with self._lock:
                record = self._record(module_id)
                enabled = bool(enabled)
                if record.enabled == enabled:
                    return self._descriptor(record)

                previous = record.enabled
                record.enabled = enabled
                try:
                    self._persist_enabled_locked()
                except Exception:
                    record.enabled = previous
                    raise
                record.error = None
                record.lifecycle = "starting" if enabled else "stopping"
                runtime = record.runtime
                needs_reload = record.needs_reload

            if not enabled:
                try:
                    runtime.close()
                except Exception as exc:
                    error = f"{exc.__class__.__name__}: {exc}"
                else:
                    error = None
                snapshot = runtime.snapshot()
                with self._lock:
                    record.error = error
                    record.needs_reload = True
                    record.lifecycle = "disabled"
                    descriptor = self._descriptor(record)
                self._publish(module_id, snapshot)
                return descriptor

            try:
                if needs_reload:
                    runtime = self._create_runtime(record.definition)
                runtime.start()
            except Exception as exc:
                error = f"{exc.__class__.__name__}: {exc}"
                lifecycle = "failed"
            else:
                error = None
                lifecycle = "running"
            snapshot = runtime.snapshot()
            with self._lock:
                record.runtime = runtime
                record.needs_reload = lifecycle == "failed" and needs_reload
                record.lifecycle = lifecycle
                record.error = error
                descriptor = self._descriptor(record)
            self._publish(module_id, snapshot)
            return descriptor

    def definition(self, module_id: str) -> ModuleDefinition:
        return self._record(module_id).definition

    def runtime(self, module_id: str) -> ModuleRuntime:
        return self._record(module_id).runtime

    def scene_renderer(self, module_id: str | None, scene_key: str) -> str | None:
        scene_record = self._scene_record(module_id, scene_key)
        return scene_record[1].renderer if scene_record is not None else None

    def sync_display_scene(
        self,
        module_id: str | None,
        scene_key: str,
        width: int,
        height: int,
    ) -> None:
        """Activate the selected frame source and pause every other one."""
        with self._lock:
            records = list(self._records.values())
        for record in records:
            frame_scenes = [
                scene
                for scene in record.definition.manifest.frontend.esp32_scenes
                if scene.renderer == "frame_source"
            ]
            if not frame_scenes:
                continue
            selected = next(
                (
                    scene
                    for scene in frame_scenes
                    if record.enabled
                    and record.lifecycle in {"registered", "running"}
                    and record.definition.manifest.id == module_id
                    and scene.scene_key == scene_key
                ),
                None,
            )
            runtime = record.runtime
            scene_setter = getattr(runtime, "set_display_scene", None)
            if callable(scene_setter):
                scene_setter(selected.scene_key if selected is not None else None, width, height)
                continue
            size_setter = getattr(runtime, "set_output_size", None)
            if selected is not None and callable(size_setter):
                size_setter(width, height)
            active_setter = getattr(runtime, "set_scene_active", None)
            if callable(active_setter):
                active_setter(selected is not None)

    def display_frame(self, module_id: str | None, scene_key: str):
        scene_record = self._scene_record(module_id, scene_key, renderer="frame_source")
        if scene_record is None:
            return None
        runtime, scene = scene_record[0].runtime, scene_record[1]
        getter = getattr(runtime, "display_frame", None)
        if callable(getter):
            return getter(scene.scene_key)
        getter = getattr(runtime, "current_frame", None)
        return getter() if callable(getter) else None

    def display_frame_pending(self, module_id: str | None, scene_key: str) -> bool | None:
        scene_record = self._scene_record(module_id, scene_key, renderer="frame_source")
        if scene_record is None:
            return None
        runtime, scene = scene_record[0].runtime, scene_record[1]
        checker = getattr(runtime, "display_frame_pending", None)
        if callable(checker):
            return bool(checker(scene.scene_key))
        checker = getattr(runtime, "has_pending_frame", None)
        return bool(checker()) if callable(checker) else True

    def display_transport_profile(self, module_id: str | None, scene_key: str) -> dict[str, object]:
        scene_record = self._scene_record(module_id, scene_key)
        if scene_record is None:
            return {}
        runtime, scene = scene_record[0].runtime, scene_record[1]
        profile = scene.transport.model_dump(exclude_none=True)
        provider = getattr(runtime, "display_transport_profile", None)
        if callable(provider):
            dynamic = provider(scene.scene_key)
            if not isinstance(dynamic, dict):
                raise TypeError("display_transport_profile must return a dict")
            profile.update(dynamic)
        return profile

    def deactivate_display_scenes(self) -> None:
        self.sync_display_scene(None, "", 240, 320)

    def _record(self, module_id: str) -> ModuleRecord:
        with self._lock:
            try:
                return self._records[module_id]
            except KeyError as exc:
                raise KeyError(f"unknown module: {module_id}") from exc

    def _scene_record(
        self,
        module_id: str | None,
        scene_key: str,
        *,
        renderer: str | None = None,
    ) -> tuple[ModuleRecord, SceneManifest] | None:
        if module_id is None:
            return None
        with self._lock:
            item = self._scenes.get(scene_key)
        if item is None or item[0].definition.manifest.id != module_id:
            return None
        if not item[0].enabled:
            return None
        if item[0].lifecycle not in {"registered", "running"}:
            return None
        if renderer is not None and item[1].renderer != renderer:
            return None
        return item

    def _ordered_records(self) -> list[ModuleRecord]:
        with self._lock:
            return sorted(
                self._records.values(),
                key=lambda item: (item.definition.manifest.navigation.order, item.definition.manifest.name),
            )

    def _create_runtime(self, definition: ModuleDefinition) -> ModuleRuntime:
        module_id = definition.manifest.id
        runtime_factory = _load_runtime_factory(definition)
        return _create_runtime(
            runtime_factory,
            on_state=lambda state, item=module_id: self._publish(item, state),
            context=ModuleRuntimeContext(module_id=module_id, root=definition.root),
        )

    def _persist_enabled_locked(self) -> None:
        if self._config_path is None:
            return
        self._enabled_overrides = {
            module_id: record.enabled
            for module_id, record in self._records.items()
        }
        _save_enabled_overrides(self._config_path, self._enabled_overrides)

    def _publish(self, module_id: str, state: dict[str, object]) -> None:
        self._on_state(module_id, dict(state))

    @staticmethod
    def _descriptor(record: ModuleRecord) -> dict[str, object]:
        definition = record.definition
        manifest = definition.manifest
        scene = definition.default_esp32_scene()
        return {
            "id": manifest.id,
            "name": manifest.name,
            "version": manifest.version,
            "description": manifest.description,
            "module_type": manifest.module_type,
            "enabled": record.enabled,
            "lifecycle": record.lifecycle,
            "error": record.error,
            "navigation": manifest.navigation.model_dump(),
            "app_route": definition.app_route(),
            "esp32_route": definition.esp32_route(),
            "default_scene_id": scene.id if scene else None,
            "scene_key": scene.scene_key if scene else None,
            "scene_renderer": scene.renderer if scene else None,
        }


def discover_module_definitions(modules_dir: Path) -> list[ModuleDefinition]:
    if not modules_dir.exists():
        return []
    definitions: list[ModuleDefinition] = []
    for manifest_path in sorted(modules_dir.glob("*/module.yaml")):
        raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
        manifest = ModuleManifest.model_validate(raw)
        definitions.append(ModuleDefinition(manifest=manifest, root=manifest_path.parent))
    ids = [item.manifest.id for item in definitions]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate module id in module manifests")
    return definitions


def _load_enabled_overrides(config_path: Path | None) -> dict[str, bool]:
    if config_path is None:
        return {}
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
        enabled = payload.get("enabled", {})
        if not isinstance(enabled, dict):
            return {}
        return {
            str(module_id): value
            for module_id, value in enabled.items()
            if isinstance(module_id, str) and isinstance(value, bool)
        }
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return {}


def _save_enabled_overrides(config_path: Path, enabled: dict[str, bool]) -> None:
    config_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = config_path.with_suffix(config_path.suffix + ".tmp")
    temporary.write_text(
        json.dumps({"enabled": enabled}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(config_path)


def _load_runtime_factory(definition: ModuleDefinition):
    runtime = definition.manifest.runtime
    module_name, separator, attribute = runtime.entry.partition(":")
    if not separator or not module_name or not attribute:
        raise ValueError(f"invalid module entry: {runtime.entry}")
    if runtime.type == "python_builtin":
        if not module_name.startswith("espagent_demo."):
            raise ValueError(f"unsupported built-in module entry: {runtime.entry}")
        return getattr(import_module(module_name), attribute)
    if runtime.type != "python_module":
        raise ValueError(f"unsupported module runtime type: {runtime.type}")

    root = definition.root.resolve()
    source_path = (root / module_name).resolve()
    if not source_path.is_relative_to(root) or source_path.suffix != ".py" or not source_path.is_file():
        raise ValueError(f"module runtime must be a Python file inside its module root: {runtime.entry}")
    loaded = load_python_module(source_path)
    return getattr(loaded, attribute)


def load_python_module(source_path: Path):
    """Load a trusted module source file once under a stable private name."""
    source_path = source_path.resolve()
    digest = sha1(str(source_path).encode("utf-8")).hexdigest()[:12]
    import_name = f"_espagent_module_{digest}"
    loaded = sys.modules.get(import_name)
    if loaded is None:
        spec = util.spec_from_file_location(import_name, source_path)
        if spec is None or spec.loader is None:
            raise ImportError(f"unable to load module runtime: {source_path}")
        loaded = util.module_from_spec(spec)
        sys.modules[import_name] = loaded
        try:
            spec.loader.exec_module(loaded)
        except Exception:
            sys.modules.pop(import_name, None)
            raise
    return loaded


def _create_runtime(factory, *, on_state, context: ModuleRuntimeContext):
    parameters = signature(factory).parameters
    kwargs = {"on_state": on_state}
    if "context" in parameters:
        kwargs["context"] = context
    return factory(**kwargs)


def _default_entry(items):
    if not items:
        return None
    return next((item for item in items if item.default), items[0])
