@echo off
rem ============================================================
rem  Start the spotlight wallpaper (desktop mode, no console).
rem  To stop it: press Ctrl+Alt+Q, or run stop-wallpaper.bat
rem ============================================================
cd /d "%~dp0"
start "" "%~dp0wallpaper.pyw"
exit /b
