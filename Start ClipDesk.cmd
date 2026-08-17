@echo off
REM The one thing an end user needs to run. Sets up whatever is missing on the
REM first run, then starts the app; on later runs it goes straight to starting.
REM
REM -ExecutionPolicy Bypass is what lets this work on a copy that arrived by
REM email or download: Windows marks those files as untrusted, and the default
REM RemoteSigned policy would otherwise refuse to run the .ps1.
setlocal
cd /d "%~dp0"
title ClipDesk
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\run.ps1" %*
if errorlevel 1 (
    echo.
    echo ClipDesk stopped unexpectedly. The messages above explain why.
    pause
)
endlocal
