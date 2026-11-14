"""
Microsoft Rewards 自动任务 —— 图形界面（tkinter，零第三方依赖）

启动：
    python gui.py          或        python main.py --gui

界面分四页：控制台 / 运行参数 / 账号与授权 / 通知推送。
所有任务在后台线程执行，日志通过队列送到主线程，界面不会卡死。
"""
from __future__ import annotations

import os
import queue
import sys
import threading
import time
import traceback
import webbrowser
from tkinter import Tk, ttk, messagebox, filedialog, StringVar, BooleanVar, IntVar, Text, END
from tkinter.scrolledtext import ScrolledText

# 兼容两种启动方式：python gui.py 与 python main.py --gui
_main_mod = sys.modules.get("__main__")
if _main_mod is not None and getattr(_main_mod, "BASE_DIR", None):
    cli = _main_mod
else:
    import main as cli

import tasks                                   # noqa: E402
from rewards import AUTH_URL                    # noqa: E402

INT_KEYS = {
    "search_span_seconds", "search_batch_min", "search_batch_max",
    "delay_min_ms", "delay_max_ms", "http_timeout", "daemon_interval_minutes",
    "daemon_start_delay_min_s", "daemon_start_delay_max_s", "daemon_idle_seconds",
}
NOTIFY_FIELDS = [
    ("wecom", "企业微信机器人 key"),
    ("dingtalk", "钉钉 access_token"),
    ("feishu", "飞书 hook id"),
    ("pushme", "PushMe key"),
    ("bark", "Bark key"),
]
TASK_ORDER = ["sign", "read", "promos", "quiz", "search", "streak", "daily_set", "claim_points", "rescan"]

BG = "#f6f7f9"
CARD = "#ffffff"
ACCENT = "#2563eb"
MUTED = "#6b7280"
FONT = ("Microsoft YaHei UI", 10)
FONT_SMALL = ("Microsoft YaHei UI", 9)
MONO = ("Consolas", 9)


