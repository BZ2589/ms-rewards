"""网络就绪检测。

开机自启动时，Windows 的网络栈常常比程序晚十几秒才就绪（Wi-Fi 关联中、DNS 未起、
VPN 还在拨号）。这时候发请求只会得到 `getaddrinfo failed` 之类的错误，日志变成一片
红，还会白白把「登录态待确认」「地区检测失败」写进状态。

所以所有会联网的入口（启动体检、守护每一轮、测试一轮、正式运行）都先过这道闸：
真连得上 443 才动手，连不上就安静地等，每 60 秒报一次，随时可被「停止」打断。

只用标准库（socket），不依赖第三方。
"""
from __future__ import annotations

import os
import socket
import time

# 只探测微软自家域名：这是真正要访问的目标，通不通才说明问题
HOSTS = (("rewards.bing.com", 443), ("cn.bing.com", 443))
_PROXY_KEYS = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy")


def tcp_ok(host: str, port: int = 443, timeout: float = 4.0) -> bool:
    """能建立 TCP 连接就算通（DNS 失败、拒绝连接、超时都算不通）。"""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False
    except Exception:
        return False


def ready(timeout: float = 4.0) -> bool:
    """网络是否可用。

    判定顺序：
      1) 能直连微软 443 → 通；
      2) 有 HTTP(S)_PROXY 环境变量时，DNS 能解析就算通（这种机器通常禁止直连 443，
         真正的请求会走代理，不能因为直连失败就无限等待）。
    """
    if any(tcp_ok(h, p, timeout) for h, p in HOSTS):
        return True
    if any(os.environ.get(k) for k in _PROXY_KEYS):
        try:
            socket.getaddrinfo(HOSTS[0][0], 443)
            return True
        except OSError:
            return False
    return False


def wait_ready(log=None, sleep=None, poll_s: float = 15.0,
               max_wait_s: float = 0, should_stop=None,
               probe_timeout: float = 3.0) -> bool:
    """阻塞等待网络就绪。

    默认「一直等」：开机后用户可能去忙别的事，几十分钟后才联网也该能自己接上。
    检查频率故意放得很松（默认 15 秒一次，探测超时 3 秒），等待期间几乎不占资源，
    日志每 5 分钟才报一句，不会把日志刷满。

    :param log: 回调 log(icon, text)；为 None 则静默
    :param sleep: 回调 sleep(seconds)，用来支持可中断等待；为 None 用 time.sleep
    :param poll_s: 多久检查一次网络
    :param max_wait_s: 最长等待秒数，0（默认）表示一直等
    :param should_stop: 回调，返回 True 则放弃等待
    :param probe_timeout: 单次探测的超时秒数（越小越省时间）
    :return: True=已就绪 / False=被停止或超时
    """
    _sleep = sleep or time.sleep

    def _nap(sec: float) -> bool:
        """分段睡（1 秒一片），保证「停止/退出」最多 1 秒内就被响应。"""
        left = sec
        while left > 0:
            if should_stop is not None and should_stop():
                return False
            step = min(1.0, left)
            _sleep(step)
            left -= step
        return True

    if ready(timeout=probe_timeout):
        return True
    t0 = time.time()
    last_report = time.time()
    if log:
        log("📡", f"网络还没就绪（DNS/连接失败），每 {int(poll_s)} 秒自动检查一次，"
                  f"通了立刻继续；期间不发任何请求")
    while True:
        if should_stop is not None and should_stop():
            if log:
                log("⏹", "等待联网时被停止")
            return False
        if max_wait_s and time.time() - t0 > max_wait_s:
            if log:
                log("🔴", f"等网络超过 {int(max_wait_s // 60)} 分钟仍未就绪，本次放弃")
            return False
        if not _nap(poll_s):
            if log:
                log("⏹", "等待联网时被停止")
            return False
        if ready(timeout=probe_timeout):
            waited = int(time.time() - t0)
            if log:
                log("✅", f"网络已连通（等了 {waited // 60} 分 {waited % 60} 秒），继续")
            return True
        if log and time.time() - last_report >= 300:
            last_report = time.time()
            mins = int((time.time() - t0) // 60)
            log("📡", f"仍在等网络…（已等 {mins} 分钟，每 {int(poll_s)} 秒自动检查一次）")
