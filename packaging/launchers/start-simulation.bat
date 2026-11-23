@echo off
cd /d "%~dp0"
set FESTCAST_SIMULATION=true
set FESTCAST_DATA_DIR=%~dp0data-sim
echo SIMULATION MODE: OBS is simulated. Nothing shown is a real camera, programme or recording.
call start.bat
