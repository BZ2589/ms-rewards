"""
图形界面统一入口（兼容旧调用方式：python gui.py / python main.py --gui）

优先使用 conda 环境 test001 的 PyQt5 界面（gui_qt.py）；
若当前解释器没有 PyQt5，则自动回退到零依赖的 tkinter 版（gui_tk.py）。
"""
from __future__ import annotations

import sys


def main() -> int:
    try:
        import PyQt5.QtWidgets  # noqa: F401
    except ImportError:
        pass
    else:
        from gui_qt import main as run
        return int(run() or 0)

    try:
        from gui_tk import main as run
    except ImportError as e:
        print("❌ 无法启动图形界面：当前 Python 既没有 PyQt5，也没有 tkinter。")
        print(f"   原始错误: {e}")
        print("   推荐：用 conda 环境 test001 启动（D:\\Anaconda\\envs\\test001\\pythonw.exe gui_qt.py）")
        return 1
    return int(run() or 0)


if __name__ == "__main__":
    sys.exit(main())
