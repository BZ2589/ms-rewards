"""
生成 Windows .lnk 快捷方式 —— 纯标准库实现（MS-SHLLINK 格式），
不依赖 COM / WScript.Shell / pywin32，因此在受限环境里也能运行。

用法：
    python make_shortcut.py                       # 在桌面生成默认快捷方式
    python make_shortcut.py --out "D:\\xxx.lnk"  # 指定输出位置
    python make_shortcut.py --verify             # 额外做一次解析自测

注意：LinkInfo 里的目标路径用 ANSI 编码，因此目标/工作目录/图标路径
      必须是纯 ASCII（本项目路径满足）。若迁移到含中文的路径，
      Explorer 仍可凭 StringData 中的工作目录解析，但建议保持 ASCII 路径。
"""
from __future__ import annotations

import argparse
import os
import struct
import sys

CLSID = bytes.fromhex("0114020000000000c000000000000046")
# LinkFlags: HasLinkTargetIDList | HasLinkInfo | HasName | HasWorkingDir
#            | HasArguments | HasIconLocation | IsUnicode
FLAGS = 0x01 | 0x02 | 0x04 | 0x10 | 0x20 | 0x40 | 0x80    # 0xF7


def _unicode_string(s: str) -> bytes:
    """StringData 项：2 字节字符数 + UTF-16LE 内容（无结尾 0）。"""
    return struct.pack("<H", len(s)) + s.encode("utf-16le")


def _idlist_for_path(path: str) -> bytes:
    """用 Windows Shell32 官方 API 取得目标路径的真实 PIDL（LinkTargetIDList）。

    ILCreateFromPathW / ILGetSize / ILFree 是普通 Win32 导出函数，
    不涉及 COM 对象实例化，也不需要 pywin32。
    """
    import ctypes
    from ctypes import wintypes
    path = os.path.normpath(path)                 # 兼容正斜杠写法
    sh = ctypes.WinDLL("shell32", use_last_error=True)
    sh.ILCreateFromPathW.argtypes = [wintypes.LPCWSTR]
    sh.ILCreateFromPathW.restype = ctypes.c_void_p
    sh.ILGetSize.argtypes = [ctypes.c_void_p]
    sh.ILGetSize.restype = ctypes.c_uint
    sh.ILFree.argtypes = [ctypes.c_void_p]
    pidl = sh.ILCreateFromPathW(path)
    if not pidl:
        raise OSError(f"ILCreateFromPathW 失败: {path} ({ctypes.get_last_error()})")
    try:
        n = sh.ILGetSize(pidl)
        buf = (ctypes.c_ubyte * n)()
        ctypes.memmove(buf, ctypes.c_void_p(pidl), n)
        return bytes(buf)
    finally:
        sh.ILFree(pidl)


def _ansi(s: str) -> bytes:
    """LinkInfo 里的路径是 ANSI 串：优先纯 ASCII，含中文则退回系统 ANSI 代码页（GBK）。

    Explorer 实际上是以 LinkTargetIDList（Unicode PIDL）为准，LocalBasePath
    只是提示，所以中文路径用 mbcs 编码仍能打开。
    """
    try:
        return s.encode("ascii")
    except UnicodeEncodeError:
        return s.encode("mbcs", errors="replace")


def _link_info(target: str) -> bytes:
    """构造 LinkInfo + VolumeID + LocalBasePath + CommonPathSuffix。"""
    local = _ansi(target) + b"\x00"
    suffix = b"\x00"
    label = b"\x00"
    # VolumeLabelOffset = 0x10 表示无 Unicode 标签字段（0x14 才需要额外 4 字节）
    volume = struct.pack("<IIII", 0x10 + len(label), 3, 0, 0x10) + label
    hdr_size = 0x1C
    vol_off = hdr_size
    local_off = vol_off + len(volume)
    suffix_off = local_off + len(local)
    total = suffix_off + len(suffix)
    head = struct.pack("<IIIIIII", total, hdr_size, 1, vol_off,
                       local_off, 0, suffix_off)
    return head + volume + local + suffix


