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

[Dirs]
; 크래시 덤프 저장 위치. 서비스 계정으로 크래시가 나도 항상 쓸 수 있도록
; 명시적으로 모든 사용자에게 쓰기 권한을 부여한다.
Name: "{commonappdata}\ezLab QA Monitor\Dumps"; Permissions: users-modify

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

[Code]
// 크래시 시 .NET 관리 코드 스택 트레이스 자동 분석(DumpAnalyzer.exe + ClrMD)을 위해
// 이지랩 앱들에 대해 WER(Windows Error Reporting) 로컬 덤프 자동 캡처를 등록한다.
//
// 주의: 이 인스톨러는 32비트 프로세스로 실행되므로, Inno Setup 기본 Reg*
// 함수(RegWriteStringValue 등)로 HKLM에 쓰면 WOW64 리다이렉션 때문에
// SOFTWARE\WOW6432Node\... 에만 기록된다. 하지만 실제 크래시 대상인
// ezFinder 등은 64비트 프로세스라서 네이티브 64비트 레지스트리 뷰를 봐야
// WER이 이 설정을 찾는다. 그래서 advapi32.dll을 직접 호출해
// KEY_WOW64_64KEY 플래그로 네이티브 뷰에 쓴다.
const
  // HKEY_LOCAL_MACHINE는 Inno Setup Pascal Scripting에 이미 내장된
  // 상수라서 여기서 다시 선언하지 않는다 (재선언하면 컴파일 오류남).
  KEY_WOW64_64KEY         = $0100;
  KEY_ALL_ACCESS          = $000F003F;
  REG_OPTION_NON_VOLATILE = 0;
  REG_SZ                  = 1;
  REG_DWORD               = 4;
  WerAppCount             = 14;

var
  WerAppNames: array[0..13] of String;

function RegCreateKeyExW(hKey: Longint; lpSubKey: String; Reserved: Longint;
  lpClass: Longint; dwOptions: Longint; samDesired: Longint;
  lpSecurityAttributes: Longint; var phkResult: Longint; var lpdwDisposition: Longint): Longint;
  external 'RegCreateKeyExW@advapi32.dll stdcall';

function RegSetValueExStr(hKey: Longint; lpValueName: String; Reserved: Longint;
  dwType: Longint; lpData: String; cbData: Longint): Longint;
  external 'RegSetValueExW@advapi32.dll stdcall';

function RegSetValueExDword(hKey: Longint; lpValueName: String; Reserved: Longint;
  dwType: Longint; var lpData: Longint; cbData: Longint): Longint;
  external 'RegSetValueExW@advapi32.dll stdcall';

function RegDeleteKeyExW(hKey: Longint; lpSubKey: String; samDesired: Longint;
  Reserved: Longint): Longint;
  external 'RegDeleteKeyExW@advapi32.dll stdcall';

function RegCloseKey(hKey: Longint): Longint;
  external 'RegCloseKey@advapi32.dll stdcall';

procedure InitWerAppNames();
begin
  WerAppNames[0]  := 'ezfinder.exe';
  WerAppNames[1]  := 'ezfinder updator.exe';
  WerAppNames[2]  := 'ezcapture.exe';
  WerAppNames[3]  := 'ezcapture updator.exe';
  WerAppNames[4]  := 'ezcam.exe';
  WerAppNames[5]  := 'ezcam updator.exe';
  WerAppNames[6]  := 'ezmemo.exe';
  WerAppNames[7]  := 'ezmemo updator.exe';
  WerAppNames[8]  := 'ezzip.exe';
  WerAppNames[9]  := 'ezzip updator.exe';
  WerAppNames[10] := 'ezmanager.exe';
  WerAppNames[11] := 'ezmanager updator.exe';
  WerAppNames[12] := 'ezfinderservice.exe';
  WerAppNames[13] := 'ezmanagerservice.exe';
end;

// 네이티브 64비트 뷰로 열어서 DumpFolder/DumpCount/DumpType을 쓴다.
procedure Wer64RegisterOne(exeName, dumpFolder: String);
var
  hKey, disp, res: Longint;
  dumpCount, dumpType: Longint;
  keyPath: String;
begin
  keyPath := 'SOFTWARE\Microsoft\Windows\Windows Error Reporting\LocalDumps\' + exeName;
  res := RegCreateKeyExW(HKEY_LOCAL_MACHINE, keyPath, 0, 0, REG_OPTION_NON_VOLATILE,
    KEY_ALL_ACCESS or KEY_WOW64_64KEY, 0, hKey, disp);
  if res <> 0 then
    Exit;

  RegSetValueExStr(hKey, 'DumpFolder', 0, REG_SZ, dumpFolder, (Length(dumpFolder) + 1) * 2);
  dumpCount := 10;
  RegSetValueExDword(hKey, 'DumpCount', 0, REG_DWORD, dumpCount, 4);
  dumpType := 2;
  RegSetValueExDword(hKey, 'DumpType', 0, REG_DWORD, dumpType, 4);

  RegCloseKey(hKey);
end;

procedure RegisterWerLocalDumps();
var
  i: Integer;
  dumpFolder: String;
begin
  InitWerAppNames();
  dumpFolder := ExpandConstant('{commonappdata}\ezLab QA Monitor\Dumps');
  ForceDirectories(dumpFolder);
  for i := 0 to WerAppCount - 1 do
    Wer64RegisterOne(WerAppNames[i], dumpFolder);
end;

procedure UnregisterWerLocalDumps();
var
  i: Integer;
begin
  InitWerAppNames();
  for i := 0 to WerAppCount - 1 do
    RegDeleteKeyExW(HKEY_LOCAL_MACHINE,
      'SOFTWARE\Microsoft\Windows\Windows Error Reporting\LocalDumps\' + WerAppNames[i],
      KEY_WOW64_64KEY, 0);
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
    RegisterWerLocalDumps();
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
    UnregisterWerLocalDumps();
end;
