"""逐接口测试（GUI 的「测试一轮」）。

和正式跑全部任务的区别：
  * 每个接口/得分点只试 1~2 次（搜索 1 次、阅读 1 篇、卡片最多 2 张），几分钟内跑完；
  * 每项都记录「接口 / HTTP 状态 / 服务器返回 / 是否真的加分 / 失败原因」；
  * 积分一律以服务器查询为准（DAPI options=105 / 613），绝不用本地估算；
  * 不写任务完成标记，测完不影响正式跑。

产出：
  * 日志里逐条结果 + 末尾汇总表；
  * logs/endpoint-test-YYYYMMDD-HHMMSS.json（给用户/开发者看细节）。
"""
from __future__ import annotations

import json
import os
import re
import time

from http_client import HttpError
from rewards import REWARDS

ICON = {"ok": "✅", "fail": "❌", "skip": "⏭️"}

# 需要 Token 的项：Token 不可用时直接标 skip，并明确提示去刷新授权
NEED_TOKEN = "需要 OAuth Token"


def _load_cards(api, limit: int) -> list[dict]:
    try:
        return api.discover_cards()[:limit]
    except Exception:
        return []


def run(api, max_cards: int = 2) -> dict:
    """逐个接口跑一遍，返回汇总 dict。"""
    t0 = time.time()
    api.today = int(time.strftime("%Y%m%d"))
    items: list[dict] = []

    # 网络没通就先等，别一堆 getaddrinfo failed 刷屏（与正式跑共用同一个闸门）
    from tasks import _wait_network
    if not _wait_network(api):
        return _finish(api, items, 0, t0)

    def add(name: str, endpoint: str, status: str, detail: str,
            before=None, after=None, delta=None) -> dict:
        it = {"name": name, "endpoint": endpoint, "status": status, "detail": detail}
        if before is not None:
            it["before"] = before
        if after is not None:
            it["after"] = after
        if delta is not None:
            it["delta"] = delta
        items.append(it)
        api.log("🧪", f"{ICON[status]} {name}：{detail}")
        return it

    def stopped() -> bool:
        if api.stopped():
            api.log("⏹", "测试一轮：已按用户要求中止")
            return True
        return False

    api.log("🧪", "════ 逐接口测试开始（每项 1~2 次，全部以服务器数据为准）════", True,
            category="test")

    # 测试目标：就是今天的随机目标（没有就现场生成，绝不单独推送）。
    # 和守护模式共用同一份，所以测出来的配额进度可以直接对照正式跑的停止线。
    _smax, _rmax = 60, 30
    try:
        _info0 = api.get_rewards_info()
        if _info0:
            _smax = int(_info0["pc"]["max"])
            _rmax = int(_info0["read_max"])
    except Exception:
        pass
    _sgoal = api.daily_target("search", _smax)
    _rgoal = api.daily_target("read", _rmax)
    api.log("🎯", f"测试目标：搜索 {_sgoal}/{_smax} 分 · 阅读 {_rgoal}/{_rmax} 分"
                  f"（今日随机目标，不拿满；正式跑/守护按这个停）"
                  f"｜测试模式每项只跑 1 次验证接口，不受今日目标限制", True, category="test")

    # ---------------------------------------------------------------- 1 登录态
    jar_names = {c.name for c in api.http.jar}
    has_u = "_U" in jar_names
    try:
        r = api.http.get(f"{REWARDS}/dashboard", headers={
            "User-Agent": api.ua_pc, "Referer": f"{REWARDS}/"})
        m = re.search(r'\\?"balance\\?"\s*:\s*(\d+)', r.text)
        page_bal = m.group(1) if m else "?"
        if r.ok and has_u:
            add("Cookie 登录态", "GET rewards.bing.com/dashboard", "ok",
                f"HTTP {r.status}，Cookie {len(jar_names)} 条（_U 存在），页面积分 {page_bal}")
        elif r.ok:
            add("Cookie 登录态", "GET rewards.bing.com/dashboard", "fail",
                f"HTTP {r.status}，但 Cookie 里没有 _U（未登录）→ 去「账号与授权」重新一键登录")
        else:
            add("Cookie 登录态", "GET rewards.bing.com/dashboard", "fail",
                f"HTTP {r.status}，页面打不开")
    except HttpError as e:
        add("Cookie 登录态", "GET rewards.bing.com/dashboard", "fail",
            f"HTTP {e.status}（Cookie 可能已过期）")
    except Exception as e:
        add("Cookie 登录态", "GET rewards.bing.com/dashboard", "fail",
            f"{type(e).__name__}: {e}")

    if stopped():
        return _finish(api, items, 0, t0)

    # ---------------------------------------------------------------- 2 Token
    token_ok = False
    try:
        token_ok = api.renew_token()
        if token_ok:
            add("OAuth 授权", "POST login.live.com/oauth20_token.srf", "ok",
                f"access_token {len(api.token or '')} 字符，refresh_token 已自动续期")
        else:
            add("OAuth 授权", "POST login.live.com/oauth20_token.srf", "fail",
                "refresh_token 失效/未授权 → 去「账号与授权」点「一键登录 · 自动获取 Cookie + 授权」刷新")
    except Exception as e:
        add("OAuth 授权", "POST login.live.com/oauth20_token.srf", "fail",
            f"{type(e).__name__}: {e}")

    # ---------------------------------------------------------------- 3 积分
    start_balance = 0
    try:
        start_balance = api.get_balance(ttl=0)
        if start_balance > 0:
            add("积分读取", "GET dapi/me?options=105", "ok", f"当前积分 {start_balance}")
        else:
            add("积分读取", "GET dapi/me?options=105", "fail",
                "拿不到积分（DAPI 与 dashboard 均失败）→ 多半是 Cookie/Token 失效")
    except Exception as e:
        add("积分读取", "GET dapi/me?options=105", "fail", f"{type(e).__name__}: {e}")

    # ---------------------------------------------------------------- 4 配额
    info = None
    try:
        api._dapi613 = ({}, 0)
        info = api.get_rewards_info()
        if info:
            add("配额读取", "GET dapi/me?options=613", "ok",
                f"PC 搜索 {info['pc']['progress']}/{info['pc']['max']}，"
                f"阅读 {info['read_progress']}/{info['read_max']}（来源 {info['source']}）")
        else:
            add("配额读取", "GET dapi/me?options=613", "fail",
                "DAPI 与 earn 页面都没解析出配额（Token/Cookie 可能失效）")
    except Exception as e:
        add("配额读取", "GET dapi/me?options=613", "fail", f"{type(e).__name__}: {e}")

    if stopped():
        return _finish(api, items, start_balance, t0)

    # ---------------------------------------------------------------- 5 PC 签到
    if not token_ok:
        add("PC 签到", "POST dapi/me/activities (Gamification_DailyCheckIn)", "skip",
            f"{NEED_TOKEN}，未执行")
    else:
        try:
            p = api.sign_pc()
            if p > 0:
                add("PC 签到", "POST dapi/me/activities (Gamification_DailyCheckIn)", "ok",
                    f"+{p} 积分")
            elif p == 0:
                add("PC 签到", "POST dapi/me/activities (Gamification_DailyCheckIn)", "skip",
                    "接口正常但本次 p=0（今天已签到，或已被 App 签到占用）")
            else:
                add("PC 签到", "POST dapi/me/activities (Gamification_DailyCheckIn)", "fail",
                    "请求未能执行（见日志里的 HTTP 错误）")
        except Exception as e:
            add("PC 签到", "POST dapi/me/activities (Gamification_DailyCheckIn)", "fail",
                f"{type(e).__name__}: {e}")

    # ---------------------------------------------------------------- 6 App 签到
    if not token_ok:
        add("App 签到", "POST dapi/me/activities (type=103)", "skip", f"{NEED_TOKEN}，未执行")
    else:
        try:
            p = api.sign_app()
            if p > 0:
                add("App 签到", "POST dapi/me/activities (type=103)", "ok", f"+{p} 积分")
            elif p == 0:
                add("App 签到", "POST dapi/me/activities (type=103)", "skip",
                    "接口正常但本次 p=0（今天已签到过，isDuplicate）")
            else:
                add("App 签到", "POST dapi/me/activities (type=103)", "fail", "请求失败（见日志）")
        except Exception as e:
            add("App 签到", "POST dapi/me/activities (type=103)", "fail", f"{type(e).__name__}: {e}")

    # ---------------------------------------------------------------- 7 阅读（1 篇）
    if not token_ok:
        add("新闻阅读", "POST dapi/me/activities (ENUS_readarticle3_30points)", "skip",
            f"{NEED_TOKEN}，未执行")
    else:
        try:
            prog = api.get_read_progress()
            if not prog:
                add("新闻阅读", "POST dapi/me/activities (ENUS_readarticle3_30points)", "fail",
                    "DAPI 返回的 promotions 里没有这个阅读任务（活动可能已下线/未开放）")
            elif prog["progress"] >= prog["max"]:
                add("新闻阅读", "POST dapi/me/activities (ENUS_readarticle3_30points)", "skip",
                    f"今天已读满 {prog['progress']}/{prog['max']} 分，再读不会加分，"
                    f"跳过验证（明天配额刷新后会自动测到）")
            else:
                b0 = api.get_balance(ttl=0)
                r = api.do_read_once()
                time.sleep(1.5)
                b1 = api.get_balance(ttl=0)
                if r is None:
                    add("新闻阅读", "POST dapi/me/activities (ENUS_readarticle3_30points)", "fail",
                        "POST 失败（见日志里的 HTTP 状态）", b0, b1, 0)
                elif r.get("isDuplicate"):
                    add("新闻阅读", "POST dapi/me/activities (ENUS_readarticle3_30points)", "skip",
                        f"服务器标记 isDuplicate（{prog['progress']}/{prog['max']} 已读完）", b0, b1, 0)
                elif b1 > b0:
                    add("新闻阅读", "POST dapi/me/activities (ENUS_readarticle3_30points)", "ok",
                        f"读到 1 篇，积分 {b0} → {b1}（+{b1 - b0}），进度 {prog['progress']}/{prog['max']}",
                        b0, b1, b1 - b0)
                else:
                    add("新闻阅读", "POST dapi/me/activities (ENUS_readarticle3_30points)", "fail",
                        f"接口返回 p={r.get('points')}，但积分没有变化（{b0} → {b1}）", b0, b1, 0)
        except Exception as e:
            add("新闻阅读", "POST dapi/me/activities (ENUS_readarticle3_30points)", "fail",
                f"{type(e).__name__}: {e}")

    if stopped():
        return _finish(api, items, start_balance, t0)

    # ---------------------------------------------------------------- 8 PC 搜索（1 次）
    try:
        pre = api.pc_progress_now()
        if not pre:
            add("PC 搜索", f"GET/POST {api.search_host}/rewardsapp/reportActivity", "fail",
                "读不到搜索配额，无法判断是否加分")
        elif pre["progress"] >= pre["max"]:
            add("PC 搜索", f"GET/POST {api.search_host}/rewardsapp/reportActivity", "skip",
                f"配额已满 {pre['progress']}/{pre['max']} 分，再搜不会加分，跳过验证")
        else:
            q = api.get_search_word()
            r = api.search_once(q, before=pre["progress"])
            # 同一个词一天只算一次分，命中重复词就换一个再试（最多 3 个词）
            tried = [q]
            for _ in range(2):
                if r["ok"] or "换词" not in r["reason"]:
                    break
                q = api.get_search_word()
                if q in tried:
                    break
                tried.append(q)
                r = api.search_once(q, before=r["after"] or pre["progress"])
            if r["ok"]:
                how = ("上报响应已确认" if r.get("source") == "reportActivity"
                       else f"入账耗时 {r['delay']:.0f}s")
                add("PC 搜索", f"GET/POST {api.search_host}/rewardsapp/reportActivity", "ok",
                    f"搜「{q}」→ 配额 {r['before']}→{r['after']}（+{r['delta']}），"
                    f"IG={r['ig'][:8]}… 上报 increment={r['increment']}，{how}",
                    r["before"], r["after"], r["delta"])
            else:
                add("PC 搜索", f"GET/POST {api.search_host}/rewardsapp/reportActivity", "fail",
                    f"试了 {len(tried)} 个词（{'、'.join(tried)}）都没加分：{r['reason']}",
                    r["before"], r["after"], r["delta"])
    except Exception as e:
        add("PC 搜索", f"GET/POST {api.search_host}/rewardsapp/reportActivity", "fail",
            f"{type(e).__name__}: {e}")

    if stopped():
        return _finish(api, items, start_balance, t0)

    # ---------------------------------------------------------------- 9 活动卡片（最多 2 张）
    cards = _load_cards(api, max_cards)
    if not cards:
        add("活动卡片", "POST dapi/me/activities (offerid=卡片)", "skip",
            "没有可加分的新卡片（可能今天都做完了，或页面里只有 0 分展示位）")
    else:
        for c in cards:
            label = c.get("title") or c.get("offerId")
            try:
                b0 = api.get_balance(ttl=0)
                r = api.claim_card(c)
                b1 = api.get_balance(ttl=0) if r["ok"] else b0
                if r["ok"] and r["points"] > 0:
                    add(f"活动卡片 · {label}", f"POST dapi/me/activities (offerid={c['offerId']})",
                        "ok", f"+{r['points']}p，余额 {b0} → {b1}", b0, b1, r["points"])
                elif r["ok"]:
                    add(f"活动卡片 · {label}", f"POST dapi/me/activities (offerid={c['offerId']})",
                        "skip", r["reason"] or "已完成", b0, b1, 0)
                else:
                    add(f"活动卡片 · {label}", f"POST dapi/me/activities (offerid={c['offerId']})",
                        "fail", r["reason"] or "未知原因", b0, b1, 0)
            except Exception as e:
                add(f"活动卡片 · {label}", f"POST dapi/me/activities (offerid={c['offerId']})",
                    "fail", f"{type(e).__name__}: {e}")
            time.sleep(1.5)

    if stopped():
        return _finish(api, items, start_balance, t0)

    # ---------------------------------------------------------------- 10 每日活动
    try:
        links = api.get_daily_set_links()
        left = [c for c in api.discover_cards() if c["kind"] == "daily"]
        if left:
            add("每日活动", "GET rewards.bing.com/dashboard → POST dapi/me/activities", "fail",
                f"{len(links)} 个活动链接，仍有 {len(left)} 张每日卡片没完成："
                f"{', '.join((c.get('title') or c['offerId']) for c in left[:3])}")
        else:
            add("每日活动", "GET rewards.bing.com/dashboard → POST dapi/me/activities", "ok",
                f"{len(links)} 个活动链接，对应卡片都已完成（rnoreward=1 的页面本身不计分）")
    except Exception as e:
        add("每日活动", "GET rewards.bing.com/dashboard", "fail", f"{type(e).__name__}: {e}")

    # ---------------------------------------------------------------- 11 待领积分（只读）
    try:
        amount = api.check_claimable_points()
        if amount > 0:
            add("待领积分", "GET rewards.bing.com/dashboard（解析 alt=可领取）", "skip",
                f"检测到 {amount} 积分待领取（领取按钮只有 DOM，正式跑时用浏览器点）")
        else:
            add("待领积分", "GET rewards.bing.com/dashboard（解析 alt=可领取）", "ok",
                "当前没有待领取积分")
    except Exception as e:
        add("待领积分", "GET rewards.bing.com/dashboard", "fail", f"{type(e).__name__}: {e}")

    # ---------------------------------------------------------------- 12 连签（只读）
    try:
        st = api.check_streak()
        if not st.get("ok"):
            add("连签检测", "GET rewards.bing.com/earn（文本解析）", "fail",
                "页面取不到（网络/503），本轮无法核对连签状态")
        else:
            detail = f"连续 {st['days']} 天" if st["days"] is not None else "连续天数未取到"
            if st["tasks"]:
                detail += "；" + "，".join(f"{k} {v}" for k, v in st["tasks"].items())
            if st["stamps"] is not None:
                detail += f"；印章 {st['stamps']}/{st['stamps_max'] or 12}"
            add("连签检测", "GET rewards.bing.com/earn（文本解析）", "ok", detail)
    except Exception as e:
        add("连签检测", "GET rewards.bing.com/earn", "fail", f"{type(e).__name__}: {e}")

    return _finish(api, items, start_balance, t0)


