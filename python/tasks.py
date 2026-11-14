"""
任务层 —— 每个任务都是独立函数，注册在 TASKS 里。

想删掉某个功能？两种方式（不用改代码）：
  1) config.json 里把 "tasks": {"xxx": false}
  2) 命令行 python main.py --task sign,read （只跑指定的几个）
想加新任务？照着下面写一个 @task(...) 函数即可。
"""
from __future__ import annotations

import random
import time
import traceback
from collections import OrderedDict
from datetime import datetime, timedelta, timezone

import idle_guard
import netguard

# 用户在东八区：起床时间一律按 UTC+8 算，跟系统时区设置无关
TZ8 = timezone(timedelta(hours=8))

# 搜索 1 次、阅读 1 篇都固定 +3 分（与 rewards.POINT_STEP 一致）
POINT_STEP = 3

TASKS: "OrderedDict[str, dict]" = OrderedDict()


def task(name: str, label: str, needs_token: bool = False):
    def deco(fn):
        TASKS[name] = {"label": label, "fn": fn, "needs_token": needs_token}
        return fn
    return deco


def _date(api, name: str) -> int:
    return int(api.state.setdefault("tasks", {}).get(name, 0))


def _mark(api, name: str) -> None:
    api.state.setdefault("tasks", {})[name] = api.today
    api.save_state()


def task_status(api, name: str) -> tuple[bool, str]:
    """这个任务今天是否已经达标 —— 给调度器在「等空闲」之前做跳过判断用。

    为什么单独做一层：任务函数自己也有完成判断，但那是**已经轮到这个任务、
    已经等完空闲、已经打了「开始」日志之后**才知道。守护模式下每个任务前都要
    等电脑空闲（默认 5 分钟），已完成的任务白占一个空窗期和一段间隔，于是重开
    守护时看起来「又从已完成的任务开始了」。

    这里只做便宜的检查（多数是 1 次带缓存的 DAPI 请求，签到直接读本地标记）。
    任何一步拿不到数据就返回「未完成」，让任务照常执行 —— 宁可多做，绝不少做。
    """
    today = getattr(api, "today", 0) or int(time.strftime("%Y%m%d"))
    try:
        if name == "sign":
            if _date(api, "sign") == today and int(api.state.get("sign_point", -1)) >= 0:
                return True, f"今日已签入 +{api.state.get('sign_point')}"

        elif name == "read":
            # 不看本地日期标记，直接问服务器：标记可能因跨天/重装而与实际不一致
            p = api.get_read_progress()
            if p:
                goal = api.daily_target("read", p["max"])
                if p["progress"] >= goal:
                    return True, f"已读 {p['progress']}/{goal} 分"

        elif name == "search":
            p = api.pc_progress_now()
            if p:
                goal = api.daily_target("search", p["max"])
                if p["progress"] >= goal:
                    return True, f"搜索 {p['progress']}/{goal} 分"

        elif name in ("promos", "daily_set", "rescan"):
            # 卡片类要看「现在还有没有能领的」，光凭日期标记会漏掉下午才解锁的卡
            if _date(api, "promos") == today:
                cards = api.discover_cards()
                if name == "daily_set":
                    cards = [c for c in cards if c["kind"] == "daily"]
                if not cards:
                    return True, ("今日卡片已全部领完" if name != "daily_set"
                                  else "每日活动无待完成项")

        elif name == "claim_points":
            # 这个必须实时问服务器：随时可能冒出新的待领积分
            if api.check_claimable_points() <= 0:
                return True, "当前无可领取积分"

        elif name == "streak":
            if _date(api, "streak") == today:
                return True, f"今日已检测（连签 {api.state.get('streak_days', '?')} 天）"
    except Exception as e:
        api.debug("⏭️", f"{name} 完成判定失败（按未完成处理）: {type(e).__name__}: {e}")
    return False, ""


def _dom_allowed(api) -> bool:
    """现在是否允许动用浏览器 DOM（会弹出 Edge 窗口，可能打扰正在用电脑的人）。

    手动运行（测试一轮 / 正式运行）：允许，窗口会自动最小化不抢焦点。
    守护模式：默认只在「确认真无人使用」时才允许，四个信号综合判定
      （idle_guard.truly_away）——
      ① 屏幕锁定 → 确定没人；
      ② 屏幕保护运行 → 确定没人；
      ③ 前台有全屏窗口 → 大概率在看视频 / 开会，判定有人；
      ④ 空闲超过判定线（daemon_dom_away_minutes，默认 10 分钟）且无全屏 → 没人。
    没过判定本轮就不做，不要求手动补：守护每 1~2 小时一轮，
    哪一轮确认没人了会自动把 DOM 兜底补上。
    """
    if api.dry_run or not api.cfg.get("browser_dom", True):
        return False
    if not api.daemon_mode:
        return True
    if not api.cfg.get("daemon_dom_when_away", True):
        return False                      # 用户明确要求守护永不弹浏览器
    minutes = max(1, int(api.cfg.get("daemon_dom_away_minutes", 10) or 10))
    ok, why = idle_guard.truly_away(minutes * 60)
    if ok:
        api.debug("🤫", f"判定电脑真无人使用（{why}），允许浏览器兜底（窗口自动最小化）")
        return True
    api.debug("🤫", f"电脑可能仍有人看（{why}），本轮不动用浏览器 DOM，留给后续轮次自动补做")
    return False


def _stopped(api, label: str) -> bool:
    """循环体里检查停止按钮。返回 True 时调用方应立即 return。"""
    if api.stopped():
        api.log("⏹", f"{label}：已按用户要求中止")
        return True
    return False


