@echo off
rem Restarts FestCast inside the running start.bat window (e.g. to load new code). Does nothing if start.bat isn't running.
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\restart.ps1"
set CODE=%ERRORLEVEL%
echo.
if "%~1"=="" pause
exit /b %CODE%
