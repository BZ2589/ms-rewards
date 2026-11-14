"""
通知推送 —— 对应原油猴脚本的 Utils.sendWebhook() / Webhooks 配置。

渠道（notify 里的键）：wecom / dingtalk / feishu / pushme / bark 填 key 或完整
Webhook URL；win 是 Windows 系统通知（本机右下角弹窗，True/False，无需 key）。
每一条能推送的消息都有独立开关（notify.categories，见 SWITCH_RESULT / SWITCH_SYSTEM）。
"""
from __future__ import annotations

import base64
import os
import subprocess
import time
from urllib.parse import parse_qs, urlparse

# ---- 通知开关定义（GUI「通知配置」卡直接用这两份；key 即 notify.categories 的键）----
# 任务成果：该任务的成功与失败都算这一条
SWITCH_RESULT = [
    ("sign", "签到完成", "App/PC 签到成功或失败"),
    ("read", "阅读完成", "新闻阅读达标或未达标（含原因）"),
    ("search", "搜索完成", "PC 搜索达标或未达标（含原因）"),
    ("cards", "活动卡片", "活动卡片 / 每日活动的完成情况（含失败原因）"),
    ("claim", "待领积分", "待领积分领取成功或失败"),
    ("lane", "优先快线", "守护开头快线（签到+1 搜索+3 卡）的完成情况"),
    ("summary", "每日汇总", "每轮结束的 📊 汇总（各任务成败、积分变化、今日目标）"),
    ("test", "测试一轮", "测试目标与逐接口测试汇总"),
]
# 系统与异常
SWITCH_SYSTEM = [
    ("auth", "授权成功", "自动获取 OAuth Token 成功"),
    ("token", "Token 失效", "Token 拿不到，需要去「账号与授权」重新授权"),
    ("region", "地区异常", "IP 非大陆 / 非国内，任务被暂停"),
    ("restricted", "搜索受限", "连续多次搜索不加分，疑似被限制"),
    ("target", "明日目标", "守护每天收尾后推送明天的随机目标"),
    ("daydone", "今日收工", "今日目标全部完成后进入长睡眠，并告知次日几点开始"),
]

# 各渠道官方 Webhook 的查询参数名，用于从「完整 URL」里识别出 key
_CHANNEL_PARAM = {
    "wecom": "key",
    "dingtalk": "access_token",
    "pushme": "push_key",
}
# URL 里没有查询参数的渠道：key 在路径第几段（-1 = 最后一段）
_CHANNEL_PATH_SEG = {"bark": 0, "feishu": -1}


def extract_key(channel: str, raw: str) -> str:
    """把用户输入归一成纯 key。

    两种输入都认识：
      - 纯 key：原样返回
      - 完整 Webhook URL：提取查询参数（key= / access_token= / push_key=）；
        URL 里没带参数的（飞书 hook/<id>、Bark /<key>/…）按渠道取路径里对应的一段
    """
    raw = (raw or "").strip()
    if not raw or not raw.lower().startswith(("http://", "https://")):
        return raw
    try:
        p = urlparse(raw)
        param = _CHANNEL_PARAM.get(channel)
        if param:
            vals = parse_qs(p.query).get(param)
            if vals and vals[0].strip():
                return vals[0].strip()
        segs = [s for s in p.path.split("/") if s]
        if not segs:
            return raw
        idx = _CHANNEL_PATH_SEG.get(channel, -1)
        return segs[idx]
    except Exception:
        return raw


# ------------------------------------------------------------------ Windows 系统通知
# 纯标准库：后台起一个 PowerShell，用 NotifyIcon 的气球通知（Win10/11 会显示成系统 Toast）。
# 用 -EncodedCommand 传脚本（UTF-16LE base64），标题/正文里有什么引号换行都不会破语法。
_WIN_TOAST_PS = r"""
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$n = New-Object System.Windows.Forms.NotifyIcon
try {
  $icoPath = '__ICON__'
  if (Test-Path -LiteralPath $icoPath) { $n.Icon = New-Object System.Drawing.Icon($icoPath) }
  else { $n.Icon = [System.Drawing.SystemIcons]::Information }
  $n.Visible = $true
  $n.BalloonTipTitle = __TITLE__
  $n.BalloonTipText  = __TEXT__
  $n.ShowBalloonTip(8000) | Out-Null
  Start-Sleep -Seconds 9
} finally {
  $n.Visible = $false
  $n.Dispose()
}
"""