def _idle_gate(api, label: str) -> bool:
    """守护模式专用：等到电脑没人用（默认 5 分钟无键鼠输入）再执行。

    这样任务都落在你离开电脑的时段，即使某个任务要弹浏览器也不会打扰人。
    """
    if api.dry_run or not api.cfg.get("daemon_require_idle", True):
        return True
    need = float(api.cfg.get("daemon_idle_seconds", 300) or 0)
    if need <= 0:
        return True
    poll = max(5, int(api.cfg.get("daemon_idle_poll_seconds", 20) or 20))
    max_wait = max(0, int(api.cfg.get("daemon_idle_wait_max_minutes", 0) or 0)) * 60
    now_idle = idle_guard.idle_seconds()
    if now_idle >= need:
        return True
    api.log("😴", f"「{label}」先等你离开电脑：已空闲 {idle_guard.human(now_idle)}"
                  f" / 需要 {idle_guard.human(need)}")
    last = [0.0]

    def tick(idle: float, waited: float) -> None:
        if waited - last[0] >= 300:                 # 每 5 分钟报一次，别刷屏
            last[0] = waited
            api.log("😴", f"仍在等空闲：{idle_guard.human(idle)} / {idle_guard.human(need)}"
                          f"（已等 {idle_guard.human(waited)}）")

    ok = idle_guard.wait_until_idle(need, poll, max_wait, api.stopped, tick)
    if ok:
        api.log("😴", f"电脑已空闲 {idle_guard.human(idle_guard.idle_seconds())}，"
                      f"开始执行「{label}」")
    return ok


def _pace_gap(api, next_label: str) -> bool:
    """守护模式专用：两个任务之间随机歇一会儿，把任务自然摊开。

    整轮有总时长上限（daemon_round_max_minutes，默认 120 分钟）：
    接近上限时不再拉开间隔，剩余任务尽快连续做完。
    """
    lo = int(api.cfg.get("daemon_task_gap_min_s", 300) or 0)
    hi = int(api.cfg.get("daemon_task_gap_max_s", 720) or 0)
    wait = random.randint(lo, max(lo, hi))
    if wait <= 0:
        return True
    deadline = getattr(api, "_round_deadline", None)
    if deadline:
        remain = deadline - time.time()
        if remain <= 90:
            api.log("⏳", "整轮时长已到上限，剩余任务连续执行（不再拉开间隔）")
            return True
        if wait > remain - 120:
            wait = max(60, int(remain - 120))
            api.log("⏳", f"为守住整轮时长上限，本轮间隔缩短为 {wait // 60} 分 {wait % 60} 秒")
    api.log("⏳", f"分散节奏：{wait // 60} 分 {wait % 60} 秒后再做「{next_label}」"
                  f"（点「停止」可立即中断）")
    api.sleep(wait * 1000)
    return not api.stopped()


# ---------------------------------------------------------------- 今日目标 / 长睡眠
def goals_done_today(api) -> tuple[bool, str]:
    """今天随机抽的目标（搜索 + 阅读）是否都已完成。

    只读服务器数据（DAPI 缓存 10 秒），失败一律当「还没完成」——宁可多跑一轮，
    也不要在目标没达成时误睡过去。
    :return: (是否完成, 说明文字)
    """
    if api.dry_run:
        return False, "dry-run 不做目标判定"
    on = api.cfg.get("tasks", {}) or {}
    want_search = bool(on.get("search", True))
    want_read = bool(on.get("read", True))
    if not want_search and not want_read:
        return False, "搜索与阅读任务都已关闭，没有可判定的目标"

    info = None
    try:
        info = api.get_rewards_info()
    except Exception as e:
        api.debug("🎯", f"目标判定读取配额异常: {type(e).__name__}: {e}")
    parts: list[str] = []

    if want_search:
        if not info or not info.get("pc"):
            return False, "读不到搜索配额"
        smax = int(info["pc"]["max"] or 0)
        if smax <= 1:
            parts.append("搜索无配额")
        else:
            sgoal = api.daily_target("search", smax)
            sprog = int(info["pc"]["progress"] or 0)
            if sprog < sgoal:
                return False, f"搜索 {sprog}/{sgoal}"
            parts.append(f"搜索 {sprog}/{sgoal}")

    if want_read:
        rp = {}
        try:
            rp = api.get_read_progress() or {}
        except Exception as e:
            api.debug("🎯", f"目标判定读取阅读进度异常: {type(e).__name__}: {e}")
        rmax = int(rp.get("max") or (info or {}).get("read_max") or 0)
        rprog = int(rp.get("progress") or (info or {}).get("read_progress") or 0)
        if rmax <= 1:
            parts.append("阅读无配额")
        else:
            rgoal = api.daily_target("read", rmax)
            if rprog < rgoal:
                return False, f"阅读 {rprog}/{rgoal}"
            parts.append(f"阅读 {rprog}/{rgoal}")

    return True, "、".join(parts) or "无可判定目标"


def next_wake_time(api, now: float | None = None) -> datetime:
    """下一个「东八区正常人起床」时刻。

    daemon_wake_hour（默认 7 点）起，往后随机 0~daemon_wake_window_minutes 分钟，
    每天的时间都不一样，看起来不像定时脚本。今天的点已经过了就取明天。
    """
    hour = max(0, min(23, int(api.cfg.get("daemon_wake_hour", 7) or 7)))
    window = max(0, min(600, int(api.cfg.get("daemon_wake_window_minutes", 90) or 0)))
    offset = random.randint(0, window * 60) if window else 0
    now_dt = datetime.fromtimestamp(time.time() if now is None else now, TZ8)
    target = now_dt.replace(hour=hour, minute=0, second=0, microsecond=0) + timedelta(seconds=offset)
    if target <= now_dt:
        target += timedelta(days=1)
    return target


