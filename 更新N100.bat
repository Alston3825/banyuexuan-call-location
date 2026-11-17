@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>&1
if errorlevel 1 goto python
py -3 host\deploy\deploy_n100.py menu
goto end
:python
python host\deploy\deploy_n100.py menu
:end
pause
