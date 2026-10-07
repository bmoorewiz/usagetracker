@echo off
rem Windows: double-click to install Python (if needed) and set up Frontier Usage.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install-windows.ps1" %*
echo.
pause
