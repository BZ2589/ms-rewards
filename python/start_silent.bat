@echo off
rem 静默启动：只驻留右下角托盘，不显示主窗口（和开机自启动完全一致）
rem 要打开界面：右键托盘图标 → 打开主窗口
start "" "D:\Anaconda\envs\test001\pythonw.exe" "%~dp0gui_qt.py" --minimized
