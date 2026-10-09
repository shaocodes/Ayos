@echo off
setlocal
title Ayos (simulated PC)
cd /d "%~dp0"
rem Rehearsal mode: a pretend PC. Nothing on this computer is changed, and no administrator rights are needed.
set "PY="
py -3 -c "import sys" >nul 2>&1 && set "PY=py -3"
if not defined PY python -c "import sys" >nul 2>&1 && set "PY=python"
if defined PY goto run
echo Python was not found. Install it from https://www.python.org/downloads/ and tick "Add python.exe to PATH".
pause
exit /b 1

:run
%PY% -m ayos --sim --port 8021 %*
if errorlevel 1 pause
