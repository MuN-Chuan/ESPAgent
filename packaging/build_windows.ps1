param(
    [switch]$SkipApplicationBuild,
    [switch]$SkipInstaller,
    [switch]$SkipWebViewBootstrapper
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Spec = Join-Path $PSScriptRoot "ESPAgent.spec"
$InnoScript = Join-Path $PSScriptRoot "ESPAgent.iss"
$VendorDir = Join-Path $PSScriptRoot "vendor"
$WebViewBootstrapper = Join-Path $VendorDir "MicrosoftEdgeWebview2Setup.exe"
$BuildPython = Join-Path $Root "apps\demo_host\.venv\Scripts\python.exe"

Push-Location $Root
try {
    if (-not $SkipApplicationBuild) {
        if (-not (Test-Path $BuildPython)) {
            throw "Application virtual environment was not found: $BuildPython"
        }
        $HasPyInstaller = & $BuildPython -c "import importlib.util; print('1' if importlib.util.find_spec('PyInstaller') else '0')"
        if ($HasPyInstaller -ne "1") {
            & $BuildPython -m pip install -r (Join-Path $PSScriptRoot "requirements-build.txt")
            if ($LASTEXITCODE -ne 0) {
                throw "Could not install the application packaging dependencies"
            }
        }

        & $BuildPython -m PyInstaller --noconfirm --clean $Spec
        if ($LASTEXITCODE -ne 0) {
            throw "PyInstaller failed with exit code $LASTEXITCODE"
        }
    }

    $Executable = Join-Path $Root "dist\ESPAgent\ESPAgent.exe"
    if (-not (Test-Path $Executable)) {
        throw "Packaged executable was not created: $Executable"
    }

    if ($SkipInstaller) {
        Write-Host "[ESPAgent] Application directory: $(Split-Path $Executable)"
        return
    }

    if (-not $SkipWebViewBootstrapper -and -not (Test-Path $WebViewBootstrapper)) {
        New-Item -ItemType Directory -Force -Path $VendorDir | Out-Null
        Invoke-WebRequest `
            -Uri "https://go.microsoft.com/fwlink/p/?LinkId=2124703" `
            -OutFile $WebViewBootstrapper `
            -UseBasicParsing
    }

    $Iscc = Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe"
    if (-not (Test-Path $Iscc)) {
        throw "Inno Setup 6 was not found at: $Iscc"
    }
    & $Iscc $InnoScript
    if ($LASTEXITCODE -ne 0) {
        throw "Inno Setup failed with exit code $LASTEXITCODE"
    }

    Write-Host "[ESPAgent] Installer: $Root\dist\installer\ESPAgent-Setup-x64.exe"
}
finally {
    Pop-Location
}