def sleep_until_wake(api) -> bool:
    """今日目标已完成 → 不检测空闲、不开任务，直接睡到次日东八区起床时间。

    :return: True=睡到点起来了，可以开始新的一天；False=睡到一半被停止
    """
    if api.dry_run:
        target = next_wake_time(api)
        api.log("🌙", f"[dry-run] 今日目标已完成，正式运行会睡到 {target:%m-%d %H:%M}（东八区）")
        return True
    done, why = goals_done_today(api)
    target = next_wake_time(api)
    total = max(0, target.timestamp() - time.time())
    if done:
        api.log("😴", f"今天已收工（{why}）：{target:%m-%d %H:%M} 再开始，"
                      f"中间不检测空闲、不跑任务，约 {idle_guard.human(total)}", True,
                category="daydone")
    else:
        # 目标其实还没完成（例如某一项掉了），那就不该长睡，交给外层按原节奏轮询
        api.log("🌙", f"目标尚未全部完成（{why}），不做长睡眠，继续按轮询节奏执行")
        return False
    remaining = total
    while remaining > 0:
        if api.stopped():
            api.log("⏹", "长睡眠期间收到停止指令，守护退出")
            return False
        step = min(remaining, 600)          # 每 10 分钟醒一次，保证停止按钮随时有效
        api.sleep(int(step * 1000))
        remaining -= step
    api.log("🌅", f"起床时间到（{datetime.fromtimestamp(target.timestamp(), TZ8):%m-%d %H:%M}），"
                  f"开始新一天的任务")
    return not api.stopped()


# ------------------------------------------------------------------ 签到
@task("sign", "每日签入(PC+App)", needs_token=True)
def task_sign(api) -> bool:
    api.debug("💻", f"签到状态: 记录日期={_date(api,'sign')} 今日={api.today} "
                    f"上次积分={api.state.get('sign_point')} token={'有' if api.token else '无'}")
    if _date(api, "sign") == api.today and int(api.state.get("sign_point", -1)) >= 0:
        api.log("✅", f"签入已完成({api.state.get('sign_point')}积分)")
        return True

    api.random_delay()
    total, ok = 0, False

    app = api.sign_app()
    if app >= 0:
        ok = True
        total += app
        api.log("📱", f"App签入静默成功 +{app}积分" if app > 0 else "App签入已确认，无新增积分")
    else:
        api.log("🟡", "App 签到失败（DAPI 需要有效 Token）")

    api.random_delay()
    pc = api.sign_pc()
    if pc >= 0:
        ok = True
        total += pc
        api.log("💻", f"PC签入成功！+{pc}积分" if pc > 0
                else "PC签入已确认（今天已签过或本次不计分）")

    if ok:
        api.state["sign_point"] = total
        _mark(api, "sign")
        api.log("🔵", f"签入任务完成！总积分 +{total}", True, category="sign")
        return True
    api.fail_notes["sign"] = "App/PC 签到均失败（多半与 Token 有关）"
    api.log("🟡", "签入失败，下一轮重试")
    return False


