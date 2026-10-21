from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules


ROOT = Path(SPECPATH).parent
APP_DIR = ROOT / "apps" / "demo_host"

datas = [
    (str(APP_DIR / "desktop_ui"), "apps/demo_host/desktop_ui"),
    (str(APP_DIR / "display_ui"), "apps/demo_host/display_ui"),
    (str(APP_DIR / "static"), "apps/demo_host/static"),
    (str(APP_DIR / "modules"), "apps/demo_host/modules"),
    (
        str(APP_DIR / "espagent_demo" / "hermes_gateway_bridge.py"),
        "apps/demo_host/espagent_demo",
    ),
    (str(ROOT / "firmware" / "releases" / "esp32_display"), "firmware/releases/esp32_display"),
    (str(ROOT / "firmware" / "releases" / "esp01s_wled"), "firmware/releases/esp01s_wled"),
    (
        str(ROOT / "firmware" / "releases" / "esp32s3_supermini_wled"),
        "firmware/releases/esp32s3_supermini_wled",
    ),
    (str(ROOT / "test" / "video" / "test.mp4"), "test/video"),
]
datas += collect_data_files("sounddevice")
datas += collect_data_files("Pinyin2Hanzi")
datas += collect_data_files("webview")
datas += collect_data_files("esptool")

hiddenimports = (
    collect_submodules("espagent_demo")
    + collect_submodules("webview")
    + collect_submodules("uvicorn")
    + collect_submodules("dxcam")
    + [
        "start_demo_host",
        "cv2",
        "numpy",
        "PIL.Image",
        "PIL.ImageGrab",
        "pydantic_settings",
        "serial",
        "sounddevice",
        "websocket",
    ]
)

a = Analysis(
    [str(ROOT / "start_espagent_app.py")],
    pathex=[str(ROOT), str(APP_DIR)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "pytest",
        "IPython",
        "jupyter",
        "matplotlib",
        "notebook",
        "tkinter",
    ],
    noarchive=False,
    optimize=1,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="ESPAgent",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="ESPAgent",
)
