"""
Microsoft Rewards 自动任务 —— 图形界面（PyQt5 版，主推入口）

环境：conda 环境 test001（Python 3.9 + PyQt5 5.15 / Qt 5.15）
启动：D:\\Anaconda\\envs\\test001\\pythonw.exe gui_qt.py
      （pythonw 不弹黑色控制台；也可直接双击桌面快捷方式）

结构：左侧导航 + 右侧堆叠页（控制台 / 运行参数 / 账号授权 / 通知推送 / 关于）
所有任务在后台线程执行，日志通过 Qt 信号槽回主线程，界面不会卡死。
本文件可作为 PyInstaller 的入口直接打包（不依赖 main.py --gui）。
"""
from __future__ import annotations

import html
import os
import random
import re
import sys
import tempfile
import threading
import time
import traceback
import webbrowser
from PyQt5.QtCore import QEvent, QObject, QPointF, QRect, QRectF, QTimer, QUrl, Qt, pyqtSignal
from PyQt5.QtGui import (QColor, QDesktopServices, QFont, QIcon, QPainter, QPen, QPixmap,
                         QPolygonF)
from PyQt5.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QFileDialog, QFormLayout, QFrame,
    QGridLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QListWidget, QMenu,
    QMainWindow, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QScrollArea,
    QSpinBox, QStackedWidget, QStyle, QStyleOptionProgressBar, QSystemTrayIcon,
    QTextBrowser, QVBoxLayout, QSizePolicy, QWidget,
)
# 兼容两种启动方式：python gui_qt.py 与 python main.py --gui
_m = sys.modules.get("__main__")
if _m is not None and getattr(_m, "BASE_DIR", None):
    cli = _m
else:
    import main as cli                                    # noqa: E402
import tasks                                              # noqa: E402
import netguard                                           # noqa: E402
import endpoint_test                                      # noqa: E402
import notify                                             # noqa: E402
import browser_login as bl                                # noqa: E402
from rewards import AUTH_URL, PUZZLE_MAX    # noqa: E402
def _find_icon() -> str:
    """源码运行时图标在项目目录；PyInstaller onefile 打包后在 _MEIPASS。"""
    for base in (getattr(sys, "_MEIPASS", None), cli.BASE_DIR):
        if base:
            p = os.path.join(base, "app.ico")
            if os.path.isfile(p):
                return p
    return ""
ICO_PATH = _find_icon()

# ------------------------------------------------------------ 开机自启动（注册表 HKCU\...\Run）
# 不用 WScript.Shell（本机安全策略禁 COM），winreg 写当前用户 Run 键即可，无需管理员权限。
_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_RUN_NAME = "MSRewardsConsole"


def autostart_enabled() -> bool:
    """开机自启动当前是否已启用（读注册表，注册表才是唯一事实来源）。"""
    if os.name != "nt":
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_READ) as k:
            val, _t = winreg.QueryValueEx(k, _RUN_NAME)
            return bool(val)
    except OSError:
        return False


def autostart_value() -> str:
    """读回当前注册表里记的自启动命令（没有则空串）。"""
    if os.name != "nt":
        return ""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_READ) as k:
            val, _t = winreg.QueryValueEx(k, _RUN_NAME)
            return str(val or "")
    except OSError:
        return ""


def autostart_self_heal() -> str:
    """打包版被搬到别处后，注册表还指着旧路径 → 自动改指当前 exe。

    只在"旧命令里的程序/脚本已经不存在"时才动手：否则同时存在源码版和 exe 版，
    谁后启动就把自启动抢过去，反而乱。
    返回给用户看的提示（无需处理时返回空串）。
    """
    if os.name != "nt" or not getattr(sys, "frozen", False):
        return ""
    cur = autostart_value()
    if not cur:
        return ""
    import re
    refs = [p for p in re.findall(r'"([^"]+)"', cur) if p.lower().endswith((".exe", ".py"))]
    if not refs and cur.strip().startswith('"'):
        refs = [cur.strip().strip('"')]
    if not refs:
        return ""
    if all(os.path.isfile(p) for p in refs):
        return ""                                   # 旧目标还在，不是搬家，别抢
    want = f'"{sys.executable}" --minimized'
    if os.path.normcase(cur.strip()) == os.path.normcase(want):
        return ""
    try:
        autostart_set(True)
    except Exception:
        return ""
    return f"开机自启动路径已更新为当前位置：{sys.executable}"


def autostart_set(on: bool) -> str:
    """开/关机自启动。返回实际写入的命令（关掉时返回空串）。

    命令 = pythonw.exe gui_qt.py --minimized：无黑框、启动后直接缩到托盘。
    """
    if os.name != "nt":
        return ""
    import winreg
    if on:
        if getattr(sys, "frozen", False):
            # 打包后就是 exe 自己，不能再拼解释器 + 脚本路径（那会指向临时解压目录）
            cmd = f'"{sys.executable}" --minimized'
        else:
            py = sys.executable
            if py.lower().endswith("\\python.exe"):          # 优先用无黑框的 pythonw
                pyw = py[:-10] + "pythonw.exe"
                if os.path.isfile(pyw):
                    py = pyw
            cmd = f'"{py}" "{os.path.abspath(__file__)}" --minimized'
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, _RUN_NAME, 0, winreg.REG_SZ, cmd)
        return cmd
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, _RUN_NAME)
    except OSError:
        pass
    return ""
NOTIFY_FIELDS = [
    ("wecom", "企业微信 WebhookKey"),
    ("dingtalk", "钉钉 access_token"),
    ("feishu", "飞书 hook id"),
    ("pushme", "PushMe key"),
    ("bark", "Bark key"),
]
# 通知开关（定义在 notify.SWITCH_RESULT / SWITCH_SYSTEM，共 13 项，每条消息一项）
NOTIFY_SWITCH_RESULT = notify.SWITCH_RESULT
NOTIFY_SWITCH_SYSTEM = notify.SWITCH_SYSTEM
TASK_ORDER = ["sign", "read", "promos", "quiz", "search", "streak", "daily_set", "claim_points", "rescan"]
PAGE_NAMES = ["控制台", "运行参数", "账号与授权", "通知推送", "关于"]   # 窗口标题里的「· 所在页面」
ROW1_ITEMS = [("cookie", "Cookie"), ("token", "OAuth"), ("region", "地区"), ("points", "积分")]
# 第二行：今日任务数据。全部来自服务器（DAPI options=613），不本地估算。
TODAY_ITEMS = [("today_earned", "今日积分"), ("quota", "搜索配额"), ("read", "阅读配额"),
               ("cards", "待领卡片"), ("puzzle", "拼图进度"), ("goal", "兑换目标")]
FIELD_W = 190          # 参数页参数框统一宽度：两个分组框的参数列左边缘才对得齐
READ_POINTS_PER_ARTICLE = 3   # 阅读 10 篇 = 30 分（offer 本身固定每篇 3 分），界面统一用分数
TODAY_TIPS = {
    "today_earned": "今天已经拿到的积分（服务器返回的 todays_points，不是本地估算）",
    "quota": "PC 搜索配额：今日已得 / 上限（括号里是程序给自己定的今日目标）",
    "read": "阅读任务进度（分数）：每读完 1 篇给 3 分，10 篇拿满 30 分",
    "cards": "还没领的活动卡片：张数 / 可领积分合计",
    "puzzle": "手机端拼图：集满 12 块给 1000 分",
    "goal": "你设置的兑换目标：当前积分 / 兑换所需积分（百分比为完成度）",
}
SEARCH_FIELDS = [
    ("search_span_seconds", "搜索间隔（秒，实际 ±15 随机）", 1, 3600, 30),
    ("search_batch_min", "每轮最少搜索次数", 1, 200, 4),
    ("search_batch_max", "每轮最多搜索次数", 1, 200, 7),
    ("search_target_min_pct", "每日搜索目标下限（占配额 %，不会被拿满）", 5, 94, 55),
    ("search_target_max_pct", "每日搜索目标上限（占配额 %，最大 95）", 5, 95, 90),
    ("read_target_min_pct", "每日阅读目标下限（占配额 %，不会被拿满）", 5, 94, 40),
    ("read_target_max_pct", "每日阅读目标上限（占配额 %，最大 95）", 5, 95, 85),
]
SCHEDULE_FIELDS = [
    ("delay_min_ms", "操作间最小延迟（毫秒）", 0, 120000, 3000),
    ("delay_max_ms", "操作间最大延迟（毫秒）", 0, 120000, 8000),
    ("daemon_interval_minutes", "守护：两轮间隔（分钟）", 1, 1440, 60),
    ("daemon_interval_jitter_minutes", "守护：间隔随机浮动（±分钟）", 0, 1440, 30),
    ("daemon_start_delay_min_s", "守护启动随机延迟 min（秒）", 0, 3600, 5),
    ("daemon_start_delay_max_s", "守护启动随机延迟 max（秒）", 0, 3600, 95),
    ("daemon_idle_seconds", "守护：电脑空闲多少秒才动手", 30, 7200, 300),
    ("daemon_idle_poll_seconds", "守护：等待空闲时的检查间隔（秒）", 5, 600, 20),
    ("daemon_idle_wait_max_minutes", "守护：最多等空闲多久（0=一直等）", 0, 1440, 0),
    ("daemon_task_gap_min_s", "守护：任务之间最少间隔（秒）", 0, 7200, 300),
    ("daemon_task_gap_max_s", "守护：任务之间最多间隔（秒）", 0, 7200, 720),
    ("daemon_round_max_minutes", "守护：整轮时长上限（0=不限，分钟）", 0, 1440, 120),
    ("daemon_wake_hour", "守护：次日开始整点（东八区，小时）", 0, 23, 7),
    ("daemon_wake_window_minutes", "次日开始随机窗口（整点后 0~N 分钟）", 0, 600, 90),
    ("priority_lane_cards", "快线：先做几张每日卡片（连签用）", 1, 10, 3),
    ("priority_lane_seconds", "快线：整段时限（秒）", 30, 900, 180),
    ("http_timeout", "HTTP 超时（秒）", 1, 300, 15),
]
LEVEL_COLOR = {
    "DEBUG": "#8b949e", "INFO": "#24292f", "WARN": "#9a6700",
    "ERROR": "#cf222e", "OK": "#1a7f37", "SYS": "#2563eb",
}
QSS = """
QWidget { font-family: 'Microsoft YaHei UI', 'Segoe UI', sans-serif; font-size: 10pt; color: #1f2937; }
QMainWindow, #central { background: #f4f6fa; }
#nav { background: #ffffff; border: none; outline: none; }
#nav::item { padding: 13px 14px; margin: 4px 8px; border-radius: 8px; color: #475569;
             border: none; outline: none; }
#nav::item:selected { background: #2563eb; color: #ffffff; font-weight: 600;
                      border: none; outline: none; }
#nav::item:hover:!selected { background: #eef2ff; color: #1d4ed8; outline: none; }
#header { background: #ffffff; border-bottom: 1px solid #e5e7eb; }
#title { font-size: 15pt; font-weight: 700; color: #0f172a; }
#subtitle { color: #64748b; font-size: 9pt; }
#badge { background: #ecfdf5; color: #047857; border: 1px solid #a7f3d0;
         border-radius: 10px; padding: 3px 10px; font-size: 9pt; }
#statCard { background: #ffffff; border: 1px solid #e5e7eb; border-radius: 10px; }
#statCard #statTitle { color: #64748b; font-size: 9pt; }
#statCard #statValue { color: #0f172a; font-size: 11pt; font-weight: 700; }
#todayProgress { background: #e8eef7; border: 1px solid #dbe3ee; border-radius: 9px; }
#todayProgress::chunk { background: #2563eb; border-radius: 8px; }
#statTitle { color: #64748b; font-size: 9pt; }
#statValue { color: #0f172a; font-size: 12pt; font-weight: 700; }
#pageTitle { font-size: 13pt; font-weight: 700; color: #0f172a; }
#pageHint { color: #64748b; font-size: 9pt; }
/* QGroupBox 由 Card 类自绘（标题在卡片内部 + 浅灰分割线），QSS 不再管它的边框 */
QPushButton { background: #ffffff; border: 1px solid #d1d5db; border-radius: 7px;
              padding: 7px 10px; color: #1f2937; }
QPushButton:hover { background: #f1f5f9; border-color: #94a3b8; }
QPushButton:pressed { background: #e2e8f0; }
QPushButton:disabled { background: #f1f5f9; color: #9ca3af; border-color: #e5e7eb; }
QPushButton#primary { background: #2563eb; border: 1px solid #2563eb; color: #ffffff; font-weight: 600; }
QPushButton#primary:hover { background: #1d4ed8; }
QPushButton#primary:disabled { background: #93c5fd; border-color: #93c5fd; color: #eff6ff; }
QPushButton#danger { background: #fff1f2; border: 1px solid #fecdd3; color: #be123c; }
QPushButton#danger:hover { background: #ffe4e6; }
QPushButton#saveBtn { background: #d1fae5; border: 1px solid #34d399; color: #065f46; font-weight: 600; }
QPushButton#saveBtn:hover { background: #a7f3d0; border-color: #10b981; }
QPushButton#saveBtn:pressed { background: #6ee7b7; }
QPushButton#refreshBtn { border: none; background: transparent; font-size: 17pt;
                         font-weight: 600; color: #2563eb; }
QCheckBox { spacing: 7px; padding: 3px 2px; }
QCheckBox::indicator { width: 16px; height: 16px; border-radius: 4px;
                       border: 1px solid #cbd5e1; background: #ffffff; }
QCheckBox::indicator:checked { background: #2563eb; border-color: #2563eb; }
QSpinBox, QLineEdit, QComboBox, QPlainTextEdit {
    background: #ffffff; border: 1px solid #d1d5db; border-radius: 6px;
    padding: 5px 8px; selection-background-color: #bfdbfe;
}
QSpinBox:focus, QLineEdit:focus, QComboBox:focus { border-color: #2563eb; }
#logView { background: #fbfcfe; border: 1px solid #dbe2ea; border-radius: 8px;
           font-family: Consolas, 'Cascadia Mono', monospace; font-size: 9pt; }
QScrollBar:vertical { background: transparent; width: 10px; margin: 0; border: none; }
QScrollBar:horizontal { background: transparent; height: 10px; margin: 0; border: none; }
QScrollBar::handle:vertical { background: #cbd5e1; border-radius: 5px; min-height: 36px; }
QScrollBar::handle:horizontal { background: #cbd5e1; border-radius: 5px; min-width: 36px; }
QScrollBar::handle:vertical:hover, QScrollBar::handle:horizontal:hover { background: #94a3b8; }
QScrollBar::handle:vertical:pressed, QScrollBar::handle:horizontal:pressed { background: #64748b; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; border: none; background: none; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; border: none; background: none; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical,
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal { background: transparent; }
QScrollBar::up-arrow:vertical, QScrollBar::down-arrow:vertical,
QScrollBar::left-arrow:horizontal, QScrollBar::right-arrow:horizontal { width: 0; height: 0; background: none; }
/* 右键菜单（含托盘菜单）：圆角、无系统旧边框、选中项淡蓝底 */
QMenu { background: #ffffff; border: 1px solid #e2e8f0; border-radius: 9px; padding: 6px; }
QMenu::item { padding: 8px 22px 8px 14px; border-radius: 6px; color: #1f2937; min-width: 132px; }
QMenu::item:selected { background: #eff6ff; color: #1d4ed8; }
QMenu::item:disabled { color: #9ca3af; }
QMenu::separator { height: 1px; background: #eef2f7; margin: 5px 10px; }
QMenu::icon { padding-left: 8px; }
QComboBox QAbstractItemView { background: #ffffff; border: 1px solid #e2e8f0; border-radius: 8px;
                              padding: 4px; outline: none; selection-background-color: #eff6ff;
                              selection-color: #1d4ed8; }
QToolTip { background: #111827; color: #f9fafb; border: none; padding: 5px 9px; font-size: 9pt; }
QStatusBar { background: #ffffff; border-top: 1px solid #e5e7eb; color: #64748b; }
"""
UI_ASSET_DIR = os.path.join(tempfile.gettempdir(), "msrewards-ui")


