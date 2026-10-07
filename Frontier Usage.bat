@echo off
rem Windows: double-click to open the Frontier Usage window.
cd /d "%~dp0"
where pyw >nul 2>nul && (start "" pyw gui.py & exit /b)
where pythonw >nul 2>nul && (start "" pythonw gui.py & exit /b)
echo Python 3 is not installed.
echo Install it from https://www.python.org/downloads/ - tick "Add python.exe to PATH" - then double-click this again.
start https://www.python.org/downloads/windows/
pause
