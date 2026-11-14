"""Windows 空闲检测 —— 判断「电脑有多久没人动了」。

只用 ctypes 调系统 API（GetLastInputInfo / GetTickCount），零第三方依赖。
非 Windows 平台一律返回 0（= 不认为空闲），守护模式因此不会卡死。

用途：守护模式只在用户离开电脑（默认 5 分钟无键鼠输入）后才动手，
这样即使任务需要弹浏览器，也不会打断正在用电脑的人。
"""
from __future__ import annotations

import ctypes
import os
import time


class _LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]


class _RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class _MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("rcMonitor", _RECT), ("rcWork", _RECT),
                ("dwFlags", ctypes.c_uint)]


SPI_GETSCREENSAVERRUNNING = 0x0072
MONITOR_DEFAULTTONEAREST = 2


def _user32():
    return ctypes.windll.user32


# ---------------------------------------------------------------- 会话状态
def is_locked() -> bool | None:
    """当前会话是不是锁屏状态（拿不到就返回 None）。

    用 OpenInputDesktop 是否成功来判断：锁屏时会话已切到 Winlogon 桌面，
    普通进程打不开输入桌面。仅用于日志提示，不影响任务执行。
    """
    if os.name != "nt":
        return None
    try:
        u32 = _user32()
        u32.OpenInputDesktop.restype = ctypes.c_void_p
        h = u32.OpenInputDesktop(0, False, 0x0100)      # DESKTOP_SWITCHDESKTOP
        if h:
            u32.CloseDesktop(ctypes.c_void_p(h))
            return False
        return True
    except Exception:
        return None


def idle_seconds() -> float:
    """距离最后一次键盘/鼠标输入过了多少秒。

    GetTickCount 是 32 位毫秒计数（约 49.7 天回绕），
    这里用「按位取反加一」的差值算法，回绕也不会算错。
    """
    if os.name != "nt":
        return 0.0
    try:
        lii = _LASTINPUTINFO()
        lii.cbSize = ctypes.sizeof(_LASTINPUTINFO)
        if not _user32().GetLastInputInfo(ctypes.byref(lii)):
            return 0.0
        now = ctypes.windll.kernel32.GetTickCount() & 0xFFFFFFFF
        delta_ms = (now - lii.dwTime) & 0xFFFFFFFF
        # 差值 > 3 天基本只可能是系统长时间休眠后的异常值，按「刚操作过」处理
        if delta_ms > 3 * 24 * 3600 * 1000:
            return 0.0
        return delta_ms / 1000.0
    except Exception:
        return 0.0


def is_screensaver_running() -> bool | None:
    """屏幕保护是否正在运行（拿不到就返回 None）。"""
    if os.name != "nt":
        return None
    try:
        u32 = _user32()
        on = ctypes.c_int(0)
        if not u32.SystemParametersInfoW(SPI_GETSCREENSAVERRUNNING, 0,
                                         ctypes.byref(on), 0):
            return None
        return bool(on.value)
    except Exception:
        return None


def is_fullscreen_active() -> bool | None:
    """前台窗口是否铺满了所在的显示器（全屏视频 / 会议 / 演示的典型特征）。

    这是「空闲 ≠ 没人」的关键防线：看视频、开会的人可以十分钟不碰键鼠，
    空闲计时器照样增长，但屏幕上放着全屏画面——这时不能打扰。
    """
    if os.name != "nt":
        return None
    try:
        u32 = _user32()
        hwnd = u32.GetForegroundWindow()
        if not hwnd:
            return False
        hmon = u32.MonitorFromWindow(ctypes.c_void_p(hwnd),
                                     MONITOR_DEFAULTTONEAREST)
        if not hmon:
            return False
        mi = _MONITORINFO()
        mi.cbSize = ctypes.sizeof(_MONITORINFO)
        if not u32.GetMonitorInfoW(ctypes.c_void_p(hmon), ctypes.byref(mi)):
            return None
        r = _RECT()
        if not u32.GetWindowRect(ctypes.c_void_p(hwnd), ctypes.byref(r)):
            return None
        m = mi.rcMonitor
        tol = 4       # 允许几个像素的边框误差
        return (abs(r.left - m.left) <= tol and abs(r.top - m.top) <= tol
                and abs(r.right - m.right) <= tol
                and abs(r.bottom - m.bottom) <= tol)
    except Exception:
        return None


def truly_away(min_idle_seconds: float) -> tuple[bool, str]:
    """综合判定「电脑是不是真的没人在用」。

    判定顺序：
      ① 锁屏 → 确定没人；② 屏保运行 → 确定没人；
      ③ 前台有全屏窗口 → 大概率在看视频/开会/演示，判定有人；
      ④ 空闲超过 min_idle_seconds → 判定没人。

    :return: (是否真无人, 判定原因——直接可写进日志)
    """
    if os.name != "nt":
        idle = idle_seconds()
        return idle >= min_idle_seconds, f"空闲 {human(idle)}"
    if is_locked():
        return True, "屏幕已锁定"
    scr = is_screensaver_running()
    if scr:
        return True, "屏幕保护运行中"
    if is_fullscreen_active():
        return False, "前台是全屏窗口（可能在看视频或开会）"
    idle = idle_seconds()
    if idle >= min_idle_seconds:
        return True, f"已空闲 {human(idle)}，无全屏播放"
    return False, f"仅空闲 {human(idle)}（判定线 {human(min_idle_seconds)}）"


def human(seconds: float) -> str:
    """把秒数写成「1 小时 3 分」这种好读的形式。"""
    s = int(max(0, seconds))
    if s < 60:
        return f"{s} 秒"
    if s < 3600:
        return f"{s // 60} 分 {s % 60} 秒"
    return f"{s // 3600} 小时 {(s % 3600) // 60} 分"


def wait_until_idle(need_seconds: float, poll_seconds: float = 20,
                    max_wait_seconds: float = 0,
                    should_stop=None, on_tick=None) -> bool:
    """阻塞等待，直到连续空闲达到 need_seconds。

    :param max_wait_seconds: >0 时最多等这么久（等不到也放行）；0 表示一直等。
    :param should_stop: 可调用对象，返回 True 时立刻放弃等待。
    :param on_tick: 每轮回调（用来打日志），签名 on_tick(idle, waited)。
    :return: True=可以执行；False=被停止。
    """
    started = time.time()
    poll = max(1.0, float(poll_seconds))
    while True:
        if should_stop is not None and should_stop():
            return False
        idle = idle_seconds()
        waited = time.time() - started
        if on_tick is not None:
            on_tick(idle, waited)
        if idle >= need_seconds:
            return True
        if max_wait_seconds and waited >= max_wait_seconds:
            return True
        # 用「还差多少」和 poll 取小，避免刚过阈值还要多等一整轮
        gap = max(1.0, min(poll, need_seconds - idle + 1.0))
        # 分段睡，停止按钮能及时生效
        deadline = time.time() + gap
        while time.time() < deadline:
            if should_stop is not None and should_stop():
                return False
            time.sleep(min(0.5, max(0.05, deadline - time.time())))
