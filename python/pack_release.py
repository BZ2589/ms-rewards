"""
一键打包成「便携版单文件 exe」并生成发布文件夹 / 桌面快捷方式。

    python pack_release.py                 # 打包 + 组装 dist\MSRewards\ + 桌面快捷方式
    python pack_release.py --no-shortcut   # 只打包，不建快捷方式
    python pack_release.py --onedir        # 改成文件夹版（启动更快、体积更散）

设计原则：
  * 单文件 exe，不放安装包 —— 解压即用，不写注册表（除了可选的"开机自启"）、
    不需要管理员权限、卸载就是删文件夹。
  * 配置 / Cookie / 日志 / 浏览器 profile 全部写在 exe 旁边（main.BASE_DIR），
    所以 exe 必须待在它自己的文件夹里，不要直接丢桌面。
  * 桌面快捷方式指向 exe，工作目录 = exe 所在文件夹。
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PY = r"D:\Anaconda\envs\test001\python.exe"
NAME = "MSRewards"
MIRROR = "https://pypi.tuna.tsinghua.edu.cn/simple"

HIDDEN = [
    "main", "tasks", "rewards", "http_client", "notify", "netguard",
    "endpoint_test", "idle_guard", "browser_login", "browser_dom",
    "make_shortcut", "psutil",
]
# 用不到的大块头，排除掉能少几 MB、也少点误报
EXCLUDE = ["tkinter", "matplotlib", "numpy", "pandas", "scipy", "PIL.ImageQt",
           "PyQt5.QtQuick", "PyQt5.QtQml", "PyQt5.QtWebEngineWidgets",
           "PyQt5.Qt3DCore", "test", "unittest"]

# conda 版 Python 把 C 依赖放在 Library\bin，PyInstaller 默认找不到，
# 缺 ffi-8.dll 会让 ctypes 直接炸（idle_guard / browser_login 全靠它），
# 缺 libssl/libcrypto 则所有 HTTPS 请求挂掉。这里显式带上。
CONDA_DLLS = ["ffi-8.dll", "liblzma.dll", "libbz2.dll",
              "libssl-3-x64.dll", "libcrypto-3-x64.dll",
              "expat.dll", "sqlite3.dll", "zlib.dll", "zlib1.dll"]

README = """\
Microsoft Rewards 自动任务控制台 —— 便携版
=========================================

【怎么用】
1. 把整个 MSRewards 文件夹放到一个你找得到、且有写权限的位置，例如 D:\\MSRewards
   （不要只把 exe 拖到桌面 —— 配置、日志、登录状态都会写在 exe 旁边，
     散在桌面上很难看，也容易误删。）
2. 双击 MSRewards.exe 打开界面。
3. 第一次用：左侧「账号与授权」→ 一键登录，在弹出的 Edge 里登录微软账号，
   登录完成后窗口会自动关掉。
4. 之后点「正式运行」，或者打开「开机自启 + 守护」，就自动挂机了。
5. 想要桌面图标：左侧「关于」页 → 「🖥 在桌面创建快捷方式」，一次搞定
   （别自己去右键 exe 建，那样起始位置可能不对）。

【别人第一次打开可能挡一下】
* Windows 会弹「Windows 已保护你的电脑」——这是未签名程序的正常提示，
  点「更多信息」→「仍要运行」即可。
* 程序靠系统自带的 Edge 完成登录，Win10/11 默认都有。

【数据在哪】
config.json      设置（界面上改的就是它）
auth.json        登录令牌
cookies.txt      登录 Cookie
state.json       今日目标、进度、连签天数
hot_words.json   当天抓的热词
logs\\            每天一个日志文件（默认只留最近 7 天，控制台页可改；第 8 天先删最老的）
.browser-profile 独立 Edge 配置（只给本程序用，不碰你日常浏览器）

换电脑 / 备份：整个文件夹拷走即可。卸载：删掉文件夹，再去界面里关掉「开机自启」。

