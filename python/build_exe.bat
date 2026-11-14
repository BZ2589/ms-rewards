@echo off
chcp 65001 >nul
rem 一键打包成便携版 exe；具体流程在 pack_release.py（删旧产物→PyInstaller→组装→快捷方式）
rem 用法：build_exe.bat           单文件 exe
rem       build_exe.bat --onedir  文件夹版（启动更快）
rem       build_exe.bat --no-shortcut
cd /d "%~dp0"
set "PY=D:\Anaconda\envs\test001\python.exe"

if not exist "%PY%" (
    echo [ERROR] Python not found: %PY%
    echo         Edit PY in this script to point at a Python with PyQt5.
    pause & exit /b 1
)

"%PY%" pack_release.py %*
if errorlevel 1 (
    echo [ERROR] Build failed. See output above.
    pause & exit /b 1
)
pause
