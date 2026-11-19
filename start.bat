@echo off
setlocal
cd /d "%~dp0"
title FestCast
if not exist .venv\Scripts\python.exe (
  echo [FestCast] Creating Python environment...
  py -3.12 -m venv .venv || python -m venv .venv || (echo Python 3.12+ is required & pause & exit /b 1)
  .venv\Scripts\python -m pip install --upgrade pip >nul
  .venv\Scripts\python -m pip install -r requirements.txt || (echo Dependency install failed & pause & exit /b 1)
)
if not exist frontend\dist\index.html (
  where npm >nul 2>nul || (echo Node.js is required to build the dashboard once. & pause & exit /b 1)
  echo [FestCast] Building dashboard...
  pushd frontend
  call npm install --no-fund --no-audit || (popd & pause & exit /b 1)
  call npm run build || (popd & pause & exit /b 1)
  popd
)
if not exist .env copy .env.example .env >nul
echo.
echo   FestCast is starting. The dashboard opens automatically (the address is printed below).
echo   Keep this window open during the event. If FestCast ever crashes it restarts itself here.
echo   Closing this window stops FestCast: OBS keeps recording, the TV output pauses until FestCast is back.
echo.
set OPENFLAG=--open
:run
.venv\Scripts\python -m backend.main %OPENFLAG%
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
