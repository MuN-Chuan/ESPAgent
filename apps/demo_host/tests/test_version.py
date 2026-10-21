from pathlib import Path

from espagent_demo.version import APP_VERSION


ROOT = Path(__file__).resolve().parents[3]


def test_application_version_matches_packaging_metadata() -> None:
    pyproject = (ROOT / "apps" / "demo_host" / "pyproject.toml").read_text(encoding="utf-8")
    inno_setup = (ROOT / "packaging" / "ESPAgent.iss").read_text(encoding="utf-8")

    assert f'version = "{APP_VERSION}"' in pyproject
    assert f'#define AppVersion "{APP_VERSION}"' in inno_setup
