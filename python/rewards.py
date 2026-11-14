"""
Microsoft Rewards 自动化 —— 核心层。

由 ScriptCat #6241「微软积分商城签到（全能智能重构版）」移植而来（MIT License，
原作者 liyan20001124-byte）。本文件把原油猴脚本里的
RewardsAuto / Utils / API 三个对象合并为一个 RewardsAPI 类：
  - 登录态：浏览器 Cookie（rewards.bing.com / bing.com）+ OAuth refresh_token
  - 所有任务均为纯 HTTP 请求，不需要浏览器、不需要 Playwright
"""
from __future__ import annotations

import json
import os
import random
import re
import time
import uuid
from datetime import datetime, timedelta
from urllib.parse import quote, parse_qs, urlparse, unquote

from http_client import HttpClient, HttpError
from notify import Notifier

# ---------------------------------------------------------------- 常量
UA = {
    "pc": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0",
    "mobile": "Mozilla/5.0 (Linux; Android 16; Redmi K20 Pro Build/BP4A.251205.006; ) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/144.0.7559.132 Mobile Safari/537.36 EdgA/131.0.0.0",
    "app": "Mozilla/5.0 (Linux; Android 16; Redmi K20 Pro Build/BP4A.251205.006; ) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/144.0.7559.132 Mobile Safari/537.36 BingSapphire/32.6.2110003560",
}
APP_ID = "SAAndroid/32.6.2110003560"
CHANNEL = "SAAndroid"
OFFER_READ = "ENUS_readarticle3_30points"
OFFER_PC_SIGN = "Gamification_DailyCheckIn"
# 搜索 1 次、阅读 1 篇都固定 +3 分，所以「分」总是 3 的倍数，每日目标也必须对齐到 3，
# 否则永远差 1~2 分到不了目标线。
POINT_STEP = 3

# 手机端拼图玩法：每天给 1 块，集满 12 块给 1000 分。服务器只回「已集块数」，
# 上限 12 与奖励分数是固定规则，不在接口里，所以写死在这里。
PUZZLE_MAX = 12
PUZZLE_BONUS = 1000

# 搜索上报用的固定 IID（与真实 Edge 里 widget.js 发出的完全一致，实测可用）
SEARCH_IID_HEADER = "SERP.5047"
SEARCH_IID_REPORT = "SERP.5057"

CLIENT_ID = "0000000040170455"
SCOPE = "service::prod.rewardsplatform.microsoft.com::MBI_SSL"
REDIRECT_URI = "https://login.live.com/oauth20_desktop.srf"
AUTH_URL = (
    "https://login.live.com/oauth20_authorize.srf?client_id=0000000040170455"
    "&response_type=code&scope=service::prod.rewardsplatform.microsoft.com::MBI_SSL"
    "&redirect_uri=https://login.live.com/oauth20_desktop.srf"
)
TOKEN_URL = "https://login.live.com/oauth20_token.srf"

DAPI = "https://prod.rewardsplatform.microsoft.com/dapi/me"
DAPI_ACT = "https://prod.rewardsplatform.microsoft.com/dapi/me/activities"
REWARDS = "https://rewards.bing.com"

SEARCH_POOL = [
    "what is the weather forecast tomorrow", "how do I make sourdough bread at home",
    "where can I find cheap flights to tokyo", "why is the sky blue scientific explanation",
    "how to learn rust programming in 2026", "what time does the world cup final start",
    "how to fix a leaky kitchen faucet step by step", "what are the best vr games of 2026",
    "how to start a vegetable garden in spring", "where to watch new movies this week",
    "how to take care of a bonsai tree", "what is the difference between python async and threading",
    "how to meditate properly for beginners", "what is the origin of halloween traditions",
    "how do solar panels actually work", "what is the best mechanical keyboard for typing",
    "how to tie a windsor knot tie", "what causes northern lights aurora borealis",
    "how to brew the perfect espresso at home", "what are the symptoms of vitamin d deficiency",
    "how to sleep better naturally tonight", "why do cats purr when they are happy",
    "best coffee shops in san francisco downtown", "italian restaurants near times square",
    "tokyo cherry blossom season 2026 forecast", "rtx 5070 ti benchmark vs rtx 4080 super",
    "iphone 17 release date and features", "tesla stock price today nasdaq",
    "best noise cancelling headphones under 300", "fastest electric cars 0 to 60 mph",
    "vintage camera brands collectors guide", "budget gaming laptop with rtx 4070 2026",
    "easy chocolate chip cookies recipe from scratch", "30 minute home workout routine no equipment",
    "stretching exercises for lower back pain relief", "easy origami crane folding instructions",
    "git rebase vs merge which one to use", "markdown cheat sheet with examples",
    "japanese hiragana chart pronunciation", "ancient rome history quick overview",
    "pomodoro technique for focus and productivity", "healthy breakfast ideas under 10 minutes",
    "天气预报", "今日新闻热点", "美食食谱家常菜", "旅游攻略", "健康养生知识",
    "科技资讯", "电影推荐", "股票行情", "体育赛事", "历史上的今天",
]
# 热词来源（按顺序降级）：B 站官方热搜 → B 站聚合榜 → 微博/百度热搜 → 内置词库
# kind 决定解析方式：
#   bilibili-square  → {"data":{"trending":{"list":[{"keyword":...}]}}}
#   bilibili-hotword → {"list":[{"keyword":...}]}
#   list             → {"data":[{"title":...}]}（第三方每日热榜聚合）
HOT_SOURCES = [
    ("B站热搜", "bilibili-square",
     "https://api.bilibili.com/x/web-interface/search/square?limit={limit}"),
    ("B站热词", "bilibili-hotword",
     "https://s.search.bilibili.com/main/hotword"),
    ("B站热榜(聚合)", "list",
     "https://hot.baiwumm.com/api/bilibili"),
    ("B站热榜(聚合2)", "list",
     "https://cnxiaobai.com/DailyHotApi/bilibili"),
    ("微博热搜", "list",
     "https://hot.baiwumm.com/api/weibo"),
    ("百度热搜", "list",
     "https://hot.baiwumm.com/api/baidu"),
]
def _parse_hot_words(kind: str, data) -> list[str]:
    """从各家热榜 JSON 里抽出关键词列表（保持原始顺序、去重、去 HTML）。"""
    if not isinstance(data, dict):
        return []
    if kind == "bilibili-square":
        raw = (((data.get("data") or {}).get("trending") or {}).get("list")) or []
        items = [x.get("keyword") or x.get("show_name") for x in raw if isinstance(x, dict)]
    elif kind == "bilibili-hotword":
        items = [x.get("keyword") for x in (data.get("list") or []) if isinstance(x, dict)]
    else:
        raw = data.get("data") or data.get("result") or []
        items = [(x.get("title") or x.get("name")) if isinstance(x, dict) else x for x in raw]
    out: list[str] = []
    for w in items:
        if not w:
            continue
        s = re.sub(r"<[^>]+>", "", str(w)).strip()
        s = re.sub(r"\s+", " ", s)
        if 1 < len(s) <= 60 and "http" not in s.lower() and s not in out:
            out.append(s)
    return out
SKIP_PATTERNS = [
    "referral", "refer and earn", "sweepstake", "entries", "install the",
    "set bing as your default", "bing wallpaper", "punch card", "ancient coin",
    "sea of thieves", "rewards extension", "redemption goal", "order history",
    "claim your gift", "shop to earn", "set goal", "Available tomorrow",
    "Offer is Locked", "Earn -1 points",
]


def _uid() -> str:
    return uuid.uuid4().hex.upper()


def _long_id() -> str:
    return _uid() + _uid()[:24]


def _today_num() -> int:
    return int(datetime.now().strftime("%Y%m%d"))


def _today_str() -> str:
    n = datetime.now()
    return f"{n.month}/{n.day}/{n.year}"


LOG_KEEP_DAYS = 7          # 日志默认只留最近 7 天，第 8 天先删最老的再写今天的


def prune_logs(logs_dir: str, keep_days: int = LOG_KEEP_DAYS, protect=()) -> list:
    """按天清理日志：总共只留 keep_days 天（含今天），返回被删掉的文件名列表。

    * 只认 `YYYY-MM-DD.log` 和 `endpoint-test-YYYYMMDD-HHMMSS.json`，其它文件一律不碰；
    * 按文件名里的日期排序（不是 mtime），跨月/跨年、中间断几天都不会算错；
    * protect 里的文件名（= 今天那份）永远不删，并且占用一个名额 ——
      这样"第 8 天先删最老的，再准备当天的日志"之后，目录里正好还是 7 天。
    """
    try:
        keep_days = max(1, min(3650, int(keep_days)))
    except (TypeError, ValueError):
        keep_days = LOG_KEEP_DAYS
    protect = set(protect or ())
    try:
        names = os.listdir(logs_dir)
    except OSError:
        return []

    daily, tests = [], []
    for n in names:
        p = os.path.join(logs_dir, n)
        if not os.path.isfile(p):
            continue
        m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})\.log", n)
        if m:
            try:                                    # 名字像日期但日期非法（如 13-99）就不认
                datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except ValueError:
                continue
            daily.append((m.group(1) + m.group(2) + m.group(3), n, p))
            continue
        m = re.fullmatch(r"endpoint-test-(\d{8})-(\d{6})\.json", n)
        if m:
            try:
                datetime.strptime(m.group(1), "%Y%m%d")
            except ValueError:
                continue
            tests.append((m.group(1) + m.group(2), n, p))

    removed = []
    for group in (daily, tests):
        group.sort(key=lambda t: t[0], reverse=True)      # 新的在前
        used = sum(1 for it in group if it[1] in protect)
        if group is daily and protect:
            used = max(used, 1)                           # 今天这份可能还没落盘，也要占一个名额
        remain = keep_days - used
        for _key, name, path in group:
            if name in protect:
                continue                                  # 今天这份留着，名额已扣
            if remain > 0:
                remain -= 1
                continue
            try:
                os.remove(path)
                removed.append(name)
            except OSError:
                pass
    return removed


