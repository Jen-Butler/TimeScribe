[Setup]
AppName=TimeScribe
AppVersion=0.1.1
AppPublisher=Rising Tide Group
DefaultDirName={autopf}\TimeScribe
DefaultGroupName=TimeScribe
UninstallDisplayIcon={app}\TimeScribe.exe
OutputBaseFilename=TimeScribe-Setup-0.1.1
OutputDir=installer_out
Compression=lzma2
SolidCompression=yes
PrivilegesRequired=lowest
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes

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

[Code]
var
  ResultCode: Integer;

// Stop TimeScribe (and any ActivityWatch we launched from our bundle) before
// uninstalling so no files are locked.
function InitializeUninstall(): Boolean;
begin
  Exec('taskkill.exe', '/IM TimeScribe.exe /F', '', SW_HIDE,
       ewWaitUntilTerminated, ResultCode);
  if DirExists(ExpandConstant('{app}\aw')) then begin
    Exec('taskkill.exe', '/IM aw-qt.exe /F', '', SW_HIDE,
         ewWaitUntilTerminated, ResultCode);
    Exec('taskkill.exe', '/IM aw-server.exe /F', '', SW_HIDE,
         ewWaitUntilTerminated, ResultCode);
    Exec('taskkill.exe', '/IM aw-watcher-window.exe /F', '', SW_HIDE,
         ewWaitUntilTerminated, ResultCode);
    Exec('taskkill.exe', '/IM aw-watcher-afk.exe /F', '', SW_HIDE,
         ewWaitUntilTerminated, ResultCode);
  end;
  Result := True;
end;

// After files are removed, offer a full data purge: config, digests, drafts,
// logs, and the Credential Manager entries (API keys + Halo refresh token).
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then begin
    if MsgBox('Also delete all TimeScribe data?' + #13#10 + #13#10 +
              'This removes your settings, activity digests, draft time entries, ' +
              'logs, and stored credentials (API keys, HaloPSA sign-in). ' +
              'Posted time entries in HaloPSA are not affected.',
              mbConfirmation, MB_YESNO) = IDYES then
    begin
      DelTree(ExpandConstant('{localappdata}\timescribe'), True, True, True);
      Exec('cmd.exe', '/c cmdkey /delete:timescribe & cmdkey /delete:timescribe.halo',
           '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
    end;
  end;
end;