def build(target: str, args: str = "", workdir: str = "", icon: str = "",
          name: str = "") -> bytes:
    """返回完整的 .lnk 二进制内容。"""
    header = (
        struct.pack("<I", 0x4C) + CLSID +
        struct.pack("<II", FLAGS, 0x20) +          # LinkFlags, FILE_ATTRIBUTE_ARCHIVE
        b"\x00" * 24 +                             # 3 x FILETIME
        struct.pack("<III", 0, 0, 1) +             # FileSize, IconIndex, SW_SHOWNORMAL
        struct.pack("<HHII", 0, 0, 0, 0)           # HotKey / Reserved1/2/3
    )
    assert len(header) == 76, len(header)
    idlist = _idlist_for_path(target)
    # StringData 顺序固定：NAME → WORKING_DIR → ARGUMENTS → ICON_LOCATION
    strings = (_unicode_string(name) + _unicode_string(workdir)
               + _unicode_string(args) + _unicode_string(icon))
    return (header + struct.pack("<H", len(idlist)) + idlist
            + _link_info(target) + strings + struct.pack("<I", 0))


def verify(data: bytes) -> dict:
    """反向解析，确认结构自洽（用于自检，不依赖 Windows API）。"""
    assert data[:4] == struct.pack("<I", 0x4C), "HeaderSize 错误"
    assert data[4:20] == CLSID, "CLSID 错误"
    flags = struct.unpack_from("<I", data, 0x14)[0]
    assert flags & 1, "缺少 LinkTargetIDList"
    n = struct.unpack_from("<H", data, 76)[0]
    pos = 78 + n                                  # 跳过 IDListSize + IDList
    li_size, li_hdr, li_flags, vol_off, local_off, _, suffix_off = \
        struct.unpack_from("<IIIIIII", data, pos)
    assert li_hdr == 0x1C and li_flags & 1, "LinkInfo 错误"
    assert vol_off + 4 <= li_size, "VolumeID 越界"
    end = pos + local_off
    local = data[end:data.index(b"\x00", end)]
    try:
        local_s = local.decode("ascii")
    except UnicodeDecodeError:
        local_s = local.decode("mbcs", errors="replace")
    pos += li_size
    out = {"flags": hex(flags), "idlist_size": n, "link_info_size": li_size, "target": local_s}
    for key in ("name", "workdir", "args", "icon"):
        n = struct.unpack_from("<H", data, pos)[0]
        pos += 2
        out[key] = data[pos:pos + n * 2].decode("utf-16le")
        pos += n * 2
    assert data[pos:pos + 4] == b"\x00" * 4, "TerminalBlock 错误"
    return out


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--out", default="")
    p.add_argument("--target", default="", help="exe 路径（默认：打包后就是自己）")
    p.add_argument("--args", default="", help="传给目标的参数，默认打包后为 --minimized")
    p.add_argument("--verify", action="store_true")
    a = p.parse_args()

    script_dir = os.path.dirname(os.path.abspath(__file__))
    frozen = getattr(sys, "frozen", False)
    if a.target:
        target = a.target
        args = a.args
        workdir = os.path.dirname(os.path.abspath(target))
        icon = target                                    # exe 自带图标
    elif frozen:
        # 打包后：快捷方式直接指向 exe 本身，不经过解释器
        target = sys.executable
        args = a.args or "--minimized"
        workdir = os.path.dirname(os.path.abspath(target))
        icon = target
    else:
        target = r"D:\Anaconda\envs\test001\pythonw.exe"
        args = a.args or f'"{os.path.join(script_dir, "gui_qt.py")}"'
        workdir = script_dir
        icon = os.path.join(script_dir, "app.ico")
    out = a.out or os.path.join(os.path.expanduser("~"), "Desktop", "MS Rewards 控制台.lnk")

    if not os.path.isfile(target):
        print(f"❌ 找不到目标程序: {target}")
        return 1
    if not os.path.isfile(icon):
        icon = ""

    data = build(target=target, args=args, workdir=workdir,
                 icon=icon, name="Microsoft Rewards 自动任务控制台")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "wb") as f:
        f.write(data)

    parsed = verify(data)
    print(f"✅ 快捷方式已生成: {out}  ({len(data)} bytes)")
    for k, v in parsed.items():
        print(f"   {k:<16} = {v}")
    if a.verify:
        with open(out, "rb") as f:
            assert verify(f.read())["target"] == target, "落盘后解析不一致"
        print("✅ 落盘复读校验通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
