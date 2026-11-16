@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 (
    python -u "%~dp0bot.py" %*
) else (
    py -3 -u "%~dp0bot.py" %*
)
if errorlevel 1 pause