# ------------------------------------------------------------------ 阅读
@task("read", "新闻阅读", needs_token=True)
def task_read(api) -> bool:
    if _date(api, "read") == api.today:
        verify = api.get_read_progress()
        if verify:
            goal = api.daily_target("read", verify["max"])
            if verify["progress"] >= goal:
                api.log("✅", f"阅读任务已完成（已验证 {verify['progress']}/{verify['max']}，"
                              f"今日目标 {goal}）")
                return True
            api.log("🟡", f"阅读标记有误（{verify['progress']}/{goal}），重置并继续")
            api.state["tasks"]["read"] = 0
        else:
            api.log("🟡", "无法验证阅读进度，跳过")
            return False

    progress = api.get_read_progress()
    if not progress:
        api.fail_notes["read"] = "无法获取阅读进度（DAPI 无数据）"
        api.log("🟡", "无法获取阅读进度，下一轮重试")
        return False
    cur, mx = progress["progress"], progress["max"]        # 单位：分（每篇 3 分）
    goal = api.daily_target("read", mx)                    # 分数目标，3 的倍数
    api.log("📖", f"阅读进度: {cur}/{mx}，今日目标 {goal}（每日随机，不会拿满）")
    if cur >= goal:
        _mark(api, "read")
        api.log("✅", f"阅读已达标 {cur}/{goal}（配额上限 {mx}，按策略不拿满）")
        return True

    # 篇数只在内部换算，界面统一用「分」
    remaining = min((goal - cur + POINT_STEP - 1) // POINT_STEP, 10)
    api.log("📖", f"还需 {goal - cur} 分，开始阅读（本轮最多 10 篇）")
    success = 0
    for i in range(remaining):
        if _stopped(api, "新闻阅读"):
            break
        r = api.do_read_once()
        if not r:
            api.log("🟡", f"阅读第 {i+1} 篇失败，中止")
            break
        success += 1
        api.log("📖", f"阅读文章 {i+1}/{remaining} +{r['points']}积分")
        api.debug("📖", f"第 {i+1} 篇响应: points={r['points']} isDuplicate={r['isDuplicate']}")
        api.random_delay()

    verify = api.get_read_progress()
    if verify and verify["progress"] >= min(goal, verify["max"]):
        _mark(api, "read")
        api.log("🔵", f"阅读任务完成！今日 {verify['progress']}/{verify['max']} 分"
                      f"（目标 {goal}）", True, category="read")
        return True
    api.fail_notes["read"] = (f"已执行但未达标（{verify['progress'] if verify else '?'}/{goal} 分），"
                              f"下一轮继续")
    api.log("🟡", "阅读已执行但未达标，下一轮继续")
    return False


# ------------------------------------------------------------------ 活动卡片
@task("promos", "活动卡片(含Quiz/拼图)")
def task_promos(api) -> bool:
    # 今天领过也要扫一遍：卡片是分批解锁的，下午/晚上可能冒出新的，
    # 「今日已完成」的跳过判断交给调度器（task_status）用实时扫描来做。
    api.debug("🧩", f"卡片标记日期={_date(api, 'promos')} 今日={api.today}")
    api.log("🧩", "扫描活动卡片…")
    cards = api.discover_cards()
    if not cards:
        _mark(api, "promos")
        api.log("✅", "无新活动卡片")
        return True
    api.log("🧩", f"发现 {len(cards)} 张可加分卡片")

    before = api.get_balance(ttl=0)
    ok = fail = 0
    failed_cards: list[dict] = []
    for i, c in enumerate(cards):
        if _stopped(api, "活动卡片"):
            return False
        api.log("  ", f"[{i+1}/{len(cards)}] [{c['kind']}] {c['title'] or c['offerId']} +{c['points']}p")
        if c["kind"] == "quiz" and not api.cfg.get("tasks", {}).get("quiz", True):
            api.debug("🧩", f"按配置跳过 Quiz 卡片: {c['offerId']}")
            continue
        r = api.claim_card(c)
        if r["ok"]:
            ok += 1
            api.log("🧩", f"   ✅ 到账 +{r['points']}p（余额 {r['balance']}）"
                    + (f"，{r['reason']}" if r["reason"] else ""))
        else:
            fail += 1
            failed_cards.append(c)
            api.log("🧩", f"   ❌ {r['reason']}")
        api.random_delay(1500, 3500)

    # DAPI 拿不到的（个别活动确实要页面交互）才用浏览器兜底
    dom_note = ""
    if failed_cards and _dom_allowed(api):
        api.log("🌐", f"DAPI 没完成的 {len(failed_cards)} 张，改用浏览器补一次…")
        r = api.browser_complete_cards(failed_cards)
        api.log("🌐", f"浏览器处理 {r['done']}/{r['tried']} 张，"
                      f"实际积分 {r['before']} → {r['after']}（+{r['gained']}）", True, category="cards")
        dom_note = f"，浏览器补做 {r['done']} 张"
    elif failed_cards and api.daemon_mode:
        # 守护模式：判定当时有人使用（或未达判定线），不弹窗口
        api.log("🤫", f"{len(failed_cards)} 张卡片需要浏览器兜底，但电脑疑似有人使用，已跳过"
                      f"（后续守护轮次确认无人时会自动补做）")
        api.fail_notes["promos"] = (
            f"{fail} 张卡片 DAPI 未到账；当时电脑疑似有人使用，浏览器兜底已跳过"
            f"（后续守护轮次会自动补做）")

    after = api.get_balance(ttl=0)
    if ok or fail:
        _mark(api, "promos")
    if fail and "promos" not in api.fail_notes:      # 守护模式跳过 DOM 时已有更准确的原因
        api.fail_notes["promos"] = f"{fail} 张卡片未到账（DAPI 与浏览器都没成功）"
    api.log("🔵", f"活动完成: {ok} 张到账 / {fail} 张失败{dom_note}；"
                  f"积分 {before} → {after}（+{max(0, after - before)}）", True, category="cards")
    return True


# ------------------------------------------------------------------ 搜索
@task("search", "PC搜索")
def task_search(api) -> bool:
    info = api.get_rewards_info()
    if not info:
        api.log("🔴", "无法获取积分信息")
        return False
    cur, mx = info["pc"]["progress"], info["pc"]["max"]
    goal = api.daily_target("search", mx)
    api.log("🔍", f"搜索配额: PC {cur}/{mx}，今日目标 {goal}（每日随机，不会拿满）")
    if cur >= goal:
        _mark(api, "search")
        api.log("✅", f"搜索已达标 {cur}/{goal}（配额上限 {mx}，按策略不拿满）")
        return True

    if api.check_search_restricted():
        _mark(api, "search")
        return True

    lo = int(api.cfg.get("search_batch_min", 4))
    hi = int(api.cfg.get("search_batch_max", 7))
    limit = random.randint(lo, max(lo, hi))
    span = int(api.cfg.get("search_span_seconds", 30))
    mode = str(api.cfg.get("search_mode", "http") or "http")
    use_browser = mode == "browser" and _dom_allowed(api)
    if mode == "browser" and not use_browser and api.daemon_mode:
        api.log("🤫", "守护模式禁用浏览器搜索，本轮改为纯 HTTP 静默执行")
        mode = "http"
    if api.cfg.get("search_until_full", True):
        # 每次都会核对真实配额，一次做到今日目标更省事（到目标自动停）
        limit = max(limit, min(25, (goal - cur + 2) // 3))
    api.debug("🔍", f"搜索参数: 本轮 {limit} 次 (范围 {lo}~{hi})，间隔 {span}±15s，"
                    f"词源={api.cfg.get('search_word_source')}，执行方式="
                    f"{'浏览器' if use_browser else 'HTTP 静默(' + api.search_host + ')'}，"
                    f"配额 {cur}/{mx}，目标 {goal}")

    if api.dry_run:
        api.log("🧪", "试跑模式：跳过真实搜索（不发请求、不计分）")
        return True

    ds = None
    if use_browser:
        api.log("🌐", "搜索使用真实浏览器执行（配置里 search_mode=browser）")
        ds = api._dom_session()
        if not ds.ensure("https://cn.bing.com/"):
            api.log("🟡", "浏览器启动失败，本轮退回 HTTP 搜索")
            ds = None

    try:
        stale = 0
        for i in range(limit):
            if cur >= goal:
                break
            if _stopped(api, "PC搜索"):
                return False
            query = api.get_search_word()
            gained = 0
            if ds:
                api.log("🔍", f"[PC] 搜索 {i+1}/{limit}: {query}")
                try:
                    ok = ds.search(query)
                    api.debug("🔍", f"第 {i+1} 次浏览器搜索{'完成' if ok else '失败'}")
                except Exception as e:
                    api.log("🟡", f"浏览器搜索失败: {type(e).__name__}: {e}")
                wait = random.randint(max(10, span - 15), span + 15) * 1000
                api.log("⏳", f"等待 {wait/1000}秒")
                api.sleep(wait)
                if api.stopped():
                    api.log("⏹", "PC搜索：已按用户要求中止")
                    return False
                fresh = api.pc_progress_now() or api.get_rewards_info()
                if fresh:
                    now = fresh["pc"]["progress"] if "pc" in fresh else fresh["progress"]
                    gained = max(0, now - cur)
                    cur = now
            else:
                # 同一个词一天只算一次分，遇到重复词就换一个再试（最多 3 个词）
                r = None
                for attempt in range(3):
                    if attempt:
                        if _stopped(api, "PC搜索"):
                            return False
                        query = api.get_search_word()
                    api.log("🔍", f"[PC] 搜索 {i+1}/{limit}: {query}"
                            + (f"（换词重试 {attempt}）" if attempt else ""))
                    r = api.search_once(query, before=cur)
                    if r["after"]:
                        cur = r["after"]
                    api.debug("🔍", f"第 {i+1} 次 HTTP 搜索: IG={r['ig'][:8]}… "
                                     f"increment={r['increment']} 配额 {r['before']}→{r['after']} "
                                     f"(+{r['delta']}, 等待 {r['delay']}s)")
                    if r["ok"]:
                        gained = r["delta"]
                        break
                    api.log("🟡", f"   未计分：{r['reason']}")
                    if "换词" not in r["reason"]:
                        break
                if i + 1 < limit:
                    wait = random.randint(max(8, span - 15), span + 15) * 1000
                    api.log("⏳", f"等待 {wait/1000}秒")
                    api.sleep(wait)
                    if api.stopped():
                        api.log("⏹", "PC搜索：已按用户要求中止")
                        return False

            if gained:
                api.log("🔍", f"第 {i+1}/{limit} 次完成，服务器配额 {cur}/{mx}（+{gained}）")
            stale = 0 if gained else stale + 1
            if cur >= goal:
                break
            if stale >= 3:
                api.fail_notes["search"] = "连续 3 次搜索配额都没增加，疑似受限，本轮提前结束"
                api.log("🟡", "连续 3 次搜索配额都没增加，判定受限，本轮提前结束", True,
                        category="restricted")
                break
    finally:
        if ds:
            ds.close()

    final = api.get_rewards_info()
    if final:
        if final["pc"]["progress"] >= goal:
            _mark(api, "search")
            api.state["restricted_times"] = 0
            api.state["last_search_progress"] = -1
            api.save_state()
            api.log("🔵", f"🔍 搜索任务完成！PC: {final['pc']['progress']}/{final['pc']['max']}"
                          f"（今日目标 {goal}，配额故意不拿满）", True, category="search")
            return True
        api.log("🟡", f"搜索已执行，未达今日目标 PC: {final['pc']['progress']}/{goal}")
        api.fail_notes["search"] = (f"本轮搜完仍未达标（{final['pc']['progress']}/{goal}），"
                                    "下一轮继续")
    return False


@task("streak", "连签检测")
def task_streak(api) -> bool:
    api.log("📅", "开始检测连签任务…")
    result = api.check_streak()
    api.debug("📅", f"连签结果: 连续 {result['days']} 天, 任务进度 {result['tasks']}, "
                    f"视觉搜索已完成={result['visual_done']}, "
                    f"印章={result['stamps']}/{result['stamps_max']}")

    # 关键：没取到 ≠ 0 天。取不到就沿用上次记录，并且不标记今日完成（下一轮再查）。
    if not result.get("ok") or result["days"] is None:
        old = api.state.get("streak_days")
        api.fail_notes["streak"] = "连签页面取不到（网络/503），下一轮重试"
        api.log("🟡", "连签检测未取到数据"
                      + (f"，沿用上次记录 {old} 天" if old is not None else "")
                      + "，下一轮再查", True, category="streak")
        return False

    api.state["streak_days"] = result["days"]
    api.save_state()
    if not result["visual_done"]:
        api.log("📅", "视觉搜索未完成，纯 HTTP 方式访问打卡链接…")
        try:
            api.http.get("https://www.bing.com/?features=vsstreak,vstooltip&form=ML2XES",
                         headers={"User-Agent": api.ua_pc})
            api.log("✅", "视觉搜索打卡链接已访问")
        except Exception as e:
            api.log("🟡", f"视觉搜索打卡失败: {e}")
        api.random_delay(3000, 5000)
    api.log("📅", "连签任务检测完成")
    _mark(api, "streak")           # 纯查询，一天查一次就够
    return True


# ------------------------------------------------------------------ 每日活动
@task("daily_set", "每日活动")
def task_daily_set(api) -> bool:
    """每日活动卡片（Gamification_DailySet_*）同样是 DAPI 一步到位。

    以前是「用浏览器打开 rnoreward=1 的活动页」，实测那条路径根本不给分
    （rnoreward=1 就是「不带奖励」的意思），真正发分的是卡片本身。
    """
    links = api.get_daily_set_links()
    api.log("📅", f"每日活动页链接 {len(links)} 个")
    cards = [c for c in api.discover_cards() if c["kind"] == "daily"]
    if not cards:
        api.log("✅", "每日活动卡片均已处理（没有待完成项）")
        return True
    before = api.get_balance(ttl=0)
    ok = 0
    for c in cards:
        if _stopped(api, "每日活动"):
            break
        r = api.claim_card(c)
        if r["ok"]:
            ok += 1
            api.log("📅", f"✅ {c['title'] or c['offerId']} +{r['points']}p（余额 {r['balance']}）")
        else:
            api.log("📅", f"❌ {c['title'] or c['offerId']}：{r['reason']}")
        api.random_delay(1500, 3000)
    after = api.get_balance(ttl=0)
    if ok < len(cards):
        api.fail_notes["daily_set"] = f"{len(cards) - ok} 张每日活动卡片未到账"
    api.log("🔵", f"每日活动完成 {ok}/{len(cards)}，积分 {before} → {after}"
                  f"（+{max(0, after - before)}）", True, category="cards")
    return True


# ------------------------------------------------------------------ 待领积分
@task("claim_points", "待领积分检测")
def task_claim_points(api) -> bool:
    amount = api.check_claimable_points()
    api.debug("🎁", f"待领积分检测结果: {amount}")
    if amount <= 0:
        api.log("✅", "无可领取积分")
        return True
    api.log("🎁", f"发现 {amount} 积分待领取")
    # 新版门户的领取按钮只剩 DOM，纯 HTTP 打不开 → 用已登录的 Edge 点一下
    if _dom_allowed(api):
        api.log("🌐", "用浏览器点击「领取」…")
        r = api.browser_claim_points()
        reason = r.get("reason") or ""
        if r["clicked"]:
            api.log("🔵", f"待领积分已领取：{r['before']} → {r['after']}"
                          f"（+{r['gained']}）", True, category="claim")
        elif reason == "zero":
            api.log("🔵", f"页面显示可领取 0（检测到的 {amount} 分应已入账）")
        elif reason == "not_logged_in":
            api.fail_notes["claim_points"] = "浏览器登录态失效，需在「账号与授权」重新一键登录"
            api.log("🟡", "待领积分：浏览器登录态失效，请重新一键登录", True, category="claim")
        elif reason == "not_rendered":
            api.fail_notes["claim_points"] = f"{amount} 分待领取，但 dashboard 没渲染出来（下一轮重试）"
            api.log("🟡", f"待领积分：dashboard 没渲染出来，{amount} 分下一轮再试", True,
                    category="claim")
        else:
            api.fail_notes["claim_points"] = f"{amount} 分待领取，但页面上没找到领取按钮"
            api.log("🟡", f"待领积分：页面上没找到「可领取 N 领取」按钮（{amount} 分仍在）", True,
                    category="claim")
        return True
    if api.daemon_mode:
        # 守护模式：判定有人使用时不弹浏览器，检测到了留给后续轮次自动补
        api.log("🤫", f"检测到 {amount} 积分待领取，但电脑疑似有人使用，本轮不弹浏览器"
                      f"（后续守护轮次确认无人时会自动领取）")
        api.fail_notes["claim_points"] = (
            f"检测到 {amount} 积分待领取；当时电脑疑似有人使用，"
            f"后续守护轮次确认无人时会自动领取")
        return True
    # 未启用浏览器 DOM 时保留旧提示，不谎报成功
    api.fail_notes["claim_points"] = "未开启「浏览器完成 DOM 任务」，纯 HTTP 领不了"
    api.log("🟡", "领取按钮依赖页面 DOM，请在「设置」里开启「浏览器完成 DOM 任务」，"
                  "或直接在浏览器里点一次", True, category="claim")
    return True


# ------------------------------------------------------------------ 二次扫描
@task("rescan", "二次扫描")
def task_rescan(api) -> bool:
    api.log("🔄", "二次扫描：检查是否有新解锁的卡片…")
    api.random_delay()
    cards = api.discover_cards()
    if not cards:
        api.log("✅", "二次扫描：无新卡片")
        return True
    api.log("🧩", f"二次扫描发现 {len(cards)} 张新卡片")
    ok = fail = 0
    for c in cards:
        if _stopped(api, "二次扫描"):
            break
        api.log("  ", f"[{c['kind']}] {c['title'] or c['offerId']} +{c['points']}p")
        if c["kind"] == "quiz" and not api.cfg.get("tasks", {}).get("quiz", True):
            continue
        r = api.claim_card(c)
        if r["ok"]:
            ok += 1
        else:
            fail += 1
            api.log("🧩", f"   ❌ {r['reason']}")
        api.random_delay(1500, 3000)
    api.log("🔵", f"二次扫描完成: {ok} 张到账 / {fail} 张失败")
    return True


# ------------------------------------------------------------------ 优先快线
def run_priority_lane(api) -> dict:
    """守护每轮开头，先把「连续奖励」相关的三件事集中在几分钟内做完。

    为什么单独拎出来：四周连续奖励（1000 分）只看当天有没有
    ①签到 ②至少 1 次搜索 ③每日活动 3 张卡。这三件事如果被摊到几小时里，
    中途关个机、断个网、或者你回来用电脑导致后面停掉，这一天的连签就白丢了。
    所以它们先做，且全程纯 HTTP（不弹浏览器、不占前台、完全静默）；
    其余任务仍按原来的节奏慢慢来。

    返回 {sign, search, cards, seconds}，仅用于日志汇总。
    """
    out = {"sign": None, "search": None, "cards": 0, "seconds": 0.0}
    if api.dry_run or not api.cfg.get("priority_lane", True):
        return out
    if not getattr(api, "today", 0):
        api.today = int(time.strftime("%Y%m%d"))     # 单独调用时也能正确判断「今天」
    budget = int(api.cfg.get("priority_lane_seconds", 180) or 180)
    want_cards = max(1, int(api.cfg.get("priority_lane_cards", 3) or 3))
    on = api.cfg.get("tasks", {})
    t0 = time.time()
    api.log("⚡", f"优先快线：签到 + 1 次搜索 + {want_cards} 张卡片"
                  f"（{budget // 60} 分 {budget % 60} 秒内做完，纯 HTTP 静默）")

    # ① 签到（App + PC），沿用原任务逻辑，成功后会自动打上今日标记
    if on.get("sign", True):
        try:
            out["sign"] = task_sign(api)
        except Exception as e:
            api.log("🟡", f"快线签到异常: {type(e).__name__}: {e}")
            out["sign"] = False

    # ② 一次搜索（连签只要求 1 次；今天已有计分记录就跳过）
    if on.get("search", True) and time.time() - t0 < budget:
        try:
            pre = api.pc_progress_now()
            if pre and pre["progress"] >= 1:
                api.debug("⚡", f"今天已有 {pre['progress']} 次搜索计分，快线跳过")
                out["search"] = True
            else:
                for attempt in range(2):
                    if time.time() - t0 >= budget or api.stopped():
                        break
                    q = api.get_search_word()
                    r = api.search_once(q)
                    if r["ok"]:
                        out["search"] = True
                        api.log("⚡", f"快线搜索「{q}」+{r['delta']}"
                                      f"（配额 {r['before']}→{r['after']}）")
                        break
                    api.debug("⚡", f"快线搜索第 {attempt+1} 次未计分: {r['reason']}")
        except Exception as e:
            api.log("🟡", f"快线搜索异常: {type(e).__name__}: {e}")
            out["search"] = False

    # ③ 每日 3 张卡片（优先 DailySet，不够再用其它加分卡补位）
    if (on.get("promos", True) or on.get("daily_set", True)) and time.time() - t0 < budget:
        try:
            cards = api.discover_cards()
            daily = [c for c in cards if c["kind"] == "daily"]
            rest = [c for c in cards if c["kind"] != "daily"]
            picked = (daily + rest)[:want_cards]
            if not picked:
                api.debug("⚡", "快线无可领卡片（可能今天已经领完）")
            for c in picked:
                if time.time() - t0 >= budget or api.stopped():
                    api.log("⏳", "快线时间到，剩余卡片留给后续常规流程")
                    break
                if c["kind"] == "quiz" and not on.get("quiz", True):
                    continue
                r = api.claim_card(c)
                if r["ok"]:
                    out["cards"] += 1
                    api.debug("⚡", f"快线卡片 {c['title'] or c['offerId']} +{r['points']}p")
                else:
                    # 快线不弹浏览器，失败的卡片留给 task_promos 按原逻辑处理
                    api.debug("⚡", f"快线卡片 {c['offerId']} 未完成: {r['reason']}")
                api.random_delay(1200, 2500)
        except Exception as e:
            api.log("🟡", f"快线卡片异常: {type(e).__name__}: {e}")

    out["seconds"] = round(time.time() - t0, 1)
    sign_txt = "✅" if out["sign"] else ("⏭️" if out["sign"] is None else "❌")
    search_txt = "✅" if out["search"] else ("⏭️" if out["search"] is None else "❌")
    api.log("⚡", f"优先快线完成（{out['seconds']} 秒）：签到 {sign_txt}｜搜索 {search_txt}｜"
                  f"卡片 {out['cards']}/{want_cards}", True, category="lane")
    return out


# ------------------------------------------------------------------ 总调度
def _wait_network(api) -> bool:
    """能不能联网先确认，再动手。

    开机自启动时系统网络栈常比程序晚十几秒才起来，这时发请求只会得到
    getaddrinfo failed，把「登录态待确认」「地区检测失败」这些假警报写进状态，
    还白白消耗重试。这里改成安静等待：守护模式一直等到通，手动运行最多等 10 分钟。
    """
    if netguard.ready():
        return True
    limit = 0 if getattr(api, "daemon_mode", False) else 600
    return netguard.wait_ready(
        log=lambda icon, text: api.log(icon, text),
        sleep=lambda s: api.sleep(int(s * 1000)),
        poll_s=15.0,          # 网络状态不用查太勤，通了一下就知道
        max_wait_s=limit,
        should_stop=api.stopped,
    )


def run_all(api, only: list[str] | None = None, pace: bool = False) -> None:
    """跑一轮全部任务。

    :param pace: 守护模式用。True 时每个任务前先等电脑空闲，
                 任务之间随机间隔几分钟把任务自然摊开；
                 整轮有总时长上限（daemon_round_max_minutes，默认 120 分钟），
                 接近上限会自动缩短/取消剩余间隔。
                 开头会先走「优先快线」把连签相关的三件事做完。
    """
    api.today = int(time.strftime("%Y%m%d"))
    api.fail_notes = {}                    # 每轮重置：失败原因只描述本轮
    api.goals_done_today = False           # 每轮重置：今日目标是否已达成（达成则外层长睡）
    api.daemon_mode = bool(pace)           # 守护模式标记：决定要不要禁用浏览器弹窗
    api._round_deadline = None
    if pace:
        cap = int(api.cfg.get("daemon_round_max_minutes", 120) or 0)
        if cap > 0:
            api._round_deadline = time.time() + cap * 60
            api.log("🌙", f"守护节奏：整轮时长上限 {cap} 分钟，任务间随机隔开，"
                          f"接近上限自动收尾")
        locked = idle_guard.is_locked()
        api.debug("🔒", f"会话状态: {'已锁屏' if locked else ('未锁屏' if locked is False else '未知')}"
                        f"（锁屏不影响执行，睡眠才会中断）")
    api.log("🚀", "启动自动化任务…" + (" [dry-run]" if api.dry_run else ""))

    # 第一道闸：网络没通就什么都不发（开机自启动时 Wi-Fi/DNS 往往还没起来）
    if not _wait_network(api):
        return

    if only:
        unknown = [n for n in only if n not in TASKS]
        if unknown:
            api.log("🔴", f"未知任务: {', '.join(unknown)}（用 --list 查看可用任务）")
        selected = [n for n in only if n in TASKS]
    else:
        selected = [n for n in TASKS if api.cfg.get("tasks", {}).get(n, True)]

    if pace and selected:
        lo_g = int(api.cfg.get("daemon_task_gap_min_s", 300))
        hi_g = int(api.cfg.get("daemon_task_gap_max_s", 720))
        gaps = max(0, len(selected) - 1)
        api.log("🌙", f"守护节奏：任务间随机隔 {lo_g // 60}~{hi_g // 60} 分钟，"
                      f"{len(selected)} 个任务间隔合计约 "
                      f"{gaps * lo_g // 60}~{gaps * hi_g // 60} 分钟"
                      f"（加上执行本身，整轮控制在 2 小时内）")

    region_ok = api.check_region()
    if not region_ok:
        api.log("🔴", "IP 非国内，已暂停全部任务", True, category="region")
        return

    # 只在确实有任务依赖 OAuth 时才去续期，避免跑纯 Cookie 任务时反复刷授权提示
    lane = bool(pace and api.cfg.get("priority_lane", True) and not api.dry_run)
    need_token = any(TASKS[n]["needs_token"] for n in selected) or lane
    token_ok = api.renew_token() if need_token else False
    if need_token and not token_ok:
        api.log("🟡", "Token 不可用，将跳过阅读，其余任务照常执行")

    # 今日目标已完成 → 今天彻底收工：不等空闲、不逐任务磨时间，交给外层睡到明天起床时间
    if pace and not only and api.cfg.get("daemon_sleep_after_done", True):
        done, why = goals_done_today(api)
        if done:
            api.goals_done_today = True
            api.log("🌙", f"今日目标已完成（{why}），今天不再执行任何任务，也不等空窗")
            try:                            # 收工也把明天的随机目标先抽好、推一条
                api.push_next_day_target()
            except Exception as e:
                api.debug("🟡", f"明日目标生成失败: {type(e).__name__}: {e}")
            return

    # 放在 Token 续期之后，保证起止积分走同一个数据源，差值才有意义
    start_balance = api.get_balance()
    api.log("📊", f"初始积分: {start_balance}")

    api.debug("⚙", f"本轮配置: verbose={api.verbose} dry_run={api.dry_run} host={api.host} "
                    f"lock_region={api.cfg.get('lock_region')} 搜索间隔={api.cfg.get('search_span_seconds')}s "
                    f"延迟={api.cfg.get('delay_min_ms')}~{api.cfg.get('delay_max_ms')}ms")
    api.debug("⚙", f"待执行任务({len(selected)}): {', '.join(selected) or '(无)'}")

    # 守护模式：先把连签相关的三件事集中做完（不等空闲、不弹窗），再按节奏慢慢跑其余任务
    if lane:
        try:
            run_priority_lane(api)
        except Exception as e:
            api.log("🟡", f"优先快线异常（不影响后续）: {type(e).__name__}: {e}")

    skipped = 0
    for idx, name in enumerate(selected, 1):
        if api.stopped():
            api.log("⏹", f"已按用户要求停止，剩余 {len(selected) - idx + 1} 个任务跳过")
            break
        meta = TASKS[name]
        if meta["needs_token"] and not token_ok:
            api.fail_notes[name] = "需要 OAuth Token（去「账号与授权」重新一键登录）"
            api.log("⏭️", f"跳过「{meta['label']}」（需要 Token）")
            api.debug("⏭️", f"跳过原因: needs_token=True 且 token_ok=False")
            continue
        # 先判断今天是否已达标 —— 已完成的直接略过，不占空窗期、不占任务间隔。
        # 显式指定任务（only，如「测试一轮」/--only）时不跳过，尊重用户点名要跑。
        if not only:
            done, why = task_status(api, name)
            if done:
                skipped += 1
                api.log("⏭️", f"「{meta['label']}」已完成，略过（{why}）")
                continue
        if pace and not _idle_gate(api, meta["label"]):
            api.log("⏹", "等待空闲时被停止，本轮结束")
            break
        api.log("──", f"▶ [{idx}/{len(selected)}] {meta['label']} 开始")
        t0 = time.time()
        try:
            result = meta["fn"](api)
            api.debug("──", f"■ {meta['label']} 结束: 返回={result} 用时={time.time()-t0:.2f}s")
        except Exception as e:
            api.log("🔴", f"{meta['label']} 异常: {type(e).__name__}: {e}")
            api.fail_notes[name] = f"运行异常 {type(e).__name__}: {e}"
            api.debug("🔴", "完整堆栈:\n" + traceback.format_exc())
        api.random_delay()
        # 目标一旦达成，剩下的任务就没必要再占用你的空窗期（外层会直接睡到明天早上）
        if pace and idx < len(selected) and not api.stopped() \
                and api.cfg.get("daemon_sleep_after_done", True):
            done, why = goals_done_today(api)
            if done:
                api.goals_done_today = True
                api.log("🌙", f"今日目标已达成（{why}），跳过剩余 {len(selected) - idx} 个任务")
                break
        if pace and idx < len(selected) and not api.stopped():
            if not _pace_gap(api, TASKS[selected[idx]]["label"]):
                api.log("⏹", "已按用户要求停止")
                break

    # 全部任务今天都已达标：没必要再等一轮，直接告诉外层「今天收工」
    if pace and not only and selected and skipped == len(selected):
        api.goals_done_today = True
        api.log("🌙", "本轮所有任务今日均已达标，不再重复执行")

    end_balance = api.get_balance(ttl=0)          # 汇总用最新值，不吃缓存
    earned = end_balance - start_balance if start_balance > 0 and end_balance > 0 else 0
    info = api.get_rewards_info()
    rp = api.get_read_progress() or {}            # 单独再取一次，避免响应残缺时显示成 0/30
    read_txt = f"{rp.get('progress', info['read_progress'] if info else 0)}/" \
               f"{rp.get('max', info['read_max'] if info else 30)}"
    pc_txt = f"{info['pc']['progress']}/{info['pc']['max']}" if info else "-"
    goals = api.state.get("daily_targets") or {}
    goal_txt = (f"（今日目标：搜索 {api.daily_target('search', info['pc']['max'] if info else 60)} 分、"
                f"阅读 {api.daily_target('read', info['read_max'] if info else 30)} 分，不拿满）"
                if goals.get("date") == api.today else "")

    def _row(label: str, name: str, done: bool, extra: str = "") -> str:
        """「任务 ✅/❌ 进度（失败原因）」一行；失败原因来自各任务记录的 fail_notes。"""
        s = f"{label}  {'✅' if done else '❌'}"
        if extra:
            s += f"  {extra}"
        if not done:
            note = api.fail_notes.get(name, "")
            if note:
                s += f"（原因：{note}）"
        return s

    smax = info["pc"]["max"] if info else 60
    sgoal = api.daily_target("search", smax)
    search_done = (_date(api, "search") == api.today
                   or bool(info and info["pc"]["progress"] >= sgoal))
    lines = [
        _row("签到", "sign", _date(api, "sign") == api.today),
        _row("阅读", "read", _date(api, "read") == api.today, read_txt),
        _row("PC 搜索", "search", search_done, pc_txt),
        _row("活动卡片", "promos", _date(api, "promos") == api.today),
        _row("待领积分", "claim_points", "claim_points" not in api.fail_notes),
        f"连签      {api.state.get('streak_days', 0)} 天",
        f"今日获取  +{earned}",
        f"总积分    {info['balance'] if info else end_balance}",
        f"用时      {time.time() - api.start_time:.1f} 秒",
        goal_txt,
    ]
    api.log("📊", "\n" + "\n".join(x for x in lines if x), True, category="summary")

    # 守护模式：一轮完整跑完后预生成明天的随机目标并推送（每天只推一次；
    # 哪天没跑守护，次日首次用到目标时也会自动静默生成，只是不推送）
    if pace and not api.dry_run and not api.stopped():
        try:
            api.push_next_day_target()
        except Exception as e:
            api.debug("🟡", f"明日目标生成失败: {type(e).__name__}: {e}")