【可能遇到的事】
* 杀毒软件报警：单文件 Python 程序常被误判，加白名单即可。若被删，可改用文件夹版。
* 开机没自己起来：界面上把「开机自启」关一次再打开，让注册表指向新位置。
* 开机后提示等网络：正常，程序会等网络通了才发请求。
"""


def log(msg: str) -> None:
    print(msg, flush=True)


def ensure_pyinstaller(py: str) -> bool:
    if subprocess.call([py, "-c", "import PyInstaller"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) == 0:
        return True
    log("[*] PyInstaller 未安装，正在安装…")
    return subprocess.call([py, "-m", "pip", "install", "pyinstaller", "-i", MIRROR]) == 0


def build(py: str, onedir: bool) -> str:
    dist = os.path.join(HERE, "dist")
    rel = os.path.join(dist, NAME)
    # 注意：dist\MSRewards\ 可能被当成日常使用目录，里面有 Cookie/日志。
    # 所以只替换程序本体，绝不整目录删除；onedir 想清空必须自己删。
    if onedir and os.path.isdir(rel):
        bad = [f for f in ("cookies.txt", "auth.json", "config.json")
               if os.path.isfile(os.path.join(rel, f))]
        if bad:
            raise SystemExit(f"[ERROR] {rel} 里有用户数据（{', '.join(bad)}），"
                             "先把它挪走再打 onedir 包。")
        shutil.rmtree(rel, ignore_errors=True)
    if os.path.isfile(os.path.join(dist, NAME + ".exe")):
        os.remove(os.path.join(dist, NAME + ".exe"))
    cmd = [py, "-m", "PyInstaller", "--noconfirm", "--clean",
           "--onedir" if onedir else "--onefile", "--windowed",
           "--name", NAME, "--icon", os.path.join(HERE, "app.ico"),
           "--add-data", "app.ico" + os.pathsep + ".",
           "--distpath", dist, "--workpath", os.path.join(HERE, "build"),
           "--specpath", HERE]
    for m in HIDDEN:
        cmd += ["--hidden-import", m]
    for m in EXCLUDE:
        cmd += ["--exclude-module", m]
    cmd.append(os.path.join(HERE, "gui_qt.py"))
    # 显式补 conda 的 C 运行时依赖（缺 ffi-8.dll 直接 ctypes 崩溃）
    lib_bin = os.path.join(os.path.dirname(os.path.abspath(py)), "Library", "bin")
    for dll in CONDA_DLLS:
        p = os.path.join(lib_bin, dll)
        if os.path.isfile(p):
            cmd += ["--add-binary", p + os.pathsep + "."]
        else:
            log(f"[i] 跳过缺失的 {dll}")

    log("[*] 开始打包（首次约 1~3 分钟）…")
    rc = subprocess.call(cmd, cwd=HERE)
    if rc != 0:
        raise SystemExit(f"[ERROR] PyInstaller 失败，退出码 {rc}")
    return os.path.join(dist, NAME + ".exe")


def assemble(onedir: bool) -> str:
    """把产物收进 dist\\MSRewards\\，并写使用说明。"""
    dist = os.path.join(HERE, "dist")
    rel = os.path.join(dist, NAME)

    if onedir:
        if not os.path.isfile(os.path.join(rel, NAME + ".exe")):
            raise SystemExit("[ERROR] onedir 产物不完整: " + rel)
    else:
        exe = os.path.join(dist, NAME + ".exe")
        if not os.path.isfile(exe):
            raise SystemExit("[ERROR] 没找到 " + exe)
        os.makedirs(rel, exist_ok=True)
        old = os.path.join(rel, NAME + ".exe")     # 只换程序本体，用户数据原样留着
        if os.path.isfile(old):
            os.remove(old)
        shutil.move(exe, old)

    with open(os.path.join(rel, "使用说明.txt"), "w", encoding="utf-8") as f:
        f.write(README)
    return rel


def make_link(rel_dir: str, out: str) -> int:
    target = os.path.join(rel_dir, NAME + ".exe")
    sys.path.insert(0, HERE)
    import make_shortcut
    # 双击快捷方式要看到界面，所以不带 --minimized（--minimized 只留给注册表开机自启）
    data = make_shortcut.build(target=target, args="", workdir=rel_dir,
                               icon=target, name="Microsoft Rewards 自动任务控制台")
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with open(out, "wb") as f:
        f.write(data)
    info = make_shortcut.verify(data)
    log(f"[OK] 快捷方式: {out}")
    for k, v in info.items():
        log(f"     {k:<15} = {v}")
    return 0


PERSONAL_FILES = {"config.json", "auth.json", "cookies.txt", "state.json", "hot_words.json"}
PERSONAL_DIRS = {"logs", ".browser-profile"}


def make_zip(rel: str) -> str:
    """压一个给别人用的包：只装程序 + 说明，个人的 Cookie/授权/配置一律剔除。"""
    import zipfile
    out = rel.rstrip("\\/") + ".zip"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for root, dirs, files in os.walk(rel):
            dirs[:] = [d for d in dirs if d not in PERSONAL_DIRS]
            for fn in files:
                if fn in PERSONAL_FILES:
                    continue
                full = os.path.join(root, fn)
                z.write(full, os.path.relpath(full, os.path.dirname(rel)))
    log(f"[OK] 分发压缩包: {out}  ({os.path.getsize(out) / 1048576:.1f} MB，已剔除个人数据)")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--py", default=DEFAULT_PY)
    ap.add_argument("--onedir", action="store_true", help="打包成文件夹版而不是单文件")
    ap.add_argument("--no-shortcut", action="store_true")
    ap.add_argument("--zip", action="store_true", help="额外压一个分发用 zip（自动剔除 Cookie/授权/配置）")
    ap.add_argument("--shortcut-out", default="")
    a = ap.parse_args()

    py = a.py
    if not os.path.isfile(py):
        raise SystemExit(f"[ERROR] 找不到解释器: {py}")
    if not ensure_pyinstaller(py):
        raise SystemExit("[ERROR] PyInstaller 安装失败")

    build(py, a.onedir)
    rel = assemble(a.onedir)
    size = 0
    for root, _dirs, files in os.walk(rel):
        for fn in files:
            size += os.path.getsize(os.path.join(root, fn))
    log(f"[OK] 发布目录: {rel}  ({size / 1048576:.1f} MB)")

    if not a.no_shortcut:
        out = a.shortcut_out or os.path.join(os.path.expanduser("~"), "Desktop",
                                            "MS Rewards 控制台.lnk")
        if os.path.exists(out) and not a.shortcut_out:
            out = os.path.join(os.path.expanduser("~"), "Desktop", "MS Rewards 控制台(exe).lnk")
            log("[i] 桌面已有旧快捷方式（指向源码版），这条改名为 …(exe).lnk")
        make_link(rel, out)
    if a.zip:
        make_zip(rel)
    log("[DONE] 可以直接把整个文件夹拷给别人（或压缩后发送）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
