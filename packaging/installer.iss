; DaVinci Director — Inno Setup script (compiled by build.py).
; Per-user install, no admin rights, into DaVinci Resolve's own user folder:
;   %APPDATA%\Blackmagic Design\DaVinci Resolve\Support\DaVinci Director
; Everything the program downloads or keeps later (components, models, transcripts, logs, caches)
; stays inside that folder too, so uninstalling leaves nothing behind.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef StageDir
  #define StageDir "..\build\stage"
#endif
#ifndef OutputBase
  #define OutputBase "DaVinciDirector-Setup"
#endif
#define PyW "{app}\python\pythonw.exe"
#define Watcher "{app}\app\davinci_director_watcher.pyw"
#define Entry "{app}\app\dd_entry.py"

[Setup]
AppId={{6F1B7E52-0C2B-4D63-9B57-3D1A7C0FFEE1}
AppName=DaVinci Director
AppVersion={#AppVersion}
AppVerName=DaVinci Director {#AppVersion}
AppPublisher=DaVinci Director
; the user picks the folder; by default next to DaVinci Resolve's user files.  An upgrade keeps the folder
; chosen before.  Everything the program downloads or keeps goes into the chosen folder, so it can be
; anywhere with enough space (several GB).
DefaultDirName={userappdata}\Blackmagic Design\DaVinci Resolve\Support\DaVinci Director
UsePreviousAppDir=yes
DisableDirPage=no
AppendDefaultDirName=yes
DisableProgramGroupPage=yes
DisableReadyPage=yes
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename={#OutputBase}
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
UninstallDisplayName=DaVinci Director
SetupIconFile=icon.ico
UninstallDisplayIcon={app}\app\davinci_director\web\icon.ico
CloseApplications=no
SetupLogging=yes

[Languages]
Name: "uk"; MessagesFile: "compiler:Languages\Ukrainian.isl"

[Files]
Source: "{#StageDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[InstallDelete]
Type: filesandordirs; Name: "{app}\app"

[Registry]
; hidden watcher at login: opens the panel when Workspace > Scripts > DaVinci Director runs in Resolve
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "DaVinciDirector"; ValueData: """{#PyW}"" ""{#Watcher}"""; Flags: uninsdeletevalue

[Run]
; starts the watcher now; it also writes the script into DaVinci Resolve's Scripts folder
Filename: "{#PyW}"; Parameters: """{#Watcher}"""; WorkingDir: "{app}\app"; Flags: nowait runhidden

[UninstallRun]
Filename: "{#PyW}"; Parameters: """{#Entry}"" --quit"; WorkingDir: "{app}\app"; Flags: runhidden waituntilterminated; RunOnceId: "quit"

[UninstallDelete]
Type: files; Name: "{userappdata}\Blackmagic Design\DaVinci Resolve\Support\Fusion\Scripts\Utility\DaVinci Director.lua"
Type: filesandordirs; Name: "{app}\app"
Type: filesandordirs; Name: "{app}\python"
Type: filesandordirs; Name: "{app}\components"
Type: filesandordirs; Name: "{app}\models"
Type: filesandordirs; Name: "{app}\hf"
Type: filesandordirs; Name: "{app}\downloads"
Type: filesandordirs; Name: "{app}\data\cache"

[Messages]
uk.FinishedLabel=DaVinci Director встановлено.%n%nВідкрийте DaVinci Resolve і виберіть у меню Workspace > Scripts > DaVinci Director.%n(Якщо Resolve вже відкритий і пункту немає — перезапустіть Resolve.)

[Code]
// a running copy (hidden watcher / open panel) locks its files: ask it to quit first
procedure QuitCopy(Dir: String);
var
  Code: Integer;
begin
  if FileExists(Dir + '\python\pythonw.exe') and FileExists(Dir + '\app\dd_entry.py') then
    Exec(Dir + '\python\pythonw.exe', '"' + Dir + '\app\dd_entry.py" --quit', Dir + '\app', SW_HIDE, ewWaitUntilTerminated, Code);
end;

function HasForeignFiles(Dir: String): Boolean;
var
  F: TFindRec;
begin
  Result := False;
  // our own folder (an upgrade, or a repair after an interrupted one: then app\ may be gone)
  if FileExists(Dir + '\app\dd_entry.py') or FileExists(Dir + '\python\pythonw.exe') or DirExists(Dir + '\data') then Exit;
  if FindFirst(Dir + '\*', F) then
  try
    repeat
      if (F.Name <> '.') and (F.Name <> '..') then begin Result := True; Exit; end;
    until not FindNext(F);
  finally
    FindClose(F);
  end;
end;

// the program gets a folder of its own (uninstall removes its subfolders), writable without admin rights
function NextButtonClick(CurPageID: Integer): Boolean;
var
  Dir, Probe: String;
begin
  Result := True;
  if CurPageID = wpSelectDir then
  begin
    Dir := RemoveBackslashUnlessRoot(WizardForm.DirEdit.Text);
    if (Length(Dir) <= 3) or (DirExists(Dir) and HasForeignFiles(Dir)) then
    begin
      MsgBox('Програмі потрібна власна тека: «' + Dir + '» ' + 'вже містить інші файли або є коренем диска.' + #13#10 +
             'Буде використано підтеку «DaVinci Director» — перевірте шлях і натисніть «Далі».', mbInformation, MB_OK);
      WizardForm.DirEdit.Text := AddBackslash(Dir) + 'DaVinci Director';
      Result := False;
      Exit;
    end;
    Probe := Dir + '\.dd-write-test';
    if not ForceDirectories(Dir) or not SaveStringToFile(Probe, 'ok', False) then
    begin
      MsgBox('У теку «' + Dir + '» не можна записувати без прав адміністратора.' + #13#10 +
             'Оберіть теку у своєму профілі або на іншому диску (знадобиться кілька ГБ місця).', mbError, MB_OK);
      Result := False;
    end
    else
      DeleteFile(Probe);
  end;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  QuitCopy(ExpandConstant('{app}'));
  Result := '';
end;

// transcripts, stories and settings are the user's work: removed only if they say so
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Data: String;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    DelTree(ExpandConstant('{%TEMP}\DaVinciDirector'), True, True, True);  // exchange folder with the Resolve script
    Data := ExpandConstant('{app}\data');
    if DirExists(Data) and not UninstallSilent() then
      if MsgBox('Видалити також ваші транскрипції, сюжети й налаштування DaVinci Director?' + #13#10 +
                '(Ні — вони залишаться в ' + Data + ')', mbConfirmation, MB_YESNO) = IDYES then
        DelTree(Data, True, True, True);
    RemoveDir(ExpandConstant('{app}'));  // only if nothing is left
  end;
end;
