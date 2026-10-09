@echo off
setlocal
title Resolv
set "AYOS_SELF=%~f0"
set "AYOS_ARGS=%*"
cd /d "%~dp0"

rem Resolv needs administrator rights to change network settings (DNS, adapter, hosts file).
rem No folder name is used inside brackets below: a folder such as "Ayos-main (1)" would break them.
fltmc >nul 2>&1
if not errorlevel 1 goto admin
echo Asking Windows for administrator rights...
powershell -NoProfile -Command "if ($env:AYOS_ARGS) { Start-Process -FilePath $env:AYOS_SELF -ArgumentList $env:AYOS_ARGS -Verb RunAs } else { Start-Process -FilePath $env:AYOS_SELF -Verb RunAs }"
exit /b

:admin
set "PY="
py -3 -c "import sys" >nul 2>&1 && set "PY=py -3"
if not defined PY python -c "import sys" >nul 2>&1 && set "PY=python"
if defined PY goto run
echo.
echo Python was not found on this PC.
echo Install Python 3.10 or newer from https://www.python.org/downloads/
echo and tick "Add python.exe to PATH" during setup. Then run this file again.
echo Or download Resolv.exe from the Releases page of the repository. It needs no Python.
echo.
pause
exit /b 1

:run
%PY% -m ayos %*
if errorlevel 1 pause