class App:
    def __init__(self, root: Tk):
        self.root = root
        self.cfg = cli.ensure_config()
        self.q: queue.Queue = queue.Queue()
        self.stop_event = threading.Event()
        self.running = False
        self.status_vars = {k: StringVar(value="未检测") for k in
                            ("cookie", "token", "region", "points", "quota")}

        root.title("Microsoft Rewards 自动任务 · 控制台")
        root.geometry("1100x780")
        root.minsize(940, 660)
        root.configure(bg=BG)
        try:
            root.call("tk", "scaling", 1.15)
        except Exception:
            pass

        self._init_style()
        self._build_header()
        self.nb = ttk.Notebook(root)
        self.nb.pack(fill="both", expand=True, padx=12, pady=(6, 0))
        self._build_console_tab()
        self._build_params_tab()
        self._build_account_tab()
        self._build_notify_tab()
        self._build_footer()

        self._load_into_ui(self.cfg)
        self._refresh_file_status()
        self.root.after(80, self._drain)
        root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------ 外观
    def _init_style(self):
        st = ttk.Style()
        try:
            st.theme_use("clam")
        except Exception:
            pass
        st.configure("TFrame", background=BG)
        st.configure("Card.TFrame", background=CARD, relief="flat")
        st.configure("TLabel", background=BG, font=FONT)
        st.configure("Card.TLabel", background=CARD, font=FONT)
        st.configure("Muted.TLabel", background=BG, foreground=MUTED, font=FONT_SMALL)
        st.configure("CardMuted.TLabel", background=CARD, foreground=MUTED, font=FONT_SMALL)
        st.configure("Title.TLabel", background=BG, font=("Microsoft YaHei UI", 15, "bold"))
        st.configure("TLabelframe", background=BG)
        st.configure("TLabelframe.Label", background=BG, font=("Microsoft YaHei UI", 10, "bold"))
        st.configure("TCheckbutton", background=CARD if False else BG, font=FONT)
        st.configure("Card.TCheckbutton", background=CARD, font=FONT)
        st.configure("TButton", font=FONT, padding=(8, 5))
        st.configure("Accent.TButton", font=("Microsoft YaHei UI", 10, "bold"),
                     background=ACCENT, foreground="#ffffff", padding=(10, 6))
        st.map("Accent.TButton", background=[("active", "#1d4ed8"), ("disabled", "#9ca3af")])
        st.configure("TNotebook.Tab", font=FONT, padding=(14, 7))

    def _build_header(self):
        h = ttk.Frame(self.root, style="TFrame")
        h.pack(fill="x", padx=16, pady=(12, 2))
        ttk.Label(h, text="Microsoft Rewards 自动任务", style="Title.TLabel").pack(side="left")
        ttk.Label(h, text="Python 后台版 · 零依赖 · MIT 开源移植", style="Muted.TLabel").pack(
            side="left", padx=(12, 0), pady=(6, 0))

    def _build_footer(self):
        f = ttk.Frame(self.root, style="TFrame")
        f.pack(fill="x", padx=16, pady=(6, 10))
        ttk.Button(f, text="💾 保存全部配置", command=lambda: self._save_all(False)).pack(side="left")
        ttk.Button(f, text="📂 打开项目目录", command=lambda: self._open_dir(cli.BASE_DIR)).pack(side="left", padx=6)
        self.footer_var = StringVar(value="")
        ttk.Label(f, textvariable=self.footer_var, style="Muted.TLabel").pack(side="left", padx=12)
        ttk.Label(f, text=f"配置目录: {cli.BASE_DIR}", style="Muted.TLabel").pack(side="right")

    # ------------------------------------------------------------ 控制台页
    def _build_console_tab(self):
        tab = ttk.Frame(self.nb, style="TFrame", padding=10)
        self.nb.add(tab, text="  🎛  控制台  ")

        # 状态卡片
        sf = ttk.LabelFrame(tab, text=" 运行状态 ", padding=10)
        sf.pack(fill="x")
        items = [("cookie", "Cookie"), ("token", "OAuth"), ("region", "地区"),
                 ("points", "积分"), ("quota", "PC 搜索配额")]
        for i, (key, label) in enumerate(items):
            cell = ttk.Frame(sf, style="TFrame")
            cell.grid(row=0, column=i, sticky="w", padx=(0, 28))
            ttk.Label(cell, text=label, style="Muted.TLabel").pack(anchor="w")
            ttk.Label(cell, textvariable=self.status_vars[key],
                      font=("Microsoft YaHei UI", 11, "bold")).pack(anchor="w")
        ttk.Button(sf, text="🔄 刷新", command=self._refresh_file_status, width=8).grid(
            row=0, column=5, sticky="e", padx=(10, 0))
        sf.columnconfigure(5, weight=1)

        # 操作按钮
        bf = ttk.LabelFrame(tab, text=" 操作 ", padding=10)
        bf.pack(fill="x", pady=(10, 0))
        self.buttons = {}
        specs = [
            ("check", "🔍 体检", "check"), ("dry", "🧪 试跑 (不发写请求)", "dry"),
            ("test", "🔬 测试一轮", "test"),
            ("run", "▶ 正式运行", "run"), ("daemon", "🔁 开始守护", "daemon"),
        ]
        for i, (key, text, mode) in enumerate(specs):
            b = ttk.Button(bf, text=text, style="Accent.TButton" if key in ("test",) else "TButton",
                           command=lambda m=mode: self._start(m))
            b.grid(row=0, column=i, padx=(0, 8), sticky="w")
            self.buttons[key] = b
        self.btn_stop = ttk.Button(bf, text="⏹ 停止", command=self._stop, state="disabled")
        self.btn_stop.grid(row=0, column=4, padx=(0, 8))
        ttk.Button(bf, text="🧹 清空日志", command=self._clear_log).grid(row=0, column=5, padx=(0, 8))
        ttk.Button(bf, text="📂 日志目录", command=lambda: self._open_dir(
            os.path.join(cli.BASE_DIR, "logs"))).grid(row=0, column=6)
        bf.columnconfigure(7, weight=1)

        # 任务开关
        tf = ttk.LabelFrame(tab, text=" 任务开关（保存后持久生效；不想跑的直接取消勾选） ", padding=10)
        tf.pack(fill="x", pady=(10, 0))
        self.task_vars = {}
        for i, name in enumerate(TASK_ORDER):
            label = tasks.TASKS[name]["label"] if name in tasks.TASKS else "Quiz 自动答题（promos 子开关）"
            v = BooleanVar(value=True)
            self.task_vars[name] = v
            ttk.Checkbutton(tf, text=label, variable=v, style="Card.TCheckbutton").grid(
                row=i // 3, column=i % 3, sticky="w", padx=(0, 18), pady=3)
        for c in range(3):
            tf.columnconfigure(c, weight=1)

        # 选项 + 日志
        of = ttk.Frame(tab, style="TFrame")
        of.pack(fill="x", pady=(10, 4))
        self.var_verbose = BooleanVar(value=True)
        self.var_autoscroll = BooleanVar(value=True)
        self.var_only_selected = BooleanVar(value=False)
        ttk.Checkbutton(of, text="详细日志（记录每个请求与决策）", variable=self.var_verbose).pack(side="left")
        ttk.Checkbutton(of, text="自动滚动", variable=self.var_autoscroll).pack(side="left", padx=14)
        ttk.Checkbutton(of, text="仅运行勾选的任务（临时单跑）", variable=self.var_only_selected).pack(side="left")
        ttk.Label(of, text="日志同时写入 logs/ 目录", style="Muted.TLabel").pack(side="right")

        lf = ttk.LabelFrame(tab, text=" 实时日志 ", padding=6)
        lf.pack(fill="both", expand=True)
        self.txt = ScrolledText(lf, height=16, font=MONO, bg="#fbfcfd", fg="#1f2328",
                                relief="flat", wrap="word", state="normal")
        self.txt.pack(fill="both", expand=True)
        self.txt.tag_config("DEBUG", foreground="#8b949e")
        self.txt.tag_config("INFO", foreground="#1f2328")
        self.txt.tag_config("WARN", foreground="#9a6700")
        self.txt.tag_config("ERROR", foreground="#cf222e")
        self.txt.tag_config("OK", foreground="#1a7f37")
        self.txt.tag_config("SYS", foreground="#2563eb")
        self._sys("界面就绪。建议顺序：账号与授权 → 体检 → 测试一轮 → 正式运行")

    # ------------------------------------------------------------ 参数页
    def _build_params_tab(self):
        tab = ttk.Frame(self.nb, style="TFrame", padding=14)
        self.nb.add(tab, text="  ⚙  运行参数  ")
        self.v = {}

        box = ttk.LabelFrame(tab, text=" 搜索任务 ", padding=12)
        box.pack(fill="x")
        self._field(box, 0, "search_span_seconds", "搜索间隔（秒，实际 ±15 随机）", 30)
        self._field(box, 1, "search_batch_min", "每轮最少搜索次数", 4)
        self._field(box, 2, "search_batch_max", "每轮最多搜索次数", 7)
        ttk.Label(box, text="搜索词来源").grid(row=3, column=0, sticky="w", pady=5)
        self.v["search_word_source"] = StringVar()
        ttk.Combobox(box, textvariable=self.v["search_word_source"], state="readonly", width=24,
                     values=["offline", "hot"]).grid(row=3, column=1, sticky="w", pady=5)
        ttk.Label(box, text="offline=内置随机词库 / hot=第三方热搜接口",
                  style="Muted.TLabel").grid(row=3, column=2, sticky="w", padx=10)

        box2 = ttk.LabelFrame(tab, text=" 防封号与调度 ", padding=12)
        box2.pack(fill="x", pady=(12, 0))
        self._field(box2, 0, "delay_min_ms", "操作间最小延迟（毫秒）", 3000)
        self._field(box2, 1, "delay_max_ms", "操作间最大延迟（毫秒）", 8000)
        self._field(box2, 2, "daemon_interval_minutes", "守护模式间隔（分钟）", 60)
        self._field(box2, 3, "daemon_start_delay_min_s", "守护启动随机延迟 min（秒）", 5)
        self._field(box2, 4, "daemon_start_delay_max_s", "守护启动随机延迟 max（秒）", 95)
        self._field(box2, 5, "daemon_idle_seconds", "守护：电脑空闲多少秒才动手", 300)
        self._field(box2, 6, "http_timeout", "HTTP 超时（秒）", 15)

        box3 = ttk.LabelFrame(tab, text=" 环境与总开关 ", padding=12)
        box3.pack(fill="x", pady=(12, 0))
        self.v["lock_region"] = BooleanVar()
        ttk.Checkbutton(box3, text="锁定国区（非大陆 IP 自动停止全部任务）",
                        variable=self.v["lock_region"]).grid(row=0, column=0, columnspan=3, sticky="w", pady=3)
        self.v["keep_running"] = BooleanVar()
        ttk.Checkbutton(box3, text="全部完成后仍继续定时检测",
                        variable=self.v["keep_running"]).grid(row=1, column=0, columnspan=3, sticky="w", pady=3)
        self.v["verbose"] = self.var_verbose   # 与控制台页共用同一个开关
        ttk.Checkbutton(box3, text="详细日志（verbose）",
                        variable=self.v["verbose"]).grid(row=2, column=0, columnspan=3, sticky="w", pady=3)
        ttk.Label(box3, text="Bing 域名").grid(row=3, column=0, sticky="w", pady=5)
        self.v["host"] = StringVar()
        ttk.Combobox(box3, textvariable=self.v["host"], state="readonly", width=24,
                     values=["www.bing.com", "cn.bing.com"]).grid(row=3, column=1, sticky="w", pady=5)
        ttk.Label(box3, text="⚠️ 修改后记得点底部「保存全部配置」", style="Muted.TLabel").grid(
            row=4, column=0, columnspan=3, sticky="w", pady=(12, 0))

    def _field(self, parent, row, key, label, default):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=4)
        self.v[key] = StringVar(value=str(default))
        ttk.Entry(parent, textvariable=self.v[key], width=12).grid(row=row, column=1, sticky="w", pady=4, padx=(10, 0))

    # ------------------------------------------------------------ 账号页
    def _build_account_tab(self):
        tab = ttk.Frame(self.nb, style="TFrame", padding=14)
        self.nb.add(tab, text="  🔑  账号与授权  ")

        cb = ttk.LabelFrame(tab, text=" ① 浏览器 Cookie（必做：PC签到 / 搜索 / 活动 / 每日活动都靠它） ", padding=12)
        cb.pack(fill="x")
        self.cookie_state = StringVar(value="")
        ttk.Label(cb, textvariable=self.cookie_state, style="Muted.TLabel").pack(anchor="w", pady=(0, 6))
        ttk.Label(cb, text="从浏览器导出 cookies.txt（Netscape 格式），或直接粘贴 DevTools 里的 Cookie 请求头整串：",
                  style="Muted.TLabel").pack(anchor="w")
        self.cookie_text = Text(cb, height=4, font=MONO, relief="flat", bg="#fbfcfd", wrap="word")
        self.cookie_text.pack(fill="x", pady=6)
        row = ttk.Frame(cb, style="TFrame")
        row.pack(fill="x")
        ttk.Button(row, text="📥 粘贴内容并保存", command=self._save_cookie_text).pack(side="left")
        ttk.Button(row, text="📄 从 cookies.txt 文件导入", command=self._import_cookie_file).pack(side="left", padx=8)
        ttk.Button(row, text="🗑 清空 Cookie", command=self._clear_cookies).pack(side="left")

        ab = ttk.LabelFrame(tab, text=" ② OAuth 授权（可选：App 签到 / 新闻阅读需要） ", padding=12)
        ab.pack(fill="x", pady=(14, 0))
        ttk.Label(ab, text="在已登录的浏览器打开授权页，登录后复制地址栏完整 URL 粘贴到下面：",
                  style="Muted.TLabel").pack(anchor="w")
        urlrow = ttk.Frame(ab, style="TFrame")
        urlrow.pack(fill="x", pady=6)
        self.auth_url_var = StringVar(value=AUTH_URL)
        e = ttk.Entry(urlrow, textvariable=self.auth_url_var, font=("Consolas", 8))
        e.pack(side="left", fill="x", expand=True)
        e.configure(state="readonly")
        ttk.Button(urlrow, text="复制链接", command=self._copy_auth_url).pack(side="left", padx=6)
        ttk.Button(urlrow, text="打开浏览器", command=lambda: webbrowser.open(AUTH_URL)).pack(side="left")

        row2 = ttk.Frame(ab, style="TFrame")
        row2.pack(fill="x", pady=(8, 4))
        ttk.Label(row2, text="授权后 URL / code").pack(side="left")
        self.auth_code_var = StringVar()
        ttk.Entry(row2, textvariable=self.auth_code_var, width=62).pack(side="left", padx=8, fill="x", expand=True)
        ttk.Button(row2, text="换取并保存 Token", command=self._do_auth).pack(side="left")

        row3 = ttk.Frame(ab, style="TFrame")
        row3.pack(fill="x", pady=(8, 0))
        ttk.Label(row3, text="refresh_token").pack(side="left")
        self.refresh_var = StringVar()
        ttk.Entry(row3, textvariable=self.refresh_var, width=62, show="*").pack(
            side="left", padx=8, fill="x", expand=True)
        ttk.Button(row3, text="直接保存", command=self._save_refresh).pack(side="left")
        ttk.Button(row3, text="清除 Token", command=self._clear_token).pack(side="left", padx=6)

        ttk.Label(tab, text="提示：Cookie 与 Token 都只保存在本目录的 cookies.txt / auth.json，不会上传到任何地方。",
                  style="Muted.TLabel", wraplength=900).pack(anchor="w", pady=(16, 0))

    # ------------------------------------------------------------ 通知页
    def _build_notify_tab(self):
        tab = ttk.Frame(self.nb, style="TFrame", padding=14)
        self.nb.add(tab, text="  🔔  通知推送  ")
        box = ttk.LabelFrame(tab, text=" 全部可选，留空即不发送 ", padding=14)
        box.pack(fill="x")
        self.notify_vars = {}
        for i, (key, label) in enumerate(NOTIFY_FIELDS):
            ttk.Label(box, text=label).grid(row=i, column=0, sticky="w", pady=7)
            self.notify_vars[key] = StringVar()
            ttk.Entry(box, textvariable=self.notify_vars[key], width=64, show="*").grid(
                row=i, column=1, sticky="w", padx=12, pady=7)
        ttk.Button(box, text="📢 发送测试通知", command=lambda: self._start("notify")).grid(
            row=len(NOTIFY_FIELDS), column=1, sticky="w", padx=12, pady=(14, 0))
        ttk.Label(tab, text="也可以直接填完整 Webhook URL（以 http 开头），程序会原样使用。",
                  style="Muted.TLabel").pack(anchor="w", pady=(14, 0))

    # ------------------------------------------------------------ 配置读写
    def _load_into_ui(self, cfg):
        for name in TASK_ORDER:
            self.task_vars[name].set(bool(cfg.get("tasks", {}).get(name, True)))
        for key, var in self.v.items():
            if key in cfg:
                var.set(cfg[key] if not isinstance(var, BooleanVar) else bool(cfg[key]))
        for key, var in self.notify_vars.items():
            var.set(cfg.get("notify", {}).get(key, "") or "")
        self.var_verbose.set(bool(cfg.get("verbose", True)))
        auth = cli._load(cli.AUTH_PATH, cli.DEFAULT_AUTH)
        self.refresh_var.set(auth.get("refresh_token", "") or "")

    def _collect(self) -> dict:
        cfg = dict(self.cfg)
        cfg["tasks"] = {n: bool(v.get()) for n, v in self.task_vars.items()}
        for key, var in self.v.items():
            val = var.get()
            if key in INT_KEYS:
                try:
                    val = int(str(val).strip())
                except ValueError:
                    val = cli.DEFAULT_CONFIG.get(key, 0)
            cfg[key] = val
        cfg["notify"] = {k: v.get().strip() for k, v in self.notify_vars.items()}
        cfg["verbose"] = bool(self.var_verbose.get())
        return cfg

    def _save_all(self, silent=True) -> bool:
        try:
            cfg = self._collect()
            cli._save(cli.CONFIG_PATH, cfg)
            self.cfg = cfg
            self.v["verbose"].set(cfg["verbose"])
            self.var_verbose.set(cfg["verbose"])
            if not silent:
                messagebox.showinfo("已保存", "配置已写入 config.json")
            self.footer_var.set(f"✅ 配置已保存  {time.strftime('%H:%M:%S')}")
            return True
        except Exception as e:
            messagebox.showerror("保存失败", str(e))
            return False

    # ------------------------------------------------------------ 日志
    def _hook(self, level, icon, msg):
        self.q.put(("LOG", level, icon, msg))

    def _sys(self, msg):
        self.q.put(("LOG", "SYS", "ℹ", msg))

    def _drain(self):
        try:
            while True:
                item = self.q.get_nowait()
                kind = item[0]
                if kind == "LOG":
                    _, level, icon, msg = item
                    self._append(level, icon, msg)
                elif kind == "STATUS":
                    for k, v in item[1].items():
                        if k in self.status_vars:
                            self.status_vars[k].set(v)
                elif kind == "COOKIE_STATE":
                    self.cookie_state.set(item[1])
                elif kind == "DONE":
                    self.running = False
                    self._set_buttons(True)
                    self._sys("任务结束，界面已恢复")
                elif kind == "ERROR":
                    self._append("ERROR", "🔴", item[1])
        except queue.Empty:
            pass
        self.root.after(80, self._drain)

    def _append(self, level, icon, msg):
        tag = "INFO" if level == "INFO" else level
        if icon in ("🔴", "❌"):
            tag = "ERROR"
        elif icon in ("🟡", "⚠️", "⏭️"):
            tag = "WARN"
        elif icon in ("🟢", "✅"):
            tag = "OK"
        line = f"[{time.strftime('%H:%M:%S')}] [{level:<5}] {icon} {msg}\n"
        self.txt.insert(END, line, tag)
        # 控制日志行数，避免长时间守护占满内存
        if int(self.txt.index("end-1c").split(".")[0]) > 3000:
            self.txt.delete("1.0", "1000.0")
        if self.var_autoscroll.get():
            self.txt.see(END)

    def _clear_log(self):
        self.txt.delete("1.0", END)
        self._sys("日志已清空（磁盘日志文件不受影响）")

    # ------------------------------------------------------------ 运行控制
    def _set_buttons(self, enabled):
        for b in self.buttons.values():
            b.configure(state="normal" if enabled else "disabled")
        self.btn_stop.configure(state="disabled" if enabled else "normal")
        self.footer_var.set("空闲" if enabled else "⏳ 任务运行中…")

    def _start(self, mode):
        if self.running:
            messagebox.showwarning("正在运行", "已有任务在运行，请先等待或点「停止」")
            return
        if not self._save_all(True):
            return
        self.running = True
        self.stop_event.clear()
        self._set_buttons(False)
        self._sys(f"启动操作: {mode}")
        threading.Thread(target=self._worker, args=(mode,), daemon=True).start()

    def _stop(self):
        self.stop_event.set()
        self._sys("⏹ 已请求停止（当前等待会立即中断，正在进行的请求完成后退出）")

    def _make_api(self, dry_run=False):
        api = cli.make_api(dry_run=dry_run, verbose=bool(self.var_verbose.get()))
        api.log_hook = self._hook
        api.stop_event = self.stop_event
        return api

    def _worker(self, mode):
        try:
            if mode == "notify":
                api = self._make_api(dry_run=True)
                api.notifier.send("🧪 测试消息：看到这条说明通知配置成功")
                self.q.put(("LOG", "OK", "🟢", "测试通知已发送"))
            elif mode == "check":
                self._do_check()
            elif mode == "daemon":
                self._do_daemon()
            elif mode == "test":
                import endpoint_test
                api = self._make_api(dry_run=False)
                try:
                    endpoint_test.run(api)
                finally:
                    api.http.save_cookies(cli.COOKIE_PATH)
            else:
                self._do_run(mode == "dry")
        except Exception as e:
            self.q.put(("ERROR", f"运行异常: {type(e).__name__}: {e}\n{traceback.format_exc()}"))
        finally:
            self.q.put(("DONE",))

    def _only(self):
        if not self.var_only_selected.get():
            return None
        return [n for n in tasks.TASKS if self.task_vars.get(n) and self.task_vars[n].get()]

    def _do_run(self, dry):
        api = self._make_api(dry_run=dry)
        tasks.run_all(api, self._only())
        api.http.save_cookies(cli.COOKIE_PATH)

    def _do_daemon(self):
        interval = max(60, int(self.cfg.get("daemon_interval_minutes", 60)) * 60)
        jitter = max(0, int(self.cfg.get("daemon_interval_jitter_minutes", 30)) * 60)
        lo = int(self.cfg.get("daemon_start_delay_min_s", 5))
        hi = int(self.cfg.get("daemon_start_delay_max_s", 95))
        import random
        first = random.randint(lo, max(lo, hi))
        idle_need = int(self.cfg.get("daemon_idle_seconds", 300) or 0)
        self._sys(f"守护模式：{first} 秒后开始等空闲；电脑空闲 {idle_need} 秒才动手，"
                  f"任务之间随机间隔，两轮间隔约 {interval // 60}±{jitter // 60} 分钟")
        if self.stop_event.wait(first):
            return
        n = 0
        while not self.stop_event.is_set():
            n += 1
            self._sys(f"===== 守护第 {n} 轮 =====")
            api = self._make_api(dry_run=False)
            tasks.run_all(api, self._only(), pace=True)
            api.http.save_cookies(cli.COOKIE_PATH)
            wait = max(60, interval + (random.randint(-jitter, jitter) if jitter else 0))
            self._sys(f"第 {n} 轮结束，{wait // 60} 分钟后进入下一轮（仍需电脑空闲）")
            if self.stop_event.wait(wait):
                break

    def _do_check(self):
        api = self._make_api(dry_run=True)
        self._sys("开始体检（只读，不改任何积分）")
        status = {}
        status["cookie"] = "✅ 已配置" if os.path.isfile(cli.COOKIE_PATH) else "❌ 未配置"
        rt = (api.auth.get("refresh_token") or "").strip()
        status["token"] = "✅ 已授权" if rt else "⚠️ 未授权"
        ok = api.check_region()
        status["region"] = "✅ CN" if ok else "❌ 非CN/失败"
        bal = api.get_balance() or api.balance_from_dashboard()
        if bal:
            status["points"] = str(bal)
            self._sys(f"Cookie 登录有效，当前积分 {bal}")
        else:
            status["points"] = "读取失败"
            self._sys("🔴 读不到积分（Cookie 可能失效或未登录）")
        info = api.get_rewards_info()
        if info:
            status["quota"] = f"{info['pc']['progress']}/{info['pc']['max']}"
            self._sys(f"搜索配额 PC {status['quota']}，阅读 {info['read_progress']}/{info['read_max']}"
                      f"（来源 {info['source']}）")
        else:
            status["quota"] = "读取失败"
        cards = api.discover_cards()
        links = api.get_daily_set_links()
        self._sys(f"活动卡片 {len(cards)} 个待领取，每日活动链接 {len(links)} 个")
        self._sys("体检完成 ✅")
        self.q.put(("STATUS", status))

    # ------------------------------------------------------------ 账号操作
    def _refresh_file_status(self):
        n = 0
        if os.path.isfile(cli.COOKIE_PATH):
            try:
                with open(cli.COOKIE_PATH, "r", encoding="utf-8", errors="ignore") as f:
                    n = sum(1 for line in f if line.strip() and not line.startswith("#"))
            except Exception:
                n = -1
        self.cookie_state.set(f"当前 cookies.txt: {'已配置 ' + str(n) + ' 条' if n > 0 else '未配置/为空'}")
        self.status_vars["cookie"].set("✅ 已配置" if n > 0 else "❌ 未配置")
        auth = cli._load(cli.AUTH_PATH, cli.DEFAULT_AUTH)
        self.status_vars["token"].set("✅ 已授权" if (auth.get("refresh_token") or "").strip() else "⚠️ 未授权")

    def _save_cookie_text(self):
        raw = self.cookie_text.get("1.0", END).strip()
        if not raw:
            messagebox.showwarning("空内容", "请先粘贴 Cookie 字符串")
            return
        try:
            api = self._make_api(dry_run=True)
            cli.import_cookies(raw, api)
            self._refresh_file_status()
            self._sys("🟢 Cookie 已保存（内容不会显示在日志里）")
        except Exception as e:
            messagebox.showerror("导入失败", str(e))

    def _import_cookie_file(self):
        path = filedialog.askopenfilename(title="选择 cookies.txt",
                                          filetypes=[("Cookie 文件", "*.txt"), ("所有文件", "*.*")])
        if not path:
            return
        try:
            api = self._make_api(dry_run=True)
            cli.import_cookies(path, api)
            self._refresh_file_status()
            self._sys(f"🟢 已从文件导入 Cookie: {os.path.basename(path)}")
        except Exception as e:
            messagebox.showerror("导入失败", str(e))

    def _clear_cookies(self):
        if not messagebox.askyesno("确认", "确定清空已保存的 Cookie？"):
            return
        try:
            os.remove(cli.COOKIE_PATH)
        except FileNotFoundError:
            pass
        self._refresh_file_status()

    def _copy_auth_url(self):
        self.root.clipboard_clear()
        self.root.clipboard_append(AUTH_URL)
        self._sys("授权链接已复制到剪贴板")

    def _do_auth(self):
        raw = self.auth_code_var.get().strip()
        if not raw:
            messagebox.showwarning("空内容", "请粘贴授权后地址栏的完整 URL 或 code")
            return
        try:
            api = self._make_api(dry_run=True)
            ok = cli.do_auth(raw, api)
            if ok:
                self.refresh_var.set(api.auth.get("refresh_token", ""))
                self._sys("🟢 授权成功，refresh_token 已保存")
                self._refresh_file_status()
        except Exception as e:
            messagebox.showerror("授权失败", str(e))

    def _save_refresh(self):
        raw = self.refresh_var.get().strip()
        api = self._make_api(dry_run=True)
        api.auth["refresh_token"] = raw
        api.save_auth()
        self._sys("🟢 refresh_token 已写入 auth.json")
        self._refresh_file_status()

    def _clear_token(self):
        api = self._make_api(dry_run=True)
        api.auth = dict(cli.DEFAULT_AUTH)
        api.save_auth()
        self.refresh_var.set("")
        self._sys("Token 已清除")
        self._refresh_file_status()

    # ------------------------------------------------------------ 杂项
    @staticmethod
    def _open_dir(path):
        try:
            os.makedirs(path, exist_ok=True)
            os.startfile(path)  # noqa: Windows
        except Exception:
            try:
                webbrowser.open("file:///" + path.replace("\\", "/"))
            except Exception:
                pass

    def _on_close(self):
        if self.running and not messagebox.askyesno("确认退出", "任务仍在运行，确定要停止并退出吗？"):
            return
        self.stop_event.set()
        self.root.destroy()


def main():
    root = Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
