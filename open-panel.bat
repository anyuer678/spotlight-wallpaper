@echo off
chcp 65001 >nul
rem ============================================================
rem  Spotlight Wallpaper - Control Panel
rem
rem  Double-click this file to open the panel.
rem  The panel (panel.pyw) starts a tiny HTTP server bound to
rem  127.0.0.1 only and shows the UI in a chromeless Edge app
rem  window.  Close that window and the server shuts down too.
rem
rem  Also reachable from the wallpaper itself: Ctrl+Alt+P
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

start "" "%PYW%" "%~dp0panel.pyw"
exit /b
