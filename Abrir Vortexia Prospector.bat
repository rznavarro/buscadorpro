@echo off
setlocal
rem Abre Vortexia Prospector con doble clic. Para cerrarlo, cierra esta ventana negra.
chcp 65001 >nul
title Vortexia Prospector
cd /d "%~dp0"

call :find_uv
if not defined UV goto :not_installed
if not exist ".venv\Scripts\python.exe" goto :not_installed

echo Abriendo Vortexia Prospector en tu navegador... (no cierres esta ventana mientras lo usas)
"%UV%" run --no-dev python -m app.cli serve %*
echo.
echo Vortexia Prospector se detuvo. Si ves un error arriba, sacale una captura.
pause
exit /b 0

:not_installed
echo.
echo  Vortexia Prospector todavia no esta instalado en esta carpeta.
echo  Haz doble clic en "Instalar Vortexia Prospector.bat" (esta en la misma carpeta).
echo.
pause
exit /b 1

:find_uv
set "UV="
for /f "delims=" %%i in ('where uv 2^>nul') do if not defined UV set "UV=%%i"
if not defined UV if exist "%USERPROFILE%\.local\bin\uv.exe" set "UV=%USERPROFILE%\.local\bin\uv.exe"
if not defined UV if exist "%LOCALAPPDATA%\Microsoft\WinGet\Links\uv.exe" set "UV=%LOCALAPPDATA%\Microsoft\WinGet\Links\uv.exe"
if not defined UV for /d %%d in ("%LOCALAPPDATA%\Microsoft\WinGet\Packages\astral-sh.uv_*") do if exist "%%d\uv.exe" set "UV=%%d\uv.exe"
exit /b 0
