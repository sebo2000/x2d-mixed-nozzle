@echo off
rem Double-click to start the X2D Mixed Nozzle window (Windows).
where pyw >nul 2>nul && (start "" pyw -3 "%~dp0x2d_mixed_gui.py" %* & exit /b 0)
where pythonw >nul 2>nul && (start "" pythonw "%~dp0x2d_mixed_gui.py" %* & exit /b 0)
echo Python 3 was not found. Install it from python.org and tick "Add python.exe to PATH".
pause
