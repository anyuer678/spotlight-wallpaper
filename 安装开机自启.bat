@echo off
chcp 65001 >nul
rem ============================================================
rem  Install boot-time autostart for the spotlight wallpaper.
rem
rem  Creates a shortcut in your Startup folder (shell:startup)
rem  pointing to launch.pyw --no-panel, so at logon ONLY the
rem  wallpaper comes up - no panel window in your face.
rem  Manage it any time via Task Manager -> Startup apps, or
rem  run 移除开机自启.bat.
rem ============================================================
cd /d "%~dp0"

set "PY="
for /f "delims=" %%P in ('where python.exe 2^>nul') do (
  if not defined PY set "PY=%%P"
)
if not defined PY (
  if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" (
    set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
  )
)
if not defined PY set "PY=pythonw.exe"

start "" "%PY%" "%~dp0autostart.py" install
exit /b
