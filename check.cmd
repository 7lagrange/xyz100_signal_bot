@echo off
call "%~dp0run.cmd" --check
if not errorlevel 1 pause
