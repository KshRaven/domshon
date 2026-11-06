@echo off
REM ======================================================================
REM pypimgr.bat - Domshon Setup / Run / Build / Update Manager (Windows)
REM Requirements: Windows 10+, Python 3.10+
REM   Runs on PowerShell 7+ (pwsh) if available, otherwise falls back to
REM   the built-in Windows PowerShell 5.1 - pypimgr.ps1 works on either.
REM Usage: pypimgr.bat [setup|init|serve|gui|build|update|docker|service|passwd|pypi]
REM ======================================================================

setlocal EnableDelayedExpansion
chcp 65001 >nul 2>&1

set "SCRIPT_DIR=%~dp0"
set "PS_SCRIPT=%SCRIPT_DIR%pypimgr.ps1"
set "PS_EXE="

if not exist "%PS_SCRIPT%" (
    echo.
    echo   [ERROR]  pypimgr.ps1 not found
    echo            Expected: %PS_SCRIPT%
    echo.
    pause
    exit /b 1
)

where pwsh >nul 2>&1
if not errorlevel 1 (
    pwsh -NoProfile -Command "exit 0" >nul 2>&1
    if not errorlevel 1 set "PS_EXE=pwsh"
)
if not defined PS_EXE set "PS_EXE=powershell"

"%PS_EXE%" -NoProfile -ExecutionPolicy Bypass -File "%PS_SCRIPT%" %*
set "RC=%ERRORLEVEL%"
if not "%~1"=="" exit /b %RC%
if not "%RC%"=="0" pause
exit /b %RC%
