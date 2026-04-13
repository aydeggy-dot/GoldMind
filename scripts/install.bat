@echo off
REM =====================================================================
REM GoldMind VPS Setup  --  run once, as Administrator.
REM
REM Installs the three scheduled tasks (bot auto-start, watchdog, NTP
REM resync), disables screen lock / auto-reboot / screensaver, and
REM configures NTP. Everything here is idempotent -- safe to re-run.
REM =====================================================================

setlocal enableextensions

REM --- Resolve project root from this script's location (parent of scripts/)
set "SCRIPT_DIR=%~dp0"
pushd "%SCRIPT_DIR%.." >nul 2>&1
set "PROJECT_DIR=%CD%"
popd >nul 2>&1

echo === GoldMind VPS Setup ===
echo Project directory: %PROJECT_DIR%

REM --- Require admin for the reg + schtasks + w32tm commands
net session >nul 2>&1
if errorlevel 1 (
    echo ERROR: this script must be run as Administrator.
    exit /b 1
)

REM --- Disable screen lock on RDP disconnect (so MT5 keeps the GUI session)
reg add "HKLM\SOFTWARE\Policies\Microsoft\Windows\Personalization" ^
    /v NoLockScreen /t REG_DWORD /d 1 /f

REM --- Don't auto-reboot from Windows Update if a user is logged on
reg add "HKLM\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU" ^
    /v NoAutoRebootWithLoggedOnUsers /t REG_DWORD /d 1 /f

REM --- Disable screensaver for the current user
reg add "HKCU\Control Panel\Desktop" /v ScreenSaveActive /t REG_SZ /d 0 /f

REM --- Configure NTP for accurate broker-server clock comparison
w32tm /config /manualpeerlist:"time.windows.com" /syncfromflags:manual /reliable:YES /update
net stop w32time >nul 2>&1
net start w32time >nul 2>&1
w32tm /resync

REM --- Choose python launcher: prefer venv, fall back to system python
set "PY=%PROJECT_DIR%\.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

REM --- Auto-start bot on boot
schtasks /create /tn "GoldMind Bot" ^
    /tr "cmd /c cd /d \"%PROJECT_DIR%\" ^&^& \"%PY%\" main.py" ^
    /sc onstart /ru SYSTEM /rl highest /f

REM --- Watchdog every 30 min
schtasks /create /tn "GoldMind Watchdog" ^
    /tr "cmd /c cd /d \"%PROJECT_DIR%\" ^&^& \"%PY%\" scripts\watchdog.py" ^
    /sc minute /mo 30 /ru SYSTEM /f

REM --- NTP resync every hour
schtasks /create /tn "GoldMind NTP Sync" /tr "w32tm /resync" /sc hourly /ru SYSTEM /f

echo.
echo Setup complete. Next steps:
echo   1. Copy config\config.example.yaml to config\config.yaml and edit
echo   2. Copy config\credentials.example.yaml to config\credentials.yaml and edit
echo   3. Run: "%PY%" scripts\preflight.py
echo   4. When preflight passes, start "GoldMind Bot" from Task Scheduler
echo.
endlocal
