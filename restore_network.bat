@echo off
setlocal
title Ayos - put network settings back
set "AYOS_SELF=%~f0"
set "AYOS_PS1=%~dp0restore_network.ps1"
cd /d "%~dp0"
rem Emergency reset. Works without Python and without Ayos running.
fltmc >nul 2>&1
if not errorlevel 1 goto admin
powershell -NoProfile -Command "Start-Process -FilePath $env:AYOS_SELF -Verb RunAs"
exit /b

:admin
powershell -NoProfile -ExecutionPolicy Bypass -File "%AYOS_PS1%" %*
pause
