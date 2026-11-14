"""
Microsoft Rewards 自动化 —— 命令行入口（零第三方依赖）

整个目录自包含：配置、Cookie、Token、状态、日志全部放在本文件所在目录内，
把整个文件夹剪切/拷贝到任意工作空间都能直接运行，无需修改任何路径。

用法：
  GUI（推荐）：账号与授权 → 「🌐 一键登录」会自动打开独立浏览器窗口，
               登录后自动采集完整 Cookie（含 HttpOnly）、验证积分并完成 OAuth 授权；
               也支持「手动复制 + 图文步骤」备用路径。
  python main.py --setup                  命令行首次配置向导（导入 Cookie + 授权）
  python main.py --check                 体检：登录态 / 地区 / 积分 / 搜索配额
  python main.py                         跑一轮全部已启用任务
  python main.py --task sign,read        只跑指定任务（后面裁剪功能很好用）
  python main.py --dry-run               只查询、不发任何写请求
  python main.py --test                  测试一轮：每个接口跑 1~2 次，逐项记录成败与原因
  python main.py --daemon                后台常驻：等电脑空闲 5 分钟才动手，任务分散到几小时
  python main.py --list                  列出所有任务及开关状态
  python main.py --disable search,quiz   关闭某任务（写回 config.json）
  python main.py --enable search         开启某任务
  python main.py --import-cookies <路径或Cookie串>
  python main.py --auth "<授权后地址栏URL或code>"

图形界面（PyQt5，使用 conda 环境 test001）：
  双击桌面「MS Rewards 控制台」快捷方式
  或 D:\\Anaconda\\envs\\test001\\pythonw.exe gui_qt.py
  或 python main.py --gui   （自动优先 PyQt5，无则回退 tkinter）

打包给第三方：运行 build_exe.bat（PyInstaller），产物在 dist/MSRewards.exe。
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from urllib.parse import urlparse, parse_qs

# Windows 控制台中文/emoji 输出兜底
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# 关键：所有路径相对程序自身，整个目录可随意搬迁。
# 打包成 exe（PyInstaller）后 __file__ 指向临时解压目录，配置会丢失，
# 因此 frozen 模式下改用 exe 所在目录，保证第三方直接双击即可持久化配置。
if getattr(sys, "frozen", False):
    # 便携优先：exe 放哪，配置/日志/Cookie/浏览器 profile 就在哪，整个文件夹可搬走。
    BASE_DIR = os.path.dirname(os.path.abspath(sys.executable))
    # 但如果 exe 被丢到没写权限的地方（Program Files、只读盘、压缩包直开），
    # 写不进去就会变成"设置一关就没"，这里探测一次并退回 %LOCALAPPDATA%\MSRewards。
    try:
        _probe = os.path.join(BASE_DIR, ".write-test")
        with open(_probe, "w", encoding="utf-8") as _f:
            _f.write("1")
        os.remove(_probe)
    except OSError:
        _fallback = os.path.join(
            os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "MSRewards")
        try:
            os.makedirs(_fallback, exist_ok=True)
            BASE_DIR = _fallback
        except OSError:
            pass
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
AUTH_PATH = os.path.join(BASE_DIR, "auth.json")
STATE_PATH = os.path.join(BASE_DIR, "state.json")
COOKIE_PATH = os.path.join(BASE_DIR, "cookies.txt")

from rewards import RewardsAPI, AUTH_URL          # noqa: E402
import tasks                                      # noqa: E402

DEFAULT_CONFIG = {
    "tasks": {
        "sign": True, "read": True, "promos": True, "quiz": True,
        "search": True, "streak": True, "daily_set": True,
        "claim_points": True, "rescan": True,
    },
    "lock_region": True,
    "host": "www.bing.com",
    "search_span_seconds": 30,
    "search_batch_min": 4,
    "search_batch_max": 7,
    "search_word_source": "bilibili",      # bilibili（B站今日热词）| hot（热搜API）| offline（内置词库）
    "search_hot_count": 50,                # 每次抓取多少个热词（5-100）
    "search_mode": "http",                 # http（纯 HTTP 静默上报）| browser（真实浏览器）
    "search_host": "cn.bing.com",          # 搜索上报域名：只有 cn.bing.com 会被计分（实测）
    "search_until_full": True,             # 搜索时一直做到今日目标（每次核对服务器配额）
    "random_daily_target": True,           # 每日目标随机、且故意不满额（不像脚本）
    "search_target_min_pct": 55,           # 搜索目标占配额百分比下限
    "search_target_max_pct": 90,           # 搜索目标占配额百分比上限（<95，永远拿不满）
    "read_target_min_pct": 40,             # 阅读目标占配额百分比下限
    "read_target_max_pct": 85,             # 阅读目标占配额百分比上限
    "delay_min_ms": 3000,
    "delay_max_ms": 8000,
    "http_timeout": 15,
    "request_min_gap_s": 1.2,              # 发往微软的两条请求之间至少隔多少秒（办完即断，不像脚本连发）
    "daemon_interval_minutes": 60,         # 两轮之间的间隔（一轮本身已被摊到几小时）
    "daemon_interval_jitter_minutes": 30,  # 间隔随机浮动 ±N 分钟
    "daemon_start_delay_min_s": 5,
    "daemon_start_delay_max_s": 95,
    "daemon_require_idle": True,           # 只在电脑空闲时才执行（不打扰使用）
    "daemon_idle_seconds": 300,            # 连续多久无键鼠输入算空闲（默认 5 分钟）
    "daemon_idle_poll_seconds": 20,        # 等待空闲时的检查间隔
    "daemon_idle_wait_max_minutes": 0,     # 最多等多久（0=一直等，直到你离开电脑）
    "daemon_task_gap_min_s": 300,          # 任务之间的随机间隔下限（5 分钟）
    "daemon_task_gap_max_s": 720,          # 任务之间的随机间隔上限（12 分钟）
    "daemon_round_max_minutes": 120,       # 整轮时长上限（分钟）：接近上限自动缩短剩余间隔，0=不限
    "daemon_sleep_after_done": True,       # 今日目标完成后收工：不再检测空闲、不再跑任务，睡到次日早上
    "daemon_wake_hour": 7,                 # 次日开始时间的基准整点（东八区，跟系统时区无关）
    "daemon_wake_window_minutes": 90,      # 在基准整点后随机 0~N 分钟内开始（07:00~08:30，每天不一样）
    "daemon_resume": False,                # 守护恢复标记：GUI 开始守护时置 True，托盘退出时清 False；
                                           # 开机自启动后 GUI 据此自动恢复守护（被断电/强杀时也保留）
    "priority_lane": True,                 # 每轮开头先集中做「签到 + 1 次搜索 + 每日卡片」（保连签）
    "priority_lane_cards": 3,              # 快线做几张卡片（每日活动是 3 张）
    "priority_lane_seconds": 180,          # 快线时间上限（秒），超了就把剩余卡片留给常规流程
    "keep_running": True,
    "browser_dom": True,               # 个别卡片失败时用已登录的 Edge 兜底（多数任务已可纯 HTTP 完成）
    "daemon_dom_when_away": True,      # 守护模式仅在判定「真无人使用」时才弹浏览器兜底
    "daemon_dom_away_minutes": 10,     # 判定真无人的空闲线（分钟）：锁屏/屏保立即算，无全屏且空闲达线才算
    "browser_dom_minimized": True,     # 手动运行时浏览器兜底窗口自动最小化，不抢焦点
    "verbose": True,                   # 详细日志：记录每个 HTTP 请求与业务决策
    "log_keep_days": 7,                # 日志只保留最近 N 天，超出先删最老的再写当天（3-365）
    "notify": {
        "wecom": "", "dingtalk": "", "feishu": "", "pushme": "", "bark": "",
        "win": True,     # Windows 系统通知（本机右下角弹窗，无需 key）
        "categories": {  # 通知开关，全部定义见 notify.SWITCH_RESULT / SWITCH_SYSTEM
            "sign": True, "read": True, "search": True, "cards": True,
            "claim": True, "lane": True, "summary": True, "test": True,
            "auth": True, "token": True, "region": True, "restricted": True,
            "target": True, "daydone": True,
        },
    },
}
DEFAULT_AUTH = {"refresh_token": "", "access_token": "", "token_time": 0}
DEFAULT_STATE = {"tasks": {}, "sign_point": -1, "restricted_times": 0,
                 "last_search_progress": -1, "daily_set_processed": [], "streak_days": 0}


def _load(path: str, default: dict) -> dict:
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            merged = dict(default)
            merged.update(data)
            return merged
        except Exception as e:
            print(f"⚠️  读取 {os.path.basename(path)} 失败: {e}，使用默认值")
    return dict(default)


def _save(path: str, data: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def make_api(dry_run: bool = False, verbose: bool | None = None) -> RewardsAPI:
    cfg = _load(CONFIG_PATH, DEFAULT_CONFIG)
    state = _load(STATE_PATH, DEFAULT_STATE)
    auth = _load(AUTH_PATH, DEFAULT_AUTH)
    if verbose is not None:
        cfg["verbose"] = verbose
    return RewardsAPI(cfg, state, auth, BASE_DIR, dry_run=dry_run)


def ensure_config() -> dict:
    cfg = _load(CONFIG_PATH, DEFAULT_CONFIG)
    if not os.path.isfile(CONFIG_PATH):
        _save(CONFIG_PATH, cfg)
    return cfg


def import_cookies(value: str, api: RewardsAPI) -> int:
    """value 可以是 Netscape cookies.txt 的路径，也可以是 `a=b; c=d` 字符串。"""
    if os.path.isfile(value):
        n = api.http.load_netscape(value)
        api.log("🟢", f"已从 {value} 导入 {n} 条 Cookie")
    else:
        n = api.http.set_cookie_string(value)
        api.log("🟢", f"已导入 {n} 条 Cookie 字符串")
    api.http.save_cookies(COOKIE_PATH)
    return n


def do_auth(value: str, api: RewardsAPI) -> bool:
    code = value.strip()
    if "code=" in code:
        code = parse_qs(urlparse(code).query).get("code", [""])[0] or code
    if not code or len(code) < 10:
        api.log("🔴", "没找到有效的 code，请粘贴授权后地址栏的完整 URL")
        return False
    ok = api.exchange_code(code)
    api.log("🟢" if ok else "🔴", "授权成功，refresh_token 已保存" if ok else "授权失败，请重新获取授权码")
    return ok


# ------------------------------------------------------------------ 向导
def setup() -> None:
    print("=" * 62)
    print("  Microsoft Rewards 自动任务 · 首次配置向导")
    print("=" * 62)
    cfg = ensure_config()
    api = make_api(dry_run=True)

    print("\n[1/3] 导入浏览器 Cookie（必做，用于 rewards.bing.com 登录态）")
    print("  方式 A：用浏览器扩展导出 cookies.txt（Netscape 格式），输入文件完整路径")
    print("  方式 B：登录 rewards.bing.com → F12 → Network → 任意请求 → 复制 Cookie 请求头整串粘贴")
    print("  （直接回车可跳过，稍后用 --import-cookies 补）")
    raw = input("Cookie 文件路径或字符串 > ").strip().strip('"')
    if raw:
        try:
            import_cookies(raw, api)
        except Exception as e:
            print(f"⚠️  Cookie 导入失败: {e}")

    print("\n[2/3] OAuth 授权（App签到 / 阅读任务需要，其余任务不依赖）")
    print("  在已登录的浏览器打开下面地址，登录后地址栏会变成")
    print("  https://login.live.com/oauth20_desktop.srf?code=xxxx... 把整串 URL 复制回来")
    print(f"\n  {AUTH_URL}\n")
    print("  如果之前油猴脚本已经授权过，也可以直接粘贴它的 refresh_token")
    raw2 = input("授权 URL / code / refresh_token（回车跳过）> ").strip().strip('"')
    if raw2:
        if raw2.startswith("http") or "code=" in raw2:
            do_auth(raw2, api)
        else:
            api.auth["refresh_token"] = raw2
            api.save_auth()
            print("🟢 refresh_token 已保存")

    print("\n[3/3] 通知推送（可选，全部回车跳过）")
    for key, name in (("wecom", "企业微信机器人 key"), ("dingtalk", "钉钉 access_token"),
                      ("feishu", "飞书 hook id"), ("pushme", "PushMe key"), ("bark", "Bark key")):
        v = input(f"  {name} > ").strip()
        if v:
            cfg.setdefault("notify", {})[key] = v
    _save(CONFIG_PATH, cfg)

    print("\n✅ 配置完成。接下来建议先跑一次体检：")
    print("   python main.py --check")
    print("   再试跑（不发写请求）：python main.py --dry-run")
    print(f"\n所有文件都在: {BASE_DIR}")


# ------------------------------------------------------------------ 体检
def check() -> None:
    api = make_api(dry_run=True)
    print("=" * 62)
    print("  运行体检")
    print("=" * 62)
    print(f"项目目录   : {BASE_DIR}")
    print(f"Cookie 文件: {'✅ 存在' if os.path.isfile(COOKIE_PATH) else '❌ 缺失（先跑 --setup 或 --import-cookies）'}")
    rt = (api.auth.get("refresh_token") or "").strip()
    print(f"OAuth Token: {'✅ 已保存 refresh_token' if rt else '⚠️  无（App签到/阅读会跳过，可用 --auth 补）'}")

    ok = api.check_region()
    print(f"地区检测   : {'✅ 通过 (CN)' if ok else '❌ 未通过'}")
    bal = api.get_balance() or api.balance_from_dashboard()
    if bal:
        print(f"Cookie 登录: ✅ 有效，当前积分 {bal}")
    else:
        print("Cookie 登录: ❌ 读不到积分（Cookie 可能失效或未登录）")

    info = api.get_rewards_info()
    if info:
        print(f"搜索配额   : PC {info['pc']['progress']}/{info['pc']['max']}  (来源 {info['source']})")
        print(f"阅读进度   : {info['read_progress']}/{info['read_max']}")
    else:
        print("搜索配额   : ❌ 无法获取")

    cards = api.discover_cards()
    print(f"活动卡片   : 发现 {len(cards)} 个待领取")
    links = api.get_daily_set_links()
    print(f"每日活动   : 找到 {len(links)} 个链接")
    print("=" * 62)


# ------------------------------------------------------------------ 主流程
def main() -> int:
    p = argparse.ArgumentParser(description="Microsoft Rewards 自动任务（Python 版，零依赖）")
    p.add_argument("--gui", action="store_true", help="启动图形界面（优先 PyQt5，自动回退 tkinter）")
    p.add_argument("--verbose", action="store_true", help="强制开启详细日志")
    p.add_argument("--quiet", action="store_true", help="关闭详细日志，只看关键信息")
    p.add_argument("--setup", action="store_true", help="首次配置向导")
    p.add_argument("--check", action="store_true", help="体检：登录态/地区/积分/配额")
    p.add_argument("--test", action="store_true",
                   help="测试一轮：逐个接口跑一次，记录每项成败与原因（推荐先跑这个）")
    p.add_argument("--list", action="store_true", help="列出所有任务及开关")
    p.add_argument("--task", help="只运行指定任务，逗号分隔，如 sign,read")
    p.add_argument("--dry-run", action="store_true", help="只查询，不发任何写请求")
    p.add_argument("--daemon", action="store_true",
                   help="后台常驻：等电脑空闲后才执行，任务间隔十几~几十分钟，整轮摊到几小时")
    p.add_argument("--enable", help="开启指定任务（写回 config.json）")
    p.add_argument("--disable", help="关闭指定任务（写回 config.json）")
    p.add_argument("--import-cookies", metavar="PATH_OR_STRING", help="导入 Cookie")
    p.add_argument("--auth", metavar="URL_OR_CODE", help="用授权码换取并保存 refresh_token")
    p.add_argument("--refresh-token", metavar="TOKEN", help="直接写入 refresh_token")
    p.add_argument("--test-notify", action="store_true", help="发送一条测试通知")
    args = p.parse_args()

    if args.gui:
        try:
            from gui import main as gui_main
            return int(gui_main() or 0)
        except ImportError as e:
            print("❌ 当前 Python 既没有 PyQt5 也没有 tkinter，无法启动图形界面。")
            print("   请用 conda 环境 test001：D:\\Anaconda\\envs\\test001\\python.exe main.py --gui")
            print(f"   原始错误: {e}")
            return 1
    if args.setup:
        setup()
        return 0
    if args.list:
        cfg = ensure_config()
        print("可用任务（--task 可指定，--enable/--disable 可开关）：")
        for name, meta in tasks.TASKS.items():
            on = cfg.get("tasks", {}).get(name, True)
            print(f"  {name:<14} {'✅' if on else '❌'}  {meta['label']}"
                  f"{'  [需要OAuth Token]' if meta['needs_token'] else ''}")
        print("  quiz          是 promos 的子开关（是否领取 Quiz 类卡片）")
        return 0
    if args.enable or args.disable:
        cfg = ensure_config()
        for n in (args.enable or "").split(","):
            if n.strip():
                cfg["tasks"][n.strip()] = True
        for n in (args.disable or "").split(","):
            if n.strip():
                cfg["tasks"][n.strip()] = False
        _save(CONFIG_PATH, cfg)
        print("✅ config.json 已更新")
        return 0

    verbose = True if args.verbose else (False if args.quiet else None)
    api = make_api(dry_run=args.dry_run, verbose=verbose)

    if args.import_cookies:
        import_cookies(args.import_cookies, api)
        return 0
    if args.auth:
        return 0 if do_auth(args.auth, api) else 1
    if args.refresh_token:
        api.auth["refresh_token"] = args.refresh_token.strip()
        api.save_auth()
        api.log("🟢", "refresh_token 已保存")
        return 0
    if args.test_notify:
        api.notifier.send("🧪 测试消息：如果你看到这条，说明通知配置成功", force=True)
        api.log("🟢", "测试通知已发送")
        return 0
    if args.check:
        check()
        return 0
    if args.test:
        import endpoint_test
        rep = endpoint_test.run(api)
        return 0 if rep.get("fail", 0) == 0 else 1

    only = [x.strip() for x in args.task.split(",") if x.strip()] if args.task else None

    if args.daemon:
        interval = max(60, int(api.cfg.get("daemon_interval_minutes", 60)) * 60)
        jitter = max(0, int(api.cfg.get("daemon_interval_jitter_minutes", 30)) * 60)
        lo = int(api.cfg.get("daemon_start_delay_min_s", 5))
        hi = int(api.cfg.get("daemon_start_delay_max_s", 95))
        first = random.randint(lo, max(lo, hi))
        idle_need = int(api.cfg.get("daemon_idle_seconds", 300) or 0)
        gap_lo = int(api.cfg.get("daemon_task_gap_min_s", 300))
        gap_hi = int(api.cfg.get("daemon_task_gap_max_s", 720))
        wake_hour = int(api.cfg.get("daemon_wake_hour", 7) or 7)
        wake_win = int(api.cfg.get("daemon_wake_window_minutes", 90) or 0)
        api.log("⏳", f"守护模式启动：{first} 秒后开始等空闲；"
                      f"电脑空闲 {idle_need} 秒才动手，任务间隔 {gap_lo//60}~{gap_hi//60} 分钟，"
                      f"两轮间隔约 {interval//60}±{jitter//60} 分钟"
                      f"（Ctrl+C 退出）")
        if api.cfg.get("daemon_sleep_after_done", True):
            api.log("😴", f"今日目标一完成就收工：直接睡到次日 {wake_hour:02d}:00"
                          f"~{wake_hour:02d}:{wake_win:02d} 之间（东八区）再开始，不再检测空闲")
        if not args.dry_run:
            time.sleep(first)
        round_no = 0
        try:
            while True:
                round_no += 1
                api.log("🔁", f"===== 第 {round_no} 轮 =====")
                try:
                    tasks.run_all(api, only, pace=True)
                except Exception as e:
                    api.log("🔴", f"本轮异常: {type(e).__name__}: {e}")
                api.http.save_cookies(COOKIE_PATH)
                if args.dry_run:
                    break
                if getattr(api, "goals_done_today", False):
                    # 今日目标已完成：不再轮询，直接睡到次日东八区起床时间
                    if tasks.sleep_until_wake(api):
                        continue
                    if api.stopped():
                        break
                wait = max(60, interval + (random.randint(-jitter, jitter) if jitter else 0))
                api.log("⏳", f"本轮结束，{wait // 60} 分钟后进入下一轮（同样要等电脑空闲）")
                time.sleep(wait)
        except KeyboardInterrupt:
            api.log("👋", "已停止")
        return 0

    tasks.run_all(api, only)
    api.http.save_cookies(COOKIE_PATH)
    return 0


if __name__ == "__main__":
    sys.exit(main())
