; Stream Guardian installer (Inno Setup 6).
; Built by the release workflow:  ISCC.exe /DAppVersion=2.0.0 installer.iss
; Per-user install (no admin prompt) into %LOCALAPPDATA%\Programs\Stream Guardian.
; Settings in %APPDATA%\StreamGuardian are left in place on uninstall.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{6F2B8F4E-3C1D-4B7A-9E52-5A1C0D7E8B31}
AppName=Stream Guardian
AppVersion={#AppVersion}
AppVerName=Stream Guardian {#AppVersion}
AppPublisher=PoizenJam
AppPublisherURL=https://github.com/PoizenJam/Stream-Guardian
AppSupportURL=https://github.com/PoizenJam/Stream-Guardian/issues
AppUpdatesURL=https://github.com/PoizenJam/Stream-Guardian/releases
DefaultDirName={localappdata}\Programs\Stream Guardian
DefaultGroupName=Stream Guardian
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=.
OutputBaseFilename=StreamGuardian-{#AppVersion}-Setup
SetupIconFile=icon.ico
UninstallDisplayIcon={app}\StreamGuardian.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
; Close a running Stream Guardian before upgrading or uninstalling
CloseApplications=yes
RestartApplications=no

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Shortcuts:"; Flags: unchecked

[Files]
Source: "dist\StreamGuardian.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "README.md"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\Stream Guardian"; Filename: "{app}\StreamGuardian.exe"
Name: "{group}\Uninstall Stream Guardian"; Filename: "{uninstallexe}"
Name: "{userdesktop}\Stream Guardian"; Filename: "{app}\StreamGuardian.exe"; Tasks: desktopicon

[Registry]
; Remove the "Start with Windows" entry the app may have created (never created here)
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueName: "StreamGuardian"; ValueType: none; Flags: uninsdeletevalue

[Run]
Filename: "{app}\StreamGuardian.exe"; Description: "Launch Stream Guardian"; Flags: nowait postinstall skipifsilent
