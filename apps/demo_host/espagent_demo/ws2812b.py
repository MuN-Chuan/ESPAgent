"""Compatibility import for the module-owned WS2812B runtime."""

from espagent_demo.module_runtime import load_python_module
from espagent_demo.paths import app_resource_dir

_runtime = load_python_module(
    app_resource_dir() / "modules" / "builtin.ws2812b" / "runtime.py"
)
globals().update({name: value for name, value in vars(_runtime).items() if not name.startswith("__")})
