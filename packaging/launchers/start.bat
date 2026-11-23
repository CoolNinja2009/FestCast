@echo off
setlocal
cd /d "%~dp0"
title FestCast
if not exist python\python.exe (echo python\ is missing: unzip the whole FestCast folder again. & pause & exit /b 1)
if not exist .env copy .env.example .env >nul
if not exist data\logs mkdir data\logs
echo.
echo   FestCast is starting. The dashboard opens automatically (the address is printed below).
echo   Keep this window open during the event. If FestCast ever crashes it restarts itself here.
echo   Closing this window stops FestCast: OBS keeps recording, the TV output pauses until FestCast is back.
echo.
set OPENFLAG=--open
:run
rem -E -s: ignore any PYTHON* variables and user-wide packages on this PC; FestCast only uses its own python\
python\python.exe -E -s -m backend.main %OPENFLAG%
set CODE=%ERRORLEVEL%
set OPENFLAG=
if "%CODE%"=="0" goto done
echo %date% %time% FestCast exited with code %CODE% - restarted automatically>> data\logs\restarts.log
echo.
echo   FestCast stopped unexpectedly (code %CODE%). Restarting in 3 seconds...
echo   OBS keeps recording and streaming meanwhile. Close this window to stop FestCast.
timeout /t 3 /nobreak >nul
goto run
:done
pause