def _finish(api, items: list[dict], start_balance: int, t0: float) -> dict:
    ok = sum(1 for i in items if i["status"] == "ok")
    fail = sum(1 for i in items if i["status"] == "fail")
    skip = sum(1 for i in items if i["status"] == "skip")
    end_balance = api.get_balance(ttl=0)
    gained = max(0, end_balance - start_balance) if start_balance else 0

    lines = [f"📋 测试结果：{ok} 成功 / {fail} 失败 / {skip} 跳过"
             f"（共 {len(items)} 项，用时 {time.time() - t0:.0f}s）"]
    for it in items:
        lines.append(f"  {ICON[it['status']]} {it['name']}：{it['detail']}")
    lines.append(f"  💰 积分：{start_balance} → {end_balance}（本轮实得 +{gained}）")
    api.log("📋", "\n" + "\n".join(lines), True, category="test")

    if fail:
        api.log("📋", "有失败项：看上面的 ❌ 行；Token 失效就去「账号与授权」重新一键登录", True,
                category="test")

    report = {
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "ok": ok, "fail": fail, "skip": skip,
        "balance_before": start_balance, "balance_after": end_balance, "gained": gained,
        "elapsed_seconds": round(time.time() - t0, 1),
        "items": items,
    }
    path = ""
    try:
        logs_dir = os.path.join(api.base_dir, "logs")
        os.makedirs(logs_dir, exist_ok=True)
        path = os.path.join(logs_dir, time.strftime("endpoint-test-%Y%m%d-%H%M%S.json"))
        with open(path, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        api.log("📄", f"详细报告已写入 {os.path.basename(path)}")
    except Exception as e:
        api.log("🟡", f"报告写入失败: {type(e).__name__}: {e}")

    report["report_path"] = path
    return report


if __name__ == "__main__":
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import main as cli

    api = cli.make_api()
    run(api)
