; Inno Setup script. Per-user install, no administrator rights needed.
; Build: iscc /DAppVersion=1.0.1 packaging\ingest.iss  (after PyInstaller has produced dist\Ingest)

#ifndef AppVersion
  #define AppVersion "1.0.1"
#endif

[Setup]
AppId={{0E7F8A29-AC3B-4887-81F4-4CAB0205E09B}
AppName=Ingest
AppVersion={#AppVersion}
AppVerName=Ingest {#AppVersion}
AppPublisher=Ingest
DefaultDirName={localappdata}\Programs\Ingest
DefaultGroupName=Ingest
DisableProgramGroupPage=yes
; Per-user only: the install folder, shortcuts and ingest:// registration all live in the user's profile.
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=Ingest-{#AppVersion}-Setup-x64
SetupIconFile=..\assets\icon.ico
UninstallDisplayIcon={app}\Ingest.exe
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"; Flags: unchecked

[InstallDelete]
; An update replaces the bundled runtime wholesale, so no DLL from the previous version lingers.
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "..\dist\Ingest\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\Ingest"; Filename: "{app}\Ingest.exe"; AppUserModelID: "Ingest.Desktop.1"
Name: "{autodesktop}\Ingest"; Filename: "{app}\Ingest.exe"; Tasks: desktopicon; AppUserModelID: "Ingest.Desktop.1"

[Registry]
; ingest:// links, for the "Send to Ingest" bookmarklet. Ingest also re-registers itself at launch.
Root: HKCU; Subkey: "Software\Classes\ingest"; ValueType: string; ValueData: "URL:Ingest"; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Classes\ingest"; ValueType: string; ValueName: "URL Protocol"; ValueData: ""
Root: HKCU; Subkey: "Software\Classes\ingest\DefaultIcon"; ValueType: string; ValueData: """{app}\Ingest.exe"",0"
Root: HKCU; Subkey: "Software\Classes\ingest\shell\open\command"; ValueType: string; ValueData: """{app}\Ingest.exe"" ""%1"""

[Run]
Filename: "{app}\Ingest.exe"; Description: "Start Ingest"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{app}"

[Code]
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  { Settings, the library and the downloaded engines (about 150 MB) live outside the program
    folder. Ask, defaulting to keep; videos in the output folders are never touched. }
  if (CurUninstallStep = usPostUninstall) and not UninstallSilent then
    if MsgBox('Also remove Ingest''s settings, library and downloaded engines?' + #13#10#13#10 +
              'Your downloaded videos stay where they are.', mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
    begin
      DelTree(ExpandConstant('{localappdata}\Ingest'), True, True, True);
      DelTree(ExpandConstant('{userappdata}\Ingest'), True, True, True);
    end;
end;