class RewardsAPI:
    def __init__(self, cfg: dict, state: dict, auth: dict, base_dir: str, dry_run: bool = False):
        self.cfg = cfg
        self.state = state
        self.auth = auth
        self.base_dir = base_dir
        self.dry_run = dry_run
        self.verbose = bool(cfg.get("verbose", True))   # 详细日志总开关
        self.log_hook = None                            # GUI 注入: (level, icon, msg)
        self.stop_event = None                          # GUI 停止按钮用

        logs_dir = os.path.join(base_dir, "logs")
        os.makedirs(logs_dir, exist_ok=True)
        # 先按保留天数清老日志，再准备今天的日志文件：第 8 天时先删最老的。
        # 今天这份算在 keep_days 名额里，所以清完 + 新建之后目录里正好 N 天。
        today_name = datetime.now().strftime("%Y-%m-%d") + ".log"
        keep_days = cfg.get("log_keep_days", LOG_KEEP_DAYS)
        if keep_days is None:
            keep_days = LOG_KEEP_DAYS
        try:
            keep_days = int(keep_days)
        except (TypeError, ValueError):
            keep_days = LOG_KEEP_DAYS
        self._keep_days = keep_days
        self._logs_dir = logs_dir
        pruned = prune_logs(logs_dir, keep_days, protect=(today_name,))
        self.log_path = os.path.join(logs_dir, today_name)
        if pruned:
            try:
                self._write("INFO", "🧹",
                            f"日志超过 {keep_days} 天，已删除最老的 {len(pruned)} 个："
                            + "、".join(sorted(pruned)))
            except Exception:
                pass
        self.http = HttpClient(os.path.join(base_dir, "cookies.txt"),
                               int(cfg.get("http_timeout", 15)),
                               min_gap=float(cfg.get("request_min_gap_s", 1.2) or 0))
        self.http.trace = self._trace
        cookie_file = os.path.join(base_dir, "cookies.txt")
        if os.path.isfile(cookie_file):
            try:
                n = self.http.load_netscape(cookie_file)
                self._loaded_cookies = n
            except Exception as e:
                self._loaded_cookies = f"加载失败: {e}"
        else:
            self._loaded_cookies = 0

        self.notifier = Notifier(cfg.get("notify", {}), self.http, self.log)
        self.token: str | None = None
        self.region = "CN"
        self.host = cfg.get("host", "www.bing.com")
        # 搜索必须走 cn.bing.com：同一个 reportActivity 接口，www.bing.com 会返回
        # 200 + IsAuthenticated=true + RewardsIncrement=1，但配额纹丝不动；
        # 换成 cn.bing.com 后立刻计分（实测一次 +3）。原因应该是奖励侧只认
        # 与 SERP 同主机的上报（IG 是与页面绑定的）。
        self.search_host = (cfg.get("search_host") or "cn.bing.com").strip()
        self.ua_pc = UA["pc"]
        self.next_action = ""
        self.hot_words: list[str] = []
        self.hot_index = 0
        self._used_words: set[str] = set()      # 今天已搜过的词（跨进程持久化，重复词不计分）
        self._stop_announced = False            # 停止提示只报一次
        self._legacy_dead = False               # 老门户接口（getuserinfo/reportactivity）已下线
        self._rwho_done = False                 # _Rwho 桌面标记是否已注入 jar
        self._balance_cache: tuple[int, float] = (0, 0.0)   # 积分的短时缓存，避免重复打 DAPI
        self._dapi613: tuple[dict, float] = ({}, 0.0)       # DAPI options=613 返回体短时缓存
        self._earn_html: tuple[str, float] = ("", 0.0)      # earn 页面短时缓存（350KB，别反复拉）
        self.start_time = time.time()
        self.fail_notes: dict[str, str] = {}                # 本轮各任务的失败原因（汇总通知用）
        self.daemon_mode = False                            # 守护模式（tasks.run_all pace=True 时置位）
        self.goals_done_today = False                        # 今日目标已达成 → 外层睡到明天起床时间
        self._load_used_words()

    # ------------------------------------------------------------ 日志
    def _roll_log_if_needed(self) -> None:
        """跨天时切到新日志文件。

        守护会连续跑好几天，如果只在构造时定死 log_path，第 2 天开始还往第 1 天那个
        文件里写，"每天一个文件 + 只留 7 天"就形同虚设。这里每次写日志顺手检查日期。
        """
        try:
            today_name = datetime.now().strftime("%Y-%m-%d") + ".log"
            if os.path.basename(self.log_path) == today_name:
                return
            logs_dir = getattr(self, "_logs_dir", os.path.dirname(self.log_path))
            keep = getattr(self, "_keep_days", LOG_KEEP_DAYS)
            pruned = prune_logs(logs_dir, keep, protect=(today_name,))
            self.log_path = os.path.join(logs_dir, today_name)
            if pruned:
                line = (f"[{time.strftime('%H:%M:%S')}] [INFO ] 🧹 日志超过 {keep} 天，"
                        f"已删除最老的 {len(pruned)} 个：" + "、".join(sorted(pruned)))
                with open(self.log_path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
        except Exception:
            pass                                   # 日志分卷失败不能影响任务本身

    def _write(self, level: str, icon: str, msg: str) -> None:
        self._roll_log_if_needed()
        line = f"[{time.strftime('%H:%M:%S')}] [{level:<5}] {icon} {msg}"
        try:
            print(line, flush=True)
        except UnicodeEncodeError:
            try:
                print(line.encode("ascii", "replace").decode(), flush=True)
            except Exception:
                pass                       # pythonw 无 stdout 时静默跳过
        except Exception:
            pass                           # 控制台输出失败不能影响任务本身
        try:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass
        if self.log_hook:
            try:
                self.log_hook(level, icon, msg)
            except Exception:
                pass

    def log(self, icon: str, msg: str, push: bool = False, level: str = "INFO",
            category: str = "summary") -> None:
        """写日志；push=True 时按 category（= notify.categories 开关键）推送到通知渠道。

        开关定义见 notify.SWITCH_RESULT / notify.SWITCH_SYSTEM，通知推送页可逐项开关。
        """
        self._write(level, icon, msg)
        if push and not self.dry_run:
            self.notifier.send(f"{icon} {msg}", category)

    def debug(self, icon: str, msg: str) -> None:
        """详细日志：只在 verbose=True 时输出，不会触发通知推送。"""
        if self.verbose:
            self._write("DEBUG", icon, msg)

    @staticmethod
    def _redact(s: str) -> str:
        """脱敏：URL / 响应里的授权码、Token、Cookie 都不能进日志。"""
        s = re.sub(r'(code|refresh_token|access_token|sig|token)=([^&\s"\']+)', r"\1=***", s, flags=re.I)
        s = re.sub(r'"(access_token|refresh_token|token)"\s*:\s*"[^"]*"', r'"\1":"***"', s, flags=re.I)
        s = re.sub(r"Bearer\s+[A-Za-z0-9._\-]+", "Bearer ***", s, flags=re.I)
        s = re.sub(r'(Cookie|Authorization|Set-Cookie)\s*[:=]\s*[^;\n"]{8,}', r"\1=***", s, flags=re.I)
        return s

    def _trace(self, method: str, url: str, status: int, elapsed: float, text: str, err: str | None) -> None:
        """每个 HTTP 请求自动记一条，便于测试时看清到底发了什么、服务器回了什么。"""
        if not self.verbose:
            return
        safe_url = self._redact(url)
        if err:
            self.debug("🌐", f"{method} {safe_url} → 异常 {err} ({elapsed:.0f}ms)")
            return
        preview = re.sub(r"\s+", " ", self._redact(text or ""))[:200]
        self.debug("🌐", f"{method} {safe_url} → {status} | {elapsed:.0f}ms | {len(text or '')}B | {preview}")

    def save_state(self) -> None:
        with open(os.path.join(self.base_dir, "state.json"), "w", encoding="utf-8") as f:
            json.dump(self.state, f, ensure_ascii=False, indent=2)

    def save_auth(self) -> None:
        with open(os.path.join(self.base_dir, "auth.json"), "w", encoding="utf-8") as f:
            json.dump(self.auth, f, ensure_ascii=False, indent=2)

    def sleep(self, ms: int) -> None:
        """可被 GUI 的停止按钮中断的等待。"""
        if self.dry_run:
            return
        if self.stop_event is not None:
            if self.stop_event.wait(ms / 1000):
                if not self._stop_announced:
                    self._stop_announced = True
                    self.log("⏹", "已收到停止指令，正在退出当前任务…")
            return
        time.sleep(ms / 1000)

    def stopped(self) -> bool:
        """GUI 停止按钮是否已按下。所有循环体必须定期检查并提前退出。"""
        return self.stop_event is not None and self.stop_event.is_set()

    def random_delay(self, lo: int | None = None, hi: int | None = None) -> None:
        lo = lo if lo is not None else int(self.cfg.get("delay_min_ms", 3000))
        hi = hi if hi is not None else int(self.cfg.get("delay_max_ms", 8000))
        self.sleep(random.randint(lo, max(lo, hi)))

    def _region(self) -> str:
        return "cn" if self.cfg.get("lock_region", True) else self.region.lower()

    def _mkt(self) -> str:
        return "&mkt=zh-CN" if self.cfg.get("lock_region", True) else ""

    # ------------------------------------------------------------ OAuth
    def exchange_token(self, params: dict) -> bool:
        """GET oauth20_token.srf，保存 refresh_token / access_token。"""
        self.debug("🔑", f"请求 Token: grant_type={params.get('grant_type')} "
                         f"client_id={params.get('client_id')}（最多3次，敏感值已脱敏）")
        for attempt in range(3):
            self.debug("🔑", f"Token 第 {attempt+1}/3 次尝试")
            try:
                r = self.http.get(TOKEN_URL, params=params, headers={"User-Agent": self.ua_pc})
                try:
                    data = r.json()
                except Exception:
                    if attempt < 2:
                        self.sleep(3210)
                    continue
                if data.get("error"):
                    self.log("🔴", f"Token错误: {data.get('error')} - {data.get('error_description','')}")
                    self.debug("🔑", f"Token 响应字段: {list(data.keys())}")
                    if data["error"] in ("invalid_grant", "invalid_request"):
                        self.auth["refresh_token"] = ""
                        self.save_auth()
                    return False
                if data.get("refresh_token") and data.get("access_token"):
                    self.auth["refresh_token"] = data["refresh_token"]
                    self.auth["access_token"] = data["access_token"]
                    self.auth["token_time"] = int(time.time() * 1000)
                    self.save_auth()
                    self.token = data["access_token"]
                    self.debug("🔑", f"Token 成功: access_token {len(data['access_token'])} 字符, "
                                     f"refresh_token {len(data['refresh_token'])} 字符, 已写入 auth.json")
                    return True
                if attempt < 2:
                    self.sleep(3210)
            except HttpError as e:
                if e.status in (400, 401):
                    self.auth["refresh_token"] = ""
                    self.save_auth()
                    return False
                if attempt < 2:
                    self.sleep(3210)
            except Exception as e:
                self.log("🟡", f"Token请求异常: {e}")
                if attempt < 2:
                    self.sleep(3210)
        return False

    def exchange_code(self, code: str) -> bool:
        return self.exchange_token({
            "client_id": CLIENT_ID,
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "grant_type": "authorization_code",
        })

    def auto_fetch_code(self) -> str | None:
        """利用已登录的 live.com Cookie 静默走一遍授权，从最终 URL 拿 code。"""
        try:
            r = self.http.get(AUTH_URL, headers={"User-Agent": self.ua_pc}, raise_on_error=False)
            code = parse_qs(urlparse(r.url).query).get("code", [None])[0]
            self.debug("🔑", f"自动授权最终地址: {self._redact(r.url)} → code {'命中' if code else '未命中'}")
            return code
        except Exception as e:
            self.log("🟡", f"自动获取授权码失败: {e}")
            return None

    def ensure_token(self) -> bool:
        """需要访问 DAPI 时才取 Token，而且优先复用本地还没过期的 access_token。

        为什么要复用：每次刷新都去换一次票，在微软那边就是多一次「登录」，既慢又显眼。
        access_token 一般有 1 小时有效期，auth.json 里存着，50 分钟内直接拿来用，
        过期了才真的去换（一次很小的请求）。
        """
        if self.token:
            return True
        at = (self.auth.get("access_token") or "").strip()
        tt = float(self.auth.get("token_time") or 0)
        if at and tt and (time.time() * 1000 - tt) < 50 * 60 * 1000:
            self.token = at
            self.debug("🔑", "复用本地缓存的 access_token（未过期，省一次换票请求）")
            return True
        return self.renew_token()

    def renew_token(self) -> bool:
        """返回 False 表示 DAPI 类任务（App签到/阅读）本次无法执行。"""
        refresh = (self.auth.get("refresh_token") or "").strip()
        tt = float(self.auth.get("token_time") or 0)
        age = f"距上次获取 {(time.time()*1000-tt)/86400000:.1f} 天" if tt > 0 else "无历史记录"
        self.debug("🔑", f"续期检查: refresh_token "
                         f"{'有(' + str(len(refresh)) + '字符)' if refresh else '无'}, {age}")
        if refresh:
            ok = self.exchange_token({
                "client_id": CLIENT_ID,
                "refresh_token": refresh,
                "scope": SCOPE,
                "grant_type": "REFRESH_TOKEN",
            })
            if ok:
                self.log("🟢", "Token 获取成功")
                return True

        self.log("🟡", "尝试用浏览器登录态自动获取授权码…")
        code = self.auto_fetch_code()
        if code:
            if self.exchange_code(code):
                self.log("🟢", "自动授权成功，Token 已获取", True, category="auth")
                return True
            # 授权码失效，再试一次
            code2 = self.auto_fetch_code()
            if code2 and self.exchange_code(code2):
                self.log("🟢", "自动授权成功，Token 已获取", True, category="auth")
                return True

        self.log("🔴", "无法获取 Token（refresh_token 已失效或未授权）", True, category="token")
        self.log("🔑", "请到界面「账号与授权」页，点「🌐 一键登录 · 自动获取 Cookie + 授权」重新授权一次；"
                       "授权后 App 签到 / 阅读 / 卡片领取 / PC 签到才会恢复", True, category="token")
        self.log("🌐", AUTH_URL)
        self.log("📋", '或手动：登录后复制地址栏完整 URL，执行 python main.py --auth "粘贴的URL"')
        return False

    def with_token_retry(self, fn):
        if not self.token:
            return None
        try:
            return fn(self.token)
        except HttpError as e:
            self.debug("🔑", f"DAPI 请求返回 HTTP {e.status}，响应片段: {self._redact(e.body)[:120]}")
            if e.status == 401:
                self.log("🟡", "Token 过期，强制重新授权…")
                self.token = None
                if self.renew_token() and self.token:
                    return fn(self.token)
            raise

    def dapi_headers(self, token: str, options: int | None = None) -> dict:
        return {
            "Content-Type": "application/json; charset=UTF-8",
            "User-Agent": UA["app"],
            "Authorization": f"Bearer {token}",
            "X-Rewards-AppId": APP_ID,
            "X-Rewards-IsMobile": "true",
            "X-Rewards-Country": self._region(),
            "X-Rewards-Language": "zh",
        }

    # ------------------------------------------------------------ 地区 / 积分
    def check_region(self, retry: int = 0) -> bool:
        if not self.cfg.get("lock_region", True):
            return True
        try:
            html = self.http.get(f"https://{self.host}/", headers={"User-Agent": self.ua_pc}).text
            m = re.search(r'Region:"(.*?)"(.*?)RevIpCC:"(.*?)"', re.sub(r"\s", "", html))
            if m:
                self.region = m.group(3).upper()
                self.debug("🌏", f"地区字段: Region={m.group(1)} RevIpCC={m.group(3)} "
                                 f"页面长度 {len(html)}B, 重试次数 {retry}")
                if self.region != "CN":
                    self.log("🔴", f"IP非大陆({self.region})，已停止全部任务", True, category="region")
                    return False
                self.log("🟢", f"地区检测通过: {self.region}")
                return True
            if retry < 2:
                self.random_delay()
                return self.check_region(retry + 1)
            self.log("🔴", "地区检测失败（页面格式不匹配）")
        except Exception as e:
            if retry < 2:
                self.log("🟡", f"地区检测异常: {e}，重试中")
                self.random_delay()
                return self.check_region(retry + 1)
            self.log("🔴", f"地区检测失败: {e}")
        return False

    def _legacy_off(self) -> bool:
        """老门户接口是否已判定下线（本进程或今天已确认过就不再请求）。"""
        if self._legacy_dead:
            return True
        if str(self.state.get("legacy_api_dead_on") or "") == str(_today_num()):
            self._legacy_dead = True
            return True
        return False

    def _mark_legacy_dead(self, why: str = "") -> None:
        if not self._legacy_dead:
            self._legacy_dead = True
            self.state["legacy_api_dead_on"] = _today_num()
            self.save_state()
            self.log("⚙", f"老门户接口已下线（{why or '401'}），本次运行不再调用它")

    def _fetch_earn_html(self, force: bool = False, retries: int = 0) -> str:
        """earn 页面 350KB，10 秒内复用，避免一次任务里反复拉。

        retries>0 时对 5xx/网络抖动退避重试；全部失败则沿用上一次的缓存
        （宁可给旧数据，也不要返回空串让上层把进度读成 0）。
        """
        html, ts = self._earn_html
        if html and not force and time.time() - ts < 10:
            return html
        try:
            fresh = self.http.get(f"{REWARDS}/earn", headers={"User-Agent": self.ua_pc},
                                  retries=retries).text
            if not fresh or len(fresh) < 20000:          # 空白页/错误页不当成有效页面
                raise ValueError(f"earn 页面内容异常（{len(fresh or '')}B）")
            self._earn_html = (fresh, time.time())
            return fresh
        except Exception as e:
            self.debug("📊", f"earn 页面获取失败: {type(e).__name__}: {e}"
                             + ("（沿用旧缓存）" if html else ""))
            return html or ""

    def balance_from_dashboard(self) -> int:
        """新门户兜底：从 dashboard 页面 RSC flight 里读 balance。"""
        try:
            r = self.http.get(f"{REWARDS}/dashboard", headers={
                "User-Agent": self.ua_pc, "Referer": f"{REWARDS}/"})
            if r.ok:
                # flight 数据里是转义 JSON：\"balance\":5665，正则必须兼容反斜杠
                m = re.search(r'\\?"balance\\?"\s*:\s*(\d+)', r.text)
                if m:
                    bal = int(m.group(1))
                    self.debug("📊", f"已从 dashboard 页面解析 balance={bal}")
                    return bal
                self.debug("📊", "dashboard 页面里没找到 balance 字段（可能未登录或门户改版）")
        except Exception as e:
            self.debug("📊", f"dashboard 页面解析失败: {type(e).__name__}: {e}")
        return 0

    def get_userinfo(self) -> dict | None:
        """老接口 /api/getuserinfo 已被新版门户下线（401）。
        今天确认过下线就直接返回 None，不再浪费 600ms + 37KB 请求；
        调用方应改用 DAPI / dashboard 页面解析。"""
        if self._legacy_off():
            return None
        try:
            r = self.http.get(
                f"{REWARDS}/api/getuserinfo?type=1&X-Requested-With=XMLHttpRequest&_={int(time.time()*1000)}",
                headers={
                    "User-Agent": self.ua_pc,
                    "Referer": f"{REWARDS}/",
                    "X-Requested-With": "XMLHttpRequest",
                },
            )
            data = r.json()
            d = data.get("dashboard") or data
            self.debug("📊", f"getuserinfo 解析: 顶层字段 {list(data.keys())[:8]}, "
                             f"dashboard字段 {list(d.keys())[:12] if isinstance(d, dict) else type(d).__name__}")
            return data
        except Exception as e:
            status = getattr(e, "status", 0)
            if status in (401, 403, 404):
                self._mark_legacy_dead(f"HTTP {status}")
            else:
                self.debug("📊", f"getuserinfo 请求失败: {type(e).__name__}: {e}")
            return None

    @staticmethod
    def _sum_counter(items) -> dict:
        out = {"progress": 0, "max": 0}
        if isinstance(items, list):
            for it in items:
                out["progress"] += int(it.get("pointProgress") or 0)
                out["max"] += int(it.get("pointProgressMax") or it.get("pointMax") or 0)
        return out

    def get_read_progress(self) -> dict | None:
        for attempt in range(2):
            try:
                if attempt:
                    self._dapi613 = ({}, 0.0)      # DAPI 偶尔返回残缺对象，强制重取一次
                resp = self._dapi_payload()
                if not resp:
                    self.debug("📖", "无 OAuth Token，跳过阅读进度（DAPI 需要授权）")
                    return None
                promos = resp.get("promotions") or []
                self.debug("📖", f"DAPI 返回 {len(promos)} 个 promotions，查找 offerid={OFFER_READ}")
                for p in promos:
                    attrs = p.get("attributes") or {}
                    if attrs.get("offerid") == OFFER_READ:
                        self.debug("📖", f"命中阅读任务: { {k: attrs.get(k) for k in ('offerid','progress','max','complete')} }")
                        return {"progress": int(attrs.get("progress") or 0),
                                "max": int(attrs.get("max") or 30)}
            except Exception as e:
                self.log("🔴", f"阅读进度获取失败: {e}")
                return None
        self.log("🟡", f"阅读任务未找到 (offerid: {OFFER_READ})")
        return None

    def get_balance(self, ttl: float = 15) -> int:
        """积分只认 DAPI（唯一可信来源）；DAPI 挂了再读 dashboard 页面。15 秒内复用。"""
        bal, ts = self._balance_cache
        if bal > 0 and time.time() - ts < ttl:
            return bal
        try:
            r = self.with_token_retry(
                lambda t: self.http.get(f"{DAPI}?channel=SAAndroid&options=105", headers=self.dapi_headers(t))
            )
            if r:
                b = (r.json().get("response") or {}).get("balance")
                if b is not None:
                    self.debug("📊", f"积分来源: DAPI options=105 → {b}")
                    self._balance_cache = (int(b), time.time())
                    return int(b)
        except Exception as e:
            self.debug("📊", f"DAPI 积分查询失败，改读 dashboard 页面: {type(e).__name__}: {e}")
        bal = self.balance_from_dashboard()
        if bal:
            self._balance_cache = (bal, time.time())
            return bal
        self.debug("📊", "DAPI 与 dashboard 都拿不到积分，返回 0")
        self._balance_cache = (0, time.time())
        return 0

    def _dapi_payload(self, ttl: float = 10) -> dict:
        """DAPI options=613（含 promotions/积分/计数器）10 秒内复用，一次任务别反复拉。"""
        data, ts = self._dapi613
        if data and time.time() - ts < ttl:
            return data
        r = self.with_token_retry(
            lambda t: self.http.get(f"{DAPI}?channel=SAAndroid&options=613", headers=self.dapi_headers(t))
        )
        if not r:
            return {}
        data = r.json().get("response") or {}
        self._dapi613 = (data, time.time())
        return data

    def _quota_from_dapi(self) -> dict | None:
        """搜索配额：DAPI promotions 里 WW_search_global_* 的 progress/max。
        这条一次 99KB 就够，比拉 350KB 的 earn 页面快得多。"""
        try:
            resp = self._dapi_payload()
            if not resp:
                return None
            for p in resp.get("promotions") or []:
                a = p.get("attributes") or {}
                oid = str(a.get("offerid") or "")
                if "search" in oid.lower() and ("pc" in oid.lower() or "global" in oid.lower()):
                    cur = int(a.get("progress") or 0)
                    mx = int(a.get("max") or 0)
                    if mx:
                        return {"balance": int(resp.get("balance") or 0),
                                "pc": {"progress": cur, "max": mx},
                                "mobile": {"progress": 0, "max": 0},
                                "read_progress": 0, "read_max": 30, "source": "dapi"}
            self.debug("📊", "DAPI 里没有搜索配额 promotion，改读 earn 页面")
            return None
        except Exception as e:
            self.debug("📊", f"DAPI 配额解析失败: {type(e).__name__}: {e}")
            return None

    def _quota_from_earn_page(self) -> dict | None:
        html = self._fetch_earn_html()
        if not html:
            return None
        clean = html.replace('\\"', '"')
        pc_cur, pc_max = 0, 60
        m = re.search(r'"pointsCounters":\{', clean)
        if m:
            start = clean.find("{", m.start())
            depth, end = 0, start
            for i in range(start, min(len(clean), start + 500)):
                if clean[i] == "{":
                    depth += 1
                elif clean[i] == "}":
                    depth -= 1
                    if depth == 0:
                        end = i + 1
                        break
            try:
                pts = json.loads(clean[start:end])
                pc_cur = int((pts.get("pc") or {}).get("progress") or 0)
                pc_max = int((pts.get("pc") or {}).get("max") or 60)
            except Exception:
                pass
        m2 = re.search(r'"combinedSearch":\{[^}]*?"progress":(\d+)[^}]*?"max":(\d+)', clean)
        if m2:
            pc_cur, pc_max = int(m2.group(1)), int(m2.group(2))
        if pc_max == 0:
            return None
        # 页面里的 totalPoints/dailyOffer 是「计数器累计值」，不是账户可用积分（会差几千），
        # 所以这里只取配额，积分一律由 DAPI 提供。
        return {"balance": 0, "pc": {"progress": pc_cur, "max": pc_max},
                "mobile": {"progress": 0, "max": 0}, "read_progress": 0,
                "read_max": 30, "source": "earn"}

    def get_rewards_info(self) -> dict | None:
        """配额：DAPI → earn 页面；积分一律取 DAPI（老接口 getuserinfo 已下线，不再尝试）。"""
        for name, fn in (("DAPI", self._quota_from_dapi),
                         ("earn页面", self._quota_from_earn_page)):
            try:
                r = fn()
            except Exception as e:
                self.debug("📊", f"配额来源 {name} 异常: {type(e).__name__}: {e}")
                r = None
            self.debug("📊", f"配额来源 {name}: {'命中 ' + str(r['pc']) if r else '未命中，继续兜底'}")
            if r:
                # 页面里的 totalPoints/dailyOffer 不可信（会差几千），积分统一用 DAPI
                try:
                    bal = self.get_balance()
                    if bal:
                        r["balance"] = bal
                except Exception:
                    pass
                rp = self.get_read_progress()
                if rp:
                    r["read_progress"] = rp["progress"]
                    r["read_max"] = rp["max"]
                self.debug("📊", f"最终配额: PC {r['pc']['progress']}/{r['pc']['max']}, "
                                 f"阅读 {r['read_progress']}/{r['read_max']}, 积分 {r['balance']}")
                return r
        self.log("🔴", "无法获取积分/配额信息（可能是 Cookie 失效）")
        return None

    def get_today_stats(self) -> dict:
        """今日数据：今日积分 / 拼图进度 / 兑换目标 / 阅读篇数。

        全部来自同一次 DAPI options=613（10 秒缓存，不会多打请求）：
          * todays_points                     —— 服务器算好的「今天拿了多少分」，不用本地猜
          * hva_puzzle_pieces_completed_amount —— 手机端拼图已集块数（上限 12）
          * goal_item.{price, attributes}      —— 我设置的兑换目标（如盒马礼品卡 19305 分）
          * offerid=ENUS_readarticle3_30points 的 activityprogress/activitymax —— 阅读篇数
        读不到的字段留 -1，界面显示「读取失败」，绝不编数字。
        """
        out = {"today_points": -1, "puzzle": -1, "puzzle_max": PUZZLE_MAX,
               "read_articles": -1, "read_articles_max": -1,
               "goal_price": 0, "goal_title": "", "balance": 0}
        try:
            resp = self._dapi_payload()
        except Exception as e:
            self.debug("📊", f"今日数据获取失败: {type(e).__name__}: {e}")
            return out
        if not resp:
            return out
        try:
            out["balance"] = int(resp.get("balance") or 0)
        except (TypeError, ValueError):
            out["balance"] = 0

        gi = resp.get("goal_item") or {}
        if isinstance(gi, dict):
            try:
                out["goal_price"] = int(gi.get("price") or 0)
            except (TypeError, ValueError):
                out["goal_price"] = 0
            attrs = gi.get("attributes") or {}
            out["goal_title"] = str(attrs.get("desc.sc_title")
                                    or attrs.get("desc.group_text")
                                    or gi.get("name") or "").strip()

        for p in resp.get("promotions") or []:
            a = p.get("attributes") or {}
            if "todays_points" in a:
                try:
                    out["today_points"] = int(a.get("todays_points"))
                except (TypeError, ValueError):
                    pass
            if "hva_puzzle_pieces_completed_amount" in a:
                try:
                    out["puzzle"] = int(a.get("hva_puzzle_pieces_completed_amount"))
                except (TypeError, ValueError):
                    pass
            if a.get("offerid") == OFFER_READ:
                try:
                    out["read_articles"] = int(a.get("activityprogress") or 0)
                    out["read_articles_max"] = int(a.get("activitymax") or 0)
                except (TypeError, ValueError):
                    pass

        self.debug("📊", f"今日数据: 今日积分 {out['today_points']}，"
                         f"拼图 {out['puzzle']}/{PUZZLE_MAX}，"
                         f"阅读 {out['read_articles']}/{out['read_articles_max']} 篇，"
                         f"兑换目标「{out['goal_title']}」{out['balance']}/{out['goal_price']}")
        return out

    def close(self) -> None:
        """一次操作做完就主动「下线」：丢 Token、清缓存、关连接。

        微软侧看到的是「登录 → 办几件事 → 离开」，而不是一个长挂着的会话，
        这样自己在手机/浏览器上同时用同一个账号时就不会一直被提示出错。
        下次要用会重新续 Token、重新读 cookies.txt，代价只有一次很小的请求。
        """
        self.token = None
        self._balance_cache = (0, 0.0)
        self._dapi613 = ({}, 0.0)
        self._earn_html = ("", 0.0)
        try:
            self.http.close()
        except Exception:
            pass

    def check_search_restricted(self) -> bool:
        info = self.get_rewards_info()
        current = info["pc"]["progress"] if info else int(self.state.get("last_search_progress", -1))
        last = int(self.state.get("last_search_progress", -1))
        cnt = int(self.state.get("restricted_times", 0))
        if last != -1:
            cnt = cnt + 1 if (current == last and current < (info["pc"]["max"] if info else 999)) else 0
        self.state["last_search_progress"] = current
        self.state["restricted_times"] = cnt
        self.save_state()
        if cnt >= 3:
            self.log("🔴", "搜索受限或账号异常，已中断今日搜索", True, category="restricted")
            return True
        return False

    # ------------------------------------------------------------ 签到 / 阅读
    def sign_app(self) -> int:
        if self.dry_run:
            self.log("🧪", "[dry-run] 跳过 App 签到 POST")
            return 0
        body = {"amount": 1, "id": _long_id(), "type": 103,
                "country": self._region(), "channel": CHANNEL}
        headers = self.dapi_headers(self.token or "")
        headers.update({"X-Rewards-PartnerId": "startapp", "X-Rewards-Flights": "rwgobig"})
        try:
            r = self.with_token_retry(lambda t: self.http.post(
                DAPI_ACT, json_body=body, headers={**headers, "Authorization": f"Bearer {t}"}))
            if r:
                resp = r.json().get("response") or {}
                self.debug("📱", f"App签到响应字段: {list(resp.keys())}, activity="
                                 f"{'有' if resp.get('activity') else '无'}, "
                                 f"isDuplicate={resp.get('isDuplicate')}")
                act = resp.get("activity")
                if act:
                    return int(act.get("p") or act.get("points") or 0)
                if resp.get("isDuplicate") or act is None:
                    return 0
        except Exception as e:
            self.log("🔴", f"App签入失败: {e}")
        return -1

    def sign_pc(self) -> int:
        """PC 签到：老接口 /api/reportactivity 已下线（401），改走 DAPI activities。

        offerid=Gamification_DailyCheckIn。返回新增积分；0 表示接口正常但今天
        没有可加的分（例如 App 签到已经占了这次每日签入）；-1 表示执行不了。
        """
        if self.dry_run:
            self.log("🧪", "[dry-run] 跳过 PC 签到 POST")
            return 0
        if not self.token:
            self.debug("💻", "PC 签到走 DAPI，需要 OAuth Token，当前未授权")
            return -1
        body = {"amount": 1, "country": self._region(), "id": _long_id(), "type": 101,
                "attributes": {"offerid": OFFER_PC_SIGN}}
        try:
            r = self.with_token_retry(
                lambda t: self.http.post(DAPI_ACT, json_body=body, headers=self.dapi_headers(t)))
            if not r:
                return -1
            resp = r.json().get("response") or {}
            act = resp.get("activity") or {}
            p = int(act.get("p") or 0)
            self.debug("💻", f"PC 签到响应: p={p} isDuplicate={resp.get('isDuplicate')} "
                             f"balance={resp.get('balance')}")
            if p > 0:
                return p
            if resp.get("isDuplicate"):
                self.debug("💻", "PC 签到今天已完成（isDuplicate）")
            else:
                self.debug("💻", "PC 签到接口已响应，但本次没有新增积分（p=0）")
            return 0
        except Exception as e:
            self.log("🟡", f"PC 签到请求失败: {type(e).__name__}: {e}")
            return -1

    def do_read_once(self) -> dict | None:
        if self.dry_run:
            self.log("🧪", "[dry-run] 跳过阅读 POST")
            return {"points": 0, "isDuplicate": False}
        body = {"amount": 1, "country": self._region(), "id": _long_id(),
                "type": 101, "attributes": {"offerid": OFFER_READ}}
        try:
            r = self.with_token_retry(lambda t: self.http.post(
                DAPI_ACT, json_body=body, headers=self.dapi_headers(t)))
            if r:
                resp = r.json().get("response") or {}
                self.debug("📖", f"阅读响应: activity.p={(resp.get('activity') or {}).get('p')}, "
                                 f"isDuplicate={resp.get('isDuplicate')}")
                return {"points": int((resp.get("activity") or {}).get("p") or 0),
                        "isDuplicate": bool(resp.get("isDuplicate"))}
        except Exception as e:
            self.log("🔴", f"阅读请求失败: {e}")
        return None

    # ------------------------------------------------------------ 活动卡片
    def _infer_kind(self, offer_id: str, title: str = "") -> str:
        s = f"{offer_id} {title}".lower()
        if "quiz" in s or "trivia" in s:
            return "quiz"
        if "puzzle" in s:
            return "puzzle"
        if "image" in s:
            return "image_creator"
        if "explore" in s or "search" in s:
            return "explore_search"
        if "dailyset" in s or "daily" in s:
            return "daily"
        if "streak" in s:
            return "streak"
        return "open_only"

    @staticmethod
    def _skipped(offer_id: str, title: str) -> bool:
        s = f"{offer_id} {title}".lower()
        return any(p.lower() in s for p in SKIP_PATTERNS)

    def _normalize_card(self, item: dict, kind: str | None = None) -> dict | None:
        if not isinstance(item, dict):
            return None
        offer = item.get("offerId") or item.get("offerid") or item.get("id") or item.get("name")
        hash_ = item.get("hash") or item.get("activityId")
        title = item.get("title") or item.get("name") or item.get("description") or ""
        points = int(item.get("points") or item.get("pointProgressMax") or item.get("max") or 0)
        done_max = int(item.get("pointProgressMax") or 0)
        done_cur = int(item.get("pointProgress") or 0)
        done = bool(item.get("isCompleted") or item.get("complete") or item.get("completed")
                    or (done_max > 0 and done_cur >= done_max))
        if not offer or not hash_ or points <= 0 or done or self._skipped(str(offer), str(title)):
            return None
        return {"title": title, "points": points, "offerId": offer, "hash": hash_,
                "kind": kind or self._infer_kind(str(offer), str(title)),
                "url": item.get("destinationUrl") or item.get("destination") or REWARDS + "/",
                "source": "getuserinfo"}

    def discover_cards(self) -> list[dict]:
        cards: list[dict] = []
        seen: set[str] = set()
        skipped_zero = 0

        def push(c):
            nonlocal skipped_zero
            if not c:
                return
            # 只领真正加分的卡片。+0p 的纯展示横幅（活动预告/壁纸/推荐等）一律跳过，
            # 领了也不加分，还白耗时间和请求次数。
            if int(c.get("points") or 0) <= 0:
                skipped_zero += 1
                self.debug("🧩", f"跳过 0 积分卡片 [{c['kind']}] {c['title'] or c['offerId']}")
                return
            key = f"{c['offerId']}:{c['hash']}"
            if key in seen:
                self.debug("🧩", f"卡片去重跳过: {key}")
                return
            seen.add(key)
            cards.append(c)
            self.debug("🧩", f"发现卡片 [{c['kind']}] {c['title'] or '(无标题)'} "
                             f"+{c['points']}p offerId={c['offerId']} "
                             f"hash={str(c['hash'])[:16]}… 来源={c['source']}")

        self.debug("🧩", "扫描活动卡片（来源: earn 页面 RSC / HTML 解析）")
        try:
            html = self._fetch_earn_html()
            clean = html.replace('\\"', '"')
            na = (re.search(r'name":"next-action"[^}]*"value":"([a-f0-9]{40,})"', html)
                  or re.search(r'next-action["\']\s*:\s*["\']([a-f0-9]{40,})["\']', html)
                  or re.search(r'"next-action"[^"]*"([a-f0-9]{40,})"', clean))
            if na:
                self.next_action = na.group(1)

            for obj in re.findall(r'\{[^{}]*"offerId"[^{}]*\}', clean):
                o = re.search(r'"offerId":"([^"]+)"', obj)
                h = re.search(r'"(?:hash|activityId|id)":"([^"]+)"', obj)
                p = re.search(r'"points":(\d+)', obj)
                t = re.search(r'"title":"([^"]+)"', obj)
                done = re.search(r'"isCompleted":true|"completed":true', obj, re.I)
                if not (o and h) or done:
                    continue
                title = t.group(1) if t else ""
                if self._skipped(o.group(1), title):
                    continue
                push({"title": title, "points": int(p.group(1)) if p else 0,
                      "offerId": o.group(1), "hash": h.group(1),
                      "kind": self._infer_kind(o.group(1), title),
                      "url": REWARDS + "/earn", "source": "earn"})

            for oid, hs in re.findall(r'data-offer-id="([^"]+)"[^>]*data-hash="([^"]+)"', html):
                if not self._skipped(oid, ""):
                    # 没有 points 字段的多半是 0 分展示位，points=0 会被 push 过滤掉
                    push({"title": "", "points": 0, "offerId": oid, "hash": hs,
                          "kind": "open_only", "url": REWARDS + "/earn", "source": "html"})
        except Exception as e:
            self.log("🔴", f"卡片解析失败: {e}")
        if skipped_zero:
            self.log("🧩", f"已过滤 {skipped_zero} 张 0 积分纯展示卡片（只做加分任务）")
        return cards

    def claim_card(self, card: dict) -> dict:
        """用 DAPI activities 完成一张卡片（新版门户仍然支持）。

        实测：POST /dapi/me/activities
              {"amount":1,"country":"cn","id":"<随机>","type":101,
               "attributes":{"offerid":"<卡片 offerId>"}}
        返回 response.activity.p = 本次得分（拼图 +5 / 每日活动 +10 / 组合卡甚至 +80），
        这比在浏览器里点按钮可靠得多，而且完全静默。

        返回 {ok, points, duplicate, balance, reason}
        """
        out = {"ok": False, "points": 0, "duplicate": False, "balance": 0,
               "reason": "", "offerId": card.get("offerId", "")}
        if self.dry_run:
            out.update(ok=True, reason="dry-run 跳过领取")
            return out
        if not self.token:
            out["reason"] = "没有 OAuth Token（卡片领取走 DAPI，需要授权），请先一键登录"
            return out
        body = {"amount": 1, "country": self._region(), "id": _long_id(), "type": 101,
                "attributes": {"offerid": card["offerId"]}}
        try:
            r = self.with_token_retry(
                lambda t: self.http.post(DAPI_ACT, json_body=body, headers=self.dapi_headers(t)))
        except HttpError as e:
            out["reason"] = f"HTTP {e.status}: {self._redact(e.body)[:100]}"
            return out
        except Exception as e:
            out["reason"] = f"{type(e).__name__}: {e}"
            return out
        if not r:
            out["reason"] = "请求未发出（Token 不可用）"
            return out
        try:
            root = r.json()
        except Exception:
            out["reason"] = f"响应不是 JSON: {self._redact(r.text)[:100]}"
            return out
        resp = root.get("response") or {}
        act = resp.get("activity") or {}
        out["points"] = int(act.get("p") or 0)
        out["duplicate"] = bool(resp.get("isDuplicate"))
        out["balance"] = int(resp.get("balance") or 0)
        if out["points"] > 0:
            out["ok"] = True
        elif out["duplicate"]:
            out["ok"] = True
            out["reason"] = "服务器标记 isDuplicate（今天已完成过）"
        else:
            out["reason"] = (f"服务器返回 code={root.get('code')} p=0，"
                             f"该卡片当前不给分（已完成 / 未解锁 / 活动已结束）")
        return out

    def claim_card_bool(self, card: dict) -> bool:
        return self.claim_card(card)["ok"]

    # ------------------------------------------------------------ 浏览器 DOM（HTTP 兜底）
    def _dom_log(self, level: str, icon: str, msg: str) -> None:
        if level == "DEBUG":
            self.debug(icon, msg)
        else:
            self.log(icon, msg)

    def _dom_session(self):
        from browser_dom import DomSession
        return DomSession(self.base_dir, log=self._dom_log,
                          minimized=bool(self.cfg.get("browser_dom_minimized", True)))

    def browser_claim_points(self) -> dict:
        """用浏览器点 dashboard 的「可领取 N 领取」。

        :return: {clicked, before, after, gained, page_amount, reason}
        """
        out = {"clicked": False, "before": 0, "after": 0, "gained": 0,
               "page_amount": None, "reason": ""}
        if self.dry_run:
            self.log("🧪", "[dry-run] 跳过浏览器领取")
            return out
        out["before"] = self.get_balance(ttl=0)
        ds = self._dom_session()
        try:
            r = ds.claim_points() or {}
            out["clicked"] = int(r.get("clicks") or 0) > 0
            out["page_amount"] = r.get("page_amount")
            out["reason"] = r.get("reason") or ""
            if out["clicked"]:
                # 入账可能延迟几秒，多探几次；ttl=0 防止读到 15 秒缓存里的旧值
                for _ in range(4):
                    time.sleep(3)
                    out["after"] = self.get_balance(ttl=0)
                    if out["after"] > out["before"]:
                        break
                out["gained"] = max(0, out["after"] - out["before"])
        finally:
            ds.close()
        return out

    def browser_complete_cards(self, cards: list[dict]) -> dict:
        """用浏览器完成 HTTP 领不了的卡片（拼图/Quiz/每日活动页）。"""
        out = {"tried": len(cards), "done": 0, "before": 0, "after": 0, "gained": 0}
        if self.dry_run or not cards:
            return out
        out["before"] = self.get_balance()
        ds = self._dom_session()
        try:
            for c in cards:
                if self.stopped():
                    self.log("⏹", "浏览器任务：已按用户要求中止")
                    break
                if ds.complete_offer(c):
                    out["done"] += 1
                self.random_delay(4000, 8000)
        finally:
            ds.close()
        out["after"] = self.get_balance()
        out["gained"] = max(0, out["after"] - out["before"])
        return out

    # ------------------------------------------------------------ 搜索
    def _hot_cache_path(self) -> str:
        return os.path.join(self.base_dir, "hot_words.json")

    def _hot_cache_load(self) -> list[str]:
        """同一自然日内复用热词，避免每轮都去请求（当天热词基本不变）。"""
        try:
            with open(self._hot_cache_path(), "r", encoding="utf-8") as f:
                data = json.load(f)
            if str(data.get("date")) == str(_today_num()):
                words = [str(w) for w in (data.get("words") or []) if str(w).strip()]
                if words:
                    self.debug("🔥", f"复用当日热词缓存 {len(words)} 个（来源 {data.get('source', '?')}）")
                    return words
        except Exception:
            pass
        return []

    def _hot_cache_save(self, words: list[str], source: str) -> None:
        try:
            with open(self._hot_cache_path(), "w", encoding="utf-8") as f:
                json.dump({"date": _today_num(), "source": source, "words": words},
                          f, ensure_ascii=False, indent=2)
        except Exception as e:
            self.debug("🔥", f"热词缓存写入失败: {e}")

    def fetch_hot_words(self, limit: int = 0) -> list[str]:
        """抓取今日热词（默认 B 站热搜），失败逐级降级；成功即按日缓存。"""
        limit = limit or int(self.cfg.get("search_hot_count", 50) or 50)
        limit = max(5, min(100, limit))
        cached = self._hot_cache_load()
        if cached:
            return cached[:limit]
        for name, kind, tpl in HOT_SOURCES:
            url = tpl.format(limit=limit)
            try:
                r = self.http.get(url, headers={
                    "User-Agent": self.ua_pc,
                    "Referer": "https://www.bilibili.com/",
                    "Accept": "application/json, text/plain, */*",
                })
                words = _parse_hot_words(kind, r.json())[:limit]
            except Exception as e:
                self.debug("🔥", f"{name} 获取失败: {type(e).__name__}: {e}")
                continue
            if len(words) >= 3:
                self.log("🔥", f"{name} 今日热词 {len(words)} 个：{' / '.join(words[:5])} …")
                self._hot_cache_save(words, name)
                return words
            self.debug("🔥", f"{name} 仅解析到 {len(words)} 个词，换下一个来源")
        self.log("🟡", "热词来源全部不可用，本轮回退内置词库")
        return []

    def get_search_word(self) -> str:
        mode = str(self.cfg.get("search_word_source", "bilibili") or "bilibili")
        if mode != "offline":
            if not self.hot_words:
                self.hot_words = self.fetch_hot_words()
                self.hot_index = 0
            # 热词按榜单顺序一个个用，跳过今天用过的（同一个词 Bing 只算一次分）
            while self.hot_index < len(self.hot_words):
                w = self.hot_words[self.hot_index]
                self.hot_index += 1
                if w in self._used_words:
                    continue
                self._remember_word(w)
                return w
            # 词池耗尽：重新洗牌再来一轮（新的一天会重新抓）
            if self.hot_words:
                self._used_words = {w for w in self._used_words if w not in self.hot_words}
                random.shuffle(self.hot_words)
                self.hot_index = 1
                w = self.hot_words[0]
                self._remember_word(w)
                self.debug("🔥", "今日热词已用尽一轮，重新洗牌复用")
                return w
            mode = "offline"

        # offline 词源：今天用过的词不再用，重复搜同一个词服务器不计分
        for _ in range(8):
            w = random.choice(SEARCH_POOL)
            if w not in self._used_words:
                self._remember_word(w)
                return w
        self._used_words = {w for w in self._used_words if w not in SEARCH_POOL}
        w = random.choice(SEARCH_POOL)
        self._remember_word(w)
        return w

    # ------------------------------------------------------------ 每日随机目标
    def _draw_target_pct(self, kind: str) -> float:
        """按配置的比例区间随机抽一个百分比。"""
        if kind == "search":
            lo_p = int(self.cfg.get("search_target_min_pct", 55))
            hi_p = int(self.cfg.get("search_target_max_pct", 90))
        else:
            lo_p = int(self.cfg.get("read_target_min_pct", 40))
            hi_p = int(self.cfg.get("read_target_max_pct", 85))
        lo_p = max(5, min(lo_p, 94))
        hi_p = max(lo_p, min(hi_p, 95))
        return random.uniform(lo_p, hi_p)

    def _today_target_store(self) -> dict:
        """今天的目标比例（search_pct / read_pct，存 state.json 跨轮次复用）。

        生成时机：
          * 守护模式每天收尾时会预生成「明日目标」→ 新的一天直接继承；
          * 前一天没生成（比如没开守护）→ 首次用到时现场静默抽一份，
            不再单独推送（目标值随成果通知一起展示）。
        """
        store = self.state.setdefault("daily_targets", {})
        if str(store.get("date")) == str(_today_num()) and "search_pct" in store:
            return store
        nxt = self.state.get("next_day_targets") or {}
        if str(nxt.get("date")) == str(_today_num()) and "search_pct" in nxt:
            pcts = {"search_pct": nxt["search_pct"], "read_pct": nxt["read_pct"]}
        else:
            pcts = {"search_pct": self._draw_target_pct("search"),
                    "read_pct": self._draw_target_pct("read")}
        store.clear()
        store.update(pcts)
        store["date"] = _today_num()
        self.save_state()
        return store

    def daily_target(self, kind: str, max_value: int) -> int:
        """当天的目标值（故意不满额，且每天随机）。

        为什么要有它：每天把搜索 60/60、阅读 30/30 都做满，行为太机械，
        容易被判脚本。目标按每天随机抽的百分比折算，硬性保证 target < max。
        存的是百分比而不是绝对值——这样明天的目标可以在今天配额未知时就先抽好。

        :param kind: "search" / "read"
        :param max_value: 服务器给的上限（配额满值，单位是「分」）
        """
        if max_value <= 1:
            return max(0, max_value)
        if not self.cfg.get("random_daily_target", True):
            return max_value
        pct = float(self._today_target_store().get(f"{kind}_pct", 70.0))
        target = int(round(max_value * pct / 100.0))
        target = (target // POINT_STEP) * POINT_STEP      # 对齐到 3 的倍数
        # 一定不满额：至少比上限低一个步长
        return max(POINT_STEP, min(max_value - POINT_STEP, target))

    def roll_next_day_targets(self) -> str:
        """预生成明天的目标比例（幂等：明天的那份已抽好就原样返回）。"""
        tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y%m%d")
        nxt = self.state.setdefault("next_day_targets", {})
        if str(nxt.get("date")) != str(tomorrow):
            nxt.clear()
            nxt["date"] = tomorrow
            nxt["search_pct"] = self._draw_target_pct("search")
            nxt["read_pct"] = self._draw_target_pct("read")
            self.save_state()
        return tomorrow

    def push_next_day_target(self) -> None:
        """守护模式一轮完整跑完后调用：生成明天目标并推送（每天只推一次）。"""
        tomorrow = self.roll_next_day_targets()
        if str(self.state.get("next_day_pushed")) == str(tomorrow):
            return
        self.state["next_day_pushed"] = tomorrow
        self.save_state()
        nxt = self.state.get("next_day_targets") or {}
        info = self.get_rewards_info()
        smax = int(info["pc"]["max"]) if info else 60
        rmax = int(info["read_max"]) if info else 30
        s = max(1, min(smax - 1, int(round(smax * float(nxt.get("search_pct", 70.0)) / 100.0))))
        r = max(1, min(rmax - 1, int(round(rmax * float(nxt.get("read_pct", 60.0)) / 100.0))))
        self.log("🔮", f"明日目标（随机，不会拿满）：搜索约 {s}/{smax} · 阅读约 {r}/{rmax}"
                       f"（明天守护/测试都按这个停）", True, category="target")

    # ------------------------------------------------------------ 搜索词使用记录
    def _load_used_words(self) -> None:
        """从 state.json 恢复今天的已用搜索词。

        为什么要持久化：Bing 对同一个搜索词一天只算一次分。以前每轮重启都从热词
        榜第一条开始，于是「第一搜索永远不加分」——日志里表现为 increment=1
        但配额纹丝不动。
        """
        try:
            d = self.state.get("search_words") or {}
            if str(d.get("date")) == str(_today_num()):
                self._used_words = {str(w) for w in (d.get("words") or []) if str(w).strip()}
        except Exception:
            self._used_words = set()

    def _remember_word(self, word: str) -> None:
        self._used_words.add(word)
        try:
            words = sorted(self._used_words)[-200:]
            self.state["search_words"] = {"date": _today_num(), "words": words}
            self.save_state()
        except Exception:
            pass

    def _ensure_rwho(self) -> None:
        """桌面端标记 _Rwho=u=d。

        原脚本是在 fetch 里设 Cookie 头，但浏览器禁止脚本设置 Cookie 头（会被忽略），
        所以真实请求仍带着登录 Cookie。Python 里显式 Cookie 头会**覆盖整个 Cookie jar**
        （_U 等登录 Cookie 全丢），搜索就成了未登录请求 → 页面正常返回但永远不计分。
        正确做法：把 _Rwho 写进 jar，让登录 Cookie 一起发。
        """
        if self._rwho_done:
            return
        try:
            from http_client import _make_cookie
            self.http.jar.set_cookie(_make_cookie(0, "_Rwho", f"u=d&ts={_today_str()}", ".bing.com"))
            self._rwho_done = True
            self.debug("🔍", f"已注入 _Rwho 桌面标记（jar 现有 {len(self.http.jar)} 条 Cookie）")
        except Exception as e:
            self.debug("🔍", f"_Rwho 注入失败: {type(e).__name__}: {e}")

    def get_search_page(self, query: str) -> str:
        self._ensure_rwho()
        url = f"https://{self.search_host}/search?q={quote(query)}&form=QBLH{self._mkt()}"
        r = self.http.get(url, headers={
            "User-Agent": self.ua_pc,
            "Referer": f"https://{self.search_host}/?form=QBLH",
        })
        # 登录态校验：未登录的搜索页不会有账号相关元素，白搜不计分
        logged = ('"id_avatar"' in r.text or "id_h" in r.text) and "Rewards" in r.text
        self.debug("🔍", f"搜索页返回 {len(r.text)}B, 关键词「{query}」, 登录态={logged}")
        return r.text

    @staticmethod
    def _extract_ig(html: str) -> str:
        """从 SERP 页面里取本次搜索的 IG（上报必须带页面的 IG，随机 UUID 无效）。"""
        for pat in (r'IG:"([0-9A-Fa-f]{16,})"', r'"IG"\s*:\s*"([0-9A-Fa-f]{16,})"'):
            m = re.search(pat, html)
            if m:
                return m.group(1)
        return ""

    def pc_progress_now(self, retries: int = 2) -> dict | None:
        """强制拉最新 PC 搜索配额（不吃缓存），搜索前后核对真实增量用。

        刚上报完的那几秒，DAPI 偶尔会返回不含 promotions 的残缺对象（记账中），
        所以失败要重试两次，别一次读不到就断定「没到账」。
        """
        for i in range(max(1, retries)):
            self._dapi613 = ({}, 0.0)
            r = self._quota_from_dapi()
            if r:
                return {"progress": r["pc"]["progress"], "max": r["pc"]["max"],
                        "balance": int(r.get("balance") or 0)}
            if i + 1 < retries:
                time.sleep(0.9)
        return None

    def search_once(self, query: str, before: int | None = None,
                    verify_timeout: float = 50.0) -> dict:
        """纯 HTTP 搜索一次，并轮询服务器配额确认是否真的计分。

        流程与真实 Edge 完全一致（抓包比对过）：
          1) GET  {search_host}/search?q=..&form=QBLH   ← 从页面取 IG
          2) POST {search_host}/rewardsapp/ncheader      IID=SERP.5047
          3) POST {search_host}/rewardsapp/reportActivity IID=SERP.5057
          4) 轮询 DAPI options=613 的真实 progress，差值 > 0 才算成功

        入账有时是秒级、有时要 30~60 秒，所以这里必须轮询而不是查一次就下结论。
        返回 {ok, query, ig, http, increment, before, after, delta, delay, reason}
        """
        out = {"ok": False, "query": query, "ig": "", "http": 0, "increment": 0,
               "before": before if before is not None else 0, "after": 0,
               "delta": 0, "delay": 0, "reason": "",
               "daily_earned": -1, "daily_limit": 0, "source": ""}
        if self.dry_run:
            out.update(ok=True, reason="dry-run 跳过真实请求")
            return out

        pre = None if before is not None else self.pc_progress_now()
        if before is None and pre:
            out["before"] = pre["progress"]
        if pre and pre["max"] and pre["progress"] >= pre["max"]:
            out["reason"] = "配额已满，跳过"
            out["after"] = pre["progress"]
            return out

        self._ensure_rwho()
        host = self.search_host
        url = f"https://{host}/search?q={quote(query)}&form=QBLH{self._mkt()}"
        headers = {
            "User-Agent": self.ua_pc,
            "Referer": f"https://{host}/?form=QBLH",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }
        try:
            page = self.http.get(url, headers=headers)
            out["http"] = page.status
            html = page.text or ""
        except HttpError as e:
            out["http"] = e.status
            out["reason"] = f"搜索页 HTTP {e.status}"
            return out
        except Exception as e:
            out["reason"] = f"搜索页请求异常: {type(e).__name__}: {e}"
            return out

        logged = ('"id_avatar"' in html or "id_h" in html) and "Rewards" in html
        if not logged:
            out["reason"] = "搜索页未显示登录态（Cookie 可能过期），此搜索不计分，请重新一键登录"
            return out
        ig = self._extract_ig(html)
        out["ig"] = ig
        if not ig:
            out["reason"] = "搜索页里没有 IG 字段（页面结构变化或未登录），无法上报"
            return out

        ajax = {
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "User-Agent": self.ua_pc,
            "Referer": url,
            "Origin": f"https://{host}",
            "X-Requested-With": "XMLHttpRequest",
        }
        try:
            nh = self.http.post(
                f"https://{host}/rewardsapp/ncheader?ver={str(int(time.time() * 1000))[:8]}"
                f"&IID={SEARCH_IID_HEADER}&IG={ig}&ajaxreq=1",
                data="wb=1%3bi%3d1%3bv%3d1", headers=ajax)
            self.debug("🔍", f"ncheader: HTTP {nh.status}，{len(nh.text)}B")
        except Exception as e:
            self.debug("🔍", f"ncheader 失败(不阻断): {type(e).__name__}: {e}")

        try:
            rc = self.http.post(
                f"https://{host}/rewardsapp/reportActivity?IG={ig}&IID={SEARCH_IID_REPORT}"
                f"&q={quote(query)}&form=QBLH&ajaxreq=1",
                data=f"url={quote(url)}&V=web", headers=ajax)
        except HttpError as e:
            out["reason"] = f"上报接口 HTTP {e.status}: {self._redact(e.body)[:80]}"
            return out
        except Exception as e:
            out["reason"] = f"上报请求异常: {type(e).__name__}: {e}"
            return out

        body = rc.text or ""
        if '"IsAuthenticated":false' in body:
            out["reason"] = "上报返回未登录（Cookie 里没有 _U），请重新一键登录"
            return out
        m = re.search(r'"RewardsIncrement":(-?\d+)', body)
        out["increment"] = int(m.group(1)) if m else 0

        # 首选判据：上报响应里自带「今日搜索已得积分」，这是服务器算出来的，
        # 比再问一次 DAPI 更快也更准（实测 before=18 → 响应 21）。
        t0 = time.time()
        me = re.search(r'"DailySearchPointsEarned":(-?\d+)', body)
        ml = re.search(r'"DailySearchPointsLimit":(-?\d+)', body)
        out["daily_earned"] = int(me.group(1)) if me else -1
        out["daily_limit"] = int(ml.group(1)) if ml else 0
        if out["before"] > 0 and out["daily_earned"] > out["before"]:
            out["after"] = out["daily_earned"]
            out["delta"] = out["daily_earned"] - out["before"]
            out["ok"] = True
            out["delay"] = round(time.time() - t0, 1)
            out["source"] = "reportActivity"
            return out

        # 兜底：轮询 DAPI 真实配额（有时响应里的计数滞后，要等几十秒才入账）
        # 只轮两次就够：reportActivity 没给增量基本就是「这个词今天搜过了」，
        # 多轮询只是白占服务器的会话。
        deadline = t0 + verify_timeout
        post = None
        for gap in (5, 15):
            if time.time() >= deadline:
                break
            time.sleep(min(gap, max(1.0, deadline - time.time())))
            post = self.pc_progress_now()
            if not post:
                continue
            out["after"] = post["progress"]
            out["delta"] = max(0, post["progress"] - out["before"])
            if out["delta"] > 0:
                break
        out["delay"] = round(time.time() - t0, 1)
        out["ok"] = out["delta"] > 0
        if out["ok"]:
            out["source"] = "dapi"
            return out

        limit = out["daily_limit"] or (post["max"] if post else 0)
        now = post["progress"] if post else out["before"]
        if out["increment"] > 0:
            out["reason"] = (f"上报返回 RewardsIncrement={out['increment']}，但今日搜索积分仍是 "
                             f"{now}/{limit}：多半是这个搜索词今天已经搜过"
                             f"（Bing 同一个词一天只算一次），换词再试")
        else:
            out["reason"] = (f"服务器没有给出增量，今日搜索积分仍是 {now}/{limit}"
                             f"（可能已达上限/账号受限/风控）")
        return out

    def report_search(self, query: str) -> bool:
        """兼容旧调用：内部已改为 search_once（带真实增量核对）。"""
        return self.search_once(query)["ok"]

    # ------------------------------------------------------------ 每日活动
    def get_daily_set_links(self) -> list[str]:
        """从 rewards 页面抓 rnoreward=1 的每日活动链接（替代原脚本的 DOM 点击）。"""
        links: list[str] = []
        for page in (REWARDS + "/dashboard", REWARDS + "/earn"):
            try:
                html = self.http.get(page, headers={"User-Agent": self.ua_pc}).text
                found = re.findall(r'href=["\']([^"\']*rnoreward=1[^"\']*)["\']', html)
                self.debug("📅", f"页面 {page} 长度 {len(html)}B，匹配到 {len(found)} 个 rnoreward=1 链接")
                for href in found:
                    href = href.replace("&amp;", "&")
                    if href.startswith("//"):
                        href = "https:" + href
                    elif href.startswith("/"):
                        href = "https://cn.bing.com" + href
                    elif not href.startswith("http"):
                        href = "https://cn.bing.com/" + href
                    if href not in links:
                        links.append(href)
            except Exception:
                continue
        return links

    # ------------------------------------------------------------ 连签 / 待领积分
    # 印章进度只能读页面上结构化的 progressbar 字段：
    #   \"aria-label\":\"收集的印章\",\"aria-valuemax\":12,\"aria-valuemin\":0,\"aria-valuenow\":2
    # 千万别用「(\d+) 个印章」这种正则 —— 说明文案里写着「收集 12 个印章可赚取 1,000 分」，
    # 会永远返回 12（曾因此谎报 12/12）。
    _STAMP_NOW = r"收集的印章[\s\S]{0,320}?aria-valuenow\\?\"?\s*:\s*(\d+)"
    _STAMP_MAX = r"收集的印章[\s\S]{0,320}?aria-valuemax\\?\"?\s*:\s*(\d+)"

    def _streak_text(self) -> tuple:
        """取连签数据所在页面并转纯文本：earn 优先，失败退到 dashboard。

        返回 (html, text)；两个来源都拿不到时返回 ("", "")。
        """
        for url in (f"{REWARDS}/earn", f"{REWARDS}/dashboard"):
            name = url.rsplit("/", 1)[-1]
            try:
                html = self.http.get(url, headers={"User-Agent": self.ua_pc},
                                     retries=1).text
            except Exception as e:
                self.log("🟡", f"连签页面 {name} 获取失败: {type(e).__name__}: {e}")
                continue
            if not html or len(html) < 20000:
                self.log("🟡", f"连签页面 {name} 内容异常（{len(html or '')}B），换下一个来源")
                continue
            text = re.sub(r"\s+", " ", re.sub(r"<[^>]*>", " ", html))
            if "每日连续打卡" in text:
                self.debug("📅", f"连签检测: {name} {len(html)}B → 纯文本 {len(text)} 字符")
                return html, text
            self.log("🟡", f"连签页面 {name} 里没有连签区块，换下一个来源")
        return "", ""

    def check_streak(self) -> dict:
        out = {"days": None, "tasks": {}, "visual_done": True,
               "stamps": None, "stamps_max": None, "ok": False}
        try:
            html, text = self._streak_text()
            if not text:
                self.log("🔴", "连签检测失败：页面取不到，沿用上次记录，下一轮再试")
                return out
            out["ok"] = True

            m = re.search(r"每日连续打卡\s*(\d+)", text)
            if m:
                out["days"] = int(m.group(1))
                self.log("📅", f"每日连续打卡：{out['days']} 天")

            patterns = [
                ("必应搜索连续打卡", r"必应搜索连续打卡[\s\S]*?搜索:\s*(\d+)/(\d+)"),
                ("每日连续打卡活动", r"每日连续打卡活动[\s\S]*?活动:\s*(\d+)/(\d+)"),
                ("必应应用连续打卡", r"必应应用连续打卡[\s\S]*?签到:\s*(\d+)/(\d+)"),
                ("视觉搜索连续打卡", r"视觉搜索连续打卡[\s\S]*?活动:\s*(\d+)/(\d+)"),
            ]
            for name, pat in patterns:
                mm = re.search(pat, text)
                if mm:
                    cur, mx = int(mm.group(1)), int(mm.group(2))
                    out["tasks"][name] = f"{cur}/{mx}"
                    done = cur >= mx
                    self.log("✅" if done else "📅", f"{name}: {cur}/{mx}{' 已完成' if done else ''}")
                    if name == "视觉搜索连续打卡" and not done:
                        out["visual_done"] = False

            # 印章：优先结构化 aria 值；找不到就明确“未取到”，绝不猜
            sm_now = re.search(self._STAMP_NOW, html)
            sm_max = re.search(self._STAMP_MAX, html)
            if sm_now:
                mx = int(sm_max.group(1)) if sm_max else 12
                out["stamps_max"] = mx
                out["stamps"] = min(int(sm_now.group(1)), mx)
                self.log("📅", f"连签奖励印章进度: {out['stamps']}/{mx}")
            else:
                # 退路：整页文本里「还需 N 个印章」（满 12 个）
                fb = re.search(r"还需\s*(\d+)\s*个印章", text)
                if fb:
                    out["stamps_max"] = 12
                    out["stamps"] = max(0, 12 - int(fb.group(1)))
                    self.log("📅", f"连签奖励印章进度: {out['stamps']}/12（由“还需”倒推）")
                else:
                    self.debug("📅", "页面里没找到印章进度字段，本次不显示（不猜数字）")
        except Exception as e:
            self.log("🔴", f"连签检测异常: {type(e).__name__}: {e}")
        return out

    def check_claimable_points(self) -> int:
        try:
            html = self.http.get(REWARDS + "/dashboard", headers={"User-Agent": self.ua_pc}).text
            self.debug("🎁", f"dashboard 页面 {len(html)}B，查找 alt=\"可领取\" 标记")
            m = re.search(r'alt="可领取"[^>]*>[\s\S]*?(\d[\d,]*)', html, re.I)
            if m:
                self.debug("🎁", f"匹配到待领积分片段: {m.group(0)[:80]}")
                return int(m.group(1).replace(",", ""))
            self.debug("🎁", "未匹配到可领取积分标记")
        except Exception as e:
            self.log("🟡", f"检测可领取积分失败: {e}")
            self.debug("🎁", f"检测异常: {type(e).__name__}: {e}")
        return 0
