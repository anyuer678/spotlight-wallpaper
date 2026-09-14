@echo off
chcp 65001 >nul
rem ============================================================
rem  Remove the boot-time autostart created by 安装开机自启.bat.
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

start "" "%PY%" "%~dp0autostart.py" remove
exit /b
