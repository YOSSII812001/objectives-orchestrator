@echo off
REM Objectives Orchestrator - hourly wrapper
REM Called by Task Scheduler. Lock and health checks live in main.py.

setlocal

set "SCRIPT_DIR=%~dp0"
set "PROJECT_DIR=%SCRIPT_DIR%.."

cd /d "%PROJECT_DIR%"
if errorlevel 1 (
    echo [run_hourly] cd failed: %PROJECT_DIR%
    exit /b 1
)

py main.py
set "RC=%ERRORLEVEL%"

endlocal & exit /b %RC%
