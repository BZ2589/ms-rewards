"""
零依赖 HTTP 客户端 —— 基于 Python 标准库 urllib / http.cookiejar。
对外提供接近 requests 的最小接口，方便在没有 pip 环境的机器 / 服务器上直接运行。

对应原油猴脚本里的 Utils.xhr()（GM_xmlhttpRequest 封装）。
"""
from __future__ import annotations

import gzip
import http.cookiejar
import io
import json as _json
import locale
import os
import random
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any


def _make_ssl_context() -> ssl.SSLContext:
    """构建可用的 TLS 上下文：逐张加载 Windows 系统证书，跳过解析失败的证书。

    坑：conda-forge 的 Python 3.9（OpenSSL 3.6.3）在 create_default_context() 里
    一次性加载 Windows 证书库时，遇到库中某张格式异常的证书会整体抛
    `SSLError: [ASN1: NOT_ENOUGH_DATA] not enough data`，导致本环境**所有**
    HTTPS 请求都失败（已在真实机器上复现）。逐张 load_verify_locations 并
    忽略坏证书即可绕过，同时保留完整的服务器证书校验。
    """
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED
    loaded = 0
    if os.name == "nt":
        for store in ("CA", "ROOT"):
            try:
                certs = ssl.enum_certificates(store)
            except (ssl.SSLError, OSError):
                continue
            for der, enc, _typ in certs:
                try:
                    ctx.load_verify_locations(cadata=ssl.DER_cert_to_PEM_cert(der))
                    loaded += 1
                except (ssl.SSLError, ValueError):
                    continue        # 跳过 Windows 证书库里的坏证书
    else:
        try:
            ctx.load_default_certs()
            loaded = -1             # 非 Windows 平台走默认逻辑
        except ssl.SSLError:
            pass
    if loaded == 0:
        # 一张系统证书都没拿到时再退回默认上下文（宁可报证书错也不裸奔）
        try:
            return ssl.create_default_context()
        except ssl.SSLError:
            pass
    return ctx


class Utf8CookieJar(http.cookiejar.MozillaCookieJar):
    """cookies.txt 一律按 UTF-8 读写，绕开系统默认编码（中文 Windows 是 GBK）。

    坑：Python 3.9 的 FileCookieJar.load() / MozillaCookieJar.save() 内部是
    `open(filename)`（**不带 encoding**），用的是 locale 默认编码。中文 Windows 上
    默认 GBK，于是：
      * 读我们写的 UTF-8 cookies.txt（含中文注释或非 ASCII Cookie 值）→
        UnicodeDecodeError: 'gbk' codec can't decode byte 0x..
      * 写含非 ASCII 值的 Cookie → UnicodeEncodeError。
    实测就是「一键登录读到 Cookie 后、验证登录态时炸掉」。所以这里覆写 load/save，
    显式用 UTF-8；读取时还兼容历史/第三方导出的 GBK、latin-1 文件。
    """

    @staticmethod
    def _read_text(path: str) -> str:
        with open(path, "rb") as f:
            raw = f.read()
        text = ""
        for enc in ("utf-8-sig", "utf-8", locale.getpreferredencoding(False), "latin-1"):
            try:
                text = raw.decode(enc)
                break
            except (UnicodeDecodeError, LookupError):
                continue
        else:
            text = raw.decode("utf-8", "replace")
        # 标准库是文本模式读（通用换行），这里走二进制+StringIO，必须自己把 CRLF 归一，
        # 否则 Windows 换行的 cookies.txt 会让最后一个字段（value）带回车。
        return text.replace("\r\n", "\n").replace("\r", "\n")

    def load(self, filename=None, ignore_discard=False, ignore_expires=False):
        if filename is None:
            if self.filename is not None:
                filename = self.filename
            else:
                raise ValueError(http.cookiejar.MISSING_FILENAME_TEXT)
        text = self._read_text(filename)
        self._really_load(io.StringIO(text), filename, ignore_discard, ignore_expires)

    def save(self, filename=None, ignore_discard=False, ignore_expires=False):
        if filename is None:
            if self.filename is not None:
                filename = self.filename
            else:
                raise ValueError(http.cookiejar.MISSING_FILENAME_TEXT)
        lines = [self.header]
        now = time.time()
        for cookie in self:
            if not ignore_discard and cookie.discard:
                continue
            if not ignore_expires and cookie.is_expired(now):
                continue
            secure = "TRUE" if cookie.secure else "FALSE"
            initial_dot = "TRUE" if cookie.domain.startswith(".") else "FALSE"
            expires = "" if cookie.expires is None else str(cookie.expires)
            if cookie.value is None:            # 与标准库行为保持一致
                name, value = "", cookie.name
            else:
                name, value = cookie.name, cookie.value
            lines.append("\t".join([cookie.domain, initial_dot, cookie.path,
                                    secure, expires, name, value]))
        text = "\n".join(lines) + "\n"
        tmp = filename + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.replace(tmp, filename)


