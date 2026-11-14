"""浏览器 DOM 任务 —— 纯 HTTP 做不到的操作，用已登录的 Edge（CDP）完成。

为什么需要它：
  微软新版 Rewards 门户（Next.js）已下线 /api/reportactivity 等老接口（401），
  「可领取积分」「拼图」「Quiz」这些只剩页面按钮，必须真的在页面里点。

采用与「一键登录」相同的独立 Edge profile（登录状态已持久化），
通过手写 CDP（browser_login._MiniWS）导航 + 执行 JS 点击，无需 Playwright。

对外接口：
  DomSession                    会话封装（启动/复用窗口、导航、执行 JS、按文本点击）
  DomSession.claim_points()     点 dashboard 的「可领取 N 领取」
  DomSession.complete_offer(c)  打开卡片链接并完成（拼图跳过 / Quiz 作答 / 普通访问）
"""
from __future__ import annotations

import json
import os
import random
import re
import time
import urllib.parse

import browser_login as bl

DASHBOARD = "https://rewards.bing.com/dashboard"

# 页面是否处于未登录状态（出现明显的登录入口）
JS_LOGGED_OUT = r"""
(() => {
  const txt = (e) => (e.innerText || '').replace(/\s+/g, ' ').trim();
  const els = [...document.querySelectorAll('a,button,[role=button]')];
  return els.some(e => /^(登录|登入|Sign in|Sign In)$/i.test(txt(e)));
})()
"""

# 按文本正则点击：模拟完整鼠标事件序列，兼容 React/Next.js 的合成事件
JS_CLICK = r"""
(() => {
  const pats = %s;
  const vis = e => {
    const r = e.getBoundingClientRect();
    const s = getComputedStyle(e);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'
           && s.pointerEvents !== 'none';
  };
  const txt = e => (e.innerText || e.getAttribute('aria-label') || '').replace(/\s+/g, ' ').trim();
  const els = [...document.querySelectorAll('button,[role=button],a,[role=link]')].filter(vis);
  for (const p of pats) {
    const re = new RegExp(p);
    for (const el of els) {
      const t = txt(el);
      if (!t || t.length > 140 || !re.test(t)) continue;
      el.scrollIntoView({ block: 'center', inline: 'center' });
      const r = el.getBoundingClientRect();
      const opt = { bubbles: true, cancelable: true, view: window,
                    clientX: r.left + r.width / 2, clientY: r.top + r.height / 2 };
      try { el.dispatchEvent(new PointerEvent('pointerdown', opt)); } catch (e) {}
      el.dispatchEvent(new MouseEvent('mousedown', opt));
      try { el.dispatchEvent(new PointerEvent('pointerup', opt)); } catch (e) {}
      el.dispatchEvent(new MouseEvent('mouseup', opt));
      el.click();
      return { ok: true, text: t.slice(0, 100) };
    }
  }
  return { ok: false, visible: els.map(txt).filter(Boolean).slice(0, 12) };
})()
"""

JS_READY = "[document.readyState, location.href].join('|')"


