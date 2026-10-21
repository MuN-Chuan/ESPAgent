#define AppName "ESPAgent"
#define AppVersion "0.1.1"
#define AppPublisher "ESPAgent"
#define AppExeName "ESPAgent.exe"

[Setup]
AppId={{72BB7AE8-3741-4EC8-9AF3-9EB84AE83964}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={localappdata}\Programs\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
OutputDir=..\dist\installer
OutputBaseFilename=ESPAgent-Setup-x64
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.17763
UninstallDisplayIcon={app}\{#AppExeName}
SetupLogging=yes
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "chinesesimp"; MessagesFile: "compiler:Languages\Chinese.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "快捷方式:"; Flags: unchecked

[Files]
Source: "..\dist\ESPAgent\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "vendor\MicrosoftEdgeWebview2Setup.exe"; DestDir: "{tmp}"; Flags: deleteafterinstall skipifsourcedoesntexist

[Icons]
Name: "{group}\ESPAgent"; Filename: "{app}\{#AppExeName}"; Parameters: "--log-file ""{localappdata}\ESPAgent\logs\espagent_app.log"""
Name: "{autodesktop}\ESPAgent"; Filename: "{app}\{#AppExeName}"; Parameters: "--log-file ""{localappdata}\ESPAgent\logs\espagent_app.log"""; Tasks: desktopicon

[Run]
Filename: "{tmp}\MicrosoftEdgeWebview2Setup.exe"; Parameters: "/silent /install"; StatusMsg: "正在安装 Microsoft WebView2 Runtime..."; Flags: waituntilterminated skipifdoesntexist; Check: not WebView2Installed
Filename: "{app}\{#AppExeName}"; Parameters: "--log-file ""{localappdata}\ESPAgent\logs\espagent_app.log"""; Description: "启动 ESPAgent"; Flags: nowait postinstall skipifsilent

[Code]
function WebView2Installed: Boolean;
var
  Version: String;
begin
  Result :=
    RegQueryStringValue(HKCU, 'Software\Microsoft\EdgeUpdate\Clients\{F1E7E31E-5F2B-4A5D-8C8A-4A7F8A8F3C7D}', 'pv', Version) or
    RegQueryStringValue(HKLM, 'SOFTWARE\Microsoft\EdgeUpdate\Clients\{F1E7E31E-5F2B-4A5D-8C8A-4A7F8A8F3C7D}', 'pv', Version) or
    RegQueryStringValue(HKLM64, 'SOFTWARE\Microsoft\EdgeUpdate\Clients\{F1E7E31E-5F2B-4A5D-8C8A-4A7F8A8F3C7D}', 'pv', Version);
end;
