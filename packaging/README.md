# Windows application package

The package contains only the ESPAgent Windows application, built-in modules,
display assets, test video, and firmware images. It does not bundle Hermes,
WSL, Home Assistant, Fish Audio services, AI models, API keys, or user
credentials.

Build the application directory and installer:

```powershell
powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1
```

Build only the portable application directory:

```powershell
powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1 -SkipInstaller
```

Recompile only the installer after changing `ESPAgent.iss`:

```powershell
powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1 -SkipApplicationBuild
```

Outputs:

- `dist\ESPAgent\ESPAgent.exe`
- `dist\installer\ESPAgent-Setup-x64.exe`

The installed application stores mutable data under `%LOCALAPPDATA%\ESPAgent`.
An existing `.env.local` can be placed there to connect external services.
The installer optionally includes Microsoft's official Evergreen WebView2
bootstrapper because the desktop shell requires WebView2.

The build script always uses `apps\demo_host\.venv` so that the packaged
dependencies exactly match the application runtime instead of whichever
packages happen to exist in the system Python installation.
