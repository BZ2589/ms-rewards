"""
浏览器登录态自动采集 —— 通过 Chrome DevTools Protocol (CDP) 读取 Cookie。

设计目标：用户在弹出的独立浏览器窗口里登录一次，程序自动把完整 Cookie
（**包括手动复制拿不到的 HttpOnly Cookie**）和 OAuth 授权码收回来，全程零复制粘贴。

纯标准库实现：socket 手写 RFC6455 WebSocket 客户端，不依赖
selenium / playwright / websocket-client / requests / PyQtWebEngine。
因此 PyInstaller 打包和 Python 3.8+ 均无额外依赖。

隐私边界：
  * 使用项目目录下的独立 profile（.browser-profile），不碰日常浏览器配置；
  * CDP 只监听 127.0.0.1；
  * 只读取 Cookie 与标签页 URL，不读取密码、历史、书签；
  * Cookie 明文只写入本目录 cookies.txt，日志里全部脱敏。
"""
from __future__ import annotations

import base64
import http.cookiejar
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable

LOGIN_URL = "https://rewards.bing.com/"
PROBE_URLS = [
    "https://rewards.bing.com/", "https://www.bing.com/", "https://cn.bing.com/",
    "https://login.live.com/", "https://account.live.com/",
    "https://www.microsoft.com/", "https://rewards.bing.com/dashboard",
]
# 只要 Cookie 落到这些域，就与 Rewards 登录态有关
RELEVANT_SUFFIXES = (
    ".bing.com", ".live.com", ".microsoft.com", ".msn.com",
    ".office.com", ".microsoftonline.com", ".bing.net",
)
# 微软账号登录后才会出现的认证 Cookie。
# 注意：MUID / MUIDB / _EDGE_S / GRNID / _EDGE_V 等**匿名浏览也会写入**（已实测），
# 绝不能当作登录标志，否则会误判成已登录。
LOGIN_MARKERS = {
    "_U", "KievRPSAuthEng", "MSPAuth", "MSPProf", "MSPPre", "WLSSC",
    "ESTSAUTH", "ESTSAUTHPERSISTENT", "AADSSO", "RPSAuth",
}

LogFn = Callable[[str, str, str], None]          # (level, icon, msg)


def _noop(*_args, **_kwargs):
    pass


# ---------------------------------------------------------------- 浏览器定位
def find_browser() -> str:
    """返回本机 Edge / Chrome 可执行文件；找不到则抛异常。"""
    candidates = [
        os.path.join(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                     "Microsoft", "Edge", "Application", "msedge.exe"),
        os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"),
                     "Microsoft", "Edge", "Application", "msedge.exe"),
        os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"),
                     "Google", "Chrome", "Application", "chrome.exe"),
        os.path.join(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                     "Google", "Chrome", "Application", "chrome.exe"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""),
                     "Google", "Chrome", "Application", "chrome.exe"),
    ]
    for p in candidates:
        if p and os.path.isfile(p):
            return p
    for name in ("msedge", "chrome", "msedge.exe", "chrome.exe"):
        p = shutil.which(name)
        if p:
            return p
    raise RuntimeError("未找到 Edge / Chrome 浏览器，请改用「手动复制」方式")


def _free_port(start: int = 9222, span: int = 30) -> int:
    for port in range(start, start + span):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError("9222-9251 端口全被占用，无法启动调试浏览器")


