@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>&1
if errorlevel 1 goto python
py -3 host\deploy\deploy_n150.py menu
goto end
:python
python host\deploy\deploy_n150.py menu
:end
pause