def _ui_assets() -> dict:
    """生成 QSS 用的小图标（数值框上下箭头 / 下拉箭头 / 勾选对勾），返回路径表。

    Qt 样式表不支持 CSS 三角形、也不便内嵌矢量，于是用 QPainter 画成 PNG 放临时目录，
    首次生成后复用。纯外观资源，不影响任何业务。
    """
    d = UI_ASSET_DIR
    os.makedirs(d, exist_ok=True)
    paths = {"up": os.path.join(d, "spin_up.png"), "down": os.path.join(d, "spin_down.png"),
             "chevron": os.path.join(d, "combo_chevron.png"), "check": os.path.join(d, "check.png")}
    if all(os.path.isfile(p) for p in paths.values()):
        return paths

    def _canvas(w: int, h: int, scale: int = 3):
        pm = QPixmap(w * scale, h * scale)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.scale(scale, scale)
        return pm, p

    def _arrow(up: bool):
        pm, p = _canvas(12, 8)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#64748b"))
        pts = ([QPointF(6, 1.2), QPointF(10.6, 6.4), QPointF(1.4, 6.4)] if up else
               [QPointF(6, 6.8), QPointF(1.4, 1.6), QPointF(10.6, 1.6)])
        p.drawPolygon(QPolygonF(pts))
        p.end()
        return pm

    def _chevron():
        pm, p = _canvas(12, 8)
        p.setPen(QPen(QColor("#64748b"), 1.6, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        p.drawPolyline(QPolygonF([QPointF(2, 2.6), QPointF(6, 6.2), QPointF(10, 2.6)]))
        p.end()
        return pm

    def _check():
        pm, p = _canvas(18, 18, scale=1)         # 勾选图标按实际像素画，避免缩放糊掉
        p.setPen(QPen(QColor("#ffffff"), 2.0, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        p.drawPolyline(QPolygonF([QPointF(4.0, 9.4), QPointF(7.6, 13.0), QPointF(14.0, 5.2)]))
        p.end()
        return pm

    _arrow(True).save(paths["up"], "PNG")
    _arrow(False).save(paths["down"], "PNG")
    _chevron().save(paths["chevron"], "PNG")
    _check().save(paths["check"], "PNG")
    return paths


def sanitize_qss(css: str) -> str:
    """剔除以 '# ' 开头的伪注释行。

    Qt 样式表里注释只能写成 /* ... */；以 # 开头的行会被当成 ID 选择器，
    并且会把紧随其后的规则一起吞掉（曾导致 QPushButton 起的所有样式全部失效）。
    这里只匹配「# 后跟空白」的行，不会误伤 #nav / #statCard 这类 ID 选择器。
    """
    return re.sub(r"(?m)^[ \t]*#[ \t].*$", "", css)


def extra_qss() -> str:
    """依赖运行时生成的图标，追加在主 QSS 之后。"""
    p = {k: v.replace("\\", "/") for k, v in _ui_assets().items()}
    return f"""
/* ---- 数值框 / 下拉框：自绘小箭头，替换系统老旧三角按钮 ---- */
QSpinBox {{ padding-right: 26px; }}
QSpinBox::up-button, QSpinBox::down-button {{
    subcontrol-origin: border; width: 22px; border: none; background: transparent;
}}
QSpinBox::up-button   {{ subcontrol-position: top right;    margin: 3px 3px 0 0; }}
QSpinBox::down-button {{ subcontrol-position: bottom right; margin: 0 3px 3px 0; }}
QSpinBox::up-button:hover, QSpinBox::down-button:hover {{ background: #eef2f7; border-radius: 5px; }}
QSpinBox::up-button:pressed, QSpinBox::down-button:pressed {{ background: #dbe4f0; border-radius: 5px; }}
QSpinBox::up-arrow   {{ image: url({p['up']});   width: 9px; height: 6px; }}
QSpinBox::down-arrow {{ image: url({p['down']}); width: 9px; height: 6px; }}
QComboBox {{ padding-right: 26px; }}
QComboBox::drop-down {{ subcontrol-origin: padding; subcontrol-position: top right;
                        width: 26px; border: none; background: transparent; }}
QComboBox::drop-down:hover {{ background: #eef2f7; border-top-right-radius: 6px;
                              border-bottom-right-radius: 6px; }}
QComboBox::down-arrow {{ image: url({p['chevron']}); width: 11px; height: 7px; }}
QCheckBox::indicator:checked {{ image: url({p['check']}); }}
QCheckBox::indicator:hover {{ border-color: #93b4f5; }}
"""
GUIDE_HTML = """
<div style="font-family:'Microsoft YaHei UI','Segoe UI';font-size:10pt;line-height:1.7;color:#1f2937">
<h3 style="color:#0f172a;margin-bottom:6px">手动获取 Cookie · 分 5 步</h3>
<p style="background:#fffbeb;border:1px solid #fde68a;padding:9px;border-radius:7px;color:#92400e">
<b>为什么首选「一键登录」：</b>必应的登录凭据放在 <b>HttpOnly</b> Cookie 里，
<code>document.cookie</code> 根本读不到。一键登录通过浏览器调试接口直接读完整 Cookie（含 HttpOnly），
还会自动验证登录态；手动方式则必须从 <b>Network 面板</b> 复制整串请求头。
</p>
<ol>
<li>点下方「打开登录页」，在浏览器里登录你的微软账号（rewards.bing.com）。</li>
<li>按 <b>F12</b> 打开开发者工具 → 切到 <b>网络 / Network</b> 面板 → 按 <b>F5</b> 刷新页面。</li>
<li>点请求列表里任意一条 <code>rewards.bing.com</code> 或 <code>getuserinfo</code> 请求
    → 右侧 <b>标头 / Headers</b> → <b>请求标头 / Request Headers</b>。</li>
<li>找到 <code>Cookie:</code> 那一行，右键复制整串值（很长，形如
    <code>_U=xxx; MUID=xxx; KievRPSAuthEng=xxx; ...</code>）。</li>
<li>回到本页点「📋 从剪贴板粘贴」→「💾 保存并验证登录」。界面会立刻用接口检查登录态并显示当前积分。</li>
</ol>
<p><b>粘贴框还认识这些格式：</b>扩展导出的 cookies.txt（Netscape 格式）、
从 CDP / 扩展复制的 JSON 数组、一行一个 <code>name=value</code>、甚至直接填 cookies.txt 的文件路径。</p>
<p style="color:#be123c"><b>⚠️ Cookie 等同于账号登录凭据</b>，只保存在本目录 cookies.txt，
不要发给任何第三方，也不要贴到聊天或论坛里。</p>
</div>
"""
GUIDE_TEXT = """手动获取 Microsoft Rewards Cookie（5 步）

1. 打开 https://rewards.bing.com/ 并登录微软账号
2. 按 F12 → Network（网络）面板 → F5 刷新
3. 点任意一条 rewards.bing.com / getuserinfo 请求 → Headers（标头）→ Request Headers（请求标头）
4. 找到 Cookie: 那一行，右键复制整串值（形如 _U=xxx; MUID=xxx; ...）
   ※ 不要复制 document.cookie，它读不到 HttpOnly Cookie
5. 回到控制台「账号与授权」→ 从剪贴板粘贴 → 保存并验证登录

也支持 cookies.txt（Netscape）、JSON 数组、一行一个 name=value、或文件路径。
Cookie 等同登录凭据，请勿外传。
"""


class RefreshRingButton(QPushButton):
    """矢量自绘的圆环刷新按钮（纯 QPainter，不用任何图片）：

    * 浅蓝圆环 = 底环，整圈永远都在；
    * 深蓝圆弧 = 进度，从 12 点方向顺时针画，弧长代表距离下次自动刷新
      已经流逝的时间比例；深蓝画满（到点）就自动刷新一次；
    * 正在刷新时，深蓝弧变成一段绕中心旋转的图样；
    * 平时仍是普通按钮，点一下立刻刷新。
    """

    RING = 3.4                                   # 环线宽度
    C_LIGHT = "#bfdbfe"                          # 浅蓝底环
    C_DARK = "#2563eb"                           # 深蓝进度弧
    C_LIGHT_HOVER = "#93c5fd"

    def __init__(self, parent=None):
        super().__init__(parent)
        self._ratio = 0.0                        # 0~1：距下次自动刷新已流逝比例
        self._spinning = False
        self._angle = 0                          # 旋转中的角度
        self._timer = QTimer(self)
        self._timer.setInterval(40)
        self._timer.timeout.connect(self._tick)
        self.setCursor(Qt.PointingHandCursor)

    def set_ratio(self, ratio: float):
        """外部每秒推进一次进度弧（0~1）；刷新旋转期间不接受推进。"""
        if not self._spinning:
            self._ratio = max(0.0, min(1.0, float(ratio)))
            self.update()

    def set_spinning(self, on: bool):            # 与旧接口同名，调用方不用改
        if on and not self._spinning:
            self._spinning = True
            self._timer.start()
        elif not on and self._spinning:
            self._spinning = False
            self._timer.stop()
            self._angle = 0
            self._ratio = 0.0
            self.update()

    def _tick(self):
        self._angle = (self._angle + 8) % 360    # 绕正中心匀速旋转
        self.update()

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        w = min(self.width(), self.height())
        pad = self.RING / 2 + 2.0
        arc_rect = QRectF(pad, pad, w - pad * 2, w - pad * 2)
        cx = self.width() / 2.0
        cy = self.height() / 2.0
        centered = QRectF(cx - arc_rect.width() / 2, cy - arc_rect.height() / 2,
                          arc_rect.width(), arc_rect.height())
        # 浅蓝底环：整圈
        pen = QPen(QColor(self.C_LIGHT_HOVER if self.underMouse() else self.C_LIGHT), self.RING)
        pen.setCapStyle(Qt.FlatCap)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        p.drawEllipse(arc_rect)
        # 深蓝：旋转中的图样 / 或时间进度弧（12 点方向起，顺时针）
        pen.setColor(QColor(self.C_DARK))
        pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        if self._spinning:
            p.drawArc(centered, int((90 - self._angle) * 16), -100 * 16)
        elif self._ratio >= 0.995:
            p.drawEllipse(arc_rect)              # 满：整圈深蓝
        elif self._ratio > 0.004:
            p.drawArc(centered, 90 * 16, int(-360 * 16 * self._ratio))
        p.end()


class Card(QGroupBox):
    """圆角卡片分组框：标题画在卡片内部左上角（不再骑在边框上），
    标题下一条浅灰分割线，内容区从分割线下方开始。"""

    TOP = 44                                     # 标题区高度（含分割线）

    def __init__(self, title: str = "", parent=None):
        super().__init__(title, parent)
        f = self.font()
        f.setBold(True)
        self.setFont(f)

    def setLayout(self, layout):
        super().setLayout(layout)

    def showEvent(self, ev):
        """左右边距对齐标题（16px）；顶部沿用 QGroupBox 给标题预留的空间，
        分割线下方正好是内容区。"""
        super().showEvent(ev)
        lay = self.layout()
        if lay is not None:
            m = lay.contentsMargins()
            if m.left() != 16 or m.bottom() != 12:
                lay.setContentsMargins(16, m.top(), 16, 12)

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(QPen(QColor("#dfe4ec"), 1))
        p.setBrush(QColor("#ffffff"))
        p.drawRoundedRect(QRectF(0.5, 0.5, self.width() - 1, self.height() - 1), 10, 10)
        # 标题：卡片内部左上角（颜色、字重与原来一致，只是挪进来了）
        p.setPen(QColor("#334155"))
        p.setFont(self.font())
        p.drawText(QRect(16, 0, self.width() - 32, self.TOP - 10),
                   Qt.AlignLeft | Qt.AlignVCenter, self.title())
        # 浅灰分割线：标题与内容之间
        p.setPen(QPen(QColor("#e5e7eb"), 1))
        y = self.TOP - 8.5
        p.drawLine(QPointF(14, y), QPointF(self.width() - 14, y))
        p.end()


class TaskProgressBar(QProgressBar):
    """今日任务进度条：文字自绘，按色块是否盖住中心自动切换深/白，任何百分比都看得清。"""

    def paintEvent(self, ev):
        opt = QStyleOptionProgressBar()
        self.initStyleOption(opt)
        opt.text = ""
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        self.style().drawControl(QStyle.CE_ProgressBar, opt, p, self)
        span = max(1, self.maximum() - self.minimum())
        ratio = (self.value() - self.minimum()) / span
        chunk_w = int(self.width() * max(0.0, min(1.0, ratio)))
        f = QFont(self.font())
        f.setPointSize(10)
        f.setBold(True)
        p.setFont(f)
        text = self.format().replace("%p", str(self.value())).replace("%v", str(self.value()))
        rect = self.rect().adjusted(12, 0, -12, 0)
        # 先整行画深色字，再在蓝色块内裁剪重画白字 —— 两个色块交界处的字都看得清
        p.setPen(QColor("#1e293b"))
        p.drawText(rect, Qt.AlignCenter, text)
        p.save()
        p.setClipRect(0, 0, max(0, chunk_w), self.height())
        p.setPen(QColor("#ffffff"))
        p.drawText(rect, Qt.AlignCenter, text)
        p.restore()
        p.end()


class Signals(QObject):
    """后台线程 → 主线程的安全通道（Qt 跨线程信号自动排队）。"""
    log = pyqtSignal(str, str, str)          # level, icon, msg
    status = pyqtSignal(dict)
    check_done = pyqtSignal(str)             # 一次刷新结束（参数=按钮 key），对应 ⟳ 停转
    cookie_state = pyqtSignal(str)
    browser = pyqtSignal(str, str)           # 阶段, 说明（一键登录进度）
    browser_done = pyqtSignal(dict)          # 一键登录 / 验证结果
    done = pyqtSignal()
    error = pyqtSignal(str)
    rest = pyqtSignal(bool, str)             # 守护进入/退出「等待」状态（第二个参数=原因）
def _deep_merge(dst: dict, patch: dict) -> None:
    """递归合并 patch 进 dst（嵌套 dict 逐层合，而不是整层覆盖）。"""
    for k, val in patch.items():
        if isinstance(val, dict) and isinstance(dst.get(k), dict):
            _deep_merge(dst[k], val)
        else:
            dst[k] = val


class App(QMainWindow):
    def __init__(self):
        super().__init__()
        self.cfg = cli.ensure_config()
        self.signals = Signals()
        self.stop_event = threading.Event()
        self.running = False
        # 守护在两轮之间（含睡到次日）时 worker 线程还活着，但界面必须回到可用状态，
        # 否则整夜 running=True → 按钮全灰、圆环冻结、数据永远不刷新。
        self._daemon_resting = False
        self._worker_mode = ""                     # 当前 worker 是哪个模式起的
        self._hidden_at = 0.0                      # 收进托盘/最小化的时刻
        self._last_tick_wall = time.time()          # 上次 tick 的墙钟（识别睡眠/休眠）
        self._ui_loading = False                   # 载入配置时屏蔽「改动自动保存」回写
        self.task_vars: dict[str, QCheckBox] = {}
        self.v: dict[str, QWidget] = {}
        self.notify_vars: dict[str, QLineEdit] = {}
        self.notify_cat_vars: dict[str, QCheckBox] = {}
        self.win_notify_var: QCheckBox | None = None
        self.status_labels: dict[str, QLabel] = {}
        self.buttons: dict[str, QPushButton] = {}
        # 一键登录（浏览器自动采集）相关状态
        self.browser_session: bl.BrowserSession | None = None
        self.login_busy = False
        self.login_stop = threading.Event()
        # 只在「真正退出程序」时置位：用来打断还在等网络/等空闲的后台线程，
        # 和 stop_event（停止任务，可再次开始）区分开。
        self.app_stop = threading.Event()
        self.login_buttons: dict[str, QPushButton] = {}
        self.setWindowTitle(f"Microsoft Rewards 自动任务 · {PAGE_NAMES[0]}")
        if os.path.isfile(ICO_PATH):
            self.setWindowIcon(QIcon(ICO_PATH))
        self.resize(1180, 810)
        self.setMinimumSize(1000, 680)
        self._build_shell()
        self._build_console_page()
        self._build_params_page()
        self._build_account_page()
        self._build_notify_page()
        self._build_about_page()
        self._connect_signals()
        self._load_into_ui(self.cfg)
        self._refresh_file_status()
        self._saved_cfg = self._collect()          # 基准快照：用于「撤销修改」与切页保存提醒
        self._update_dirty_ui()
        self._dirty_timer = QTimer(self)           # 定时探测：改了没保存才亮出「保存/撤销」
        self._dirty_timer.timeout.connect(self._update_dirty_ui)
        self._dirty_timer.start(600)
        QTimer.singleShot(150, self._startup_login_state)
        QTimer.singleShot(2000, self._startup_auto_check)   # 进入软件先自动体检一次
        self._reset_countdown()                    # 圆环开始向「下次自动刷新」计时
        self._ring_timer = QTimer(self)            # 每秒推进一次两个圆环的深蓝进度弧
        self._ring_timer.timeout.connect(self._ring_tick)
        self._ring_timer.start(1000)
        self._last_auto_refresh = time.time()      # 上次真刷新的时刻（从托盘唤醒时做节流用）
        self._daemon_wanted = False                # 守护是否应当处于运行状态（含托盘文案、断电恢复）
        self._really_quit = False                  # False=关窗口只缩托盘；True=托盘退出
        self._tray_hint_shown = False
        self._init_tray()
        QTimer.singleShot(2500, self._resume_daemon)   # 上次守护开着 → 开机自动恢复
        self._sys("界面就绪。建议顺序：账号与授权 → 体检 → 测试一轮 → 正式运行（手动跑一次）/ 开始守护（长期挂着）")
        self.statusBar().showMessage("空闲  ·  配置目录 " + cli.BASE_DIR)
    # ------------------------------------------------------------ 外壳
    def _build_shell(self):
        central = QWidget()
        central.setObjectName("central")
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        header = QFrame()
        header.setObjectName("header")
        header.setFixedHeight(66)
        hl = QHBoxLayout(header)
        hl.setContentsMargins(20, 8, 20, 8)
        title = QLabel("Microsoft Rewards 自动任务")
        title.setObjectName("title")
        hl.addWidget(title)
        hl.addStretch(1)
        # 配置按钮移到顶栏右侧：没改过配置时保存/撤销隐藏，改了才出现
        save = QPushButton("💾  保存配置")
        save.setObjectName("saveBtn")
        save.setToolTip("把界面上所有修改写入 config.json（运行任务前也会自动保存一次）")
        save.clicked.connect(lambda: self._save_all(False))
        hl.addWidget(save)
        self.btn_save = save
        undo = QPushButton("↩️  撤销修改")
        undo.setToolTip("放弃界面上未保存的修改，重新从 config.json 读回上一次保存的值")
        undo.clicked.connect(self._revert_config)
        hl.addWidget(undo)
        self.btn_undo = undo
        opendir = QPushButton("📂  项目目录")
        opendir.setToolTip("打开程序所在的 config.json / cookies.txt / logs 目录")
        opendir.clicked.connect(lambda: self._open_dir(cli.BASE_DIR))
        hl.addWidget(opendir)
        save.setVisible(False)                     # 初始无修改：只留「项目目录」
        undo.setVisible(False)
        root.addWidget(header)
        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        self.nav = QListWidget()
        self.nav.setObjectName("nav")
        self.nav.setFixedWidth(196)
        for name in ("🎛  控制台", "⚙  运行参数", "🔑  账号与授权", "🔔  通知推送", "📦  关于"):
            self.nav.addItem(name)
        self.nav.setCurrentRow(0)
        body.addWidget(self.nav)
        self.stack = QStackedWidget()
        body.addWidget(self.stack, 1)
        root.addLayout(body, 1)
        self.nav.currentRowChanged.connect(self._on_nav_change)
        self.setCentralWidget(central)
    def _page(self, title: str, hint: str) -> tuple[QWidget, QVBoxLayout]:
        """创建一个可滚动页面，返回 (页面, 内容布局)。"""
        outer = QWidget()
        ol = QVBoxLayout(outer)
        ol.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)   # 内容超宽就压缩/换行，不出底部横滑条
        inner = QWidget()
        lay = QVBoxLayout(inner)
        lay.setContentsMargins(22, 16, 22, 18)
        lay.setSpacing(12)
        t = QLabel(title)
        t.setObjectName("pageTitle")
        lay.addWidget(t)
        h = QLabel(hint)
        h.setObjectName("pageHint")
        h.setWordWrap(True)
        hrow = QHBoxLayout()                       # 提示行：右侧可以挂控件（如 ⟳ 刷新）
        hrow.setContentsMargins(0, 0, 0, 0)
        hrow.addWidget(h, 1)
        lay.addLayout(hrow)
        self._last_hint_row = hrow
        scroll.setWidget(inner)
        ol.addWidget(scroll)
        self.stack.addWidget(outer)
        return outer, lay
    # ------------------------------------------------------------ 控制台页
    def _build_console_page(self):
        _, lay = self._page("🎛  控制台", "状态一览、任务开关与实时日志。任务在后台线程运行，界面不会卡住。")
        # 状态卡（第一行）：与今日任务无关的 4 项，各自独立圆角卡片，名称与状态同行
        def _stat_card(key: str, label: str, tip: str = "") -> QFrame:
            card = QFrame()
            card.setObjectName("statCard")
            if tip:
                card.setToolTip(tip)
            h = QHBoxLayout(card)
            h.setContentsMargins(14, 7, 14, 7)
            h.setSpacing(8)
            lt = QLabel(label)
            lt.setObjectName("statTitle")
            if tip:
                lt.setToolTip(tip)
            lv = QLabel("未检测")
            lv.setObjectName("statValue")
            if tip:
                lv.setToolTip(tip)
            self.status_labels[key] = lv
            lv.setAlignment(Qt.AlignRight | Qt.AlignVCenter)   # 值在卡片内右对齐
            h.addWidget(lt)
            h.addWidget(lv, 1)
            return card

        card_row = QHBoxLayout()
        card_row.setSpacing(10)
        for key, label in ROW1_ITEMS:
            card_row.addWidget(_stat_card(key, label), 1)
        lay.addLayout(card_row)
        # 进度条行：今日任务完成进度，右端挂本行自己的 ⟳ 刷新按钮
        prow = QHBoxLayout()
        prow.setSpacing(10)
        self.progress_bar = TaskProgressBar()
        self.progress_bar.setObjectName("todayProgress")
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setFixedHeight(28)
        self.progress_bar.setFormat("今日任务完成进度  %p%")
        prow.addWidget(self.progress_bar, 1)
        refresh_today = RefreshRingButton()
        refresh_today.setObjectName("refreshBtn")
        refresh_today.setFixedSize(34, 34)
        refresh_today.setToolTip(
            "刷新今日任务数据（只读）：今日积分、搜索/阅读配额、待领卡片、拼图进度、兑换目标。\n"
            "圆环上的深蓝弧 = 距下次自动刷新的时间进度，画满自动刷新一次；点一下立刻刷新。")
        refresh_today.clicked.connect(lambda: self._start("today"))
        self.buttons["today_refresh"] = refresh_today
        prow.addWidget(refresh_today)
        lay.addLayout(prow)
        # 今日任务卡（第二行）：与今天得分相关的状态，各自独立卡片，3 列均分、放不下自动换行
        grid2 = QGridLayout()
        grid2.setHorizontalSpacing(10)
        grid2.setVerticalSpacing(10)
        for i, (key, label) in enumerate(TODAY_ITEMS):
            grid2.addWidget(_stat_card(key, label, TODAY_TIPS.get(key, "")), i // 3, i % 3)
        for col in range(3):
            grid2.setColumnStretch(col, 1)
        lay.addLayout(grid2)
        # ⟳ 刷新①放在页面灰色提示文字同一行右端；体检期间图标自转，结束停住
        refresh = RefreshRingButton()
        refresh.setObjectName("refreshBtn")
        refresh.setFixedSize(34, 34)
        refresh.setToolTip(
            "体检 / 刷新状态（只读，不改任何东西）：\n"
            "Cookie 是否登录、OAuth 是否有效、地区、当前积分。\n"
            "圆环上的深蓝弧 = 距下次自动刷新的时间进度，画满自动刷新一次"
            "（仅当你正看着控制台这一页）；点一下立刻刷新。")
        refresh.clicked.connect(lambda: self._start("check"))
        self.buttons["refresh"] = refresh
        self._last_hint_row.addWidget(refresh)
        # 操作按钮
        opbox = Card("操作")
        opl = QHBoxLayout(opbox)
        opl.setSpacing(8)
        specs = [
            ("dry", "🧪  试跑", "dry", False),
            ("test", "🔬  测试一轮", "test", True),
            ("run", "🚀  正式运行", "run", False),
            ("daemon", "🛡️  开始守护", "daemon", False),
        ]
        tips = {
            "dry": "试跑：整轮流程走一遍，但所有写操作（搜索/阅读/领取）都不真发请求，用来确认配置没问题。",
            "test": "测试一轮：每个接口/得分点各跑 1~2 次（搜索 1 次、阅读 1 篇、卡片最多 2 张），"
                    "逐项记录成功或失败的原因，并生成 JSON 报告。改完配置后先点这个。",
            "run": "正式运行：立刻按顺序把 8 个任务完整做一遍，十几分钟内做完就停。日常手动跑用这个。",
            "daemon": "开始守护：一直待命，只在电脑空闲（默认 5 分钟无键鼠操作）后才动手；"
                      "任务之间随机隔 5~12 分钟，整轮 2 小时内收尾；今日目标完成后直接睡到次日早上。\n"
                      "关掉主窗口守护也照常跑（缩在右下角托盘），关机重启后自动恢复。",
        }
        for key, text, mode, primary in specs:
            b = QPushButton(text)
            if primary:
                b.setObjectName("primary")
            b.setToolTip(tips.get(key, ""))
            b.clicked.connect(lambda _, m=mode: self._start(m))
            opl.addWidget(b)
            self.buttons[key] = b
        stop = QPushButton("🛑  停止")
        stop.setObjectName("danger")
        stop.setEnabled(False)
        stop.setVisible(False)              # 只在任务运行时才出现，平时不占位置
        stop.clicked.connect(self._stop)
        self.btn_stop = stop
        opl.addWidget(stop)
        opl.addStretch(1)
        lay.addWidget(opbox)
        # 任务开关（勾选/取消自动保存）
        tbox = Card("任务开关（点一下自动保存）")
        grid = QGridLayout(tbox)
        grid.setSpacing(4)
        for i, name in enumerate(TASK_ORDER):
            raw = tasks.TASKS[name]["label"] if name in tasks.TASKS else "Quiz 自动答题"
            label = re.sub(r"[（(][^（）()]*[)）]", "", raw).strip()   # 去掉括号说明，开关栏更清爽
            cb = QCheckBox(label)
            cb.setToolTip(f"{raw}　·　勾选/取消后立即写入 config.json")
            self.task_vars[name] = cb
            cb.toggled.connect(lambda on, n=name: self._autosave_task(n, on))
            grid.addWidget(cb, i // 3, i % 3)
        lay.addWidget(tbox)
        # 选项（日志相关按钮也放在这一行，紧跟在日志区上方）
        opt = QHBoxLayout()
        opt.setSpacing(10)
        clear = QPushButton("🧹 清空日志")
        clear.clicked.connect(self._clear_log)
        opt.addWidget(clear)
        logs = QPushButton("📂 日志目录")
        logs.setToolTip("界面日志同时写入 logs\\ 目录下的按天文件（logs\\YYYY-MM-DD.log）")
        logs.clicked.connect(lambda: self._open_dir(os.path.join(cli.BASE_DIR, "logs")))
        opt.addWidget(logs)
        opt.addSpacing(6)
        opt.addWidget(QLabel("保留"))
        self.v["log_keep_days"] = QSpinBox()
        self.v["log_keep_days"].setRange(3, 365)
        self.v["log_keep_days"].setValue(7)
        self.v["log_keep_days"].setSuffix(" 天")
        self.v["log_keep_days"].setFixedWidth(124)
        self.v["log_keep_days"].setMinimumHeight(30)
        self.v["log_keep_days"].setToolTip(
            "日志只保留最近 N 天（默认 7）：超过时先删最老的那份，再写当天的\n"
            "只删 logs\\YYYY-MM-DD.log 和 endpoint-test-*.json，其它文件不动")
        self.v["log_keep_days"].valueChanged.connect(
            lambda v: self._autosave({"log_keep_days": int(v)}))
        opt.addWidget(self.v["log_keep_days"])
        opt.addSpacing(10)
        self.var_verbose = QCheckBox("详细日志")
        self.var_verbose.setChecked(True)
        self.var_verbose.setToolTip("勾选后日志会记录每个请求的决策细节（原始请求明细只写文件，不进界面）；改动自动保存")
        self.var_verbose.toggled.connect(self._autosave_verbose)
        opt.addWidget(self.var_verbose)
        self.var_autoscroll = QCheckBox("自动滚动")
        self.var_autoscroll.setChecked(True)
        opt.addWidget(self.var_autoscroll)
        self.var_only_selected = QCheckBox("仅运行勾选的任务")
        self.var_only_selected.setToolTip("只跑上面任务开关里勾选的那些，其他任务本轮跳过（临时单跑用）")
        opt.addWidget(self.var_only_selected)
        opt.addStretch(1)
        lay.addLayout(opt)
        # 日志
        self.log_view = QPlainTextEdit()
        self.log_view.setObjectName("logView")
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(3000)
        self.log_view.setLineWrapMode(QPlainTextEdit.WidgetWidth)
        f = QFont("Consolas", 9)
        self.log_view.setFont(f)
        lay.addWidget(self.log_view, 1)
    # ------------------------------------------------------------ 参数页
    @staticmethod
    def _flabel(text: str) -> QLabel:
        """表单里可自动换行的说明标签：避免长说明把页面最小宽度撑爆、冒出横向滚动条。"""
        lab = QLabel(text)
        lab.setWordWrap(True)
        return lab
    @staticmethod
    def _wrap_check(text: str) -> tuple[QWidget, QCheckBox]:
        """可换行的勾选项：勾选框不带文字，说明文字放右侧自动折行（点文字同样能切换）。

        返回 (整行容器, 勾选框本身)——self.v 里存勾选框，容器只负责摆进表单。
        """
        wrap = QWidget()
        h = QHBoxLayout(wrap)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(6)
        cb = QCheckBox()
        lab = QLabel(text)
        lab.setWordWrap(True)

        def _toggle(ev):
            cb.toggle()
            ev.accept()
            return True
        lab.mousePressEvent = _toggle                # 点文字 = 点勾选框
        h.addWidget(cb, 0)
        h.addWidget(lab, 1)
        return wrap, cb
    @staticmethod
    def _form_grid(box: QGroupBox):
        """参数表单：两列网格。

        说明列（第 0 列）吃掉剩余宽度并自动换行，参数框固定宽度靠右，
        这样文字说明不会被参数框挤成两三行，页面也不会冒出横向滚动条。
        """
        holder = QWidget()
        holder.setMaximumWidth(900)               # 限制参数区总宽度：说明与参数框挨着，右侧留白不乱拉
        holder.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        hb = QHBoxLayout(box)                      # 不再显式置零边距：用 Card 自带的
        hb.addWidget(holder, 1)                   # 给 stretch，holder 才能长到 900 宽；超出部分自然留白
        grid = QGridLayout(holder)
        grid.setContentsMargins(0, 2, 0, 0)
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(9)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 0)
        # 参数列固定占位：两个分组框里的参数框左边缘必须落在同一条竖线上
        grid.setColumnMinimumWidth(1, FIELD_W)

        def add(label: QWidget, field: QWidget | None = None) -> None:
            r = grid.rowCount()
            if field is None:
                grid.addWidget(label, r, 0, 1, 2)      # 整行控件（勾选项说明）
            else:
                grid.addWidget(label, r, 0)            # 不给对齐方式：让说明占满整列，文字才不挤
                grid.addWidget(field, r, 1)            # 参数框统一宽度、统一左边缘
        return grid, add
    def _build_params_page(self):
        _, lay = self._page("⚙  运行参数", "调整搜索节奏与防封号策略。修改后点顶部「💾 保存配置」即可。")
        box = Card("搜索任务与每日目标")
        grid, add = self._form_grid(box)
        # 勾选项放最上面：这两条是「开关」，先决定做不做，再看具体数字
        wrap1, cb1 = self._wrap_check("搜索一直做到「今日目标」（每次核对真实配额，到达目标自动停）")
        cb1.setChecked(True)
        self.v["search_until_full"] = cb1
        add(wrap1)
        wrap2, cb2 = self._wrap_check(
            "每日目标随机且不满额（搜索/阅读每天随机一个目标，永远拿不满，更像真人）")
        cb2.setChecked(True)
        self.v["random_daily_target"] = cb2
        add(wrap2)
        for key, label, lo, hi, default in SEARCH_FIELDS:
            sb = QSpinBox()
            sb.setRange(lo, hi)
            sb.setValue(default)
            sb.setFixedWidth(FIELD_W)
            self.v[key] = sb
            add(self._flabel(label), sb)
        src = QComboBox()
        src.addItems(["bilibili", "hot", "offline"])
        src.setFixedWidth(FIELD_W)
        src.setToolTip("bilibili = B站今日热词（默认）\nhot = 第三方热搜 API\noffline = 程序内置词库")
        self.v["search_word_source"] = src
        add(self._flabel("搜索词来源（B站热词 / 热搜 / 内置）"), src)
        cnt = QSpinBox()
        cnt.setRange(5, 100)
        cnt.setValue(50)
        cnt.setFixedWidth(FIELD_W)
        self.v["search_hot_count"] = cnt
        add(self._flabel("热词抓取条数（B站最多 50）"), cnt)
        mode = QComboBox()
        mode.addItems(["http", "browser"])
        mode.setFixedWidth(FIELD_W)
        mode.setToolTip("http = 纯 HTTP 静默上报（推荐，不弹浏览器）\nbrowser = 用真实浏览器执行搜索")
        self.v["search_mode"] = mode
        add(self._flabel("搜索执行方式（静默 HTTP / 浏览器）"), mode)
        shost = QComboBox()
        shost.addItems(["cn.bing.com", "www.bing.com"])
        shost.setFixedWidth(FIELD_W)
        shost.setToolTip("必须用 cn.bing.com 上报才会计分（实测）；www.bing.com 会返回成功但永不加分")
        self.v["search_host"] = shost
        add(self._flabel("搜索上报域名（cn.bing.com 计分）"), shost)
        lay.addWidget(box)
        box2 = Card("防封号与调度")
        grid2, add2 = self._form_grid(box2)
        wrap3, cb3 = self._wrap_check("守护时只在电脑空闲后才执行（避免弹窗/占资源打扰使用）")
        cb3.setChecked(True)
        self.v["daemon_require_idle"] = cb3
        add2(wrap3)
        wrap3b, cb3b = self._wrap_check(
            "今日目标一完成就收工：不再检测空闲、不再跑任务，直接睡到次日东八区早上再开始"
            "（整晚不占资源、不弹任何窗口）")
        cb3b.setChecked(True)
        self.v["daemon_sleep_after_done"] = cb3b
        add2(wrap3b)
        wrap4, cb4 = self._wrap_check(
            "每轮开头先集中做「签到 + 1 次搜索 + 3 张卡片」（3 分钟内、纯 HTTP 静默，保连签）")
        cb4.setChecked(True)
        self.v["priority_lane"] = cb4
        add2(wrap4)
        wrap5d, cb5d = self._wrap_check(
            "守护模式只在确认电脑真没人用时才动用浏览器兜底"
            "（锁屏 / 屏保 / 无全屏且空闲超时判定；窗口自动最小化；"
            "没过判定就留给后续守护轮次自动补做，不用手动跑）")
        cb5d.setChecked(True)
        self.v["daemon_dom_when_away"] = cb5d
        add2(wrap5d)
        sb_away = QSpinBox()
        sb_away.setRange(3, 120)
        sb_away.setValue(10)
        sb_away.setFixedWidth(FIELD_W)
        sb_away.setToolTip("锁屏 / 屏保立即判定为无人；未锁屏时需要「无全屏窗口且连续空闲达到此分钟数」")
        self.v["daemon_dom_away_minutes"] = sb_away
        add2(self._flabel("判定真无人的空闲线（分钟，锁屏/屏保立即算）"), sb_away)
        for key, label, lo, hi, default in SCHEDULE_FIELDS:
            sb = QSpinBox()
            sb.setRange(lo, hi)
            sb.setValue(default)
            sb.setFixedWidth(FIELD_W)
            self.v[key] = sb
            add2(self._flabel(label), sb)
        lay.addWidget(box2)
        box3 = Card("环境与总开关")
        v3 = QVBoxLayout(box3)
        self.v["lock_region"] = QCheckBox("锁定国区（非大陆 IP 自动停止全部任务）")
        self.v["keep_running"] = QCheckBox("全部完成后仍继续定时检测")
        self.v["browser_dom"] = QCheckBox("个别卡片失败时用浏览器兜底（搜索/阅读/签到/大多数卡片已可纯 HTTP 完成）")
        self.v["browser_dom_minimized"] = QCheckBox(
            "浏览器兜底窗口自动最小化（手动运行时也不抢焦点）")
        self.cb_autostart = QCheckBox("开机自启动（开机后静默运行并恢复守护状态，直接缩到右下角托盘）")
        self.cb_autostart.setChecked(autostart_enabled())   # 注册表是唯一事实来源
        self.cb_autostart.toggled.connect(lambda on: self._set_autostart(on, "ui"))
        verbose2 = QCheckBox("详细日志（verbose，与控制台页联动）")
        verbose2.toggled.connect(self._autosave_verbose)
        self.verbose_boxes = [self.var_verbose, verbose2]
        for cb in (self.v["lock_region"], self.v["keep_running"], self.v["browser_dom"],
                   self.v["browser_dom_minimized"], self.cb_autostart, verbose2):
            v3.addWidget(cb)
        for cb in self.verbose_boxes:
            cb.toggled.connect(self._sync_verbose)
        row = QHBoxLayout()
        row.addWidget(QLabel("Bing 域名"))
        host = QComboBox()
        host.addItems(["www.bing.com", "cn.bing.com"])
        self.v["host"] = host
        row.addWidget(host)
        row.addStretch(1)
        v3.addLayout(row)
        lay.addWidget(box3)
        lay.addStretch(1)
    def _sync_verbose(self, value: bool):
        for cb in self.verbose_boxes:
            if cb.isChecked() != value:
                cb.setChecked(value)   # 值相同后不会再次递归
    # ------------------------------------------------------------ 账号页
    def _build_account_page(self):
        _, lay = self._page(
            "🔑  账号与授权",
            "推荐「一键登录」：程序打开独立浏览器窗口，你只管登录，Cookie 与授权码自动收回来并验证。"
            "所有凭据只写入本目录，不会上传。")

        # 登录状态显示栏：放在页面提示文字下面（不放进任何分组框里）
        self.auto_state = QLabel("⏸  正在检查登录状态…")
        self.auto_state.setWordWrap(True)
        self.auto_state.setStyleSheet(
            "background:#eff6ff;border:1px solid #bfdbfe;border-radius:7px;"
            "padding:9px;color:#1e40af;")
        lay.addWidget(self.auto_state)

        # ---------- 一键登录（自动采集） ----------
        auto = Card("一键登录　推荐 · 全自动，能拿到手动复制拿不到的 HttpOnly Cookie")
        al = QVBoxLayout(auto)
        intro = QLabel(
            "点按钮会打开一个<b>独立的 Edge 窗口</b>（专用配置，不干扰日常浏览器）。"
            "在那个窗口登录微软账号即可 —— <b>程序自动检测登录完成 → 读取完整 Cookie → "
            "调用接口验证积分 → 顺带完成 OAuth 授权</b>，全程不用复制粘贴。")
        intro.setWordWrap(True)
        intro.setObjectName("pageHint")
        al.addWidget(intro)
        row = QGridLayout()
        row.setSpacing(8)
        b_auto = QPushButton("🌐  一键登录 · 自动获取 Cookie + 授权")
        b_auto.setObjectName("primary")
        b_auto.setMinimumHeight(38)
        b_auto.clicked.connect(lambda: self._auto_login(refresh_only=False))
        row.addWidget(b_auto, 0, 0, 1, 3)         # 主按钮独占一行，4 个按钮挤一行会撑出横滑条
        self.login_buttons["auto"] = b_auto
        # 手动复制做成开关按钮：默认隐藏手动区，点一次展开/再点收起
        b_manual = QPushButton("📋  手动复制")
        b_manual.setMinimumHeight(38)
        b_manual.setCheckable(True)
        b_manual.setToolTip("备用方案：从自己已登录的浏览器里复制 Cookie 手动粘贴。默认收起，点这里展开。")
        row.addWidget(b_manual, 1, 0)
        b_refresh = QPushButton("🔄  刷新 Cookie（免登录）")
        b_refresh.setMinimumHeight(38)
        b_refresh.setToolTip("上次登录状态保存在 .browser-profile，直接读取最新 Cookie，不用再登录")
        b_refresh.clicked.connect(lambda: self._auto_login(refresh_only=True))
        row.addWidget(b_refresh, 1, 1)
        self.login_buttons["refresh"] = b_refresh
        b_stop = QPushButton("⏹  停止等待")
        b_stop.setObjectName("danger")
        b_stop.setEnabled(False)
        b_stop.setVisible(False)        # 没在等登录时不占位置，开始等了才出现
        b_stop.setToolTip("正在等你在浏览器里登录/授权时，点它立即放弃本次等待（不影响已保存的 Cookie）")
        b_stop.clicked.connect(self._stop_auto_login)
        row.addWidget(b_stop, 1, 2)
        self.login_buttons["stop"] = b_stop
        al.addLayout(row)
        wrap5, cb5 = self._wrap_check("同一次自动完成 OAuth 授权（App 签到 / 新闻阅读需要，建议勾选）")
        cb5.setChecked(True)
        self.var_auto_oauth = cb5
        al.addWidget(wrap5)
        self.cookie_state = QLabel("")
        self.cookie_state.setObjectName("pageHint")
        self.cookie_state.setWordWrap(True)       # 这行字较长，必须可换行，否则撑出横滑条
        al.addWidget(self.cookie_state)
        privacy = QLabel(
            "🔒 登录状态保存在本目录 .browser-profile；调试接口只监听 127.0.0.1，"
            "只读取 Cookie 与标签页地址，不读取密码 / 历史 / 书签。")
        privacy.setWordWrap(True)
        privacy.setObjectName("pageHint")
        al.addWidget(privacy)
        lay.addWidget(auto)
        # ---------- 手动复制（备用，默认收起） ----------
        manual = Card("手动复制　备用：自动方案不可用，或想用自己已登录的浏览器")
        ml = QVBoxLayout(manual)
        mrow = QHBoxLayout()
        mrow.setSpacing(8)
        b_guide = QPushButton("📖  打开登录页 + 查看图文步骤")
        b_guide.clicked.connect(self._manual_guide)
        mrow.addWidget(b_guide)
        b_paste = QPushButton("📋  从剪贴板粘贴")
        b_paste.clicked.connect(self._paste_from_clipboard)
        mrow.addWidget(b_paste)
        b_file = QPushButton("📄  选择 cookies.txt")
        b_file.clicked.connect(self._import_cookie_file)
        mrow.addWidget(b_file)
        mrow.addStretch(1)
        ml.addLayout(mrow)
        self.manual_hint = QLabel("支持：Cookie 请求头整串 / cookies.txt（Netscape）/ JSON 数组 / 一行一个 name=value")
        self.manual_hint.setObjectName("pageHint")
        self.manual_hint.setWordWrap(True)
        ml.addWidget(self.manual_hint)
        self.cookie_text = QPlainTextEdit()
        self.cookie_text.setFixedHeight(88)
        self.cookie_text.setPlaceholderText(
            "把 Network 面板里 Cookie: 后面那一整串粘进来（形如 _U=...; MUID=...; ...），或点上方「从剪贴板粘贴」…")
        ml.addWidget(self.cookie_text)
        srow = QHBoxLayout()
        b_save = QPushButton("💾  保存并验证登录")
        b_save.setObjectName("primary")
        b_save.clicked.connect(self._save_cookie_text)
        srow.addWidget(b_save)
        b_clear = QPushButton("🗑  清空 Cookie")
        b_clear.setObjectName("danger")
        b_clear.clicked.connect(self._clear_cookies)
        srow.addWidget(b_clear)
        srow.addStretch(1)
        ml.addLayout(srow)
        lay.addWidget(manual)
        manual.setVisible(False)                   # 默认收起，点「📋 手动复制」展开/收起
        b_manual.toggled.connect(manual.setVisible)
        # ---------- OAuth 授权 ----------
        ab = Card("OAuth 授权（可选：App 签到 / 新闻阅读需要）")
        oal = QVBoxLayout(ab)
        hint = QLabel("勾了上面的「同时自动完成 OAuth」就不用管这一节；这里留给手动 / 补授权。",
                      objectName="pageHint")
        hint.setWordWrap(True)
        oal.addWidget(hint)
        urlrow = QHBoxLayout()
        self.auth_url_edit = QLineEdit(AUTH_URL)
        self.auth_url_edit.setReadOnly(True)
        self.auth_url_edit.setFont(QFont("Consolas", 8))
        urlrow.addWidget(self.auth_url_edit, 1)
        cp = QPushButton("复制链接")
        cp.clicked.connect(self._copy_auth_url)
        urlrow.addWidget(cp)
        op = QPushButton("在登录窗口打开")
        op.clicked.connect(self._open_auth_in_browser)
        urlrow.addWidget(op)
        oal.addLayout(urlrow)
        crow = QHBoxLayout()
        b_capture = QPushButton("🔍  自动捕获授权码并换取 Token")
        b_capture.clicked.connect(self._capture_auth_code)
        crow.addWidget(b_capture)
        self.login_buttons["capture"] = b_capture
        crow.addWidget(QLabel("（登录窗口打开授权页后，程序从地址栏抓 code）", objectName="pageHint"), 1)
        oal.addLayout(crow)
        row2 = QHBoxLayout()
        row2.addWidget(QLabel("授权后 URL / code"))
        self.auth_code_var = QLineEdit()
        self.auth_code_var.setPlaceholderText("https://login.live.com/oauth20_desktop.srf?code=... 或直接贴 code")
        row2.addWidget(self.auth_code_var, 1)
        ba = QPushButton("换取并保存 Token")
        ba.clicked.connect(self._do_auth)
        row2.addWidget(ba)
        oal.addLayout(row2)
        row3 = QHBoxLayout()
        row3.addWidget(QLabel("refresh_token"))
        self.refresh_var = QLineEdit()
        self.refresh_var.setEchoMode(QLineEdit.Password)
        row3.addWidget(self.refresh_var, 1)
        bs = QPushButton("直接保存")
        bs.clicked.connect(self._save_refresh)
        row3.addWidget(bs)
        bc = QPushButton("清除 Token")
        bc.setObjectName("danger")
        bc.clicked.connect(self._clear_token)
        row3.addWidget(bc)
        oal.addLayout(row3)
        lay.addWidget(ab)
        # 勾选「同一次自动完成 OAuth」时，OAuth 手动授权区整块隐藏（反之显示）
        ab.setVisible(not self.var_auto_oauth.isChecked())
        self.var_auto_oauth.toggled.connect(lambda v: ab.setVisible(not v))
        lay.addStretch(1)
    # ------------------------------------------------------------ 通知页
    def _build_notify_page(self):
        _, lay = self._page("🔔  通知推送", "渠道留空即不发送；也可以直接填完整 Webhook URL（以 http 开头）。每条消息都能在「通知配置」里单独开关。")
        box = Card("推送渠道")
        form = QFormLayout(box)
        form.setSpacing(10)
        for key, label in NOTIFY_FIELDS:
            e = QLineEdit()
            e.setEchoMode(QLineEdit.Password)
            e.setPlaceholderText("留空 = 不启用；填 key 或整个 Webhook URL 都行，保存时自动提取 key")
            self.notify_vars[key] = e
            form.addRow(label, e)
        win_cb = QCheckBox("Windows 系统通知（本电脑右下角弹窗，无需 key）")
        win_cb.setToolTip("用系统自带的通知弹窗推送，和上面的 Webhook 互不影响；\n"
                          "如果系统设置里关了「通知」，弹窗可能不显示")
        win_cb.setChecked(True)
        win_cb.toggled.connect(lambda on: self._autosave({"notify": {"win": bool(on)}}))
        self.win_notify_var = win_cb
        form.addRow("", win_cb)
        test = QPushButton("📢  发送测试通知")
        test.setObjectName("primary")
        test.clicked.connect(lambda: self._start("notify"))
        form.addRow("", test)
        lay.addWidget(box)
        # 通知配置：每一条能推送的消息一个开关，勾选即自动保存
        box2 = Card("通知配置")
        v2 = QVBoxLayout(box2)
        v2.setSpacing(6)
        tip_lab = QLabel("任务成果（成功与失败都算这一条）")
        tip_lab.setObjectName("statTitle")
        v2.addWidget(tip_lab)
        for key, label, tip in NOTIFY_SWITCH_RESULT:
            cb = QCheckBox(label)
            cb.setToolTip(tip)
            cb.setChecked(True)
            cb.toggled.connect(lambda on, k=key: self._autosave({"notify": {"categories": {k: bool(on)}}}))
            self.notify_cat_vars[key] = cb
            v2.addWidget(cb)
        tip_lab2 = QLabel("系统与异常")
        tip_lab2.setObjectName("statTitle")
        tip_lab2.setContentsMargins(0, 8, 0, 0)
        v2.addWidget(tip_lab2)
        for key, label, tip in NOTIFY_SWITCH_SYSTEM:
            cb = QCheckBox(label)
            cb.setToolTip(tip)
            cb.setChecked(True)
            cb.toggled.connect(lambda on, k=key: self._autosave({"notify": {"categories": {k: bool(on)}}}))
            self.notify_cat_vars[key] = cb
            v2.addWidget(cb)
        tip = QLabel("勾选即自动保存；「发送测试通知」不受这里的开关影响")
        tip.setObjectName("pageHint")
        tip.setContentsMargins(0, 8, 0, 0)
        v2.addWidget(tip)
        lay.addWidget(box2)
        lay.addStretch(1)
    # ------------------------------------------------------------ 关于页
    def _build_about_page(self):
        _, lay = self._page("📦  关于", "项目来源、运行环境与数据存放位置。")
        box = Card("项目信息")
        fl = QFormLayout(box)
        fl.setSpacing(10)
        try:
            from PyQt5.QtCore import PYQT_VERSION_STR, QT_VERSION_STR
            qt_ver = f"PyQt5 {PYQT_VERSION_STR} / Qt {QT_VERSION_STR}"
        except Exception:
            qt_ver = "未知"
        info = [
            ("原脚本", "ScriptCat #6241 · Microsoft Rewards 助手 v3.0.2"),
            ("原作者", "liyan20001124-byte（MIT License，署名已保留）"),
            ("运行环境", f"Python {sys.version.split()[0]} · {qt_ver}"),
            ("解释器", sys.executable),
            ("项目目录", cli.BASE_DIR),
            ("配置文件", "config.json / cookies.txt / auth.json / state.json / logs/"),
        ]
        for k, v in info:
            lab = QLabel(v)
            lab.setTextInteractionFlags(Qt.TextSelectableByMouse)
            lab.setWordWrap(True)
            fl.addRow(k, lab)
        lay.addWidget(box)

        box2 = Card("快捷方式与目录")
        hb = QHBoxLayout(box2)
        b_link = QPushButton("🖥  在桌面创建快捷方式")
        b_link.setObjectName("primary")
        b_link.setToolTip("在当前用户桌面生成一条指向本程序的快捷方式；"
                          "打包版指向 exe，源码版指向 pythonw + gui_qt.py（不会弹黑框）")
        b_link.clicked.connect(self._create_desktop_shortcut)
        b_dir = QPushButton("📂  打开程序所在目录")
        b_dir.clicked.connect(lambda: self._open_dir(cli.BASE_DIR))
        b_data = QPushButton("📄  打开日志目录")
        b_data.clicked.connect(lambda: self._open_dir(os.path.join(cli.BASE_DIR, "logs")))
        for b in (b_link, b_dir, b_data):
            hb.addWidget(b)
        hb.addStretch(1)
        tip = QLabel("换电脑 / 备份：把整个程序目录拷走即可；移动后若开机自启没生效，"
                     "在这里关掉再打开一次「开机自启」即可重新指向。")
        tip.setStyleSheet("color:#94a3b8; font-size:12px;")
        tip.setWordWrap(True)
        vb = QVBoxLayout()
        vb.addLayout(hb)
        vb.addWidget(tip)
        box2.setLayout(vb)
        lay.addWidget(box2)
        lay.addStretch(1)

    def _create_desktop_shortcut(self):
        """在当前用户桌面建一条指向本程序的快捷方式（打包后指向 exe 自身）。"""
        try:
            import make_shortcut as ms
        except Exception as e:
            self._sys(f"生成快捷方式缺少 make_shortcut 模块：{type(e).__name__}")
            return
        out = os.path.join(os.path.expanduser("~"), "Desktop", "Microsoft Rewards 控制台.lnk")
        if os.path.exists(out):
            r = QMessageBox.question(self, "快捷方式已存在",
                                     "桌面已有同名快捷方式，覆盖它吗？",
                                     QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if r != QMessageBox.Yes:
                return
        try:
            if getattr(sys, "frozen", False):
                target = sys.executable
                data = ms.build(target=target, args="",
                                workdir=os.path.dirname(os.path.abspath(target)),
                                icon=target, name="Microsoft Rewards 自动任务控制台")
            else:
                py = sys.executable
                if py.lower().endswith("\\python.exe"):
                    pw = py[:-10] + "pythonw.exe"
                    if os.path.isfile(pw):
                        py = pw
                script = os.path.abspath(__file__)
                data = ms.build(target=py, args=f'"{script}"',
                                workdir=os.path.dirname(script),
                                icon=ICO_PATH, name="Microsoft Rewards 自动任务控制台")
            os.makedirs(os.path.dirname(out), exist_ok=True)
            with open(out, "wb") as f:
                f.write(data)
        except Exception as e:
            self._sys(f"快捷方式创建失败：{type(e).__name__}: {e}")
            return
        self._sys(f"✅ 已在桌面创建快捷方式：{out}")
        self.statusBar().showMessage("✅ 桌面快捷方式已创建", 5000)
    # ------------------------------------------------------------ 信号
    def _connect_signals(self):
        self.signals.log.connect(self._append_log)
        self.signals.status.connect(self._on_status)
        self.signals.cookie_state.connect(self.cookie_state.setText)
        self.signals.browser.connect(self._on_browser_stage)
        self.signals.browser_done.connect(self._on_browser_done)
        self.signals.done.connect(self._on_done)
        self.signals.rest.connect(self._on_daemon_rest)
        self.signals.error.connect(lambda m: self._append_log("ERROR", "🔴", m))
        self.signals.check_done.connect(self._on_check_done)
    def _sys(self, msg: str):
        self.signals.log.emit("SYS", "ℹ", msg)
    def _append_log(self, level: str, icon: str, msg: str):
        # HTTP 请求明细（🌐 GET/POST…）只写文件日志，不进界面——界面只显示人类可读的内容。
        # 需要看原始请求时打开 logs\YYYY-MM-DD.log。
        if icon == "🌐":
            return
        tag = "INFO" if level == "INFO" else level
        if icon in ("🔴", "❌"):
            tag = "ERROR"
        elif icon in ("🟡", "⚠️", "⏭️"):
            tag = "WARN"
        elif icon in ("🟢", "✅"):
            tag = "OK"
        color = LEVEL_COLOR.get(tag, LEVEL_COLOR["INFO"])
        pad = level.ljust(5).replace(" ", "&nbsp;")
        line = (f'<span style="color:#94a3b8">[{time.strftime("%H:%M:%S")}]</span> '
                f'<span style="color:{color}">[{pad}] {html.escape(icon)} {html.escape(msg)}</span>')
        self.log_view.appendHtml(line)
        if self.var_autoscroll.isChecked():
            sb = self.log_view.verticalScrollBar()
            sb.setValue(sb.maximum())
    def _on_status(self, status: dict):
        for k, v in status.items():
            if k == "progress_pct":                # 今日任务进度条
                try:
                    self.progress_bar.setValue(max(0, min(100, int(v))))
                except (TypeError, ValueError):
                    pass
            elif k in self.status_labels:
                self.status_labels[k].setText(v)
    def _on_check_done(self, key: str = ""):
        """某一次刷新结束：让对应的圆环停止旋转；两个都停了就重新开始倒计时。"""
        btn = self.buttons.get(key or "refresh")
        if btn:
            btn.set_spinning(False)
        busy = any(getattr(self.buttons.get(k), "_spinning", False)
                   for k in ("refresh", "today_refresh"))
        if not busy:
            self._reset_countdown()
    def _apply_rest_ui(self, reason: str = ""):
        """守护处于等待状态时的界面：刷新可用，但不能再起第二个 worker。

        reason: sleep=已收工等次日 / idle=等电脑空闲 / gap=任务间隙 / round=等下一轮
        """
        hints = {
            "sleep": "已收工，等次日自动开始",
            "idle": "等你离开电脑，空闲后才动手",
            "gap": "任务间隙，稍后继续",
            "round": "本轮结束，等下一轮",
        }
        self.running = False
        for k, b in self.buttons.items():
            b.setEnabled(k not in ("dry", "test", "run", "daemon"))
        self.btn_stop.setVisible(True)
        self.btn_stop.setEnabled(True)
        self.statusBar().showMessage(
            f"🌙 守护待命：{hints.get(reason, '等待中')}（可刷新、可停止）")
    def _on_daemon_rest(self, resting: bool, reason: str = ""):
        """后台线程报告「进入/退出等待」。等待期间界面恢复可用，但绝不自动刷新。"""
        self._daemon_resting = bool(resting)
        if resting:
            self._apply_rest_ui(reason)
        else:
            self.running = True
            self._set_buttons(False)
    def _on_done(self):
        mode = getattr(self, "_worker_mode", "")
        self._worker_mode = ""
        for key in ("refresh", "today_refresh"):   # 圆环停转并重新倒计时
            b = self.buttons.get(key)
            if b:
                b.set_spinning(False)
        self._reset_countdown()
        if mode in ("check", "today", "notify") and self._daemon_wanted:
            # 守护还在后台（正在干活或待命），这次只是手动刷新结束：别碰守护状态
            if self._daemon_resting:
                self._apply_rest_ui("")
            else:
                self.running = True
                self._set_buttons(False)
            return
        self.running = False
        self._daemon_resting = False
        self._set_buttons(True)
        if self._daemon_wanted:                    # 守护被停止/异常退出 → 状态复位并落盘
            self._daemon_wanted = False
            self._autosave({"daemon_resume": False})
            self._update_tray_actions()
        self._sys("任务结束，界面已恢复")

    # ------------------------------------------------------------ 托盘 / 自启动 / 守护恢复
    def _init_tray(self):
        """右下角托盘图标：关窗口不退出，右键菜单可开始/停止守护、开关自启动、退出。"""
        self.tray = QSystemTrayIcon(QIcon(ICO_PATH) if os.path.isfile(ICO_PATH) else QIcon(), self)
        self.tray.setToolTip("Microsoft Rewards 自动任务")
        menu = QMenu()
        # 圆角菜单需要半透明窗口，否则圆角外侧会露出白方块
        menu.setAttribute(Qt.WA_TranslucentBackground, True)
        act_open = menu.addAction("🪟  打开主窗口")
        act_open.triggered.connect(self._show_from_tray)
        menu.addSeparator()
        self.act_daemon = menu.addAction("🌙  开始守护")
        self.act_daemon.triggered.connect(self._tray_toggle_daemon)
        self.act_auto = menu.addAction("⚡  开机自启动")
        self.act_auto.setCheckable(True)
        menu.addSeparator()
        act_quit = menu.addAction("⏻  退出")
        act_quit.triggered.connect(self._quit_from_tray)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._tray_activated)
        self._set_autostart(autostart_enabled(), None)     # 初始状态读注册表，不触发写操作
        if autostart_enabled():                            # 打包版搬家后自动修好指向
            note = autostart_self_heal()
            if note:
                self._sys(note)
        self.tray.show()

    def _set_autostart(self, on: bool, source):
        """开机自启动开关：写注册表并同步「设置页勾选框」和「托盘菜单勾选项」。

        :param source: 触发来源（哪个控件动了一下）；None=只读初始化，不写注册表。
        """
        cmd = ""
        if source is not None:
            try:
                cmd = autostart_set(bool(on))
            except Exception as e:
                self._sys(f"开机自启动设置失败：{type(e).__name__}: {e}")
                on = autostart_enabled()
        for w in (getattr(self, "act_auto", None), getattr(self, "cb_autostart", None)):
            if w is not None:
                w.blockSignals(True)
                w.setChecked(bool(on))
                w.blockSignals(False)
        if cmd:
            self._sys(("已开启开机自启动" if on else "已关闭开机自启动")
                      + f"（{'pythonw 静默启动，开机后直接缩到托盘' if on else ''}）")
            self.statusBar().showMessage("✅ 开机自启动已更新  " + time.strftime('%H:%M:%S'), 5000)

    def _update_tray_actions(self):
        act = getattr(self, "act_daemon", None)
        if act:
            act.setText("⏹  停止守护" if self._daemon_wanted else "🌙  开始守护")
        if getattr(self, "tray", None):
            self.tray.setToolTip("Microsoft Rewards 自动任务"
                                 + ("（守护运行中）" if self._daemon_wanted else ""))

    def _show_from_tray(self):
        self.show()
        self.setWindowState(self.windowState() & ~Qt.WindowMinimized)
        self.raise_()
        self.activateWindow()

    def _tray_activated(self, reason):
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            self._show_from_tray()

    def _tray_toggle_daemon(self):
        if self.running:
            if self._daemon_wanted:
                self._stop()
            else:
                QMessageBox.warning(None, "正在运行", "已有任务在运行，请先等待或点「停止」")
            return
        self._start("daemon")

    def _quit_from_tray(self):
        """托盘「退出」：停守护、清恢复标记、真正退出程序。"""
        self._really_quit = True
        self.app_stop.set()
        self.stop_event.set()
        self._autosave({"daemon_resume": False})
        self.close()
        QTimer.singleShot(200, QApplication.quit)

    def _resume_daemon(self):
        """启动后检查：上次退出/断电时守护开着 → 自动恢复。"""
        if self._daemon_wanted or self.running:
            return
        if not bool(self.cfg.get("daemon_resume")):
            return
        self._sys("检测到上次退出时守护模式开着，自动恢复守护（托盘图标可随时停止）")
        # 启动阶段保持静默：不再弹托盘气泡，只在日志里说明
        self._start("daemon")
    def _clear_log(self):
        self.log_view.clear()
        self._sys("日志已清空（磁盘日志文件不受影响）")
    # ------------------------------------------------------------ 配置读写
    def _on_nav_change(self, idx: int):
        """切换页面：窗口标题跟随页面名；切换前检查有没有改了没保存的配置。

        保存 = 写入 config.json 后切换；取消 = 放弃本次修改（恢复为已保存的值）再切换，
        这样切到别的页面也不会再被反复询问。
        """
        if 0 <= idx < len(PAGE_NAMES):
            self.setWindowTitle(f"Microsoft Rewards 自动任务 · {PAGE_NAMES[idx]}")
        if idx == self.stack.currentIndex():
            self.stack.setCurrentIndex(idx)
            return
        if self._config_dirty():
            btn = QMessageBox.question(self, "配置未保存", "配置未保存，是否保存？",
                                       QMessageBox.Save | QMessageBox.Cancel)
            if btn == QMessageBox.Save:
                self._save_all(True)
            else:
                self._revert_config()
        self.stack.setCurrentIndex(idx)
    def _config_dirty(self) -> bool:
        try:
            return self._saved_cfg != self._collect()
        except Exception:
            return False
    def _update_dirty_ui(self):
        """有未保存的修改才显示「保存/撤销」按钮，没改过就藏起来，顶栏更干净。"""
        dirty = self._config_dirty()
        self.btn_save.setVisible(dirty)
        self.btn_undo.setVisible(dirty)
    def _revert_config(self):
        """撤销配置修改：重新从 config.json 读一遍，恢复到上一次保存的值。"""
        self.cfg = cli.ensure_config()
        self._load_into_ui(self.cfg)
        self._saved_cfg = self._collect()
        self.statusBar().showMessage(f"↩ 已撤销未保存的修改，恢复为上次保存的值  {time.strftime('%H:%M:%S')}", 5000)
        self._sys("配置修改已撤销（恢复为 config.json 里保存的值）")
        self._update_dirty_ui()

    def _load_into_ui(self, cfg: dict):
        self._ui_loading = True                    # 这一刻的赋值不算「用户改动」，别触发自动保存
        try:
            for name in TASK_ORDER:
                self.task_vars[name].setChecked(bool(cfg.get("tasks", {}).get(name, True)))
            for key, w in self.v.items():
                val = cfg.get(key)
                if val is None:
                    continue
                if isinstance(w, QSpinBox):
                    try:
                        w.setValue(int(val))
                    except (ValueError, TypeError):
                        pass
                elif isinstance(w, QComboBox):
                    i = w.findText(str(val))
                    if i >= 0:
                        w.setCurrentIndex(i)
                elif isinstance(w, QCheckBox):
                    w.setChecked(bool(val))
            for key, e in self.notify_vars.items():
                e.setText(cfg.get("notify", {}).get(key, "") or "")
            cats = (cfg.get("notify") or {}).get("categories") or {}
            for key, cb in self.notify_cat_vars.items():
                cb.setChecked(bool(cats.get(key, True)))   # 缺省全开
            if self.win_notify_var is not None:
                self.win_notify_var.setChecked(bool((cfg.get("notify") or {}).get("win", True)))
            for cb in self.verbose_boxes:          # 两处勾选框显式同步，避免 Qt 同值不触发信号
                cb.setChecked(bool(cfg.get("verbose", True)))
            auth = cli._load(cli.AUTH_PATH, cli.DEFAULT_AUTH)
            self.refresh_var.setText(auth.get("refresh_token", "") or "")
        finally:
            self._ui_loading = False
    def _collect(self) -> dict:
        cfg = dict(self.cfg)
        cfg["tasks"] = {n: cb.isChecked() for n, cb in self.task_vars.items()}
        for key, w in self.v.items():
            if isinstance(w, QSpinBox):
                cfg[key] = w.value()
            elif isinstance(w, QComboBox):
                cfg[key] = w.currentText()
            elif isinstance(w, QCheckBox):
                cfg[key] = w.isChecked()
        # 通知配置归一：贴完整 Webhook URL 也能存，保存时自动提取出纯 key；
        # 类别开关一并收集（通知推送页的「通知配置」卡）
        cfg["notify"] = {k: notify.extract_key(k, e.text()) for k, e in self.notify_vars.items()}
        cfg["notify"]["categories"] = {k: cb.isChecked() for k, cb in self.notify_cat_vars.items()}
        if self.win_notify_var is not None:
            cfg["notify"]["win"] = self.win_notify_var.isChecked()
        cfg["verbose"] = self.var_verbose.isChecked()
        return cfg
    def _autosave(self, patch: dict):
        """把个别改动立刻写进 config.json（任务开关、详细日志这类一点就该生效的项）。

        只更新传入的键，不碰界面上其他还没保存的修改；同时刷新「已保存」基准，
        免得切页时被误判成有未保存改动。
        """
        if getattr(self, "_ui_loading", False):
            return
        try:
            disk = cli._load(cli.CONFIG_PATH, cli.DEFAULT_CONFIG)
            _deep_merge(disk, patch)
            _deep_merge(self.cfg, patch)
            if hasattr(self, "_saved_cfg"):
                _deep_merge(self._saved_cfg, patch)
            cli._save(cli.CONFIG_PATH, disk)
            self.statusBar().showMessage(f"✅ 已自动保存  {time.strftime('%H:%M:%S')}", 5000)
        except Exception as e:
            self._sys(f"自动保存失败：{type(e).__name__}: {e}")

    def _autosave_task(self, name: str, on: bool):
        self._autosave({"tasks": {name: bool(on)}})

    def _autosave_verbose(self, on: bool):
        self._autosave({"verbose": bool(on)})

    def _save_all(self, silent: bool = True) -> bool:
        try:
            cfg = self._collect()
            cli._save(cli.CONFIG_PATH, cfg)
            self.cfg = cfg
            self._saved_cfg = dict(cfg)            # 新基准，撤销修改以此为准
            if not silent:
                QMessageBox.information(self, "已保存", "配置已写入 config.json")
            self.statusBar().showMessage(f"✅ 配置已保存  {time.strftime('%H:%M:%S')}", 5000)
            self._update_dirty_ui()
            return True
        except Exception as e:
            QMessageBox.critical(self, "保存失败", str(e))
            return False
    # ------------------------------------------------------------ 运行控制
    def _set_buttons(self, enabled: bool):
        for b in self.buttons.values():
            b.setEnabled(enabled)
        self.btn_stop.setVisible(not enabled)       # 只在运行中露出来，空闲时整行更干净
        self.btn_stop.setEnabled(not enabled)
        self.statusBar().showMessage("空闲" if enabled else "⏳ 任务运行中…  可点「停止」中断等待")
    def _start(self, mode: str):
        if self._daemon_resting and mode in ("dry", "test", "run", "daemon"):
            QMessageBox.information(
                self, "守护待命中",
                "守护进程还在后台待命（已收工，等下次自动开始）。\n"
                "要手动跑任务，请先点「停止」结束守护。")
            return
        if self.running:
            QMessageBox.warning(self, "正在运行", "已有任务在运行，请先等待或点「停止」")
            return
        if not self._save_all(True):
            return
        self._worker_mode = mode
        self.running = True
        self.stop_event.clear()
        self._set_buttons(False)
        if mode == "check":
            self.buttons["refresh"].set_spinning(True)        # 体检中 ⟳① 自转
        elif mode == "today":
            self.buttons["today_refresh"].set_spinning(True)  # 今日数据刷新中 ⟳② 自转
        if mode == "daemon":                       # 守护状态要断电可恢复，立刻落盘
            self._daemon_wanted = True
            self._autosave({"daemon_resume": True})
            self._update_tray_actions()
        self._sys(f"启动操作: {mode}")
        threading.Thread(target=self._worker, args=(mode,), daemon=True).start()
    def _stop(self):
        self.stop_event.set()
        self._sys("⏹ 已请求停止（当前等待会立即中断，正在进行的请求完成后退出）")
    def _make_api(self, dry_run: bool = False):
        api = cli.make_api(dry_run=dry_run, verbose=self.var_verbose.isChecked())
        api.log_hook = lambda level, icon, msg: self.signals.log.emit(level, icon, msg)
        api.stop_event = self.stop_event
        return api
    def _worker(self, mode: str):
        try:
            if mode == "notify":
                api = self._make_api(dry_run=True)
                try:
                    api.notifier.send("🧪 测试消息：看到这条说明通知配置成功", force=True)
                finally:
                    api.close()
                self.signals.log.emit("OK", "🟢", "测试通知已发送")
            elif mode == "check":
                self._do_check()
            elif mode == "today":
                self._do_today_check()
            elif mode == "daemon":
                self._do_daemon()
            elif mode == "test":
                self._do_test()
            else:
                self._do_run(mode == "dry")
        except Exception as e:
            self.signals.error.emit(f"运行异常: {type(e).__name__}: {e}\n{traceback.format_exc()}")
        finally:
            self.signals.done.emit()
    def _only(self):
        if not self.var_only_selected.isChecked():
            return None
        return [n for n in tasks.TASKS if n in self.task_vars and self.task_vars[n].isChecked()]
    def _do_run(self, dry: bool):
        api = self._make_api(dry_run=dry)
        try:
            tasks.run_all(api, self._only())
            api.http.save_cookies(cli.COOKIE_PATH)
        finally:
            api.close()                        # 跑完立刻断开，别把会话挂在服务器上
        self._do_today_check(True)             # 跑完顺手把「今日任务」行和进度条同步一遍
    def _do_test(self):
        """测试一轮：每个接口只跑 1~2 次，逐项记录成败与原因。"""
        api = self._make_api(dry_run=False)
        try:
            rep = endpoint_test.run(api)
            self.signals.status.emit({"points": str(rep.get("balance_after") or "—")})
        finally:
            api.http.save_cookies(cli.COOKIE_PATH)
            api.close()                        # 测试完断开
            self._do_today_check(True)         # 测试会真实计分，结束后同步今日数据
    def _do_daemon(self):
        """守护：只在电脑空闲时动手，任务之间随机拉开；今日目标一完成就睡到次日起床时间。"""
        interval = max(60, int(self.cfg.get("daemon_interval_minutes", 60)) * 60)
        jitter = max(0, int(self.cfg.get("daemon_interval_jitter_minutes", 30)) * 60)
        lo = int(self.cfg.get("daemon_start_delay_min_s", 5))
        hi = int(self.cfg.get("daemon_start_delay_max_s", 95))
        first = random.randint(lo, max(lo, hi))
        idle_need = int(self.cfg.get("daemon_idle_seconds", 300) or 0)
        gap_lo = int(self.cfg.get("daemon_task_gap_min_s", 300))
        gap_hi = int(self.cfg.get("daemon_task_gap_max_s", 720))
        cap = int(self.cfg.get("daemon_round_max_minutes", 120) or 0)
        self._sys(f"守护模式：{first} 秒后开始等空闲；电脑空闲 {idle_need} 秒才动手，"
                  f"任务间隔 {gap_lo // 60}~{gap_hi // 60} 分钟，"
                  f"整轮上限 {cap} 分钟，两轮间隔约 {interval // 60}±{jitter // 60} 分钟")
        if self.cfg.get("daemon_sleep_after_done", True):
            wh = int(self.cfg.get("daemon_wake_hour", 7) or 7)
            ww = int(self.cfg.get("daemon_wake_window_minutes", 90) or 0)
            self._sys(f"今日目标一完成就收工：直接睡到次日 {wh:02d}:00~{wh:02d}:{ww:02d}"
                      f"（东八区）之间再开始，期间不检测空闲")
        if self.stop_event.wait(first):
            return
        n = 0
        while not self.stop_event.is_set():
            n += 1
            self.signals.rest.emit(False, "")   # 新一轮开始：界面切回「运行中」
            self._sys(f"===== 守护第 {n} 轮 =====")
            api = self._make_api(dry_run=False)
            # 等空闲 / 任务间隙这些长等待里，把界面交还给用户（可刷新、可停止）
            api.wait_hook = lambda kind: self.signals.rest.emit(bool(kind), kind)
            done_today = False
            slept = False
            try:
                tasks.run_all(api, self._only(), pace=True)
                api.http.save_cookies(cli.COOKIE_PATH)
                done_today = bool(getattr(api, "goals_done_today", False))
                if done_today and not self.stop_event.is_set():
                    # 今日目标已完成：不检测空闲、不跑任务，一直睡到明天东八区起床时间
                    self.signals.rest.emit(True, "sleep")   # 长睡前先放开界面，不然整夜锁死
                    slept = tasks.sleep_until_wake(api)
            finally:
                api.close()                    # 每轮结束断开，下一轮再重新连
            self._do_today_check(True)         # 每轮结束同步「今日任务」行（守护一跑几小时，别让它一直旧着）
            if self.stop_event.is_set():
                break
            if slept:
                continue                       # 睡醒了：新的一天，直接进下一轮
            wait = max(60, interval + (random.randint(-jitter, jitter) if jitter else 0))
            self.signals.rest.emit(True, "round")   # 两轮之间的等待：界面同样放开
            self._sys(f"第 {n} 轮结束，{wait // 60} 分钟后进入下一轮（同样要等电脑空闲）")
            if self.stop_event.wait(wait):
                break
    # ---------- 网络闸门：开机后可能很久才联网，所以一律「一直等」 ----------
    def _net_gate(self, quiet: bool = False, poll_s: float = 15.0, on_wait=None) -> bool:
        """等网络就绪再继续；只要不是退出程序就一直等下去。

        开机自启动时 Windows 的网络栈可能比程序晚几分钟，更常见的是用户开机就去
        忙别的（洗脸、看视频），半小时后才连上 Wi-Fi。所以这里**不设超时**：
        每 15 秒轻量探一次（探测超时 3 秒），通了立刻继续，日志 5 分钟才报一句。
        返回 False 只表示「用户退出了程序」。
        """
        if netguard.ready():
            return True
        if on_wait is not None:
            try:
                on_wait()
            except Exception:
                pass
        return netguard.wait_ready(
            poll_s=poll_s,
            max_wait_s=0,                                  # 一直等
            should_stop=self.app_stop.is_set,              # 只有退出程序才放弃
            log=(None if quiet else (lambda icon, text: self._sys(text))))

    def _do_check(self, quiet: bool = False):
        """刷新①（体检）：只刷第一行状态条 —— Cookie / OAuth / 地区 / 当前积分。"""
        api = self._make_api(dry_run=True)
        status = {}
        try:
            if not quiet:
                self._sys("开始体检（只读，不改任何积分）")
            # 开机自启动时网络常常还没通：一直等到通为止，别把「地区/积分读取失败」写进状态
            if not self._net_gate(quiet, on_wait=lambda: self.signals.status.emit(
                    {"region": "⏳ 等待网络", "points": "⏳ 等待网络"})):
                return                                   # 只有退出程序才会走到这里
            status["cookie"] = "✅ 已配置" if os.path.isfile(cli.COOKIE_PATH) else "❌ 未配置"
            rt = (api.auth.get("refresh_token") or "").strip()
            status["token"] = "✅ 已授权" if rt else "⚠️ 未授权"
            ok = api.check_region()
            status["region"] = "✅ 中国大陆" if ok else "❌ 非中国大陆 / 读取失败"
            try:
                api.ensure_token()          # 读积分走 DAPI；Token 有效就复用，不每次都换票
            except Exception:
                pass
            bal = api.get_balance() or api.balance_from_dashboard()
            if bal:
                status["points"] = str(bal)
                if not quiet:
                    self._sys(f"Cookie 登录有效，当前积分 {bal}")
            else:
                status["points"] = "读取失败"
                if not quiet:
                    self._sys("🔴 读不到积分（Cookie 可能失效或未登录）")
            if not quiet:
                self._sys("体检完成 ✅")
            self.signals.status.emit(status)
        finally:
            api.close()                     # 体检完断开，不留会话

    def _do_today_check(self, quiet: bool = False):
        """刷新②：只刷「今日任务」这一行 + 进度条。

        数字全部以服务器为准，来源都是 DAPI options=613（10 秒缓存，一次刷新
        最多打一个请求）：
          * 今日积分 → todays_points（服务器自己算的今天得分，不是本地差值）
          * 搜索配额 → WW_search_global_* 的 progress/max
          * 阅读配额 → 阅读任务进度换算成分数展示（服务器给篇数，每篇 3 分）
          * 待领卡片 → 卡片扫描
          * 拼图进度 → hva_puzzle_pieces_completed_amount（集满 12 块给 1000 分）
          * 兑换目标 → goal_item.price 与我当前积分比
        """
        api = self._make_api(dry_run=True)
        if not quiet:
            self._sys("刷新今日任务数据（只读）")
        status: dict = {}
        try:
            if not self._net_gate(quiet):
                return
            today = int(time.strftime("%Y%m%d"))
            try:
                api.ensure_token()          # 今日数据都走 DAPI，需要 Token（有效就复用）
            except Exception:
                pass
            stats = api.get_today_stats()
            info = api.get_rewards_info()
            cur = mx = 0
            search_goal = None
            if info:
                cur, mx = int(info["pc"]["progress"]), int(info["pc"]["max"])
                try:
                    search_goal = api.daily_target("search", mx)
                except Exception:
                    pass
                status["quota"] = f"{cur}/{mx}" + (f"（目标 {search_goal}）" if search_goal else "")
            else:
                status["quota"] = "读取失败"

            # 今日积分：直接读服务器给的 todays_points
            tp = int(stats.get("today_points", -1))
            status["today_earned"] = f"+{tp}" if tp >= 0 else "读取失败"

            # 阅读配额：服务器给的是篇数，每篇固定 3 分，界面统一换算成「分」，
            # 括号里给今日目标（也是分，和搜索一个口径）。
            ra = int(stats.get("read_articles", -1))
            ram = int(stats.get("read_articles_max", -1))
            read_goal = None
            if ra >= 0 and ram > 0:
                per = READ_POINTS_PER_ARTICLE
                try:
                    read_goal = api.daily_target("read", ram * per)     # 分数目标
                except Exception:
                    read_goal = None
                status["read"] = f"{ra * per}/{ram * per}" + (
                    f"（目标 {read_goal}）" if read_goal else "")
            else:
                status["read"] = "读取失败"

            cards = api.discover_cards()
            if cards:
                pts = sum(int(c.get("points") or 0) for c in cards)
                status["cards"] = f"{len(cards)} 张 / {pts} 分"
            else:
                status["cards"] = "✅ 无待领"

            # 拼图进度：集满 12 块给 1000 分
            pz = int(stats.get("puzzle", -1))
            pz_max = int(stats.get("puzzle_max") or PUZZLE_MAX)
            status["puzzle"] = f"{pz}/{pz_max}" if pz >= 0 else "读取失败"

            # 兑换目标：当前积分 / 目标商品所需积分
            bal = int(stats.get("balance") or 0) or api.get_balance()
            price = int(stats.get("goal_price") or 0)
            if price > 0 and bal > 0:
                pct = min(100, int(round(bal * 100.0 / price)))
                status["goal"] = f"{bal}/{price}（{pct}%）"
            elif price > 0:
                status["goal"] = f"—/{price}"
            else:
                status["goal"] = "未设置目标"

            # 进度条：今天能做完的那几项各算完成度再取平均
            #（签到 / 搜索 / 阅读 / 加分卡片 / 每日活动，读不到的自动跳过）
            comps: list[float] = []
            done = api.state.get("tasks", {})
            comps.append(1.0 if int(done.get("sign", 0)) == today else 0.0)
            if info and mx > 0:
                comps.append(min(1.0, cur / (search_goal or mx)))
            if ra >= 0 and ram > 0:
                comps.append(min(1.0, (ra * per) / (read_goal or ram * per)))   # 统一按分数比
            comps.append(1.0 if not cards else 0.0)
            comps.append(1.0 if not [c for c in cards if c.get("kind") == "daily"] else 0.0)
            pct = int(round(sum(comps) / len(comps) * 100)) if comps else 0
            status["progress_pct"] = pct
            if not quiet:
                self._sys(f"今日任务：积分 +{tp if tp >= 0 else '?'}，搜索 {status['quota']}，"
                          f"阅读 {status['read']}，待领卡片 {status['cards']}，"
                          f"拼图 {status['puzzle']}，兑换目标 {status['goal']}，"
                          f"完成进度 {pct}%")
            self.signals.status.emit(status)
        finally:
            api.close()                     # 刷完就断开，不占着会话

    def _startup_auto_check(self):
        """进入软件自动体检一次（状态行 + 今日行），圆环旋转，结束后重新倒计时。"""
        if self.running or self.login_busy:
            QTimer.singleShot(10000, self._startup_auto_check)   # 忙时稍后重试
            return
        self._sys("启动体检中…（之后圆环画满时自动刷新一次）")
        self._start_auto_refresh()

    # ---------- 圆环倒计时：深蓝弧画满 = 到了下次自动刷新的时间 ----------
    def _auto_check_allowed(self) -> bool:
        """自动刷新只在「人就在电脑前看着控制台」时才连服务器。

        没焦点、最小化、或者在别的页面上就完全不发请求（圆环也会冻结）。
        """
        try:
            if not self.isVisible() or self.isMinimized():
                return False
            if not self.isActiveWindow():
                return False
            return self.stack.currentIndex() == 0
        except Exception:
            return False

    def _reset_countdown(self):
        """重新随机一个 5 分钟左右的周期，圆环进度归零。"""
        self._check_interval = int(300 * random.uniform(0.8, 1.2))
        self._check_left = self._check_interval
        for k in ("refresh", "today_refresh"):
            b = self.buttons.get(k)
            if b:
                b.set_ratio(0.0)

    # ---------- 收进托盘/最小化后不再空转 ----------
    def _pause_ring(self):
        """窗口收起来后停掉 1 秒定时器。

        圆环只是给人看的，缩到托盘/最小化时没人看得见，每秒唤醒一次纯属浪费电。
        """
        t = getattr(self, "_ring_timer", None)
        if t is not None and t.isActive():
            t.stop()

    def _resume_ring(self, force: bool = False):
        """窗口重新露出来：恢复计时，并补一次刷新。

        规则：在托盘/最小化里待的时间 > 一个刷新周期（约 5 分钟），数据肯定旧了，
        直接刷（force 时同理），不再受 60 秒防抖限制——用户从托盘唤回就是想看最新数字。
        """
        t = getattr(self, "_ring_timer", None)
        if t is None:
            return
        if not t.isActive():
            t.start(1000)
        if self.running or self.login_busy:
            return                                  # 正在跑任务时本来就不刷
        now = time.time()
        away = now - self._hidden_at if self._hidden_at else 0.0
        stale = force or away >= max(120.0, float(getattr(self, "_check_interval", 300)))
        if not stale and now - getattr(self, "_last_auto_refresh", 0.0) < 60:
            return                                  # 刚刷过，别连着打两次请求
        self._hidden_at = 0.0
        self._reset_countdown()
        self._start_auto_refresh(force=True)        # 用户主动唤回：允许突破「睡眠期不刷新」

    def showEvent(self, ev):
        super().showEvent(ev)
        self._resume_ring()

    def hideEvent(self, ev):
        self._hidden_at = time.time()
        self._pause_ring()
        super().hideEvent(ev)

    def changeEvent(self, ev):
        super().changeEvent(ev)
        if ev.type() == QEvent.WindowStateChange:
            if self.isMinimized():
                self._hidden_at = time.time()
                self._pause_ring()
            else:
                self._resume_ring()

    def _ring_tick(self):
        """每秒：冻结条件（跑任务 / 等登录 / 正在刷新 / 人不在控制台）时不计时；
        否则推进深蓝弧，画满即自动体检一次。"""
        # 墙钟跳变 = 电脑睡过/休眠过：定时器停摆了几小时，界面数字必然过期，先补刷
        now = time.time()
        jumped = bool(self._last_tick_wall) and (now - self._last_tick_wall) > 90
        self._last_tick_wall = now
        if self._daemon_resting:
            return                                  # 守护睡着：不自动刷新，等用户来操作
        rbtn = self.buttons.get("refresh")
        tbtn = self.buttons.get("today_refresh")
        if (self.running or self.login_busy
                or getattr(rbtn, "_spinning", False) or getattr(tbtn, "_spinning", False)):
            return                                  # 冻结：刷新完再从 0 重新走
        if not self._auto_check_allowed():
            return                                  # 人不在控制台页面：圆环冻结，不发请求
        if jumped:
            self._reset_countdown()
            self._start_auto_refresh()
            return
        self._check_left -= 1
        ratio = 1.0 - self._check_left / max(1, self._check_interval)
        for k in ("refresh", "today_refresh"):
            self.buttons[k].set_ratio(ratio)
        if self._check_left <= 0:
            self._start_auto_refresh()

    def _start_auto_refresh(self, force: bool = False):
        """圆环画满：自动体检一次（状态行 + 今日行），两个圆环同时进入旋转状态。

        force=True 只给「用户主动唤回窗口」用；守护睡着时绝不自动发请求。
        """
        if self.running or self.login_busy:
            return
        if self._daemon_resting and not force:
            return
        self.buttons["refresh"].set_spinning(True)
        self.buttons["today_refresh"].set_spinning(True)

        def _run():
            try:
                self._do_check(True)
                self._do_today_check(True)
            except Exception:
                pass
            finally:
                self._last_auto_refresh = time.time()
                self.signals.check_done.emit("refresh")
                self.signals.check_done.emit("today_refresh")
        threading.Thread(target=_run, daemon=True).start()
    # ------------------------------------------------------------ 账号操作
    @staticmethod
    def _parse_userinfo(ui) -> tuple[int, bool]:
        """从 getuserinfo 结果里取积分，并判断是否真的登录。
        注意：未登录时接口也可能返回一段 JSON，所以不能只看「有没有响应」，
        必须要求 availablePoints > 0 或存在 userStatus.counters。
        """
        if not isinstance(ui, dict):
            return 0, False
        d = ui.get("dashboard") or ui
        if not isinstance(d, dict):
            return 0, False
        us = d.get("userStatus") or {}
        try:
            bal = int(d.get("availablePoints") or us.get("availablePoints") or 0)
        except (TypeError, ValueError):
            bal = 0
        counters = us.get("counters") or d.get("counters") or {}
        return bal, bool(bal > 0 or counters)
    @staticmethod
    def _cookie_login_state(api) -> tuple[int, bool]:
        """判断 Cookie 是否登录并取当前积分。
        老接口 /api/getuserinfo 已被新版门户下线（401），改用 dashboard 页面解析：
        拿到 balance 就说明 Cookie 是有效的登录态。不依赖 OAuth Token，登录后立即可用。
        """
        bal = api.balance_from_dashboard()
        return bal, bal > 0
    def _make_api_plain(self):
        """后台线程里用的 API：日志仍通过信号回界面，但不占用任务运行的控制位。"""
        api = cli.make_api(dry_run=True)
        api.log_hook = lambda level, icon, msg: self.signals.log.emit(level, icon, msg)
        return api
    def _refresh_file_status(self):
        n = 0
        if os.path.isfile(cli.COOKIE_PATH):
            try:
                with open(cli.COOKIE_PATH, "r", encoding="utf-8", errors="ignore") as f:
                    n = sum(1 for line in f if line.strip() and not line.startswith("#"))
            except Exception:
                n = -1
        self.cookie_state.setText(
            f"当前 cookies.txt: {'已配置 ' + str(n) + ' 条' if n > 0 else '未配置/为空'}"
            "　·　下次可点「刷新 Cookie（免登录）」直接更新")
        self.status_labels["cookie"].setText("✅ 已配置" if n > 0 else "❌ 未配置")
        auth = cli._load(cli.AUTH_PATH, cli.DEFAULT_AUTH)
        self.status_labels["token"].setText("✅ 已授权" if (auth.get("refresh_token") or "").strip() else "⚠️ 未授权")
    # ---------- 一键登录：主线程入口 / 按钮态 / 信号槽 ----------
    def _startup_login_state(self):
        """启动时后台核对一次登录态：已有有效 Cookie 就直接显示「已登录」，别停在「尚未开始」。"""
        if not os.path.isfile(cli.COOKIE_PATH):
            self.auto_state.setText("⏸  尚未登录 —— 点上方「一键登录」开始")
            return
        self.auto_state.setText("🔎 正在核对已保存的登录状态…")
        threading.Thread(target=self._startup_login_worker, daemon=True).start()
    def _startup_login_worker(self):
        api = None
        try:
            # 开机时网络还没通就一直等（用户可能开着机就去忙别的了），别一上来就判「登录状态待确认」
            if not netguard.ready():
                self.signals.browser.emit("📡 等待网络", "网络还没就绪，联网后自动核对登录状态…")
                if not netguard.wait_ready(poll_s=15, max_wait_s=0,
                                           should_stop=self.app_stop.is_set):
                    return
            api = self._make_api_plain()
            bal, valid = self._cookie_login_state(api)
            has_token = bool((api.auth.get("refresh_token") or "").strip())
            if valid:
                extra = ("OAuth 授权有效，全部功能可用" if has_token
                         else "OAuth 未授权（App 签到/新闻阅读会跳过），需要时点「一键登录」补上")
                self.signals.browser.emit("✅ 已登录", f"Cookie 有效，当前积分 {bal} · {extra}")
                self.signals.status.emit({"points": str(bal), "cookie": "✅ 已验证",
                                          "token": "✅ 已授权" if has_token else "⚠️ 未授权"})
            else:
                self.signals.browser.emit(
                    "⚠️ 登录状态待确认", "已保存 Cookie 但没验证通过（可能失效或暂读不到积分），"
                    "建议点「一键登录」重新登录一次")
                self.signals.status.emit({"cookie": "⚠️ 待确认"})
        except Exception as e:
            self.signals.browser.emit("⚠️ 无法核对", f"{type(e).__name__}: {e}")
        finally:
            if api is not None:
                api.close()                 # 核对完就断开
    def _set_login_buttons(self, busy: bool):
        for key in ("auto", "refresh", "capture"):
            if key in self.login_buttons:
                self.login_buttons[key].setEnabled(not busy)
        # 「停止等待」只在真的在等登录/授权时出现，平时收起（和主界面的「停止」同理）
        self.login_buttons["stop"].setVisible(busy)
        self.login_buttons["stop"].setEnabled(busy)
    def _start_busy(self, what: str) -> bool:
        if self.login_busy or self.running:
            QMessageBox.warning(self, "稍等一下", "已有登录/验证或任务正在运行，请先等待或点「停止」")
            return False
        self.login_busy = True
        self.login_stop.clear()
        self._set_login_buttons(True)
        self.signals.log.emit("SYS", "🚀", f"开始：{what}")
        return True
    def _on_browser_stage(self, stage: str, text: str):
        self.auto_state.setText(f"<b>{html.escape(stage)}</b> · {html.escape(text)}")
        self.signals.log.emit("DEBUG", "🧩", f"[登录] {stage} · {text}")
    def _on_browser_done(self, res: dict):
        """所有弹窗都在这个主线程槽里做，后台线程绝不碰 Qt 控件。"""
        self.login_busy = False
        self._set_login_buttons(False)
        self._refresh_file_status()
        mode = res.get("mode", "auto")
        points = res.get("points")
        if points is not None:
            self.status_labels["points"].setText(str(points))
            self.status_labels["cookie"].setText("✅ 已验证")
        if res.get("token"):
            self.refresh_var.setText(cli._load(cli.AUTH_PATH, cli.DEFAULT_AUTH).get("refresh_token", ""))
        err = res.get("error", "")
        if err:
            self.auto_state.setText(f"<b>⚠️ 未完成</b> · {html.escape(err)}")
            QMessageBox.warning(self, "未完成", err)
            return
        if res.get("cancelled"):
            self._sys("已停止等待")
            return
        if mode == "verify":
            QMessageBox.information(self, "Cookie 有效",
                                    f"✅ 登录态验证通过\n\n"
                                    f"已写入 {res.get('cookie_n', 0)} 条 Cookie\n"
                                    f"当前积分：{points if points is not None else '读取失败'}")
            return
        if mode == "capture":
            QMessageBox.information(self, "授权完成", "🟢 已自动捕获授权码，refresh_token 已保存")
            return
        lines = [f"✅ Cookie：已自动读取 {res.get('cookie_n', 0)} 条（含 HttpOnly）并通过登录验证"]
        if points is not None:
            lines.append(f"📊 当前积分：{points}")
        if res.get("token"):
            lines.append("✅ OAuth 授权：已自动完成")
        elif res.get("note"):
            lines.append("⚠️ OAuth：" + res["note"])
        lines.append("登录窗口已自动关闭。下次点「刷新 Cookie（免登录）」即可，不用重新登录。")
        self.auto_state.setText("<b>✅ 完成</b> · Cookie 已验证，配置已保存")
        QMessageBox.information(self, "配置完成", "\n".join(lines))
    def _auto_login(self, refresh_only: bool):
        if not self._start_busy("刷新 Cookie（免登录）" if refresh_only else "一键登录 · 自动获取 Cookie + 授权"):
            return
        do_oauth = bool(self.var_auto_oauth.isChecked()) and not refresh_only
        threading.Thread(target=self._auto_login_worker,
                         args=(refresh_only, do_oauth), daemon=True).start()
    def _auto_login_worker(self, refresh_only: bool, do_oauth: bool):
        import browser_login as _bl
        res = {"mode": "auto", "cookie_n": 0, "points": None,
               "token": False, "error": "", "cancelled": False, "note": ""}
        try:
            self.signals.browser.emit("启动", "正在打开独立的浏览器窗口…")
            sess = self.browser_session
            if sess is None or not sess.alive():
                sess = _bl.BrowserSession(
                    cli.BASE_DIR,
                    log=lambda l, i, m: self.signals.log.emit(l, i, m))
                self.browser_session = sess
            how = sess.start(_bl.LOGIN_URL, reuse=True)
            self.signals.log.emit("DEBUG", "🧩", f"浏览器 {how}，调试端口 {sess.port}")
            if refresh_only:
                self.signals.browser.emit("读取中", "正在从已保存的登录状态读取最新 Cookie…")
            else:
                self.signals.browser.emit(
                    "等待登录",
                    "请在弹出的 Edge 窗口里登录微软账号；登录完成会自动继续，最多等 5 分钟。")
            deadline = time.time() + (60 if refresh_only else 300)
            next_verify = 0.0
            loop = 0
            got = False
            while time.time() < deadline and not self.login_stop.is_set():
                loop += 1
                cookies = sess.cdp.all_cookies()
                markers = _bl.login_markers(cookies)
                force = loop % 6 == 0                    # 每 ~15 秒兜底校验一次，避免漏掉标志
                if (markers or force) and time.time() >= next_verify:
                    next_verify = time.time() + 8        # 校验节流，别把接口打太频繁
                    n = _bl.save_cookies("netscape", _bl.cookies_to_netscape(cookies),
                                         cli.COOKIE_PATH)
                    if markers:
                        self.signals.browser.emit(
                            "已读取", f"读到 {len(cookies)} 条 Cookie（相关域 {n} 条，"
                                     f"命中 {', '.join(markers[:3])}），正在验证登录态…")
                        self.signals.log.emit("DEBUG", "🧩", f"CDP Cookie {len(cookies)} 条 → 写入 {n} 条")
                    api = self._make_api_plain()
                    bal, valid = self._cookie_login_state(api)
                    if valid:
                        got, res["cookie_n"], res["points"] = True, n, bal
                        self.signals.browser.emit("登录成功", f"✅ Cookie 有效，当前积分 {bal}")
                        break
                self.login_stop.wait(2.5)
            if not got:
                if self.login_stop.is_set():
                    res["cancelled"] = True
                else:
                    res["error"] = ("没检测到登录完成。请确认是在弹出的那个 Edge 窗口里登录的；"
                                    "若页面已登录但仍失败，可点「刷新 Cookie（免登录）」重试，"
                                    "或改用下方「手动复制」。")
                return
            # bing.com 的 _U Cookie 是搜索积分的凭据，rewards 登录不会自动带上；
            # 打开一次 bing 主页让 SSO 自动写入后再补采一次。
            try:
                cookies = sess.cdp.all_cookies()
                if "_U" not in {c.get("name") for c in cookies}:
                    self.signals.browser.emit("补登 Bing", "正在把登录态带到 bing.com（搜索任务需要 _U）…")
                    try:
                        sess.cdp.open_tab("https://cn.bing.com/")
                    except Exception:
                        pass
                    self.login_stop.wait(8)
                    cookies = sess.cdp.all_cookies()
                n2 = _bl.save_cookies("netscape", _bl.cookies_to_netscape(cookies),
                                      cli.COOKIE_PATH)
                res["cookie_n"] = n2
                has_u = "_U" in {c.get("name") for c in cookies}
                self.signals.log.emit("DEBUG", "🧩",
                                      f"最终 Cookie {len(cookies)} 条 → 写入 {n2} 条（含 _U: {has_u}）")
                if not has_u:
                    res["note"] = ("未拿到 bing.com 的 _U Cookie：搜索任务可能不计分。"
                                   "可在登录窗口手动访问一次 bing.com 并确认右上角已登录，再点「刷新 Cookie」。")
            except Exception as e:
                self.signals.log.emit("WARN", "🟡", f"补采 _U 失败：{type(e).__name__}: {e}")
            if do_oauth:
                self.signals.browser.emit("等待授权", "已在登录窗口打开授权页，正在自动捕获授权码…")
                try:
                    sess.cdp.open_tab(AUTH_URL)
                except Exception as e:
                    self.signals.log.emit("WARN", "🟡", f"打开授权页失败：{type(e).__name__}: {e}")
                auth_deadline = time.time() + 120
                while time.time() < auth_deadline and not self.login_stop.is_set():
                    code = sess.cdp.find_auth_code()
                    if code:
                        self.signals.browser.emit("授权中", "已捕获授权码，正在换取 Token…")
                        api = self._make_api_plain()
                        if cli.do_auth(code, api):
                            res["token"] = True
                            self.signals.browser.emit("授权成功", "✅ OAuth 授权完成")
                            break
                    self.login_stop.wait(1.5)
                if not res["token"] and not self.login_stop.is_set():
                    res["note"] = "授权码自动捕获超时，可在第③节手动粘贴授权 URL 补上（不影响 Cookie）"
            else:
                res["note"] = "本次未执行（未勾选自动授权或使用了刷新模式）"
        except Exception as e:
            res["error"] = f"{type(e).__name__}: {e}\n\n可改用下方「手动复制」方式。"
            self.signals.log.emit("ERROR", "🔴", f"自动登录异常：{traceback.format_exc()}")
        finally:
            # Cookie/授权都收完了就自动关掉登录窗口，不需要用户手动关
            try:
                sess = self.browser_session
                if sess is not None and sess.alive():
                    if sess.close_window():
                        self.signals.log.emit("SYS", "ℹ",
                                              "登录窗口已自动关闭（登录状态仍保存在 .browser-profile）")
            except Exception:
                pass
            self.signals.browser_done.emit(res)
    def _stop_auto_login(self):
        self.login_stop.set()
        self.signals.browser.emit("停止中", "已请求停止，正在收尾…")
    # ---------- 手动路径：图文引导 / 智能粘贴 / 保存即验证 ----------
    def _manual_guide(self):
        dlg = QDialog(self)
        dlg.setWindowTitle("手动获取 Cookie · 图文步骤")
        dlg.resize(680, 520)
        v = QVBoxLayout(dlg)
        tb = QTextBrowser()
        tb.setOpenExternalLinks(True)
        tb.setHtml(GUIDE_HTML)
        v.addWidget(tb, 1)
        row = QHBoxLayout()
        b_open = QPushButton("🌐  打开登录页")
        b_open.setObjectName("primary")
        b_open.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(bl.LOGIN_URL)))
        row.addWidget(b_open)
        b_copy = QPushButton("📋  复制步骤文字")
        b_copy.clicked.connect(lambda: (QApplication.clipboard().setText(GUIDE_TEXT),
                                        self._sys("步骤文字已复制到剪贴板")))
        row.addWidget(b_copy)
        row.addStretch(1)
        b_close = QPushButton("关闭")
        b_close.clicked.connect(dlg.accept)
        row.addWidget(b_close)
        v.addLayout(row)
        dlg.exec_()
    def _paste_from_clipboard(self):
        raw = QApplication.clipboard().text().strip()
        if not raw:
            QMessageBox.warning(self, "剪贴板是空的", "请先按图文步骤复制 Cookie 请求头整串")
            return
        try:
            kind, payload = bl.normalize_cookie_input(raw)
        except ValueError as e:
            QMessageBox.warning(self, "没认出来", f"{e}\n\n点「📖 打开登录页 + 查看图文步骤」看具体做法。")
            return
        self.cookie_text.setPlainText(raw)
        count = (payload.count(";") + 1) if kind == "header" else sum(
            1 for l in payload.splitlines() if l.strip() and not l.startswith("#"))
        desc = "Cookie 请求头" if kind == "header" else "cookies.txt / JSON（Netscape）"
        self.manual_hint.setText(f"✅ 已识别为{desc}，约 {count} 项 —— 点「保存并验证登录」写入。")
        self._sys(f"🟢 剪贴板内容已识别为{desc}（内容不进日志）")
    def _save_and_verify(self, kind: str, payload: str):
        try:
            api = self._make_api_plain()
            n = bl.save_cookies(kind, payload, cli.COOKIE_PATH, api)
        except Exception as e:
            QMessageBox.critical(self, "保存失败", f"{type(e).__name__}: {e}")
            return
        self.manual_hint.setText(f"✅ 已写入 {n} 条 Cookie，正在调用接口验证登录态…")
        self._refresh_file_status()
        self._sys(f"🟢 Cookie 已保存 {n} 条（内容不进日志），开始验证")
        if not self._start_busy("验证登录态"):
            return
        threading.Thread(target=self._verify_login_worker, args=(n,), daemon=True).start()
    def _verify_login_worker(self, n: int):
        res = {"mode": "verify", "cookie_n": n, "points": None, "error": ""}
        try:
            self.signals.browser.emit("验证中", "正在调用 getuserinfo 检查 Cookie 是否有效…")
            api = self._make_api_plain()
            bal, valid = self._cookie_login_state(api)
            if valid:
                res["points"] = bal
                self.signals.browser.emit("验证通过", f"Cookie 有效，当前积分 {bal}")
            else:
                res["error"] = ("接口没有返回有效的账号信息。常见原因：复制的不是完整的 Cookie 请求头、"
                                "漏掉了 HttpOnly 的 _U / KievRPSAuthEng，或登录的不是 Rewards 账号。\n\n"
                                "建议直接用上方「一键登录」，它会自动取全。")
        except Exception as e:
            res["error"] = f"验证请求失败：{type(e).__name__}: {e}"
        finally:
            self.signals.browser_done.emit(res)
    def _save_cookie_text(self):
        raw = self.cookie_text.toPlainText().strip()
        if not raw:
            QMessageBox.warning(self, "空内容", "请先点「📋 从剪贴板粘贴」，或按图文步骤复制后粘贴到框里")
            return
        try:
            kind, payload = bl.normalize_cookie_input(raw)
        except ValueError as e:
            QMessageBox.warning(self, "格式没认出来", str(e))
            return
        self._save_and_verify(kind, payload)
    def _import_cookie_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择 cookies.txt", "", "Cookie 文件 (*.txt);;所有文件 (*.*)")
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
            kind, payload = bl.normalize_cookie_input(content)
            self.manual_hint.setText(f"已选择：{os.path.basename(path)}")
            self._save_and_verify(kind, payload)
        except Exception as e:
            QMessageBox.critical(self, "导入失败", f"{type(e).__name__}: {e}")
    def _clear_cookies(self):
        if QMessageBox.question(self, "确认", "确定清空已保存的 Cookie？",
                                QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
            return
        try:
            os.remove(cli.COOKIE_PATH)
        except FileNotFoundError:
            pass
        self._refresh_file_status()
        self._sys("🗑 Cookie 已清空")
    # ---------- OAuth ----------
    def _copy_auth_url(self):
        QApplication.clipboard().setText(AUTH_URL)
        self._sys("授权链接已复制到剪贴板")
    def _open_auth_in_browser(self):
        sess = self.browser_session
        if sess and sess.alive():
            try:
                sess.cdp.open_tab(AUTH_URL)
                self._sys("🟢 已在登录窗口打开授权页，可点「自动捕获授权码并换取 Token」")
                return
            except Exception as e:
                self.signals.log.emit("WARN", "🟡", f"调试窗口打开失败：{e}")
        QDesktopServices.openUrl(QUrl(AUTH_URL))
        QMessageBox.information(
            self, "已在普通浏览器打开",
            "自动捕获授权码需要在「登录窗口」里打开授权页。\n\n"
            "如果用的是普通浏览器，请在登录完成后复制地址栏完整 URL，粘贴到下面手动换取；\n"
            "或者先点「一键登录」，再回来点这个按钮，就能全自动了。")
    def _capture_auth_code(self):
        sess = self.browser_session
        if not sess or not sess.alive():
            QMessageBox.information(
                self, "需要先打开登录窗口",
                "自动捕获依赖一键登录打开的那个浏览器窗口。\n"
                "请先点「🌐 一键登录」，或直接用下面的输入框手动粘贴授权 URL。")
            return
        try:
            sess.cdp.open_tab(AUTH_URL)
        except Exception:
            pass
        if not self._start_busy("自动捕获授权码"):
            return
        threading.Thread(target=self._capture_worker, args=(sess,), daemon=True).start()
    def _capture_worker(self, sess):
        res = {"mode": "capture", "token": False, "error": ""}
        try:
            self.signals.browser.emit("等待授权", "请在登录窗口完成授权，程序会自动读取地址栏…")
            deadline = time.time() + 120
            while time.time() < deadline and not self.login_stop.is_set():
                code = sess.cdp.find_auth_code()
                if code:
                    self.signals.browser.emit("授权中", "已捕获授权码，正在换取 Token…")
                    if cli.do_auth(code, self._make_api_plain()):
                        res["token"] = True
                        self.signals.browser.emit("授权成功", "✅ OAuth 授权完成")
                        break
                    res["error"] = "授权码换取失败，可在下方手动重试"
                    break
                self.login_stop.wait(1.5)
            if not res["token"] and not res["error"]:
                if self.login_stop.is_set():
                    res["cancelled"] = True
                else:
                    res["error"] = "等待授权超时，可复制地址栏完整 URL 到下方手动换取"
        except Exception as e:
            res["error"] = f"捕获失败：{type(e).__name__}: {e}"
        finally:
            self.signals.browser_done.emit(res)
    def _do_auth(self):
        raw = self.auth_code_var.text().strip()
        if not raw:
            QMessageBox.warning(self, "空内容", "请粘贴授权后地址栏的完整 URL 或 code")
            return
        try:
            api = self._make_api_plain()
            if cli.do_auth(raw, api):
                self.refresh_var.setText(api.auth.get("refresh_token", ""))
                self._sys("🟢 授权成功，refresh_token 已保存")
                self._refresh_file_status()
                QMessageBox.information(self, "授权成功", "🟢 refresh_token 已保存到 auth.json")
            else:
                QMessageBox.warning(self, "授权失败", "没换成 Token，请确认粘贴的是授权后地址栏的完整 URL")
        except Exception as e:
            QMessageBox.critical(self, "授权失败", str(e))
    def _save_refresh(self):
        api = self._make_api_plain()
        api.auth["refresh_token"] = self.refresh_var.text().strip()
        api.save_auth()
        self._sys("🟢 refresh_token 已写入 auth.json")
        self._refresh_file_status()
    def _clear_token(self):
        api = self._make_api_plain()
        api.auth = dict(cli.DEFAULT_AUTH)
        api.save_auth()
        self.refresh_var.setText("")
        self._sys("Token 已清除")
        self._refresh_file_status()
    # ------------------------------------------------------------ 杂项
    @staticmethod
    def _open_dir(path: str):
        try:
            os.makedirs(path, exist_ok=True)
            os.startfile(path)                       # noqa: Windows
        except Exception:
            try:
                webbrowser.open("file:///" + path.replace("\\", "/"))
            except Exception:
                pass
    def closeEvent(self, event):
        # 托盘「退出」才真正退出；平时点 X / Alt+F4 只是缩到托盘，守护继续跑
        if not getattr(self, "_really_quit", False):
            event.ignore()
            self.hide()
            if not self._tray_hint_shown:
                self._tray_hint_shown = True
                self.tray.showMessage(
                    "仍在后台运行",
                    "窗口已最小化到右下角托盘，守护照常执行。\n"
                    "右键托盘图标：开始/停止守护、开机自启动、退出。",
                    QSystemTrayIcon.Information, 6000)
            return
        self.stop_event.set()
        self.app_stop.set()
        self.login_stop.set()               # 中断可能还在轮询的登录线程
        event.accept()
def _activate_existing_window(prefix: str = "Microsoft Rewards 自动任务") -> bool:
    """把已经在跑的那个窗口叫到前台（隐藏到托盘的也能叫出来）。

    单实例第二次启动时用它替代「已在运行」对话框 —— 用户只是想打开界面，
    不需要被一个模态框拦住。
    """
    if os.name != "nt":
        return False
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        found: list[int] = []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def _cb(hwnd, _lparam):
            n = user32.GetWindowTextLengthW(hwnd)
            if n > 0:
                buf = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(hwnd, buf, n + 1)
                if buf.value.startswith(prefix):
                    found.append(hwnd)
                    return False
            return True

        user32.EnumWindows(_cb, 0)
        if not found:
            return False
        hwnd = found[0]
        user32.ShowWindow(hwnd, 9)                 # SW_RESTORE：还原 + 显示
        user32.SetForegroundWindow(hwnd)
        return True
    except Exception:
        return False


def main() -> int:
    # pythonw.exe（快捷方式/无黑框启动）下 sys.stdout / sys.stderr 为 None，
    # 所有 print 都会抛 AttributeError。统一重定向到空设备，业务日志仍写 logs/。
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8", errors="replace")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8", errors="replace")
    selftest = "--selftest" in sys.argv
    app = QApplication(sys.argv)
    app.setStyle("Fusion")                 # 统一用 Fusion 渲染，QSS 对数值框/下拉箭头才稳定生效
    app.setStyleSheet(sanitize_qss(QSS) + sanitize_qss(extra_qss()))
    app.setFont(QFont("Microsoft YaHei UI", 10))
    if os.path.isfile(ICO_PATH):
        app.setWindowIcon(QIcon(ICO_PATH))
    # 单实例：开机自启动 + 手动双开容易撞车（配置文件、并发任务），第二次启动直接提示退出
    if os.name == "nt" and not selftest:
        import ctypes
        _m = ctypes.windll.kernel32.CreateMutexW(None, False, "MSRewardsConsole_SingleInstance")
        globals()["_mutex_handle"] = ctypes.wintypes.HANDLE(_m) if hasattr(ctypes, "wintypes") else _m
        if ctypes.windll.kernel32.GetLastError() == 183:    # ERROR_ALREADY_EXISTS
            # 已经有实例在跑：静默把它的窗口叫到前台，不弹模态框
            _activate_existing_window()
            return 0
    win = App()
    # 开机自启动路径（--minimized）：只留右下角托盘图标，界面绝不显示。
    # 注意：别在这里 return，后面还要起守护；只是「不调用 show()」。
    silent = "--minimized" in sys.argv
    if silent:
        win.hide()
        win._pause_ring()                  # 窗口从没露过面，圆环不必每秒空转
    if selftest:
        def _run():
            errs = []
            cfg = win._collect()
            print("任务开关:", cfg["tasks"])
            print("参数:", {k: cfg[k] for k in ("search_span_seconds", "delay_min_ms", "host", "verbose", "lock_region")})
            win.signals.log.emit("DEBUG", "🧩", "自检：详细日志行")
            win.signals.log.emit("ERROR", "🔴", "自检：错误日志行")
            win.signals.log.emit("OK", "🟢", "自检：成功日志行")
            QApplication.processEvents()
            text = win.log_view.toPlainText()
            if "自检：详细日志行" not in text or "自检：错误日志行" not in text:
                errs.append("日志面板未收到信号")
            win.signals.status.emit({"points": "12345", "quota": "30/60", "progress_pct": 55})
            QApplication.processEvents()
            if win.status_labels["points"].text() != "12345":
                errs.append("状态卡未刷新")
            if win.status_labels["quota"].text() != "30/60":
                errs.append("今日任务条未刷新")
            if win.progress_bar.value() != 55:
                errs.append("进度条未刷新")
            for k in ("cookie", "token", "region", "points", "today_earned", "read", "cards",
                      "puzzle", "goal"):
                if k not in win.status_labels:
                    errs.append(f"状态项缺失: {k}")
            if "today_refresh" not in win.buttons:
                errs.append("今日刷新按钮缺失")
            # 守护待命态：界面要放开刷新、锁住再起 worker、停止按钮可见
            win.running = True
            win._set_buttons(False)
            win._on_daemon_rest(True)
            QApplication.processEvents()
            if not win.buttons["refresh"].isEnabled():
                errs.append("待命态：刷新按钮仍被禁用")
            if win.buttons["run"].isEnabled() or win.buttons["daemon"].isEnabled():
                errs.append("待命态：仍可再起一个 worker（应禁用）")
            if win.btn_stop.isHidden():
                errs.append("待命态：停止按钮不可见")
            # 待命期间手动刷新结束，不能把守护状态清掉
            win._daemon_wanted = True
            win._worker_mode = "check"
            win.signals.done.emit()
            QApplication.processEvents()
            if not win._daemon_resting:
                errs.append("待命期间刷新结束误清了守护状态")
            if win.running:
                errs.append("待命期间刷新结束未复位运行态")
            win._daemon_wanted = False
            # 守护睡着时：任何自动刷新都必须被挡住
            win._daemon_resting = True
            win._last_auto_refresh = 0.0
            win._start_auto_refresh()
            QApplication.processEvents()
            if getattr(win.buttons["refresh"], "_spinning", False):
                errs.append("守护睡着时仍自动刷新（应只有用户操作才刷）")
            # 托盘闲置超过一个刷新周期 → 必须无视 60 秒防抖，强制补刷一次
            calls = []
            win._start_auto_refresh = lambda force=False: calls.append(1)
            win._hidden_at = time.time() - 3600
            win._last_auto_refresh = time.time()
            win.running = False
            win._resume_ring()
            if not calls:
                errs.append("从托盘唤醒（闲置 1 小时）没有补刷")
            win._daemon_resting = False
            win.signals.done.emit()
            QApplication.processEvents()
            if win.running is not False:
                errs.append("运行状态未复位")
            # 账号与授权页：新控件、进度信号、登录校验、状态复位
            for k in ("auto", "refresh", "stop", "capture"):
                if k not in win.login_buttons:
                    errs.append(f"一键登录按钮缺失: {k}")
            if not win.var_auto_oauth.isChecked():
                errs.append("自动 OAuth 授权未默认勾选")
            win.signals.browser.emit("自检阶段", "自检说明")
            QApplication.processEvents()
            if "自检阶段" not in win.auto_state.text():
                errs.append("登录进度标签未刷新")
            bal, valid = win._parse_userinfo(
                {"dashboard": {"userStatus": {"availablePoints": 9070, "counters": {"pcSearch": []}}}})
            if bal != 9070 or not valid:
                errs.append("登录校验解析错误")
            if win._parse_userinfo({"dashboard": {"error": "not logged in"}})[1]:
                errs.append("未登录被误判为有效")
            win.signals.browser_done.emit({"mode": "auto", "cancelled": True})
            QApplication.processEvents()
            if win.login_busy is not False:
                errs.append("登录状态位未复位")
            # 样式表生效校验：QSS 一旦被非法行吞掉，按钮会退回系统原生外观（不报错、只变丑）
            probe = QPushButton("样式探针")
            probe.setObjectName("primary")
            probe.setAttribute(Qt.WA_DontShowOnScreen, True)   # 只在离屏存在，不闪窗
            probe.resize(160, 34)
            probe.show()
            QApplication.processEvents()
            pix = probe.grab().toImage()
            # 取左侧空白区，避开文字笔画
            if pix.pixelColor(12, pix.height() // 2).name() != "#2563eb":
                errs.append("样式表未生效（主按钮底色不是 #2563eb）")
            probe.close()
            print("页数:", win.stack.count(), "| 日志行数:", len(text.splitlines()),
                  "| 状态卡:", win.status_labels["points"].text(),
                  "| 登录按钮:", len(win.login_buttons))
            for e in errs:
                print("❌", e)
            print("=== PyQt5 GUI 自检" + ("通过 ✅" if not errs else "失败 ❌") + " ===")
            app.exit(1 if errs else 0)
        QTimer.singleShot(120, _run)
    else:
        if not silent:                     # 手动启动才显示窗口；自启动保持纯托盘
            win.show()
    return app.exec_()
if __name__ == "__main__":
    sys.exit(main())
