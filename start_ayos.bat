@echo off
setlocal
title Ayos
cd /d "%~dp0"

rem Ayos needs administrator rights to change network settings (DNS, adapter, hosts file).
fltmc >nul 2>&1
if errorlevel 1 (
  echo Asking Windows for administrator rights...
  if "%~1"=="" (
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  ) else (
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -ArgumentList '%*' -Verb RunAs"
  )
  exit /b
)

set "PY="
py -3 -c "import sys" >nul 2>&1 && set "PY=py -3"
if not defined PY python -c "import sys" >nul 2>&1 && set "PY=python"
if not defined PY (
  echo.
  echo Python was not found on this PC.
  echo Install Python 3.10 or newer from https://www.python.org/downloads/
  echo and tick "Add python.exe to PATH" during setup. Then run this file again.
  echo.
  pause
  exit /b 1
)

%PY% -m ayos %*
if errorlevel 1 pause
