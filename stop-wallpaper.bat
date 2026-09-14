@echo off
rem ============================================================
rem  Stop the spotlight wallpaper.
rem  The app also quits with Ctrl+Alt+Q; this is the fallback.
rem  Safety: it verifies the pid really belongs to a Python
rem  process before killing it, so a stale pid file can never
rem  take down an unrelated program.
rem ============================================================
cd /d "%~dp0"

if not exist wallpaper.pid (
  echo Spotlight wallpaper is not running.
  timeout /t 3 >nul
  exit /b
)

set /p PID=<wallpaper.pid

tasklist /FI "PID eq %PID%" /NH 2>nul | findstr /I "python" >nul
if errorlevel 1 (
  echo PID %PID% is not a Python process - refusing to kill it.
  echo The wallpaper is probably already stopped. Removing stale pid file.
  del wallpaper.pid >nul 2>&1
  timeout /t 4 >nul
  exit /b
)

taskkill /PID %PID% /F >nul 2>&1
if errorlevel 1 (
  echo Could not stop pid %PID%.
) else (
  echo Stopped spotlight wallpaper, pid %PID%.
)
del wallpaper.pid >nul 2>&1
timeout /t 3 >nul
