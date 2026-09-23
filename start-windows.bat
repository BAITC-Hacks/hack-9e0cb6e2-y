@echo off
rem JazAI for Windows: double-click this file. The first run installs everything,
rem later runs just start the app and open the browser.
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start_windows.ps1"
if errorlevel 1 (
  echo.
  echo JazAI stopped with an error. Read the message above and run this file again.
)
echo.
pause
