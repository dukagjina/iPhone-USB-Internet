#define MyAppName "iPhone USB Internet"
#define MyAppVersion "1.2.0"
#ifndef PayloadDir
  #define PayloadDir "dist\iPhone USB Internet"
#endif
#ifndef OutputPath
  #define OutputPath "dist\installer"
#endif

[Setup]
AppId={{5B72F4AF-F916-4C37-A641-F10DE2F0324D}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher=Zuyis
VersionInfoVersion=1.2.0.0
VersionInfoCompany=Zuyis
VersionInfoDescription=iPhone USB Internet Windows installer
VersionInfoProductName={#MyAppName}
VersionInfoProductVersion={#MyAppVersion}
VersionInfoCopyright=Copyright © 2026 Zuyis
DefaultDirName={localappdata}\Programs\iPhone USB Internet
DefaultGroupName=iPhone USB Internet
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.17763
LicenseFile=LICENSE
InfoBeforeFile=EDUCATIONAL USE.txt
SetupIconFile=src\assets\iphone-usb.ico
UninstallDisplayIcon={app}\iPhone USB Internet.exe
UninstallDisplayName={#MyAppName}
OutputDir={#OutputPath}
OutputBaseFilename=iPhone-USB-Internet-Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=no
RestartApplications=no
AppMutex=Local\iPhoneUSBInternet.App.v1,Global\iPhoneUSBInternet.Connection.v1
SetupLogging=yes

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: checkedonce

[Files]
Source: "{#PayloadDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs; Excludes: "pairing.dpapi,legacy-keys.json,PHONE RECEIVER COMMAND.txt,*.log"

[InstallDelete]
Type: files; Name: "{app}\_internal\ui\navigation.css"

[Icons]
Name: "{userprograms}\iPhone USB Internet"; Filename: "{app}\iPhone USB Internet.exe"; WorkingDir: "{app}"; IconFilename: "{app}\iPhone USB Internet.exe"; AppUserModelID: "Zuyis.iPhoneUSBInternet"
Name: "{userdesktop}\iPhone USB Internet"; Filename: "{app}\iPhone USB Internet.exe"; WorkingDir: "{app}"; IconFilename: "{app}\iPhone USB Internet.exe"; AppUserModelID: "Zuyis.iPhoneUSBInternet"; Tasks: desktopicon

[Run]
Filename: "{app}\iPhone USB Internet.exe"; Description: "Open iPhone USB Internet"; Flags: nowait postinstall skipifsilent unchecked

[Code]
function AppIsRunning: Boolean;
begin
  Result := CheckForMutexes('Local\iPhoneUSBInternet.App.v1,Global\iPhoneUSBInternet.Connection.v1');
end;

function HasWebView2: Boolean;
var Version, RuntimeKey: String;
begin
  RuntimeKey := 'Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}';
  Result := (RegQueryStringValue(HKLM32, RuntimeKey, 'pv', Version) and
    (Version <> '') and (Version <> '0.0.0.0')) or
    (RegQueryStringValue(HKCU, RuntimeKey, 'pv', Version) and
    (Version <> '') and (Version <> '0.0.0.0'));
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';
  if AppIsRunning then
    Result := 'Disconnect and close iPhone USB Internet yourself, then try again. Setup will not stop your connection.'
  else if not HasWebView2 then
    Result := 'Microsoft Edge WebView2 Runtime is required. Install it from https://developer.microsoft.com/microsoft-edge/webview2/ and run Setup again.';
end;

function InitializeUninstall: Boolean;
begin
  Result := not AppIsRunning;
  if not Result then
    MsgBox('Disconnect and close iPhone USB Internet yourself before uninstalling. Your connection has not been changed.', mbInformation, MB_OK);
end;

// Pairing, session records and window preferences live outside the install folder.
// Uninstall never deletes them, removes routes or kills processes.