# ---------------------------------------------------------------- 迷你 WebSocket
class _MiniWS:
    """够用就好的 RFC6455 客户端：握手 + 文本帧收发（客户端必须 mask）。"""

    def __init__(self, ws_url: str, timeout: float = 8.0):
        u = urllib.parse.urlparse(ws_url)
        host = "127.0.0.1"                     # 强制 IPv4，绕开 localhost→::1
        port = u.port or 80
        path = u.path or "/"
        if u.query:
            path += "?" + u.query
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        req = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(req.encode("ascii"))
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("CDP WebSocket 握手失败（连接提前关闭）")
            buf += chunk
        head, self._buf = buf.split(b"\r\n\r\n", 1)
        if b"101" not in head.split(b"\r\n", 1)[0]:
            raise ConnectionError("CDP WebSocket 升级被拒绝: " + head[:120].decode("ascii", "replace"))
        self._id = 0

    def _read(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("CDP 连接已关闭")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def _frame(self, payload: bytes, opcode: int = 1):
        header = bytearray([0x80 | opcode])
        n = len(payload)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        mask = os.urandom(4)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(header) + masked)

    def _next_message(self) -> str | None:
        """读一条完整文本消息（处理 ping / 分片 / 二进制）。"""
        parts: list[bytes] = []
        while True:
            b1, b2 = self._read(2)
            fin, opcode = b1 & 0x80, b1 & 0x0F
            masked, n = b2 & 0x80, b2 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._read(8))[0]
            mask = self._read(4) if masked else None
            data = self._read(n)
            if mask:
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
            if opcode == 9:                       # ping → pong
                self._frame(data, 10)
                continue
            if opcode == 8:
                raise ConnectionError("CDP 主动关闭连接")
            if opcode in (1, 2, 0):
                parts.append(data)
                if fin:
                    return b"".join(parts).decode("utf-8", "replace")

    def call(self, method: str, params: dict | None = None, timeout: float = 8.0) -> dict:
        self._id += 1
        rid = self._id
        self.sock.settimeout(timeout)
        self._frame(json.dumps({"id": rid, "method": method, "params": params or {}},
                               ensure_ascii=False).encode("utf-8"))
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                raw = self._next_message()
            except socket.timeout:
                break
            if not raw:
                continue
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            if msg.get("id") != rid:              # 事件通知，跳过
                continue
            if "error" in msg:
                raise RuntimeError(f"CDP {method} 返回错误: {msg['error']}")
            return msg.get("result") or {}
        raise TimeoutError(f"CDP {method} 超时")

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass


