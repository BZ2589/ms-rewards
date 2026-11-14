@echo off
rem Launch the PyQt5 GUI with conda env test001 (pythonw = no console window)
cd /d "%~dp0"
set "PYW=D:\Anaconda\envs\test001\pythonw.exe"
if exist "%PYW%" (
    start "" "%PYW%" "%~dp0gui_qt.py"
    exit /b
)
set "PY=D:\Anaconda\envs\test001\python.exe"
if exist "%PY%" (
    start "" "%PY%" "%~dp0gui_qt.py"
    exit /b
)
echo [ERROR] conda env test001 not found: D:\Anaconda\envs\test001
echo         Falling back to the system Python + smart GUI launcher...
where py >nul 2>nul
if %errorlevel%==0 (
    start "" py -3.12 gui.py
    exit /b
)
pythonw gui.py
pause