@dataclass
class Response:
    status: int
    url: str                      # 跟随重定向之后的最终 URL（OAuth 自动获码要用）
    text: str
    headers: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def json(self) -> Any:
        return _json.loads(self.text)

    def __bool__(self) -> bool:
        return self.ok


class HttpError(Exception):
    """非 2xx 响应。保留 body 便于排查 Token / 401 之类的错误。"""

    def __init__(self, status: int, url: str, body: str = ""):
        self.status = status
        self.url = url
        self.body = body
        super().__init__(f"HTTP {status} @ {url}")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _make_cookie(version, name, value, domain, path="/"):
    return http.cookiejar.Cookie(
        version, name, value,
        None, False,                       # port, port_specified
        domain, True, domain.startswith("."),
        path, True,
        False,                             # secure
        None, False,                       # expires, discard
        None, None, {}, False,
    )


class HttpClient:
    # 发往这些域名的请求强制排队：两条之间至少隔 min_gap 秒（再叠一点随机抖动）。
    # 目的不是省流量，而是别在微软眼里变成「一秒十几条」的脚本行为 —— 那样不但
    # 我们自己容易被风控，同一个账号在手机/浏览器上也会被连累得打不开页面。
    PACE_HOSTS = ("bing.com", "microsoft.com", "live.com", "msn.com")

    def __init__(self, cookie_file: str | None = None, timeout: int = 15,
                 min_gap: float = 1.2):
        self.timeout = timeout
        self.cookie_file = cookie_file
        self.min_gap = max(0.0, float(min_gap))
        self._last_at = 0.0                  # 上一条「微软系」请求的发出时刻
        # trace(method, url, status, elapsed_ms, body_preview, error) —— 由上层注入，用于详细日志
        self.trace = None
        self.jar = Utf8CookieJar(cookie_file)
        self._ctx = _make_ssl_context()
        self._build_openers()

    def _build_openers(self) -> None:
        self._follow = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar),
            urllib.request.HTTPSHandler(context=self._ctx),
            urllib.request.HTTPRedirectHandler(),
        )
        self._nofollow = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar),
            urllib.request.HTTPSHandler(context=self._ctx),
            _NoRedirect(),
        )

    # ---------- 底层 ----------
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict | None = None,
        params: dict | None = None,
        data: Any = None,
        json_body: Any = None,
        cookies: dict | None = None,
        timeout: int | None = None,
        allow_redirects: bool = True,
        raise_on_error: bool = True,
        retries: int = 0,
        retry_on: tuple = (429, 500, 502, 503, 504),
    ) -> Response:
        if params:
            url = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(params, doseq=True)

        hdrs: dict[str, str] = {
            "Accept": "*/*",
            "Accept-Encoding": "identity",   # 不让服务器 gzip，省去解压分支
            # 每条请求都自带 Connection: close —— 办完就断，不跟服务器留长连接
            "Connection": "close",
        }
        if headers:
            hdrs.update({k: str(v) for k, v in headers.items() if v is not None})

        body: bytes | None = None
        if json_body is not None:
            body = _json.dumps(json_body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
            hdrs.setdefault("Content-Type", "application/json; charset=UTF-8")
        elif data is not None:
            if isinstance(data, (dict, list, tuple)):
                body = urllib.parse.urlencode(data, doseq=True).encode("utf-8")
                hdrs.setdefault("Content-Type", "application/x-www-form-urlencoded; charset=UTF-8")
            elif isinstance(data, str):
                body = data.encode("utf-8")
            else:
                body = bytes(data)
        if cookies:
            hdrs["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())

        attempts = max(1, int(retries) + 1)
        for attempt in range(attempts):
            self._pace(url)                      # 微软系请求排队，别连发
            req = urllib.request.Request(url, data=body, headers=hdrs, method=method.upper())
            opener = self._follow if allow_redirects else self._nofollow
            t0 = time.time()

            def _trace(status, final_url, text, err=None, _t0=t0):
                if self.trace:
                    try:
                        self.trace(method.upper(), final_url, status,
                                   (time.time() - _t0) * 1000, text, err)
                    except Exception:
                        pass  # 日志回调绝不能影响主流程

            try:
                with opener.open(req, timeout=timeout or self.timeout) as resp:
                    raw = resp.read()
                    if resp.headers.get("Content-Encoding", "").lower() == "gzip":
                        raw = gzip.decompress(raw)
                    text = raw.decode("utf-8", errors="replace")
                    result = Response(resp.status, resp.geturl(), text, dict(resp.headers))
                    _trace(result.status, result.url, text)
                    return result
            except urllib.error.HTTPError as e:
                raw = e.read()
                text = raw.decode("utf-8", errors="replace")
                final = getattr(e, "url", url)
                _trace(e.code, final, text)
                if raise_on_error and e.code in retry_on and attempt < attempts - 1:
                    time.sleep(1.2 * (attempt + 1))       # 5xx / 429 退避重试
                    continue
                if not raise_on_error or e.code in (301, 302, 303, 307, 308):
                    return Response(e.code, final, text, dict(e.headers))
                raise HttpError(e.code, final, text[:500]) from None
            except Exception as e:
                _trace(0, url, "", f"{type(e).__name__}: {e}")
                if attempt < attempts - 1:                # 网络抖动也重试
                    time.sleep(1.2 * (attempt + 1))
                    continue
                raise
        raise HttpError(0, url, "重试次数用尽")

    def get(self, url: str, **kw) -> Response:
        return self.request("GET", url, **kw)

    def post(self, url: str, **kw) -> Response:
        return self.request("POST", url, **kw)

    # ---------- 会话 / 节奏 ----------
    def _pace(self, url: str) -> None:
        """微软系域名两条请求之间至少隔 min_gap 秒（+0~0.6 秒随机抖动）。"""
        if self.min_gap <= 0:
            return
        host = (urllib.parse.urlparse(url).hostname or "").lower()
        if not any(host == h or host.endswith("." + h) or host.endswith(h)
                   for h in self.PACE_HOSTS):
            return
        wait = self.min_gap + random.uniform(0, 0.6) - (time.time() - self._last_at)
        if wait > 0:
            time.sleep(wait)
        self._last_at = time.time()

    def close(self) -> None:
        """把这次会话彻底放掉（opener 关掉、连接释放）。

        程序是「用到才连、用完就走」：每个操作结束都会调它，下次再重新建立
        连接并从 cookies.txt 读回登录态，服务器侧不会看到一个长期挂着的会话。
        关掉后会重建一对空 opener，保证同一个对象还能继续用（不会因为关闭而报废）。
        """
        for op in (getattr(self, "_follow", None), getattr(self, "_nofollow", None)):
            try:
                op.close()
            except Exception:
                pass
        self._build_openers()
        self._last_at = 0.0

    # ---------- Cookie 管理 ----------
    def load_netscape(self, path: str) -> int:
        """加载浏览器导出的 Netscape 格式 cookies.txt。"""
        self.jar.load(path, ignore_discard=True, ignore_expires=True)
        return len(self.jar)

    def set_cookie_string(self, raw: str, domains=(".bing.com", ".live.com", ".microsoft.com")) -> int:
        """粘贴 DevTools 里复制的 `a=b; c=d` 形式 Cookie 字符串。"""
        count = 0
        for part in raw.split(";"):
            if "=" not in part:
                continue
            name, value = part.strip().split("=", 1)
            if not name:
                continue
            for domain in domains:
                self.jar.set_cookie(_make_cookie(0, name, value.strip(), domain))
            count += 1
        return count

    def save_cookies(self, path: str | None = None) -> None:
        target = path or self.cookie_file
        if target:
            self.jar.save(target, ignore_discard=True, ignore_expires=True)
