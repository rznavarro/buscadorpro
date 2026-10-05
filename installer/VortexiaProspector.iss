; Instalador de Windows de Vortexia Prospector (Inno Setup 6).
; Se compila con: uv run python tools/armar-instalador.py
; Instala en la carpeta del usuario (sin pedir permisos de administrador), crea los accesos
; directos con ícono en el menú Inicio y el Escritorio, y descarga Python, las librerías y el
; navegador que usa para buscar en Google Maps.

#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif
#ifndef SourceDir
  #define SourceDir "..\build\VortexiaProspector"
#endif

[Setup]
AppId={{CBA77BB8-053C-4181-84D6-7F5FB0F288F4}
AppName=Vortexia Prospector
AppVersion={#AppVersion}
AppVerName=Vortexia Prospector {#AppVersion}
AppPublisher=Vortexia
DefaultDirName={localappdata}\Programs\Vortexia Prospector
DisableDirPage=yes
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\descargas
OutputBaseFilename=VortexiaProspector-Setup
SetupIconFile={#SourceDir}\app\web\static\vortexia.ico
UninstallDisplayIcon={app}\app\web\static\vortexia.ico
UninstallDisplayName=Vortexia Prospector
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ShowLanguageDialog=no
CloseApplications=yes

[Languages]
Name: "spanish"; MessagesFile: "compiler:Languages\Spanish.isl"

[Tasks]
Name: "desktopicon"; Description: "Crear un acceso directo en el Escritorio"; GroupDescription: "Accesos directos:"

[Files]
; El código del programa. Tus datos (carpeta data: leads, contactados, capturas) nunca vienen
; en el instalador, así que actualizar no los borra.
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{autoprograms}\Vortexia Prospector"; Filename: "{app}\.venv\Scripts\pythonw.exe"; Parameters: "-m app.desktop"; WorkingDir: "{app}"; IconFilename: "{app}\app\web\static\vortexia.ico"; Comment: "Leads locales con web y WhatsApp"
Name: "{autodesktop}\Vortexia Prospector"; Filename: "{app}\.venv\Scripts\pythonw.exe"; Parameters: "-m app.desktop"; WorkingDir: "{app}"; IconFilename: "{app}\app\web\static\vortexia.ico"; Comment: "Leads locales con web y WhatsApp"; Tasks: desktopicon

[Run]
Filename: "{app}\.venv\Scripts\pythonw.exe"; Parameters: "-m app.desktop"; WorkingDir: "{app}"; Description: "Abrir Vortexia Prospector"; Flags: postinstall nowait skipifsilent; Check: PythonReady

[UninstallRun]
; Antes de desinstalar se cierra el programa si está abierto (si no, Windows no deja borrar sus archivos).
Filename: "powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -Command ""Get-CimInstance Win32_Process | Where-Object {{ $_.ExecutablePath -like '{app}\*' } | ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }; Start-Sleep -Seconds 2"""; Flags: runhidden waituntilterminated; RunOnceId: "CerrarPrograma"

[UninstallDelete]
; El código y lo que se descargó al instalar. La carpeta data (tus datos) y .env (tu configuración) no se borran.
Type: filesandordirs; Name: "{app}\.venv"
Type: filesandordirs; Name: "{app}\app"

[Code]
function InitializeSetup(): Boolean;
begin
  Result := True;
  if not IsWin64 then
  begin
    MsgBox('Vortexia Prospector necesita Windows de 64 bits.', mbError, MB_OK);
    Result := False;
  end;
end;

function PythonReady(): Boolean;
begin
  Result := FileExists(ExpandConstant('{app}\.venv\Scripts\pythonw.exe'));
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  ResultCode: Integer;
  Script: String;
begin
  if CurStep = ssPostInstall then
  begin
    // Descarga Python, las librerías y Chromium (se ve una ventana con el avance).
    WizardForm.StatusLabel.Caption := 'Descargando Python, las librerías y el navegador del programa (la primera vez tarda varios minutos)...';
    Script := ExpandConstant('{app}\Instalar Vortexia Prospector.bat');
    if (not Exec(ExpandConstant('{cmd}'), '/c ""' + Script + '" --desde-instalador"', ExpandConstant('{app}'),
                 SW_SHOWNORMAL, ewWaitUntilTerminated, ResultCode))
       or (ResultCode <> 0) or (not PythonReady()) then
      MsgBox('No se pudo terminar de instalar (faltan Python, las librerías o el navegador).' + #13#10 +
             'Revisa tu conexión a internet y vuelve a ejecutar el instalador.', mbError, MB_OK);
  end;
end;