# ---------------------------------------------------------------- CDP 会话
class CDP:
    def __init__(self, port: int, log: LogFn = _noop):
        self.port = port
        self.log = log
        self._ws: _MiniWS | None = None

    def _http(self, path: str, method: str = "GET", timeout: float = 2.5):
        url = f"http://127.0.0.1:{self.port}{path}"
        req = urllib.request.Request(url, method=method)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace") or "{}")

    def ready(self) -> bool:
        # 先用 TCP 快速排除 99% 的空闲端口（连接被拒绝是瞬时的），
        # 否则扫描端口段时每个端口都要等一次 HTTP 超时，会卡几十秒。
        try:
            with socket.create_connection(("127.0.0.1", self.port), timeout=0.25):
                pass
        except OSError:
            return False
        try:
            v = self._http("/json/version", timeout=0.8)
            return bool(v.get("Browser"))
        except Exception:
            return False

    def wait_ready(self, seconds: float = 25) -> bool:
        end = time.time() + seconds
        while time.time() < end:
            if self.ready():
                return True
            time.sleep(0.35)
        return False

    def targets(self) -> list[dict]:
        try:
            data = self._http("/json/list")
            return data if isinstance(data, list) else []
        except Exception as e:
            self.log("DEBUG", "🧩", f"读取 CDP 标签列表失败: {type(e).__name__}: {e}")
            return []

    def open_tab(self, url: str) -> dict:
        # Chrome/Edge 新版要求 PUT /json/new?<url>
        return self._http("/json/new?" + urllib.parse.quote(url, safe=""), method="PUT", timeout=5)

    def _connect(self) -> _MiniWS:
        if self._ws:
            return self._ws
        pages = [t for t in self.targets()
                 if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
        if not pages:
            raise RuntimeError("调试浏览器里没有可用标签页")
        pages.sort(key=lambda t: (("bing.com" in t.get("url", "")) or
                                  ("live.com" in t.get("url", ""))), reverse=True)
        ws_url = pages[0]["webSocketDebuggerUrl"]
        u = urllib.parse.urlparse(ws_url)
        ws_url = urllib.parse.urlunparse(("ws", f"127.0.0.1:{self.port}", u.path, "", u.query, ""))
        self._ws = _MiniWS(ws_url)
        self.log("DEBUG", "🧩", f"CDP 已连接标签页: {pages[0].get('url', '')[:70]}")
        return self._ws

    def call(self, method: str, params: dict | None = None, timeout: float = 8.0) -> dict:
        try:
            return self._connect().call(method, params, timeout)
        except (ConnectionError, OSError, TimeoutError, RuntimeError):
            # 标签页被关掉时换一个再试一次
            self.close()
            return self._connect().call(method, params, timeout)

    def all_cookies(self) -> list[dict]:
        """优先 getAllCookies（全局 jar），失败回退 getCookies（指定 URL 集合）。"""
        try:
            r = self.call("Network.getAllCookies")
            cookies = r.get("cookies") or []
            if cookies:
                return cookies
        except Exception as e:
            self.log("DEBUG", "🧩", f"Network.getAllCookies 不可用({type(e).__name__})，回退 getCookies")
        r = self.call("Network.getCookies", {"urls": PROBE_URLS})
        return r.get("cookies") or []

    def find_auth_code(self) -> str:
        """从已打开标签页的地址栏里找 OAuth 授权跳转后的 code。"""
        for t in self.targets():
            url = t.get("url", "")
            if "code=" not in url:
                continue
            host = urllib.parse.urlparse(url).netloc.lower()
            if not any(k in host for k in ("live.com", "bing.com", "microsoft.com", "localhost", "127.0.0.1")):
                continue
            code = urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get("code", [""])[0]
            if code and len(code) > 10:
                return code
        return ""

    def close(self):
        if self._ws:
            self._ws.close()
            self._ws = None


class BrowserSession:
    """带调试端口的独立浏览器窗口；profile 持久化，登录一次以后可直接刷新。"""

    def __init__(self, base_dir: str, log: LogFn = _noop, port: int | None = None):
        self.base_dir = base_dir
        self.profile = os.path.join(base_dir, ".browser-profile")
        self.log = log
        self.port = port or _free_port()
        self.cdp = CDP(self.port, log)
        self.proc: subprocess.Popen | None = None
        self.exe = ""

    def _existing_debug_port(self) -> int:
        """并行扫描端口段里已经活着的 CDP 实例（上次没关的登录窗口）。

        串行探测在本机可能要等满每个超时（未监听端口被静默丢包），
        并行后最坏只需一个超时的时间。
        """
        from concurrent.futures import ThreadPoolExecutor
        ports = [p for p in range(9222, 9252) if p != self.port]

        def probe(p: int) -> int:
            return p if CDP(p, self.log).ready() else 0

        with ThreadPoolExecutor(max_workers=15) as pool:
            for hit in pool.map(probe, ports):
                if hit:
                    return hit
        return 0

    def start(self, url: str = LOGIN_URL, reuse: bool = True,
              offscreen: bool = False) -> str:
        if reuse:
            if self.cdp.ready():
                self.log("DEBUG", "🧩", f"复用已存在的调试浏览器 (:{self.port})")
                try:
                    self.cdp.open_tab(url)
                except Exception:
                    pass
                return "复用已有窗口"
            # 端口被上次残留的窗口占着时，_free_port 会换端口，而 profile 又被占用，
            # 新 Edge 只会把 URL 转发给旧实例，永远等不到新端口就绪 —— 直接复用旧实例。
            old = self._existing_debug_port()
            if old:
                self.port, self.cdp = old, CDP(old, self.log)
                self.log("DEBUG", "🧩", f"发现上次留下的登录窗口 (:{old})，直接复用")
                try:
                    self.cdp.open_tab(url)
                except Exception:
                    pass
                return "复用上次留下的窗口"

        self.exe = find_browser()
        os.makedirs(self.profile, exist_ok=True)
        self._mark_clean_exit()          # 清掉上次留下的「异常关闭」标记，免得弹还原页
        args = [
            self.exe,
            f"--remote-debugging-port={self.port}",
            f"--user-data-dir={self.profile}",
            "--remote-allow-origins=*",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-networking",
            "--hide-crash-restore-bubble",   # 不弹「Microsoft Edge 未正常关闭」气泡
            "--new-window",
            url,
        ]
        if offscreen:
            # 后台任务用：窗口直接生在屏幕外，桌面上完全看不到（不会「弹一下」）。
            # 必须放在 URL 之前，Chromium 会把它当窗口参数而不是要打开的地址。
            args[args.index("--new-window"):args.index("--new-window")] = [
                "--window-position=-32000,-32000",
                "--window-size=1000,780",
            ]
        self.log("DEBUG", "🧩", f"启动调试浏览器: {os.path.basename(self.exe)} (:{self.port})"
                                + ("（屏幕外）" if offscreen else ""))
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self.proc = subprocess.Popen(
            args, cwd=self.base_dir,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=flags,
        )
        if not self.cdp.wait_ready(25):
            raise RuntimeError(
                "调试端口没能启动。可能原因：浏览器被组策略禁用调试、或已有 Edge 进程占用了配置。\n"
                "可以关掉所有 Edge 窗口后重试，或改用下方的「手动复制」方式。")
        self.log("DEBUG", "🧩", "CDP 已就绪")
        return "已打开新窗口"

    def alive(self) -> bool:
        if self.cdp.ready():
            return True
        return bool(self.proc and self.proc.poll() is None)

    def _mark_clean_exit(self) -> None:
        """把 profile 的退出标记改回「正常」，Edge 下次启动就不会弹还原页面。

        Chromium 判定「上次是否异常关闭」只看 Default/Preferences 里的
        profile.exit_type / profile.exited_cleanly。强杀进程树会把它写成 Crashed，
        于是每次启动都冒出一个「Microsoft Edge 未正常关闭」的还原气泡。
        """
        pref = os.path.join(self.profile, "Default", "Preferences")
        if not os.path.exists(pref):
            return
        try:
            with open(pref, "r", encoding="utf-8") as f:
                data = json.load(f)
            prof = data.setdefault("profile", {})
            if prof.get("exit_type") == "Normal" and prof.get("exited_cleanly") is True:
                return
            prof["exit_type"] = "Normal"
            prof["exited_cleanly"] = True
            with open(pref, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
            self.log("DEBUG", "🧩", "已清理「异常关闭」标记")
        except Exception as e:
            self.log("DEBUG", "🧩", f"清理退出标记失败({type(e).__name__})")

    def close_window(self) -> bool:
        """关闭我们启动的那个独立浏览器进程树（不影响日常浏览器）。

        返回 False 表示这个窗口不是本次启动的（复用/残留），需要用户手动关。

        先用 CDP 的 Browser.close 走正常退出流程；强杀会让 Edge 认为「上次异常
        关闭」，下次启动弹还原气泡。实在不退出才退回到杀进程树，并且无论如何
        收尾都把 profile 的退出标记写回 Normal。
        """
        owned = bool(self.proc and self.proc.poll() is None)
        if owned:
            try:
                self.cdp.call("Browser.close", {}, timeout=5)
            except Exception:
                pass
            end = time.time() + 4
            while time.time() < end and self.proc.poll() is None:
                time.sleep(0.25)
        self.cdp.close()
        if not owned:
            self.log("DEBUG", "🧩", "窗口并非本次启动，未强制关闭")
            return False
        if self.proc.poll() is None:
            try:
                import psutil
                parent = psutil.Process(self.proc.pid)
                for child in parent.children(recursive=True):
                    try:
                        child.terminate()
                    except Exception:
                        pass
                parent.terminate()
            except Exception:
                try:
                    self.proc.terminate()
                except Exception:
                    pass
            try:
                self.proc.wait(timeout=5)
            except Exception:
                pass
        self._mark_clean_exit()
        self.log("DEBUG", "🧩", "已关闭登录窗口（登录态保存在 .browser-profile）")
        return True


# ---------------------------------------------------------------- Cookie 处理
def relevant(cookies: list[dict]) -> list[dict]:
    out = []
    for c in cookies:
        domain = (c.get("domain") or "").lower().lstrip(".")
        if any(domain == s.lstrip(".") or domain.endswith(s) for s in RELEVANT_SUFFIXES):
            out.append(c)
    return out


def login_markers(cookies: list[dict]) -> list[str]:
    """返回命中的登录标志 Cookie 名；空列表 = 还没登录。"""
    names = {c.get("name", "") for c in cookies}
    return sorted(names & LOGIN_MARKERS)


def cookies_to_netscape(cookies: list[dict], only_relevant: bool = True) -> str:
    """CDP cookie 列表 → Netscape cookies.txt 文本（保留 domain / HttpOnly / Secure）。

    文件头一律写 **纯 ASCII 英文注释**：标准库 cookiejar 在中文 Windows 上会用 GBK
    读取 cookies.txt，一旦文件里有中文注释就会 UnicodeDecodeError（已实测踩过）。
    数据行本身也尽量保持可被任何工具读取。
    """
    data = relevant(cookies) if only_relevant else cookies
    now = int(time.time())
    lines = ["# Netscape HTTP Cookie File",
             "# Auto-collected by MS Rewards Console (Chrome DevTools Protocol).",
             "# HttpOnly is a browser-side flag and does not affect Python HTTP clients;",
             "# the '#HttpOnly_' prefix is skipped by http.cookiejar, so lines are plain.",
             ""]
    for c in data:
        name = str(c.get("name", "")).strip()
        value = str(c.get("value", ""))
        domain = (c.get("domain") or "").strip()
        if not name or not domain:
            continue
        path = (c.get("path") or "/").replace("\t", " ")
        exp = c.get("expires")
        try:
            exp = int(float(exp))
        except (TypeError, ValueError):
            exp = -1
        if exp <= 0:                              # 会话 Cookie → 给一年，避免被当成过期
            exp = now + 365 * 86400
        prefix = ""                               # 见文件头注释：不写 #HttpOnly_ 前缀
        domain_flag = "TRUE" if domain.startswith(".") else "FALSE"
        secure = "TRUE" if c.get("secure") else "FALSE"
        value = value.replace("\t", " ").replace("\r", " ").replace("\n", " ")
        lines.append(f"{prefix}{domain}\t{domain_flag}\t{path}\t{secure}\t{exp}\t{name}\t{value}")
    return "\n".join(lines) + "\n"


def cookie_header(cookies: list[dict]) -> str:
    """生成 `a=b; c=d` 串（仅用于兼容旧接口 / 调试，日志里必须脱敏）。"""
    seen, parts = set(), []
    for c in cookies:
        n = c.get("name", "")
        if not n or n in seen:
            continue
        seen.add(n)
        parts.append(f"{n}={c.get('value', '')}")
    return "; ".join(parts)


def normalize_cookie_input(raw: str) -> tuple[str, str]:
    """把用户可能粘回来的各种格式统一成 ('netscape'|'header', payload)。

    支持：
      1. Netscape cookies.txt 文本（含 #HttpOnly_ 行）
      2. DevTools 复制的 `Cookie: a=b; c=d` 或纯 `a=b; c=d`
      3. 浏览器扩展 / CDP 导出的 JSON 数组 [{"name","value","domain",...}]
      4. 一行一个 `name=value`（部分 F12 面板的复制格式）
      5. 本地 cookies.txt 文件路径
    """
    s = (raw or "").strip()
    if not s:
        raise ValueError("内容是空的，没有可导入的 Cookie")
    if len(s) < 4096 and os.path.isfile(s):
        with open(s, "r", encoding="utf-8", errors="ignore") as f:
            return "netscape", f.read()

    # JSON 数组（扩展导出 / CDP 格式）
    if s.startswith("["):
        try:
            arr = json.loads(s)
            if isinstance(arr, list) and arr and isinstance(arr[0], dict) and "name" in arr[0]:
                return "netscape", cookies_to_netscape(arr, only_relevant=False)
        except ValueError:
            pass

    # Netscape 文本：存在制表符分隔的域名行
    if re.search(r"(?m)^#?HttpOnly_?\.?[\w.-]+\t(TRUE|FALSE)\t", s) or \
       re.search(r"(?m)^\.?[\w.-]+\.(com|net|org)\t(TRUE|FALSE)\t", s):
        return "netscape", s if s.startswith("#") else "# Netscape HTTP Cookie File\n" + s

    s = re.sub(r"^\s*cookie\s*:\s*", "", s, flags=re.I)
    if ";" in s:
        parts = [p.strip() for p in s.replace("\r", "").replace("\n", ";").split(";")]
        kv = [p for p in parts if "=" in p and not p.startswith("#")]
        if kv:
            return "header", "; ".join(kv)

    lines = [l.strip() for l in s.splitlines() if l.strip() and not l.startswith("#")]
    kv = [l for l in lines if "=" in l and " " not in l.split("=")[0]]
    if kv:
        return "header", "; ".join(kv)
    raise ValueError("没识别出 Cookie 格式。请复制 Network 面板里的整串 Cookie 请求头")


def save_cookies(kind: str, payload: str, cookie_path: str, api=None) -> int:
    """写入 cookies.txt，返回条数。netscape 直接落盘；header 走 HttpClient 展开到相关域。"""
    if kind == "netscape":
        from http_client import Utf8CookieJar        # 同目录模块，显式 UTF-8 读写
        # 浏览器扩展导出的文件常用 #HttpOnly_ 前缀；cookiejar 会把它当注释跳过，先剥掉。
        cleaned = [(l[10:] if l.startswith("#HttpOnly_") else l)
                   for l in payload.splitlines()]
        text = "# Netscape HTTP Cookie File\n" + "\n".join(cleaned) + "\n"
        with open(cookie_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        jar = Utf8CookieJar()
        jar.load(cookie_path, ignore_discard=True, ignore_expires=True)   # 顺带校验格式
        return len(jar)
    if api is None:
        raise ValueError("header 格式需要传入 api")
    n = api.http.set_cookie_string(payload)
    api.http.save_cookies(cookie_path)
    return n


# ---------------------------------------------------------------- 自检
def selftest() -> int:
    import tempfile
    ok = []
    sample = [
        {"name": "_U", "value": "abc123", "domain": ".bing.com", "path": "/",
         "expires": -1, "httpOnly": True, "secure": True},
        {"name": "MUID", "value": "0F12", "domain": ".bing.com", "path": "/",
         "expires": 1999999999, "httpOnly": False, "secure": False},
        {"name": "KievRPSAuthEng", "value": "xyz", "domain": ".login.live.com",
         "path": "/", "expires": -1, "httpOnly": True, "secure": True},
        {"name": "unrelated", "value": "1", "domain": ".google.com", "path": "/",
         "expires": -1},
    ]
    txt = cookies_to_netscape(sample)
    assert ".bing.com\tTRUE\t/\tTRUE" in txt and "_U\tabc123" in txt
    assert "unrelated" not in txt
    assert not any(l.startswith("#HttpOnly_") for l in txt.splitlines()), "数据行不应带前缀"
    assert len(relevant(sample)) == 3
    ok.append("Netscape 生成 / HttpOnly 兼容处理 / 域名过滤")

    assert "KievRPSAuthEng" in login_markers(sample) and "_U" in login_markers(sample)
    anonymous = [{"name": n} for n in (
        "GC", "GRNID", "MUID", "MUIDB", "_EDGE_S", "_EDGE_V",
        "MicrosoftApplicationsTelemetryDeviceId", "ai_session", "rn_SID")]
    assert login_markers([{"name": "MUID"}]) == []
    assert login_markers(anonymous) == [], "匿名 Cookie 被误判为已登录"
    ok.append("登录标志判定（含匿名 Cookie 不误判）")

    # CDP 标签页 URL 里提取 OAuth code
    fake = [{"url": "https://rewards.bing.com/about"},
            {"url": "https://login.live.com/oauth20_desktop.srf?code=M.C123_BL2.abc&lc=2052"}]
    class _FakeCDP(CDP):
        def targets(self): return fake
    assert _FakeCDP(9222).find_auth_code() == "M.C123_BL2.abc"
    assert _FakeCDP(9222).find_auth_code() != ""
    fake[1]["url"] = "https://login.live.com/oauth20_desktop.srf?error=access_denied"
    assert _FakeCDP(9222).find_auth_code() == ""
    ok.append("OAuth 授权码自动捕获解析")

    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "cookies.txt")
        n = save_cookies("netscape", txt, p)
        assert n == 3, n
        ok.append(f"cookiejar 可回读（{n} 条）")

    cases = [
        ("a=b; c=d; e=f", "header"),
        ("Cookie: a=b; c=d", "header"),
        (".bing.com\tTRUE\t/\tTRUE\t1999999999\t_U\tabc", "netscape"),
        ('[{"name":"_U","value":"v","domain":".bing.com","path":"/"}]', "netscape"),
        ("a=b\nc=d", "header"),
    ]
    for raw, want in cases:
        kind, payload = normalize_cookie_input(raw)
        assert kind == want, (raw, kind, want)
        assert payload.strip()
        ok.append(f"粘贴识别: {raw[:26]!r} → {kind}")

    try:
        normalize_cookie_input("随便一段文字")
        raise AssertionError("应拒绝无效输入")
    except ValueError:
        ok.append("无效输入被拒绝")

    exe = find_browser()
    ok.append("浏览器定位: " + os.path.basename(exe))

    for i, s in enumerate(ok, 1):
        print(f"  {i}. OK  {s}")
    print(f"=== browser_login 自检通过（{len(ok)} 项）===")
    return 0


if __name__ == "__main__":
    sys.exit(selftest())
