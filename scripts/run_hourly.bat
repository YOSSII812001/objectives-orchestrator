@echo off
REM Objectives Orchestrator - hourly wrapper
REM Called by Task Scheduler.
REM Skips the cycle FAST and quietly when the local LLM (LM Studio) is down,
REM instead of launching Python and waiting ~30s for a timeout.

setlocal

set "SCRIPT_DIR=%~dp0"
set "PROJECT_DIR=%SCRIPT_DIR%.."

cd /d "%PROJECT_DIR%"
if errorlevel 1 (
    echo [run_hourly] cd failed: %PROJECT_DIR%
    exit /b 1
)

REM --- LM Studio health check ------------------------------------------------
REM This orchestrator needs a local LLM server (LM Studio, default :1234).
REM If the server is unreachable, there is nothing to do this cycle, so we
REM exit immediately. Change the port below if your LM Studio uses another one.
set "LMS_HEALTH_URL=http://localhost:1234/v1/models"
powershell -NoProfile -ExecutionPolicy Bypass -Command "try { Invoke-WebRequest -Uri $env:LMS_HEALTH_URL -TimeoutSec 3 -UseBasicParsing | Out-Null; exit 0 } catch { exit 1 }"
if errorlevel 1 (
    >> "logs\orchestrator.log" echo %DATE% %TIME% [run_hourly] LM Studio unreachable at %LMS_HEALTH_URL% - cycle skipped.
    endlocal & exit /b 0
)

REM --- Run one cycle ----------------------------------------------------------
py main.py
set "RC=%ERRORLEVEL%"

endlocal & exit /b %RC%
