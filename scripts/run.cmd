@echo off
REM Kept so existing notes and shortcuts still work. "Start ClipDesk.cmd" is the
REM name people are told to use.
setlocal
cd /d "%~dp0.."
call "%~dp0..\Start ClipDesk.cmd" %*
endlocal
