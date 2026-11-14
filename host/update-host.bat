@echo off
setlocal
cd /d "%~dp0"
set LOG=%~dp0update-log.txt
echo ==== %date% %time% ==== > "%LOG%"
echo.
echo  Banyuexuan host update
echo  Log file: %LOG%
echo.

if not exist "%~dp0bycall-host-amd64.tar.gz" goto nofile
if not exist "%~dp0docker-compose.yml" goto nocompose

if not exist "%~dp0models\whisper-small\model.bin" echo  NOTE: folder models\whisper-small not found - voice-to-text will be disabled until you copy it here.
echo [1/4] Checking Docker ...
docker version >> "%LOG%" 2>&1
if errorlevel 1 goto nodocker

echo [2/4] Loading new image (about 1 minute) ...
docker load -i "%~dp0bycall-host-amd64.tar.gz" >> "%LOG%" 2>&1
if errorlevel 1 goto fail

echo [3/4] Restarting host ...
docker compose up -d >> "%LOG%" 2>&1
if errorlevel 1 goto fail

echo [4/4] Waiting for host to start ...
ping -n 10 127.0.0.1 >nul
curl -s http://localhost:8100/health > "%~dp0health.txt" 2>> "%LOG%"
echo.
echo  Host answered:
type "%~dp0health.txt"
echo.
echo.
echo  If you see "version":"2026..." above, the update SUCCEEDED.
echo  If nothing is shown or there is no "version", the update FAILED - send update-log.txt to the developer.
goto end

:nofile
echo  ERROR: bycall-host-amd64.tar.gz not found next to this file.
echo  Copy it into the same folder as update-host.bat and run again.
goto end

:nocompose
echo  ERROR: docker-compose.yml is not in this folder.
echo  This file and bycall-host-amd64.tar.gz must be copied INTO the host folder,
echo  the same folder that contains docker-compose.yml. Move them there and run again.
goto end

:nodocker
echo  ERROR: Docker is not running. Start Docker Desktop, wait until it is ready, then run again.
goto end

:fail
echo  ERROR: a step failed. See update-log.txt in this folder.
type "%LOG%"

:end
echo.
pause
