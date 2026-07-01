#define AppName    "ezLab QA Monitor"
#define AppVersion "1.0"
#define AppPublisher "harryQA"
#define AppExeName "ezLabQAMonitor.exe"

[Setup]
AppId={{A1B2C3D4-E5F6-7890-ABCD-EF1234567890}
AppName={#AppName}
AppVerName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
OutputDir=dist
OutputBaseFilename=ezLabQAMonitor_Setup
SetupIconFile=ezlab.ico
Compression=lzma
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=admin

[Languages]
Name: "korean"; MessagesFile: "compiler:Languages\Korean.isl"

[Tasks]
Name: "desktopicon"; Description: "바탕화면 바로가기 만들기"; GroupDescription: "추가 작업:"
Name: "startup";     Description: "Windows 시작 시 자동 실행";  GroupDescription: "추가 작업:"

[Files]
Source: "dist\{#AppExeName}"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#AppName}";          Filename: "{app}\{#AppExeName}"
Name: "{group}\{#AppName} 제거";     Filename: "{uninstallexe}"
Name: "{userdesktop}\{#AppName}";    Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Registry]
; PrivilegesRequired=admin라 항상 관리자 권한으로 설치되므로, 자동시작은
; 설치를 수행한 계정이 아니라 이 PC에 로그인하는 모든 사용자에게 적용되도록
; HKLM에 등록한다 (HKCU로 하면 "다른 사용자로 실행"해 설치한 경우 엉뚱한
; 계정에 등록되어 실제 사용자에게는 자동시작이 걸리지 않는 문제가 있었음).
Root: HKLM; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; \
  ValueType: string; ValueName: "{#AppName}"; \
  ValueData: """{app}\{#AppExeName}"""; \
  Flags: uninsdeletevalue; Tasks: startup

[Run]
Filename: "{app}\{#AppExeName}"; Description: "지금 바로 실행"; Flags: nowait postinstall skipifsilent
