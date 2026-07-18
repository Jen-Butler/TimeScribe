[Setup]
AppName=TimeScribe
AppVersion=0.1.0
AppPublisher=Rising Tide Group
DefaultDirName={autopf}\TimeScribe
DefaultGroupName=TimeScribe
UninstallDisplayIcon={app}\TimeScribe.exe
OutputBaseFilename=TimeScribe-Setup-0.1.0
OutputDir=installer_out
Compression=lzma2
SolidCompression=yes
PrivilegesRequired=lowest
ArchitecturesInstallIn64BitMode=x64compatible

[Files]
Source: "dist\TimeScribe\*"; DestDir: "{app}"; Flags: recursesubdirs
; Bundled ActivityWatch portable (populate with: python fetch_aw.py)
Source: "aw_dist\*"; DestDir: "{app}\aw"; Flags: recursesubdirs skipifsourcedoesntexist

[Icons]
Name: "{group}\TimeScribe"; Filename: "{app}\TimeScribe.exe"
Name: "{userstartup}\TimeScribe"; Filename: "{app}\TimeScribe.exe"; Tasks: autostart

[Tasks]
Name: autostart; Description: "Start TimeScribe when I sign in to Windows"; GroupDescription: "Startup:"

[Run]
Filename: "{app}\TimeScribe.exe"; Description: "Launch TimeScribe now"; Flags: nowait postinstall skipifsilent
