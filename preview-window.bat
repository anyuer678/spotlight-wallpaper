@echo off
rem ============================================================
rem  Safe preview: opens a normal 1200x720 window with a title bar.
rem  It can NOT cover your screen. Close it with the X button,
rem  press Esc, or use Ctrl+Alt+Q.
rem
rem  While the wallpaper is running you don't need this file at all:
rem  Ctrl+Alt+W opens and closes the very same window.
rem ============================================================
cd /d "%~dp0"
start "" "%~dp0wallpaper.pyw" --window
exit /b
