@echo off
setlocal
rem Instala Vortexia Prospector en esta carpeta: uv, Python, las librerias, el navegador
rem del programa y un acceso directo en el Escritorio. Se puede ejecutar de nuevo sin problema.
rem Con --desde-instalador (lo usa VortexiaProspector-Setup.exe) no crea accesos directos
rem ni pregunta nada: de eso se encarga el instalador.
title Instalar Vortexia Prospector
cd /d "%~dp0"
set "DESDE_INSTALADOR="
if /i "%~1"=="--desde-instalador" set "DESDE_INSTALADOR=1"

echo.
echo  ===== Instalando Vortexia Prospector =====
echo  La primera vez tarda varios minutos: descarga Python, las librerias y un navegador.
echo  No cierres esta ventana: se cierra sola cuando termina.
echo.
if defined DESDE_INSTALADOR goto :check_uv
echo "%~dp0" | find /i "OneDrive" >nul && (
  echo  AVISO: esta carpeta esta dentro de OneDrive. Funciona, pero es mejor moverla fuera
  echo  ^(por ejemplo a C:\VortexiaProspector^) para que OneDrive no bloquee archivos.
  echo.
)

:check_uv

call :find_uv
if not defined UV (
  echo [1/3] Instalando uv ^(el administrador de Python^)...
  winget install --id=astral-sh.uv -e --accept-source-agreements --accept-package-agreements
  call :find_uv
)
if not defined UV (
  echo       winget no pudo instalarlo; se usa el instalador oficial de uv...
  powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://astral.sh/uv/install.ps1 | iex"
  call :find_uv
)
if not defined UV (
  echo.
  echo  No se pudo instalar uv. Revisa tu conexion a internet y vuelve a intentarlo.
  goto :fail
)
echo [1/3] uv listo.

echo [2/3] Instalando Python y las librerias del programa...
"%UV%" sync --no-dev
if errorlevel 1 goto :fail

echo [3/3] Instalando el navegador que usa para buscar en Google Maps ^(Chromium^)...
"%UV%" run --no-dev playwright install chromium
if errorlevel 1 goto :fail
if not exist ".env" copy ".env.example" ".env" >nul

if defined DESDE_INSTALADOR exit /b 0

echo Creando el acceso directo "Vortexia Prospector" en el Escritorio...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$desktop = [Environment]::GetFolderPath('Desktop');" ^
  "$link = (New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path $desktop 'Vortexia Prospector.lnk'));" ^
  "$link.TargetPath = Join-Path '%~dp0' '.venv\Scripts\pythonw.exe';" ^
  "$link.Arguments = '-m app.desktop';" ^
  "$link.WorkingDirectory = '%~dp0';" ^
  "$link.IconLocation = (Join-Path '%~dp0' 'app\web\static\vortexia.ico') + ',0';" ^
  "$link.Description = 'Abre Vortexia Prospector';" ^
  "$link.Save()"

echo.
echo  ===== Instalacion lista =====
echo  Para usar el programa: doble clic en "Vortexia Prospector" en tu Escritorio.
echo.
set "RESP="
set /p RESP=" Quieres abrirlo ahora? (S/N): "
if /i "%RESP%"=="S" start "" "%~dp0.venv\Scripts\pythonw.exe" -m app.desktop
exit /b 0

:find_uv
set "UV="
for /f "delims=" %%i in ('where uv 2^>nul') do if not defined UV set "UV=%%i"
if not defined UV if exist "%USERPROFILE%\.local\bin\uv.exe" set "UV=%USERPROFILE%\.local\bin\uv.exe"
if not defined UV if exist "%LOCALAPPDATA%\Microsoft\WinGet\Links\uv.exe" set "UV=%LOCALAPPDATA%\Microsoft\WinGet\Links\uv.exe"
if not defined UV for /d %%d in ("%LOCALAPPDATA%\Microsoft\WinGet\Packages\astral-sh.uv_*") do if exist "%%d\uv.exe" set "UV=%%d\uv.exe"
exit /b 0

:fail
echo.
echo  La instalacion no se completo. Saca una captura de esta ventana para pedir ayuda.
pause
exit /b 1
