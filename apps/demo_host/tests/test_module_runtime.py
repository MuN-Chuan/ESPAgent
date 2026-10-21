from __future__ import annotations

from pathlib import Path
import json

import pytest

from espagent_demo.module_runtime import (
    ModuleDefinition,
    ModuleManifest,
    ModuleRegistry,
    discover_module_definitions,
)


MODULES_DIR = Path(__file__).resolve().parents[1] / "modules"


def test_builtin_modules_are_discovered_from_manifests() -> None:
    definitions = discover_module_definitions(MODULES_DIR)

    assert [item.manifest.id for item in definitions] == [
        "builtin.codex-usage",
        "builtin.desktop-mirror",
        "builtin.ws2812b",
    ]
    definition = definitions[0]
    assert definition.app_route() == "/module-assets/builtin.codex-usage/app/"
    assert definition.esp32_route() == "/module-assets/builtin.codex-usage/esp32/"
    assert definition.default_esp32_scene().scene_key == "codex"
    desktop = definitions[1]
    assert desktop.app_route() == "/module-assets/builtin.desktop-mirror/app/"
    assert desktop.default_esp32_scene().scene_key == "desktop"
    assert desktop.default_esp32_scene().renderer == "frame_source"
    light_strip = definitions[2]
    assert light_strip.app_route() == "/module-assets/builtin.ws2812b/app/"
    assert light_strip.esp32_route() is None


def test_registry_descriptors_are_navigation_ready_without_starting_processes() -> None:
    published: dict[str, dict[str, object]] = {}
    registry = ModuleRegistry.discover(MODULES_DIR, on_state=published.__setitem__)

    modules = registry.list_modules()

    assert modules[0]["id"] == "builtin.codex-usage"
    assert modules[0]["navigation"]["pinned"] is True
    assert modules[0]["lifecycle"] == "registered"
    assert published["builtin.codex-usage"]["status"] == "stopped"
    assert modules[1]["id"] == "builtin.desktop-mirror"
    assert published["builtin.desktop-mirror"]["status"] == "stopped"
    assert modules[2]["id"] == "builtin.ws2812b"
    assert published["builtin.ws2812b"]["status"] == "stopped"


def test_module_local_runtime_receives_context_and_provides_display_capabilities(tmp_path: Path) -> None:
    (tmp_path / "runtime.py").write_text(
        """
from PIL import Image

class Runtime:
    def __init__(self, *, on_state, context):
        self.on_state = on_state
        self.context = context
        self.active = False
        self.pending = True
        self.image = Image.new("RGB", (240, 320), (1, 2, 3))
    def start(self): pass
    def close(self): pass
    def snapshot(self): return {"root": str(self.context.root)}
    def invoke(self, action, payload): return payload
    def set_output_size(self, width, height):
        self.image = Image.new("RGB", (width, height), (1, 2, 3))
    def set_scene_active(self, active): self.active = active
    def has_pending_frame(self): return self.pending
    def current_frame(self):
        self.pending = False
        return self.image
    def display_transport_profile(self, scene_key):
        return {"transport_mode": "indexed", "indexed_bpp": 4}
""".strip(),
        encoding="utf-8",
    )
    definition = ModuleDefinition(
        manifest=ModuleManifest.model_validate(
            {
                "id": "test.local",
                "name": "Local",
                "version": "1.0.0",
                "runtime": {"type": "python_module", "entry": "runtime.py:Runtime"},
                "frontend": {
                    "esp32_scenes": [
                        {
                            "id": "screen",
                            "path": "ui/esp32",
                            "scene_key": "local-screen",
                            "renderer": "frame_source",
                        }
                    ]
                },
            }
        ),
        root=tmp_path,
    )
    registry = ModuleRegistry([definition])

    registry.sync_display_scene("test.local", "local-screen", 320, 240)
    runtime = registry.runtime("test.local")

    assert runtime.active is True
    assert registry.scene_renderer("test.local", "local-screen") == "frame_source"
    assert registry.display_frame_pending("test.local", "local-screen") is True
    assert registry.display_frame("test.local", "local-screen").size == (320, 240)
    assert registry.display_transport_profile("test.local", "local-screen") == {
        "transport_mode": "indexed",
        "indexed_bpp": 4,
    }
    assert registry.get_state("test.local")["state"]["root"] == str(tmp_path)


def test_registry_rejects_duplicate_scene_keys(tmp_path: Path) -> None:
    (tmp_path / "runtime.py").write_text(
        "class Runtime:\n"
        "    def __init__(self, *, on_state): self.on_state = on_state\n"
        "    def start(self): pass\n"
        "    def close(self): pass\n"
        "    def snapshot(self): return {}\n"
        "    def invoke(self, action, payload): return {}\n",
        encoding="utf-8",
    )

    def definition(module_id: str) -> ModuleDefinition:
        return ModuleDefinition(
            manifest=ModuleManifest.model_validate(
                {
                    "id": module_id,
                    "name": module_id,
                    "version": "1.0.0",
                    "runtime": {"type": "python_module", "entry": "runtime.py:Runtime"},
                    "frontend": {
                        "esp32_scenes": [
                            {"id": "screen", "path": "ui/esp32", "scene_key": "duplicate"}
                        ]
                    },
                }
            ),
            root=tmp_path,
        )

    registry = ModuleRegistry([definition("test.one")])
    with pytest.raises(ValueError, match="duplicate ESP32 scene key"):
        registry.register(definition("test.two"))


def test_registry_persists_enabled_state_and_recreates_runtime(tmp_path: Path) -> None:
    module_root = tmp_path / "module"
    module_root.mkdir()
    (module_root / "runtime.py").write_text(
        "class Runtime:\n"
        "    def __init__(self, *, on_state):\n"
        "        self.on_state = on_state\n"
        "        self.starts = 0\n"
        "        self.closes = 0\n"
        "    def start(self): self.starts += 1\n"
        "    def close(self): self.closes += 1\n"
        "    def snapshot(self): return {'starts': self.starts, 'closes': self.closes}\n"
        "    def invoke(self, action, payload): return payload\n",
        encoding="utf-8",
    )
    definition = ModuleDefinition(
        manifest=ModuleManifest.model_validate(
            {
                "id": "test.toggle",
                "name": "Toggle",
                "version": "1.0.0",
                "runtime": {"type": "python_module", "entry": "runtime.py:Runtime"},
            }
        ),
        root=module_root,
    )
    config_path = tmp_path / "modules.json"
    registry = ModuleRegistry([definition], config_path=config_path)
    registry.start_all()
    original_runtime = registry.runtime("test.toggle")

    disabled = registry.set_enabled("test.toggle", False)

    assert disabled["enabled"] is False
    assert disabled["lifecycle"] == "disabled"
    assert original_runtime.closes == 1
    assert json.loads(config_path.read_text(encoding="utf-8"))["enabled"] == {"test.toggle": False}
    with pytest.raises(RuntimeError, match="module is not running"):
        registry.invoke("test.toggle", "echo", {})

    restored = ModuleRegistry([definition], config_path=config_path)
    restored.start_all()
    assert restored.list_modules()[0]["enabled"] is False
    assert restored.list_modules()[0]["lifecycle"] == "disabled"
    restored.close()

    enabled = registry.set_enabled("test.toggle", True)

    assert enabled["enabled"] is True
    assert enabled["lifecycle"] == "running"
    assert registry.runtime("test.toggle") is not original_runtime
    assert registry.runtime("test.toggle").starts == 1