class DomSession:
    """复用登录 profile 的浏览器会话。用完务必 close()。"""

    def __init__(self, base_dir: str, log=None, minimized: bool = True):
        self.base_dir = base_dir
        self._log = log or (lambda *a, **k: None)
        self.minimized = minimized        # True = 启动后立刻最小化，不抢前台焦点
        self.sess = bl.BrowserSession(base_dir, log=self._log)
        self.started = False
        self.own_window = False

    # ------------------------------------------------------------ 基础
    def _minimize(self) -> None:
        """把浏览器窗口最小化。CDP 的点击是 JS 事件派发，不依赖窗口可见。"""
        try:
            w = self.sess.cdp.call("Browser.getWindowForTarget", {}) or {}
            wid = w.get("windowId")
            if wid is None:
                return
            self.sess.cdp.call("Browser.setWindowBounds",
                               {"windowId": wid, "bounds": {"windowState": "minimized"}})
            self._log("DEBUG", "🌐", "浏览器窗口已最小化（后台执行，不抢焦点）")
        except Exception as e:
            self._log("DEBUG", "🌐", f"最小化失败({type(e).__name__})，窗口保持前台")

    def _force_visible(self) -> None:
        """让页面以为自己可见、且未被节流。

        为什么必须做（2026-11-14 实测）：窗口最小化后 document.visibilityState 变成
        hidden，新版 dashboard（Next.js）的主内容是靠可见性 / IntersectionObserver
        才渲染的 —— 于是「可领取 N 领取」按钮压根不存在于 DOM 里，
        表现为「浏览器里没找到领取按钮」。打开焦点仿真 + 把页面生命周期设为 active，
        最小化状态下也能正常渲染（实测正文 244 字 → 1386 字，按钮出现）。
        """
        try:
            self.sess.cdp.call("Emulation.setFocusEmulationEnabled",
                              {"enabled": True}, timeout=8)
        except Exception as e:
            self._log("DEBUG", "🌐", f"焦点仿真开启失败({type(e).__name__}): {e}")
        try:
            self.sess.cdp.call("Page.setWebLifecycleState", {"state": "active"}, timeout=8)
        except Exception as e:
            self._log("DEBUG", "🌐", f"页面生命周期设置失败({type(e).__name__}): {e}")

    def ensure(self, url: str = DASHBOARD) -> bool:
        if self.started:
            return True
        try:
            msg = self.sess.start(url, offscreen=self.minimized)
        except Exception as e:
            self._log("INFO", "🔴", f"浏览器启动失败: {type(e).__name__}: {e}")
            return False
        self.started = True
        self.own_window = "已打开新窗口" in msg
        self._force_visible()             # 先让页面「可见」，再最小化窗口（顺序很重要）
        if self.minimized:
            self._minimize()
            self._force_visible()         # 最小化会重置可见性，再设一次，保证内容渲染
        self._log("DEBUG", "🌐", f"DOM 会话就绪（{msg}）")
        return True

    def nav(self, url: str, marker: str | None = None, timeout: float = 30) -> bool:
        if not self.ensure(url):
            return False
        marker = marker or urllib.parse.urlparse(url).netloc.replace("www.", "")
        try:
            self.sess.cdp.call("Page.navigate", {"url": url}, timeout=15)
        except Exception as e:
            self._log("DEBUG", "🌐", f"导航异常({type(e).__name__})，改用 location.href")
            try:
                self.js(f"location.href = {json.dumps(url)}; true")
            except Exception:
                return False
        self._force_visible()             # 新文档要重新设成「可见」，否则主内容不渲染
        end = time.time() + timeout
        while time.time() < end:
            try:
                v = self.js(JS_READY, timeout=6) or ""
                if v.startswith("complete") and marker in v:
                    # 文档加载完成后再设一次「可见」：导航会重置上一页的焦点仿真
                    self._force_visible()
                    time.sleep(1.8)          # 等 React 水合 + 首屏数据
                    return True
            except Exception:
                pass
            time.sleep(0.7)
        self._log("DEBUG", "🌐", f"页面等待超时: {url[:70]}")
        return False

    def js(self, expr: str, timeout: float = 20):
        r = self.sess.cdp.call(
            "Runtime.evaluate",
            {"expression": expr, "returnByValue": True, "awaitPromise": True},
            timeout=timeout)
        if r.get("exceptionDetails"):
            raise RuntimeError(r["exceptionDetails"].get("text") or "JS 执行异常")
        return (r.get("result") or {}).get("value")

    def click(self, patterns: list[str]) -> dict:
        try:
            return self.js(JS_CLICK % json.dumps(patterns, ensure_ascii=False)) or {"ok": False}
        except Exception as e:
            self._log("DEBUG", "🌐", f"点击失败: {type(e).__name__}: {e}")
            return {"ok": False}

    def click_when_ready(self, patterns: list[str], timeout: float = 25,
                         settle: float = 0.8) -> dict:
        """轮询等待按钮出现再点。

        dashboard 是 Next.js 流式渲染，导航完成 ≠ 内容就绪（实测首屏水合后
        还要 1~3 秒才把「可领取 N 领取」画出来），所以不能只点一次。
        """
        end = time.time() + max(1.0, timeout)
        last: dict = {"ok": False}
        tries = 0
        while True:
            last = self.click(patterns)
            tries += 1
            if last.get("ok"):
                if tries > 1:
                    self._log("DEBUG", "🌐", f"第 {tries} 次尝试点到了「{last.get('text')}」")
                return last
            if time.time() >= end:
                self._log("DEBUG", "🌐", f"等了 {timeout:.0f} 秒仍未出现目标按钮"
                                        f"（页面可见按钮: {last.get('visible')}）")
                return last
            time.sleep(settle)

    def logged_in(self) -> bool:
        try:
            return not bool(self.js(JS_LOGGED_OUT))
        except Exception:
            return True

    def search(self, query: str, settle: float = 2.0) -> bool:
        """在真实浏览器里搜一次（仅当配置 search_mode=browser 时使用）。

        默认搜索已经走纯 HTTP（cn.bing.com + reportActivity，实测计入 +3），
        这里只是备选：万一哪天纯 HTTP 被风控，可以切回真实浏览器。
        """
        url = f"https://cn.bing.com/search?q={urllib.parse.quote(query)}&form=QBLH"
        if not self.nav(url, "bing.com"):
            return False
        time.sleep(settle + random.uniform(0.3, 1.2))
        try:
            return "bing.com" in str(self.js("location.hostname") or "")
        except Exception:
            return False

    def close(self) -> None:
        try:
            self.sess.cdp.close()
        except Exception:
            pass
        if self.own_window:
            try:
                self.sess.close_window()
                self._log("DEBUG", "🌐", "DOM 会话窗口已关闭")
            except Exception:
                pass

    # ------------------------------------------------------------ 业务动作
    def claim_points(self, click_timeout: float = 25) -> dict:
        """点 dashboard 的「可领取 N 领取」→ 右侧抽屉里的「领取积分」。

        2026-11-14 修复：窗口最小化会让页面 visibilityState=hidden，Next.js 主内容
        不渲染，按钮根本不存在。现在导航前后都会 _force_visible()，并且用
        click_when_ready() 轮询等待按钮出现（而不是只点一次）。

        :return: {"clicks": 0/1/2, "page_amount": int|None, "reason": 说明}
                 reason: not_logged_in / not_rendered / not_found / zero / ok_partial / ok
        """
        res = {"clicks": 0, "page_amount": None, "reason": ""}
        if not self.nav(DASHBOARD, "rewards.bing.com"):
            res["reason"] = "nav_failed"
            return res
        self._force_visible()             # 保险：点击前再确认一次「可见」，防止导航重置
        if not self.logged_in():
            self._log("INFO", "🟡", "浏览器未登录（profile 失效），请先在「账号与授权」里一键登录一次")
            res["reason"] = "not_logged_in"
            return res
        try:
            st = self.js("[document.visibilityState, document.body.innerText.length].join('|')")
            self._log("DEBUG", "🌐", f"dashboard 状态: 可见性={st}")
        except Exception:
            pass

        btn = self.click_when_ready([
            r"可领取\s*[\d,]+\s*领取",
            r"^可领取\s*[\d,]+",
            r"^领取\s*[\d,]+\s*积分$",
            r"^待领取\s*[\d,]+\s*积分$",
        ], timeout=click_timeout)
        if not btn.get("ok"):
            body_len = 0
            try:
                body_len = int(self.js("document.body ? document.body.innerText.length : 0") or 0)
            except Exception:
                pass
            if body_len < 500:
                self._log("INFO", "🟡", f"dashboard 正文只有 {body_len} 字，页面没渲染出来"
                                        f"（稍后重试）")
                res["reason"] = "not_rendered"
            else:
                self._log("INFO", "🟡", "dashboard 上没有「可领取 N 领取」，今天没有待领积分")
                res["reason"] = "not_found"
            return res

        # 从「可领取 3 领取」里抠出数字；0 就不用再点了
        m = re.search(r"(\d[\d,]*)", btn.get("text") or "")
        if m:
            try:
                res["page_amount"] = int(m.group(1).replace(",", ""))
            except ValueError:
                res["page_amount"] = None
        if res["page_amount"] == 0:
            self._log("INFO", "🌐", "dashboard 显示「可领取 0」，今天已经没有待领积分了")
            res["reason"] = "zero"
            return res

        self._log("INFO", "🌐", f"已点击「{btn['text']}」")
        res["clicks"] = 1
        time.sleep(1.8)

        # 第二步：右侧抽屉里的确认按钮「领取积分」
        btn2 = self.click_when_ready([
            r"^领取积分$",
            r"领取积分",
            r"^(确认领取|立即领取)$",
            r"^领取\s*[\d,]+\s*积分$",
            r"^领取$",
        ], timeout=12)
        if btn2.get("ok"):
            self._log("INFO", "🌐", f"已点击「{btn2['text']}」，等待积分到账…")
            res["clicks"] = 2
            res["reason"] = "ok"
            time.sleep(3)
            return res
        self._log("DEBUG", "🌐", f"未找到抽屉内的确认按钮，可见: {btn2.get('visible')}")
        res["reason"] = "ok_partial"
        return res

    def complete_offer(self, card: dict) -> bool:
        """打开卡片链接并尝试完成。puzzle 点「跳过拼图」，quiz 逐题作答，其余仅访问。"""
        url = card.get("url") or ""
        if not url.startswith("http"):
            return False
        kind = card.get("kind") or ""
        title = card.get("title") or card.get("offerId", "")
        self._log("INFO", "🌐", f"浏览器打开 [{kind}] {title} +{card.get('points')}p")
        if not self.nav(url, None, timeout=30):
            return False
        if not self.logged_in():
            self._log("INFO", "🟡", "浏览器未登录，跳过 DOM 完成")
            return False

        if "puzzle" in kind or "imagepuzzle" in url:
            time.sleep(1.5)
            res = self.click_when_ready([r"跳过拼图", r"跳过$", r"完成拼图", r"^完成$", r"^开始$"],
                                        timeout=20)
            if res.get("ok"):
                self._log("INFO", "🌐", f"拼图页已点击「{res['text']}」")
                time.sleep(2.5)
                # 跳过/完成后如果出现「领取」类按钮，再补一刀
                self.click_when_ready([r"^领取(积分)?$", r"^完成$", r"^确定$"], timeout=8)
                time.sleep(1.5)
                return True
            self._log("DEBUG", "🌐", f"拼图页未找到可点按钮: {res.get('visible')}")
            return False

        if "quiz" in kind or "quiz" in url.lower():
            answered = 0
            for _ in range(6):
                res = self.click_when_ready([r"^[A-D][\.、．:：]\s*\S", r"^选项\s*[A-D]"],
                                            timeout=15 if answered == 0 else 5)
                if not res.get("ok"):
                    break
                answered += 1
                self._log("DEBUG", "🌐", f"Quiz 第 {answered} 题已选择「{res['text'][:30]}」")
                time.sleep(2.2)
                self.click([r"^(下一题|下一步|继续|Next|提交|查看结果)$"])
                time.sleep(1.2)
            if answered:
                self._log("INFO", "🌐", f"Quiz 已作答 {answered} 题")
                return True
            self._log("DEBUG", "🌐", "Quiz 页未找到选项按钮")
            return False

        # 其它卡片（每日活动链接等）：访问即上报，已被 HTTP 任务覆盖
        time.sleep(2)
        return True


def selftest() -> int:
    """不联网的结构自检：检查 JS 模板与按钮文本规则。"""
    ok = 0
    checks = [
        ("JS_CLICK 可格式化", isinstance(JS_CLICK % json.dumps(["^领取$"]), str)),
        ("领取按钮正则", bool(re.search(r"可领取\s*[\d,]+\s*领取", "可领取 103 领取"))),
        ("Quiz 选项正则", bool(re.search(r"^[A-D][\.、．:：]\s*\S", "A. 氮"))),
        ("拼图跳过正则", bool(re.search(r"跳过拼图", "跳过拼图"))),
    ]
    for name, res in checks:
        print(("  ✅ " if res else "  ❌ ") + name)
        ok += 1 if res else 0
    print(f"{ok}/{len(checks)} 通过")
    return 0 if ok == len(checks) else 1


if __name__ == "__main__":
    raise SystemExit(selftest())
