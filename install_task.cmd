@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 (
    python "%~dp0install_task.py"
) else (
    py -3 "%~dp0install_task.py"
)
pause