def _ps_quote(s: str) -> str:
    """PowerShell 单引号字符串转义。"""
    return "'" + str(s).replace("'", "''") + "'"


def _icon_path() -> str:
    """找 app.ico：源码模式在项目目录，onefile 打包后在 _MEIPASS，onedir 在 _internal。"""
    import sys
    cands = [os.path.dirname(os.path.abspath(__file__)),
             getattr(sys, "_MEIPASS", "") or "",
             os.path.dirname(os.path.abspath(sys.executable)) if getattr(sys, "frozen", False) else ""]
    for base in cands:
        if not base:
            continue
        p = os.path.join(base, "app.ico")
        if os.path.isfile(p):
            return p
    return ""


def send_win_toast(title: str, text: str) -> bool:
    """发一条 Windows 系统通知（异步，不阻塞）。成功拉起进程即返回 True。"""
    if os.name != "nt":
        return False
    try:
        ico = _icon_path()
        script = (_WIN_TOAST_PS
                  .replace("__ICON__", ico.replace("\\", "/"))
                  .replace("__TITLE__", _ps_quote(title))
                  .replace("__TEXT__", _ps_quote(text)))
        b64 = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        subprocess.Popen(
            ["powershell", "-NoProfile", "-EncodedCommand", b64],
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return True
    except Exception:
        return False


def _flag(v, default: bool = True) -> bool:
    """宽松解析开关值：bool 直接用；字符串认 0/false/off/no 为关；缺省用 default。"""
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() not in ("0", "false", "off", "no", "")


class Notifier:
    # 通知开关（config.json 的 notify.categories，通知推送页「通知配置」卡逐项开关）
    CATEGORIES = tuple(k for k, _, _ in SWITCH_RESULT + SWITCH_SYSTEM)

    def __init__(self, cfg: dict, http, log_fn):
        self.cfg = cfg or {}
        self.http = http
        self.log = log_fn
        self.title = "Microsoft Rewards 自动任务 (Python)"

    def enabled(self, category: str) -> bool:
        return bool((self.cfg.get("categories") or {}).get(category, True))

    def send(self, message: str, category: str = "summary", force: bool = False) -> None:
        """按 category（= 开关键）推送；被关掉就静默跳过（force 用于「发送测试」）。"""
        if not force and not self.enabled(category):
            self.log("🔵", f"通知「{category}」已关闭，本条不推送")
            return
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        plain = f"> {now}\n\n{message}"

        # Windows 系统通知（本机右下角弹窗，无需 key；notify.win 开关）
        if _flag(self.cfg.get("win", True)):
            if send_win_toast(self.title, message):
                self.log("🔵", "「Windows 通知」已弹出")
            else:
                self.log("🟡", "「Windows 通知」发送失败（非 Windows 或系统限制）")
        else:
            self.log("🔵", "「Windows 通知」已关闭，本条不弹")

        jobs = [
            ("企业微信", "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=", "wecom",
             {"msgtype": "text", "text": {"content": plain}}),
            ("钉钉", "https://oapi.dingtalk.com/robot/send?access_token=", "dingtalk",
             {"msgtype": "markdown", "markdown": {"title": self.title, "text": f"> {now}\n### {self.title}\n{message}"}}),
            ("飞书", "https://open.feishu.cn/open-apis/bot/v2/hook/", "feishu",
             {"msg_type": "interactive", "card": {
                 "schema": "2.0",
                 "header": {"title": {"tag": "plain_text", "content": self.title}, "template": "orange"},
                 "body": {"elements": [{"tag": "markdown", "text_align": "center",
                                        "content": f"#### {now}\n{message}"}]}}}),
            ("PushMe", "https://push.i-i.me/?push_key=", "pushme",
             {"type": "markdown", "title": f"{self.title}[#rewards!https://rewards.bing.com/rewards.png]",
              "content": f"\n{message}"}),
            ("Bark", "https://api.day.app/", "bark",
             {"group": "rewards", "icon": "https://rewards.bing.com/rewards.png",
              "title": self.title, "markdown": f"\n{message}"}),
        ]

        for name, base, key, payload in jobs:
            raw = str(self.cfg.get(key, "") or "").strip()
            if not raw:
                continue
            # 兼容两种填法：完整 Webhook URL（自动提取 key）或直接填 key
            url = base + extract_key(key, raw)
            try:
                self.http.post(url, json_body=payload, timeout=10, raise_on_error=False)
                self.log("🔵", f"「{name}」消息推送完成")
            except Exception as e:
                self.log("🔴", f"「{name}」推送出错: {e}")
