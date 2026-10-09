@echo off
setlocal
title Ayos self-test
cd /d "%~dp0"
rem Runs every read-only check once and times the local model. Changes nothing.
set "PY="
py -3 -c "import sys" >nul 2>&1 && set "PY=py -3"
if not defined PY python -c "import sys" >nul 2>&1 && set "PY=python"
if defined PY goto run
echo Python was not found. Install it from https://www.python.org/downloads/ and tick "Add python.exe to PATH".
pause
exit /b 1

:run
%PY% -m ayos --selftest %*
echo.
echo The same text was saved to ayos_selftest.txt in this folder.
pause
