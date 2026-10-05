@echo off
setlocal
rem Instala Vortexia Prospector en esta carpeta: uv, Python, las librerias, el navegador
rem del programa y un acceso directo en el Escritorio. Se puede ejecutar de nuevo sin problema.
chcp 65001 >nul
title Instalar Vortexia Prospector
cd /d "%~dp0"

echo.
echo  ===== Instalando Vortexia Prospector =====
echo  La primera vez tarda varios minutos: descarga Python, las librerias y un navegador.
echo  No cierres esta ventana hasta que diga "Instalacion lista".
echo.
echo "%~dp0" | find /i "OneDrive" >nul && (
  echo  AVISO: esta carpeta esta dentro de OneDrive. Funciona, pero es mejor moverla fuera
  echo  ^(por ejemplo a C:\VortexiaProspector^) para que OneDrive no bloquee archivos.
  echo.
)

call :find_uv
if not defined UV (
  echo [1/4] Instalando uv ^(el administrador de Python^)...
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
echo [1/4] uv listo.

echo [2/4] Instalando Python y las librerias del programa...
"%UV%" sync
if errorlevel 1 goto :fail

echo [3/4] Instalando el navegador del programa ^(Chromium^)...
"%UV%" run playwright install chromium
if errorlevel 1 goto :fail

echo [4/4] Creando el acceso directo "Vortexia Prospector" en el Escritorio...
if not exist ".env" copy ".env.example" ".env" >nul
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$desktop = [Environment]::GetFolderPath('Desktop');" ^
  "$link = (New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path $desktop 'Vortexia Prospector.lnk'));" ^
  "$link.TargetPath = Join-Path '%~dp0' 'Abrir Vortexia Prospector.bat';" ^
  "$link.WorkingDirectory = '%~dp0';" ^
  "$link.Description = 'Abre Vortexia Prospector';" ^
  "$link.Save()"

echo.
echo  ===== Instalacion lista =====
echo  Para usar el programa: doble clic en "Vortexia Prospector" en tu Escritorio.
echo.
set "RESP="
set /p RESP=" Quieres abrirlo ahora? (S/N): "
if /i "%RESP%"=="S" start "" "%~dp0Abrir Vortexia Prospector.bat"
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
