@echo off
chcp 65001 >nul
rem ============================================================
rem  Spotlight Wallpaper - ONE entry to start everything.
rem
rem  Double-click this file. It fills in what is missing and
rem  raises what is already running:
rem    - wallpaper not running  -> starts it (desktop layer)
rem    - wallpaper running      -> left alone
rem    - panel already open     -> brought to the front
rem    - panel closed           -> opened
rem
rem  For boot-time startup use 安装开机自启.bat instead: it
rem  registers launch.pyw --no-panel so only the wallpaper
rem  comes up at logon (Ctrl+Alt+P brings the panel back).
rem ============================================================
cd /d "%~dp0"

set "PYW="
for /f "delims=" %%P in ('where pythonw.exe 2^>nul') do (
  if not defined PYW set "PYW=%%P"
)
if not defined PYW (
  if exist "%LOCALAPPDATA%\Programs\Python\Python312\pythonw.exe" (
    set "PYW=%LOCALAPPDATA%\Programs\Python\Python312\pythonw.exe"
  )
)
if not defined PYW (
  echo.
  echo   pythonw.exe not found.  Please install Python 3 and make
  echo   sure it is on your PATH, then try again.
  echo.
  timeout /t 8 >nul
  exit /b 1
)

start "" "%PYW%" "%~dp0launch.pyw"
exit /b
