@echo off
setlocal
title Ayos - put network settings back
cd /d "%~dp0"
rem Emergency reset. Works without Python and without Ayos running.
fltmc >nul 2>&1
if errorlevel 1 (
  powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b
)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0restore_network.ps1" %*
pause
