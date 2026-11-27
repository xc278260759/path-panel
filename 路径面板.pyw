# -*- coding: utf-8 -*-
"""
路径面板 (Path Panel)
------------------------------------------------
用途：把常用的路径、文本片段、图片收藏起来，用快捷键呼出面板，
      自动按类型归类（路径 / 文本 / 图片），选中后粘贴到当前输入框。

热键：
    Win + Alt + V   呼出/关闭面板
    Win + Alt + C   把当前选中或剪贴板里的内容加入收藏（自动分类）

托盘：显示面板 / 从剪贴板添加 / 设置 / 开机自启 / 退出
数据：%APPDATA%\\路径面板\\items.json（旧 paths.json 会自动迁移）
日志：%APPDATA%\\路径面板\\log.txt

依赖：Python 3.7 + Pillow + pystray + pypinyin（tkinter 自带）
"""

import ctypes
import hashlib
import json
import os
import queue
import re
import struct
import sys
import tempfile
import threading
import time
import urllib.parse
from ctypes import wintypes

APP_NAME = "路径面板"
APP_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), APP_NAME)
IMAGE_DIR = os.path.join(APP_DIR, "images")
DATA_FILE = os.path.join(APP_DIR, "items.json")
OLD_DATA_FILE = os.path.join(APP_DIR, "paths.json")
LOG_FILE = os.path.join(APP_DIR, "log.txt")
MUTEX_NAME = "Local\\PathPanel_SingleInstance"

IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp", ".tif", ".tiff",
              ".ico", ".heic", ".heif", ".avif", ".psd")
TYPE_ORDER = ("path", "text", "image")
TYPE_LABEL = {"path": "路径", "text": "文本", "image": "图片"}

MAX_TEXT_CHARS = 100000          # 单条文本上限（约 100KB）
MAX_IMAGES = 200                 # 图片副本最多保留张数
MAX_IMAGE_BYTES = 50 * 1024 * 1024   # 单张图片上限 50MB

DEFAULTS = {
    "entries": [],                # 收藏条目：[{"type": "path/text/image", "value": ...}]
    "copy_file_mode": "dir",      # dir=复制文件时记录所在目录, file=记录文件本身
    "restore_clip": True,         # 粘贴后恢复原剪贴板
    "paste_mode": "ctrl_v",       # ctrl_v / ctrl_shift_v / shift_insert
    "quote_path": False,          # 粘贴时给路径加引号
    "double_confirm": True,       # 双击条目时自动回车确认（地址栏跳转/表单提交）
    "confirm_whitelist": ["explorer.exe"],  # 这些应用里即使关闭自动确认也会回车
    "auto_collect": True,         # 自动收录复制到剪贴板的路径
    "sort_by_letter": True,       # 自动排序；关闭后回到手动排序
    "group_style": "letter",      # letter=盘符/字母标题(默认)  color=彩色分割条
    "active_tab": "path",         # 上次停留的分类页
    "autostart": False,
}

# ---------------------------------------------------------------- 基础

C_BG = "#202020"
C_BG2 = "#2b2b2b"
C_BORDER = "#2f2f2f"
C_FG = "#ffffff"
C_DIM = "#9a9a9a"
C_ACCENT = "#e8423c"
C_OK = "#63c27a"
C_BAD = "#d9534f"
C_SEL = "#3a3a3a"
C_THUMB = "#454545"

FONT = ("Microsoft YaHei UI", 10)
FONT_S = ("Microsoft YaHei UI", 9)


def ensure_dirs():
    os.makedirs(APP_DIR, exist_ok=True)


def log(msg):
    try:
        ensure_dirs()
        if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > 256 * 1024:
            with open(LOG_FILE, "r", encoding="utf-8", errors="ignore") as f:
                lines = f.readlines()[-2000:]
            with open(LOG_FILE, "w", encoding="utf-8") as f:
                f.writelines(lines)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write("%s  %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except Exception:
        pass


# ---------------------------------------------------------------- Win32

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
shell32 = ctypes.windll.shell32

WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
MOD_ALT = 0x0001
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000
CF_HDROP = 15
CF_UNICODETEXT = 13
CF_DIB = 8
GMEM_MOVEABLE = 0x0002

user32.GetClipboardData.restype = wintypes.HANDLE
user32.SetClipboardData.restype = wintypes.HANDLE
kernel32.GlobalAlloc.restype = wintypes.HANDLE
kernel32.GlobalLock.restype = ctypes.c_void_p
user32.GetClipboardData.argtypes = [wintypes.UINT]
user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
kernel32.GlobalLock.argtypes = [wintypes.HANDLE]
kernel32.GlobalUnlock.argtypes = [wintypes.HANDLE]
kernel32.GlobalFree.argtypes = [wintypes.HANDLE]
shell32.DragQueryFileW.argtypes = [wintypes.HANDLE, wintypes.UINT, wintypes.LPWSTR, wintypes.UINT]
shell32.DragQueryFileW.restype = wintypes.UINT


class MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
    ]


class GUITHREADINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("hwndActive", wintypes.HWND),
        ("hwndFocus", wintypes.HWND),
        ("hwndCapture", wintypes.HWND),
        ("hwndMenuOwner", wintypes.HWND),
        ("hwndMoveSize", wintypes.HWND),
        ("hwndCaret", wintypes.HWND),
        ("rcCaret", wintypes.RECT),
    ]


def focus_of_window(hwnd):
    """取目标窗口所在线程当前的焦点控件句柄。"""
    if not hwnd:
        return 0
    try:
        tid = user32.GetWindowThreadProcessId(hwnd, None)
        gti = GUITHREADINFO()
        gti.cbSize = ctypes.sizeof(GUITHREADINFO)
        if user32.GetGUIThreadInfo(tid, ctypes.byref(gti)):
            return int(gti.hwndFocus or 0)
    except Exception as e:
        log("GetGUIThreadInfo failed: %r" % (e,))
    return 0


user32.IsWindow.argtypes = [wintypes.HWND]
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.GetGUIThreadInfo.argtypes = [wintypes.DWORD, ctypes.POINTER(GUITHREADINFO)]
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.SetFocus.argtypes = [wintypes.HWND]
user32.SwitchToThisWindow.argtypes = [wintypes.HWND, wintypes.BOOL]
user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                                wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]


def process_name_of_hwnd(hwnd):
    """取窗口所属进程的 exe 名（小写），失败返回空串。"""
    if hwnd:
        try:
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value:
                h = kernel32.OpenProcess(0x1000, False, pid.value)  # QUERY_LIMITED_INFORMATION
                if h:
                    try:
                        buf = ctypes.create_unicode_buffer(1024)
                        size = wintypes.DWORD(1024)
                        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                            return os.path.basename(buf.value).lower()
                    finally:
                        kernel32.CloseHandle(h)
        except Exception as e:
            log("process name failed: %r" % (e,))
    return ""


def window_class(hwnd):
    try:
        buf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, buf, 256)
        return buf.value
    except Exception:
        return ""


def set_focus_cross(focus_hwnd, target_hwnd):
    """跨线程把焦点还给目标控件（地址栏/输入框）。"""
    if not focus_hwnd:
        return False
    try:
        tid_target = user32.GetWindowThreadProcessId(target_hwnd, None)
        tid_me = kernel32.GetCurrentThreadId()
        if tid_target and tid_target != tid_me:
            user32.AttachThreadInput(tid_me, tid_target, True)
            try:
                user32.SetFocus(focus_hwnd)
            finally:
                user32.AttachThreadInput(tid_me, tid_target, False)
        else:
            user32.SetFocus(focus_hwnd)
        return True
    except Exception as e:
        log("set focus failed: %r" % (e,))
        return False


def set_dpi_aware():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            user32.SetProcessDPIAware()
        except Exception:
            pass


def open_clipboard(tries=8):
    for _ in range(tries):
        if user32.OpenClipboard(None):
            return True
        time.sleep(0.02)
    return False


def clipboard_files():
    paths = []
    if not user32.IsClipboardFormatAvailable(CF_HDROP):
        return paths
    if not open_clipboard():
        return paths
    try:
        h = user32.GetClipboardData(CF_HDROP)
        if not h:
            return paths
        count = shell32.DragQueryFileW(h, 0xFFFFFFFF, None, 0)
        for i in range(min(int(count), 128)):
            n = shell32.DragQueryFileW(h, i, None, 0)
            buf = ctypes.create_unicode_buffer(int(n) + 1)
            shell32.DragQueryFileW(h, i, buf, int(n) + 1)
            if buf.value:
                paths.append(buf.value)
    except Exception as e:
        log("read CF_HDROP failed: %r" % (e,))
    finally:
        user32.CloseClipboard()
    return paths


def clipboard_text():
    if not user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
        return ""
    if not open_clipboard():
        return ""
    try:
        h = user32.GetClipboardData(CF_UNICODETEXT)
        if not h:
            return ""
        p = kernel32.GlobalLock(h)
        if not p:
            return ""
        try:
            return ctypes.wstring_at(p)
        finally:
            kernel32.GlobalUnlock(h)
    except Exception as e:
        log("read text failed: %r" % (e,))
        return ""
    finally:
        user32.CloseClipboard()


def set_clipboard_text(text):
    if not open_clipboard():
        return False
    ok = False
    try:
        user32.EmptyClipboard()
        data = text.encode("utf-16-le") + b"\x00\x00"
        h = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        if not h:
            return False
        p = kernel32.GlobalLock(h)
        if not p:
            kernel32.GlobalFree(h)
            return False
        try:
            ctypes.memmove(p, data, len(data))
        finally:
            kernel32.GlobalUnlock(h)
        if user32.SetClipboardData(CF_UNICODETEXT, h):
            ok = True
        else:
            kernel32.GlobalFree(h)
    finally:
        user32.CloseClipboard()
    return ok


def set_clipboard_files(paths):
    if not paths:
        return False
    if not open_clipboard():
        return False
    ok = False
    try:
        user32.EmptyClipboard()
        blob = ("\0".join(paths) + "\0\0").encode("utf-16-le")
        header = struct.pack("<Iiiii", 20, 0, 0, 0, 1)
        data = header + blob
        h = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        if not h:
            return False
        p = kernel32.GlobalLock(h)
        if not p:
            kernel32.GlobalFree(h)
            return False
        try:
            ctypes.memmove(p, data, len(data))
        finally:
            kernel32.GlobalUnlock(h)
        if user32.SetClipboardData(CF_HDROP, h):
            ok = True
        else:
            kernel32.GlobalFree(h)
    finally:
        user32.CloseClipboard()
    return ok


def clipboard_image():
    """剪贴板里的位图（PIL Image），没有则 None。"""
    try:
        from PIL import ImageGrab
        data = ImageGrab.grabclipboard()
        if data is not None and not isinstance(data, list):
            return data
    except Exception as e:
        log("grab clipboard image failed: %r" % (e,))
    return None


def save_image_file(img, folder, prefix="img"):
    """把 PIL 图片存成 png，返回路径。"""
    try:
        os.makedirs(folder, exist_ok=True)
        name = "%s_%s_%03d.png" % (prefix, time.strftime("%Y%m%d_%H%M%S"),
                                   int(time.time() * 1000) % 1000)
        path = os.path.join(folder, name)
        img.convert("RGB").save(path, "PNG")
        return path
    except Exception as e:
        log("save image failed: %r" % (e,))
        return ""


def set_clipboard_image(path):
    """把 png 以 CF_DIB 放进剪贴板，支持图片粘贴的程序都能收到。"""
    try:
        from PIL import Image
        img = Image.open(path).convert("RGB")
        w, h = img.size
        raw = img.tobytes()
        rows = b"".join(raw[y * w * 3:(y + 1) * w * 3] for y in range(h - 1, -1, -1))
        header = struct.pack("<IiiHHIIiiII", 40, w, h, 1, 24, 0, len(rows), 0, 0, 0, 0)
        data = header + rows
    except Exception as e:
        log("build dib failed: %r" % (e,))
        return False
    if not open_clipboard():
        return False
    ok = False
    try:
        user32.EmptyClipboard()
        hg = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        if not hg:
            return False
        p = kernel32.GlobalLock(hg)
        if not p:
            kernel32.GlobalFree(hg)
            return False
        ctypes.memmove(p, data, len(data))
        kernel32.GlobalUnlock(hg)
        if user32.SetClipboardData(CF_DIB, hg):
            ok = True
        else:
            kernel32.GlobalFree(hg)
    finally:
        user32.CloseClipboard()
    return ok


def clipboard_snapshot():
    files = clipboard_files()
    if files:
        return ("files", files)
    txt = clipboard_text()
    if txt:
        return ("text", txt)
    img = clipboard_image()
    if img is not None:
        p = save_image_file(img, os.path.join(tempfile.gettempdir(), "路径面板_snap"), "snap")
        if p:
            return ("image", p)
    return None


def restore_clipboard(snap):
    if not snap:
        return
    kind, data = snap
    if kind == "files":
        set_clipboard_files(data)
    elif kind == "image":
        set_clipboard_image(data)
    else:
        set_clipboard_text(data)


def activate_window(hwnd):
    """把目标窗口拉回前台；返回是否确认成功。"""
    if not hwnd:
        return False
    try:
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 9)
    except Exception:
        pass
    try:
        if user32.SetForegroundWindow(hwnd):
            return True
    except Exception:
        pass
    try:
        fg = user32.GetForegroundWindow()
        tid_fg = user32.GetWindowThreadProcessId(fg, None)
        tid_me = kernel32.GetCurrentThreadId()
        if tid_fg and tid_fg != tid_me:
            user32.AttachThreadInput(tid_me, tid_fg, True)
            user32.SetForegroundWindow(hwnd)
            user32.BringWindowToTop(hwnd)
            user32.AttachThreadInput(tid_me, tid_fg, False)
    except Exception as e:
        log("activate attach failed: %r" % (e,))
    if user32.GetForegroundWindow() == hwnd:
        return True
    # 前台锁兜底
    try:
        user32.SwitchToThisWindow(hwnd, True)
    except Exception as e:
        log("SwitchToThisWindow failed: %r" % (e,))
    return user32.GetForegroundWindow() == hwnd


def refocus_target_editing(hwnd, focus_hwnd):
    """目标窗口被抢过焦点后，拉回前台并尽量恢复可编辑状态。"""
    if not hwnd:
        return False
    activate_window(hwnd)
    time.sleep(0.10)
    cls = window_class(hwnd)
    if cls in ("CabinetWClass", "ExploreWClass", "WorkerW"):
        # 资源管理器地址栏失焦后会退出编辑模式，Ctrl+L 能稳定重新聚焦
        _key(0x11); _key(0x4C); _key(0x4C, True); _key(0x11, True)
        time.sleep(0.12)
        log("refocus: explorer ctrl+l (%s)" % cls)
        return True
    if focus_hwnd:
        try:
            fg_tid = user32.GetWindowThreadProcessId(foreground_hwnd(), None)
            my_tid = kernel32.GetCurrentThreadId()
            if fg_tid and fg_tid != my_tid:
                user32.AttachThreadInput(my_tid, fg_tid, True)
            user32.SetFocus(focus_hwnd)
            if fg_tid and fg_tid != my_tid:
                user32.AttachThreadInput(my_tid, fg_tid, False)
            log("refocus: setfocus=%s ok=%s" % (focus_hwnd, user32.GetFocus() == focus_hwnd))
        except Exception as e:
            log("refocus setfocus failed: %r" % (e,))
    return True


def _key(vk, up=False):
    user32.keybd_event(vk, 0, 2 if up else 0, 0)


def send_paste(mode):
    vk_ctrl, vk_shift = 0x11, 0x10
    vk_v, vk_ins = 0x56, 0x2D
    if mode == "ctrl_shift_v":
        _key(vk_ctrl); _key(vk_shift); _key(vk_v); _key(vk_v, True)
        _key(vk_shift, True); _key(vk_ctrl, True)
    elif mode == "shift_insert":
        _key(vk_shift); _key(vk_ins); _key(vk_ins, True); _key(vk_shift, True)
    else:
        _key(vk_ctrl); _key(vk_v); _key(vk_v, True); _key(vk_ctrl, True)


def workarea_of_cursor():
    pt = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    hmon = user32.MonitorFromPoint(pt, 2)
    info = MONITORINFO()
    info.cbSize = ctypes.sizeof(MONITORINFO)
    if user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
        r = info.rcWork
        return (r.left, r.top, r.right, r.bottom)
    screen = user32.GetSystemMetrics
    return (0, 0, screen(0), screen(1))


def foreground_hwnd():
    try:
        return user32.GetForegroundWindow()
    except Exception:
        return 0


def single_instance():
    h = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        return None
    return h


# ---------------------------------------------------------------- 路径处理

def clean_path(raw):
    s = (raw or "").strip().strip("\r\n").strip()
    s = s.splitlines()[0].strip() if s else ""
    while len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        s = s[1:-1].strip()
    if s.lower().startswith("file:///"):
        s = urllib.parse.unquote(s[8:])
        if not re.match(r"^[A-Za-z]:", s):
            s = s.lstrip("/")
    s = os.path.expandvars(s)
    if not s:
        return ""
    try:
        s = os.path.normpath(s)
    except Exception:
        pass
    return s


def is_probably_path(s):
    if not s:
        return False
    if re.match(r"^[A-Za-z]:[\\/]", s):
        return True
    if s.lower().startswith("file:///"):
        return True
    if s.startswith("\\\\") or s.startswith("//"):
        return True
    return False


def entries_from_clip(files, txt, mode="dir"):
    """把剪贴板/选中内容（文件列表或文本）转成待收藏的路径列表。"""
    entries = []
    if files:
        for p in files:
            if os.path.isdir(p):
                entries.append(clean_path(p))
            elif os.path.isfile(p) and mode == "dir":
                d = os.path.dirname(p)
                entries.append(clean_path(d) if d else clean_path(p))
            else:
                entries.append(clean_path(p))
        return [e for e in entries if e]
    if txt:
        for line in txt.splitlines():
            line = line.strip()
            if not line:
                continue
            p = clean_path(line)
            if not p or not is_probably_path(p):
                continue
            entries.append(p)
    return [e for e in entries if e]


def clipboard_paths_as_entries(mode="dir"):
    return entries_from_clip(clipboard_files(), clipboard_text(), mode)


# ---------------------------------------------------------------- 分类与排序

_CJK_RE = re.compile(u"[\u3400-\u4dbf\u4e00-\u9fff]")
_sort_cache = {}


def is_image_file(path):
    return os.path.splitext(path or "")[1].lower() in IMAGE_EXTS


def entry_name(it):
    """条目用于显示/排序的名字。"""
    v = it.get("value") or ""
    t = it.get("type")
    if t == "text":
        first = v.strip().splitlines()[0].strip() if v.strip() else ""
        return first or "（空文本）"
    base = os.path.basename(v.rstrip("\\/"))
    if t == "image":
        return base or "图片"
    return base or v


def _pinyin_words(s):
    try:
        from pypinyin import lazy_pinyin
        return "".join(lazy_pinyin(s)).lower()
    except Exception:
        return s.lower()


def entry_initial(name):
    """首字母分组：数字/符号 → #，中文 → 拼音首字母。"""
    s = (name or "").strip()
    if not s:
        return "#"
    c = s[0]
    if c.isdigit():
        return "#"
    if ord(c) < 128:
        return c.upper() if c.isalpha() else "#"
    if _CJK_RE.match(c):
        try:
            from pypinyin import lazy_pinyin, Style
            ini = lazy_pinyin(c, style=Style.FIRST_LETTER)[0]
            if ini and ini.isalpha():
                return ini[0].upper()
        except Exception:
            pass
    return "#"


def entry_sort_key(it):
    name = entry_name(it)
    ini = entry_initial(name)
    group = (0 if ini == "#" else 1, ini)
    key = _sort_cache.get(name)
    if key is None:
        key = _pinyin_words(name) if _CJK_RE.search(name) else name.lower()
        if len(_sort_cache) > 4000:
            _sort_cache.clear()
        _sort_cache[name] = key
    return group + (key,)


_DRIVE_RE = re.compile(r"^([A-Za-z]):[\\/]")


def path_drive_label(value):
    """盘符分组标签：C: / D: / …；UNC 归“网络”，无盘符归 #。"""
    s = (value or "").strip()
    m = _DRIVE_RE.match(s)
    if m:
        return m.group(1).upper() + ":"
    if s.startswith("\\\\") or s.startswith("//"):
        return "网络"
    return "#"


def entry_group(it, by_drive=False):
    """返回 (排序层级, 分组标签)。by_drive 时路径按盘符分组。"""
    if by_drive and it.get("type") == "path":
        lbl = path_drive_label(it.get("value"))
        if lbl == "网络":
            return (2, "网络")
        if lbl == "#":
            return (3, "#")
        return (1, lbl)
    name = entry_name(it)
    ini = entry_initial(name)
    return (0 if ini == "#" else 1, ini)


def entry_sort_key_for(it, by_drive=False):
    group = entry_group(it, by_drive)
    if by_drive and it.get("type") == "path":
        return group + ((it.get("value") or "").lower(),)
    name = entry_name(it)
    key = _sort_cache.get(name)
    if key is None:
        key = _pinyin_words(name) if _CJK_RE.search(name) else name.lower()
        if len(_sort_cache) > 4000:
            _sort_cache.clear()
        _sort_cache[name] = key
    return group + (key,)


GROUP_COLORS = ["#4f8cff", "#3fc78f", "#f0b429", "#e8613c",
                "#a06cd5", "#49b7d0", "#e07a9a", "#7aa874"]


def mix_color(c1, c2, t):
    """把 c2 按比例 t (0-1) 混进 c1，用于给卡片底着色。"""
    a = [int(c1[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(c2[i:i + 2], 16) for i in (1, 3, 5)]
    return "#%02x%02x%02x" % tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def group_color(label):
    """颜色分组模式：每个盘符/字母分组一个固定颜色。"""
    s = (label or "").strip()
    if len(s) == 2 and s[0].isalpha() and s[1] == ":":
        return GROUP_COLORS[(ord(s[0].upper()) - 65) % len(GROUP_COLORS)]
    if s in ("网络", "#"):
        return "#6a6a6a"
    return GROUP_COLORS[(sum(ord(c) for c in s)) % len(GROUP_COLORS)]


def classify_items(files, txt, img, mode="dir"):
    """把取到的内容拆成条目并判定类型。

    - 图片文件 → image
    - 其他文件/文件夹 → path（mode=dir 时文件取所在目录）
    - 纯文本：每行都像路径 → path；否则 → text
    - 剪贴板位图 → 存盘后作为 image
    """
    out = []
    if files:
        for p in files:
            v = clean_path(p)
            if not v:
                continue
            if is_image_file(v):
                out.append({"type": "image", "value": v})
            elif os.path.isfile(v) and mode == "dir":
                d = os.path.dirname(v)
                out.append({"type": "path", "value": clean_path(d) if d else v})
            else:
                out.append({"type": "path", "value": v})
        return out
    if txt:
        t = txt.replace("\x00", "").strip()
        if not t:
            return out
        if len(t) > MAX_TEXT_CHARS:
            t = t[:MAX_TEXT_CHARS]
            log("text truncated to %d chars" % MAX_TEXT_CHARS)
        lines = [ln.strip() for ln in t.splitlines() if ln.strip()]
        if lines and all(is_probably_path(ln) for ln in lines):
            for ln in lines:
                p = clean_path(ln)
                if p:
                    out.append({"type": "path", "value": p})
        else:
            out.append({"type": "text", "value": t})
        return out
    if img is not None:
        p = save_image_file(img, IMAGE_DIR)
        if p:
            try:
                if os.path.getsize(p) > MAX_IMAGE_BYTES:
                    os.remove(p)
                    log("image too large, dropped: %s" % p)
                    return out
            except Exception:
                pass
            out.append({"type": "image", "value": p})
    return out


# ---------------------------------------------------------------- 数据

class Store(object):
    def __init__(self):
        self.data = dict(DEFAULTS)
        self.data["entries"] = []
        self.load()

    @staticmethod
    def _normalize(raw):
        out = []
        if not isinstance(raw, list):
            return out
        for e in raw:
            if isinstance(e, str):
                if e.strip():
                    out.append({"type": "path", "value": e.strip()})
            elif isinstance(e, dict):
                t, v = e.get("type"), e.get("value")
                if isinstance(v, str) and v.strip() and t in ("path", "text", "image"):
                    out.append({"type": t, "value": v})
        return out

    def load(self):
        try:
            src = DATA_FILE
            if not os.path.exists(src) and os.path.exists(OLD_DATA_FILE):
                src = OLD_DATA_FILE
                log("migrate from paths.json")
            if os.path.exists(src):
                with open(src, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    for k, v in DEFAULTS.items():
                        self.data[k] = loaded.get(k, v)
                    self.data["entries"] = self._normalize(loaded.get("entries"))
                    # 老版本只有 paths 列表，迁移成 path 条目
                    migrated = False
                    if not self.data["entries"] and isinstance(loaded.get("paths"), list):
                        self.data["entries"] = [{"type": "path", "value": p}
                                                for p in loaded["paths"]
                                                if isinstance(p, str) and p.strip()]
                        migrated = bool(self.data["entries"])
                    need_save = (src != DATA_FILE or migrated)
                    if self._reclassify():
                        need_save = True
                    if need_save:
                        self.save()
        except Exception as e:
            log("load config failed: %r" % (e,))

    def _reclassify(self):
        """把老数据里类型标错的条目按新规则纠正（只处理单行、不改内容）。"""
        changed = False
        for e in self.data["entries"]:
            v = e.get("value") or ""
            t = e.get("type")
            if t == "text":
                if not v.strip() or "\n" in v.strip():
                    continue
                if is_probably_path(v.strip()):
                    p = clean_path(v)
                    e["type"] = "image" if (is_image_file(p) and os.path.isfile(p)) else "path"
                    e["value"] = p
                    changed = True
            elif t == "path":
                if is_image_file(v) and os.path.isfile(v):
                    e["type"] = "image"
                    changed = True
        if changed:
            log("reclassified legacy entries")
        return changed

    def items(self):
        return self.data["entries"]

    def counts(self):
        c = {"path": 0, "text": 0, "image": 0}
        for e in self.data["entries"]:
            c[e["type"]] = c.get(e["type"], 0) + 1
        return c

    def save(self):
        try:
            ensure_dirs()
            tmp = DATA_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, DATA_FILE)
            return True
        except Exception as e:
            log("save config failed: %r" % (e,))
            return False

    @staticmethod
    def _hash_file(path):
        try:
            with open(path, "rb") as f:
                return hashlib.md5(f.read()).hexdigest()
        except Exception:
            return None

    @staticmethod
    def _key(etype, value):
        v = (value or "").strip()
        while len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1].strip()
        if etype == "image":
            return ("image", os.path.normcase(v))
        # 文本里存的就是路径时，和 path 条目算同一条
        if etype == "path" or (etype == "text" and is_probably_path(v)):
            return ("pathlike", os.path.normcase(v.rstrip("\\/")))
        return ("text", v)

    def add_entry(self, etype, value, top=True):
        if etype == "path":
            value = clean_path(value)
            if not value:
                return False, "路径为空"
        elif etype == "text":
            value = (value or "").strip()
            if not value:
                return False, "内容为空"
        elif etype == "image":
            if not value or not os.path.isfile(value):
                return False, "图片无效"
        else:
            return False, "类型不支持"
        entries = self.data["entries"]
        key = self._key(etype, value)
        image_hash = self._hash_file(value) if etype == "image" else None
        for i, e in enumerate(entries):
            dup = False
            if etype == "image" and e["type"] == "image":
                eh = e.get("hash") or self._hash_file(e["value"])
                if eh:
                    e["hash"] = eh
                if eh and image_hash and eh == image_hash:
                    dup = True
            elif self._key(e["type"], e["value"]) == key:
                dup = True
            if not dup:
                continue
            if top:
                entries.pop(i)
                entries.insert(0, e)
                self.save()
            if etype == "image":
                try:
                    if os.path.normcase(os.path.abspath(value)) != os.path.normcase(os.path.abspath(e["value"])):
                        os.remove(value)
                except Exception:
                    pass
            return False, "已在列表中"
        entry = {"type": etype, "value": value}
        if etype == "image" and image_hash:
            entry["hash"] = image_hash
        if top:
            entries.insert(0, entry)
        else:
            entries.append(entry)
        if etype == "image":
            self._prune_images()
        self.save()
        return True, value

    def _prune_images(self):
        """图片副本超过上限时，从最旧的开始删（只动存在 images 目录里的副本）。"""
        entries = self.data["entries"]
        local = [e for e in entries if e["type"] == "image"
                 and os.path.normcase(e["value"]).startswith(os.path.normcase(IMAGE_DIR))]
        while len(local) > MAX_IMAGES:
            old = local.pop()
            try:
                entries.remove(old)
            except ValueError:
                pass
            try:
                os.remove(old["value"])
            except Exception:
                pass
            log("prune image: %s" % old.get("value"))

    def add(self, path, top=True):
        return self.add_entry("path", path, top)

    def add_many(self, items):
        added = 0
        for it in reversed(items):
            if isinstance(it, dict):
                ok, _ = self.add_entry(it.get("type"), it.get("value"), top=True)
            else:
                ok, _ = self.add(it, top=True)
            if ok:
                added += 1
        return added

    def remove_at(self, idx):
        entries = self.data["entries"]
        if 0 <= idx < len(entries):
            del entries[idx]
            self.save()

    def move(self, idx, delta):
        entries = self.data["entries"]
        j = idx + delta
        if 0 <= idx < len(entries) and 0 <= j < len(entries):
            entries[idx], entries[j] = entries[j], entries[idx]
            self.save()
            return j
        return idx


# ---------------------------------------------------------------- 热键线程

class HotkeyThread(threading.Thread):
    def __init__(self, out_q):
        super(HotkeyThread, self).__init__(daemon=True)
        self.q = out_q
        self.tid = 0
        self.ready = threading.Event()

    def run(self):
        self.tid = kernel32.GetCurrentThreadId()
        ok_show = user32.RegisterHotKey(None, 1, MOD_WIN | MOD_ALT | MOD_NOREPEAT, 0x56)  # V
        ok_add = user32.RegisterHotKey(None, 2, MOD_WIN | MOD_ALT | MOD_NOREPEAT, 0x43)   # C
        log("hotkey register: show=%s add=%s" % (bool(ok_show), bool(ok_add)))
        if not (ok_show and ok_add):
            self.q.put(("hotkey_error", None))
        self.ready.set()
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
            if msg.message == WM_HOTKEY:
                hwnd = foreground_hwnd()
                if msg.wParam == 1:
                    self.q.put(("show", hwnd))
                elif msg.wParam == 2:
                    self.q.put(("add_clip", hwnd))
        try:
            user32.UnregisterHotKey(None, 1)
            user32.UnregisterHotKey(None, 2)
        except Exception:
            pass

    def stop(self):
        if self.tid:
            try:
                user32.PostThreadMessageW(self.tid, WM_QUIT, 0, 0)
            except Exception:
                pass


# ---------------------------------------------------------------- 键盘钩子

WH_KEYBOARD_LL = 13
WM_KEYDOWN = 0x0100
WM_SYSKEYDOWN = 0x0104
LLKHF_INJECTED = 0x10

VK_ESCAPE = 0x1B
VK_RETURN = 0x0D
VK_BACK = 0x08
VK_UP = 0x26
VK_DOWN = 0x28
VK_F2 = 0x71


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", wintypes.DWORD),
        ("scanCode", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


SHIFT_SYMBOLS = ")!@#$%^&*("
CHAR_MAP = {
    0x20: (" ", " "),
    0xC0: ("`", "~"),
    0xBD: ("-", "_"),
    0xBB: ("=", "+"),
    0xDB: ("[", "{"),
    0xDD: ("]", "}"),
    0xDC: ("\\", "|"),
    0xBA: (";", ":"),
    0xDE: ("'", '"'),
    0xBC: (",", "<"),
    0xBE: (".", ">"),
    0xBF: ("/", "?"),
}


def vk_to_char(vk, shift):
    if 0x30 <= vk <= 0x39:
        return SHIFT_SYMBOLS[vk - 0x30] if shift else chr(vk)
    if 0x41 <= vk <= 0x5A:
        return chr(vk) if shift else chr(vk).lower()
    pair = CHAR_MAP.get(vk)
    if pair:
        return pair[1] if shift else pair[0]
    return ""


def key_is_text(vk):
    if vk in (VK_ESCAPE, VK_RETURN, VK_BACK, VK_UP, VK_DOWN, VK_F2):
        return True
    return bool(vk_to_char(vk, False) or vk_to_char(vk, True))


def alt_or_win_down():
    return bool(
        (user32.GetAsyncKeyState(0x12) & 0x8000) or   # Alt
        (user32.GetAsyncKeyState(0x5B) & 0x8000) or   # LWin
        (user32.GetAsyncKeyState(0x5C) & 0x8000)      # RWin
    )


class KeyboardHook(threading.Thread):
    """面板可见时接管键盘，但绝不激活窗口，目标窗口焦点保持不变。"""

    def __init__(self, app):
        super(KeyboardHook, self).__init__(daemon=True)
        self.app = app
        self.tid = 0
        self.hook = None
        self.proc = None

    def run(self):
        self.tid = kernel32.GetCurrentThreadId()
        proc_type = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
        self.proc = proc_type(self._callback)
        self.hook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, self.proc, None, 0)
        log("keyboard hook=%s" % bool(self.hook))
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
            pass
        try:
            if self.hook:
                user32.UnhookWindowsHookEx(self.hook)
        except Exception:
            pass

    def _callback(self, n_code, w_param, l_param):
        try:
            if n_code == 0 and self.app.panel_visible and not getattr(self.app, "_search_open", False) and w_param in (WM_KEYDOWN, WM_SYSKEYDOWN):
                kb = ctypes.cast(l_param, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                if kb.flags & LLKHF_INJECTED:
                    return 0
                if alt_or_win_down():
                    return 0
                vk = int(kb.vkCode)
                if key_is_text(vk):
                    shift = bool(user32.GetAsyncKeyState(0x10) & 0x8000)
                    self.app.q.put(("key", (vk, shift)))
                    return 1
        except Exception as e:
            log("keyboard hook error: %r" % (e,))
        return 0

    def stop(self):
        if self.tid:
            try:
                user32.PostThreadMessageW(self.tid, WM_QUIT, 0, 0)
            except Exception:
                pass


# ---------------------------------------------------------------- 鼠标钩子

WH_MOUSE_LL = 14
WM_LBUTTONDOWN = 0x0201


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("pt", wintypes.POINT),
        ("mouseData", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


class OutsideClickWatch(threading.Thread):
    """面板可见时，点击面板（含搜索窗）以外的位置就收起面板，不拦截点击本身。"""

    def __init__(self, app):
        super(OutsideClickWatch, self).__init__(daemon=True)
        self.app = app
        self.tid = 0
        self.hook = None
        self.proc = None

    def run(self):
        self.tid = kernel32.GetCurrentThreadId()
        proc_type = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
        self.proc = proc_type(self._callback)
        self.hook = user32.SetWindowsHookExW(WH_MOUSE_LL, self.proc, None, 0)
        log("mouse hook=%s" % bool(self.hook))
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
            pass
        try:
            if self.hook:
                user32.UnhookWindowsHookEx(self.hook)
        except Exception:
            pass

    def _callback(self, n_code, w_param, l_param):
        try:
            if n_code == 0 and w_param == WM_LBUTTONDOWN and self.app.panel_visible:
                rects = self.app.hit_rects
                if rects:
                    ms = ctypes.cast(l_param, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
                    x, y = int(ms.pt.x), int(ms.pt.y)
                    for rx, ry, rw, rh in rects:
                        if rx <= x < rx + rw and ry <= y < ry + rh:
                            return 0
                    log("outside click at %d,%d -> hide" % (x, y))
                    self.app.q.put(("hide", None))
        except Exception as e:
            log("mouse hook error: %r" % (e,))
        return 0

    def stop(self):
        if self.tid:
            try:
                user32.PostThreadMessageW(self.tid, WM_QUIT, 0, 0)
            except Exception:
                pass


# ---------------------------------------------------------------- 图标

def make_tray_image():
    from PIL import Image, ImageDraw
    size = 128
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([10, 10, size - 10, size - 10], radius=28, fill=(28, 29, 32, 255), outline=(60, 62, 68, 255), width=3)
    # 文件夹
    d.polygon([(34, 48), (34, 40), (54, 40), (62, 48), (94, 48), (94, 82), (34, 82)], fill=(232, 66, 60, 255))
    d.rectangle([34, 52, 94, 82], fill=(232, 66, 60, 255))
    # 箭头
    d.polygon([(64, 58), (78, 72), (69, 72), (69, 90), (59, 90), (59, 72), (50, 72)], fill=(255, 255, 255, 255))
    return img


# ---------------------------------------------------------------- 主程序

class App(object):
    def __init__(self, store=None):
        import tkinter as tk
        from tkinter import ttk, messagebox, filedialog, simpledialog
        self.tk = tk
        self.ttk = ttk
        self.messagebox = messagebox
        self.filedialog = filedialog
        self.simpledialog = simpledialog
        self.store = store or Store()
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title(APP_NAME)
        self.q = queue.Queue()
        self.panel = None
        self.settings_win = None
        self.tray = None
        self.tray_thread = None
        self.hotkeys = None
        self.target_hwnd = 0
        self.target_focus = 0
        self.filtered = []
        self.search_text = ""
        self.panel_visible = False
        self.active_tab = self.store.data.get("active_tab") or "path"
        if self.active_tab not in TYPE_ORDER:
            self.active_tab = "path"
        self.keyboard = None
        self._clip_seq = 0
        self._drag = (0, 0)
        self.search_win = None
        self.search_entry = None
        self.search_var = None
        self._search_open = False
        self._focus_stolen = False
        self.hit_rects = []
        self.mousewatch = None

    # ---------- 托盘
    def start_tray(self):
        import pystray
        image = make_tray_image()

        def show_action(icon, item):
            self.q.put(("show", foreground_hwnd()))

        def add_action(icon, item):
            self.q.put(("add_clip", foreground_hwnd()))

        def settings_action(icon, item):
            self.q.put(("settings", None))

        def confirm_action(icon, item):
            self.q.put(("toggle_double_confirm", None))

        def autostart_action(icon, item):
            self.q.put(("toggle_autostart", None))

        def quit_action(icon, item):
            self.q.put(("quit", None))

        menu = pystray.Menu(
            pystray.MenuItem("显示面板   Win+Alt+V", show_action, default=True),
            pystray.MenuItem("从剪贴板添加   Win+Alt+C", add_action),
            pystray.MenuItem("双击自动回车确认", confirm_action,
                             checked=lambda item: self.store.data.get("double_confirm", True)),
            pystray.MenuItem("设置...", settings_action),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("开机自启", autostart_action, checked=lambda item: self._autostart_enabled()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出", quit_action),
        )
        self.tray = pystray.Icon("path_panel", image, APP_NAME, menu)
        self.tray_thread = threading.Thread(target=self.tray.run, daemon=True)
        self.tray_thread.start()
        log("tray started")

    @staticmethod
    def _autostart_key():
        return r"Software\Microsoft\Windows\CurrentVersion\Run"

    @staticmethod
    def _autostart_command():
        if getattr(sys, "frozen", False):
            return '"%s"' % sys.executable
        script = os.path.abspath(__file__)
        pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        if not os.path.exists(pythonw):
            pythonw = sys.executable
        return '"%s" "%s"' % (pythonw, script)

    @staticmethod
    def _autostart_enabled():
        try:
            import winreg
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, App._autostart_key(), 0, winreg.KEY_READ)
            try:
                value, _ = winreg.QueryValueEx(key, APP_NAME)
                return bool(value)
            finally:
                winreg.CloseKey(key)
        except Exception:
            return False

    def set_autostart(self, enabled):
        try:
            import winreg
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, self._autostart_key(), 0, winreg.KEY_SET_VALUE)
            try:
                if enabled:
                    winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, self._autostart_command())
                else:
                    try:
                        winreg.DeleteValue(key, APP_NAME)
                    except FileNotFoundError:
                        pass
            finally:
                winreg.CloseKey(key)
            self.store.data["autostart"] = bool(enabled)
            self.store.save()
            if self.tray:
                try:
                    self.tray.update_menu()
                except Exception:
                    pass
            log("autostart -> %s" % enabled)
        except Exception as e:
            log("autostart failed: %r" % (e,))

    # ---------- 面板
    def build_panel(self):
        tk = self.tk
        if self.panel and self.panel.winfo_exists():
            return self.panel

        p = tk.Toplevel(self.root)
        p.overrideredirect(True)
        p.attributes("-topmost", True)
        p.configure(bg=C_BORDER)
        p.withdraw()

        outer = tk.Frame(p, bg=C_BORDER)
        outer.pack(fill="both", expand=True)
        body = tk.Frame(outer, bg=C_BG)
        body.pack(fill="both", expand=True, padx=1, pady=1)

        # 顶部：搜索提示 + 关闭按钮（整行可拖动）
        topbar = tk.Frame(body, bg=C_BG)
        topbar.pack(fill="x", padx=(16, 8), pady=(10, 6))
        close_btn = tk.Label(topbar, text="×", bg=C_BG, fg=C_DIM,
                             font=("Microsoft YaHei UI", 12, "bold"), cursor="hand2")
        close_btn.pack(side="right", padx=(8, 0))
        close_btn.bind("<Button-1>", lambda e: self.hide_panel())
        close_btn.bind("<Enter>", lambda e: close_btn.configure(fg=C_FG))
        close_btn.bind("<Leave>", lambda e: close_btn.configure(fg=C_DIM))

        # 搜索提示（面板不抢焦点，所以用标签而不是输入框）
        self.search_label = tk.Label(topbar, text="输入关键字筛选…（F2 打中文）", bg=C_BG, fg=C_DIM,
                                     anchor="w", font=FONT_S)
        self.search_label.pack(side="left", fill="x", expand=True)

        for widget in (topbar, self.search_label):
            widget.bind("<Button-1>", self._drag_start)
            widget.bind("<B1-Motion>", self._drag_move)

        # 分类页签：路径 / 文本 / 图片（各自带条数）
        tabs = tk.Frame(body, bg=C_BG)
        tabs.pack(fill="x", padx=(14, 8), pady=(0, 2))
        self.tab_widgets = {}
        for key in TYPE_ORDER:
            holder = tk.Frame(tabs, bg=C_BG)
            holder.pack(side="left", padx=(0, 14))
            lbl = tk.Label(holder, text=TYPE_LABEL[key], bg=C_BG, fg=C_DIM,
                           font=FONT_S, cursor="hand2")
            lbl.pack()
            bar = tk.Frame(holder, bg=C_BG, height=2, width=36)
            bar.pack(fill="x", pady=(2, 0))
            lbl.bind("<Button-1>", lambda e, k=key: self.set_tab(k))
            self.tab_widgets[key] = (lbl, bar)

        # 卡片列表（无表头，路径自动折行）
        list_wrap = tk.Frame(body, bg=C_BG)
        list_wrap.pack(fill="both", expand=True, padx=10, pady=(0, 2))
        self.list_canvas = tk.Canvas(list_wrap, bg=C_BG, highlightthickness=0, bd=0, yscrollincrement=40)
        self.list_canvas.pack(side="left", fill="both", expand=True)
        # 自绘暗色滚动条（细胶囊滑块，无箭头）
        self.sb = tk.Canvas(list_wrap, width=16, bg=C_BG, highlightthickness=0, bd=0)
        self.sb.pack(side="right", fill="y", padx=(2, 0))
        self._sb_first, self._sb_last = 0.0, 1.0
        self._sb_grab = 0
        self._sb_dragging = False
        self._sb_hover = False
        self.sb.bind("<Configure>", lambda e: self._draw_scrollbar())
        self.sb.bind("<Button-1>", self._sb_press)
        self.sb.bind("<B1-Motion>", self._sb_drag)
        self.sb.bind("<ButtonRelease-1>", self._sb_release)
        self.sb.bind("<Motion>", self._sb_hover_move)
        self.sb.bind("<Leave>", lambda e: self._sb_set_hover(False))
        self.list_canvas.configure(yscrollcommand=self._on_list_scroll)
        self.list_frame = tk.Frame(self.list_canvas, bg=C_BG)
        self.list_window = self.list_canvas.create_window((0, 0), window=self.list_frame, anchor="nw")
        self.list_frame.bind("<Configure>", lambda e: self.list_canvas.configure(scrollregion=self.list_canvas.bbox("all")))
        self.list_canvas.bind("<Configure>", self._on_canvas_resize)
        self.list_canvas.bind("<MouseWheel>", self._on_wheel)
        self.cards = []
        self.selected_index = -1

        footer = tk.Frame(body, bg=C_BG)
        footer.pack(fill="x", padx=16, pady=(2, 8))
        self.count_label = tk.Label(footer, text="", bg=C_BG, fg=C_DIM, font=FONT_S)
        self.count_label.pack(side="left")

        self.settings_btn = tk.Label(footer, text="设置", bg=C_BG, fg=C_DIM,
                                     font=FONT_S, cursor="hand2")
        self.settings_btn.pack(side="right")
        self.settings_btn.bind("<Button-1>", lambda e: self.open_settings())
        self.settings_btn.bind("<Enter>", lambda e: self.settings_btn.configure(fg=C_FG))
        self.settings_btn.bind("<Leave>", lambda e: self.settings_btn.configure(fg=C_DIM))

        p.bind("<Escape>", lambda e: self.hide_panel())
        p.bind("<FocusOut>", self._on_panel_focus_out)

        self._style_panel()
        self.panel = p
        return p

    def _style_panel(self):
        tk = self.tk
        style = self.ttk.Style(self.panel)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure("Path.Treeview", background=C_BG2, fieldbackground=C_BG2,
                        foreground=C_FG, borderwidth=0, rowheight=30, font=FONT)
        style.map("Path.Treeview",
                  background=[("selected", C_SEL)],
                  foreground=[("selected", "#ffffff")])
        style.configure("Path.Treeview.Heading", background=C_BG2, foreground=C_DIM,
                        borderwidth=0, relief="flat", font=FONT_S)
        style.map("Path.Treeview.Heading", background=[("active", C_BG2)])

    def _drag_start(self, e):
        self._drag = (e.x_root - self.panel.winfo_x(), e.y_root - self.panel.winfo_y())

    def _drag_move(self, e):
        x = e.x_root - self._drag[0]
        y = e.y_root - self._drag[1]
        self.panel.geometry("+%d+%d" % (x, y))
        self._update_hit_rects()

    def _on_panel_focus_out(self, e):
        # 关闭设置窗口时也会触发，避免误关：只处理面板内部失焦
        if self.settings_win and self.settings_win.winfo_exists():
            return

    def show_panel(self, hwnd=None):
        tk = self.tk
        self.build_panel()
        if hwnd:
            self.target_hwnd = hwnd
        elif not self.panel.winfo_viewable():
            self.target_hwnd = foreground_hwnd()
        self.target_focus = focus_of_window(self.target_hwnd)
        log("panel target=%s focus=%s(%s)" % (self.target_hwnd, self.target_focus, window_class(self.target_focus)))

        self.search_text = ""
        self._focus_stolen = False
        self.close_search_box()
        self.refresh_list()

        w, h = 460, 520
        left, top, right, bottom = workarea_of_cursor()
        # 跟随鼠标弹出：默认出现在光标右下方，靠边时自动翻转
        pt = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        x = pt.x + 16
        y = pt.y + 16
        if x + w > right:
            x = pt.x - w - 16
        if y + h > bottom:
            y = pt.y - h - 16
        x = max(left, min(x, right - w))
        y = max(top, min(y, bottom - h))
        self.panel.geometry("%dx%d+%d+%d" % (w, h, x, y))
        self.panel.attributes("-topmost", True)
        # 关键：给真正的顶层 frame 加 NOACTIVATE，确保 deiconify 也不抢前台
        hwnds = []
        try:
            frame = int(self.panel.wm_frame(), 16)
            if frame:
                hwnds.append(frame)
        except Exception as e:
            log("wm_frame failed: %r" % (e,))
        try:
            hwnds.append(self.panel.winfo_id())
        except Exception:
            pass
        hwnds = list(dict.fromkeys(hwnds))
        for hw in hwnds:
            try:
                ex = user32.GetWindowLongW(hw, -20)
                user32.SetWindowLongW(hw, -20, ex | 0x08000000 | 0x00000080)
            except Exception as e:
                log("noactivate style failed hw=%s: %r" % (hw, e))
        self.panel.deiconify()
        for hw in hwnds:
            try:
                user32.ShowWindow(hw, 4)  # SW_SHOWNOACTIVATE
            except Exception:
                pass
        log("panel hwnds=%s fg_after_show=%s" % (hwnds, foreground_hwnd()))
        self.panel_visible = True
        self.hit_rects = [(x, y, w, h)]
        log("panel shown target=%s" % self.target_hwnd)

    def _update_hit_rects(self, extra=None):
        rects = []
        try:
            if self.panel and self.panel.winfo_exists() and self.panel.winfo_viewable():
                self.panel.update_idletasks()
                rects.append((self.panel.winfo_x(), self.panel.winfo_y(),
                              self.panel.winfo_width(), self.panel.winfo_height()))
        except Exception:
            pass
        if extra:
            rects.extend(extra)
        self.hit_rects = rects

    def hide_panel(self):
        self.panel_visible = False
        self.hit_rects = []
        self.close_search_box()
        if self.panel and self.panel.winfo_exists():
            self.panel.withdraw()

    # ---------- 搜索输入窗（唯一会短暂取得焦点的地方，用于输入法打中文）
    def _search_win_geom(self):
        try:
            self.panel.update_idletasks()
            px, py = self.panel.winfo_x(), self.panel.winfo_y()
            pw = self.panel.winfo_width()
        except Exception:
            px, py, pw = 0, 0, 440
        h = 76
        left, top, right, bottom = workarea_of_cursor()
        y = py - h - 6
        if y < top:
            y = min(bottom - h, py + 40)
        return pw, h, px, y

    def open_search_box(self):
        if getattr(self, "search_win", None) is not None and self.search_win.winfo_exists():
            self.search_win.lift()
            try:
                self.search_entry.focus_force()
            except Exception:
                pass
            return
        tk = self.tk
        w = tk.Toplevel(self.root)
        self.search_win = w
        self._search_open = True
        self._focus_stolen = True
        w.overrideredirect(True)
        w.attributes("-topmost", True)
        w.configure(bg=C_BORDER)

        frame = tk.Frame(w, bg=C_BG)
        frame.pack(fill="both", expand=True, padx=1, pady=1)
        row = tk.Frame(frame, bg=C_BG)
        row.pack(fill="x", padx=10, pady=(9, 3))
        tk.Label(row, text="搜索", bg=C_BG, fg=C_DIM, font=FONT_S).pack(side="left", padx=(0, 8))
        self.search_var = tk.StringVar(value=self.search_text)
        e = tk.Entry(row, textvariable=self.search_var, bg=C_BG2, fg=C_FG,
                     insertbackground=C_FG, relief="flat", font=FONT,
                     highlightthickness=1, highlightbackground=C_BG2, highlightcolor=C_ACCENT)
        e.pack(side="left", fill="x", expand=True, ipady=5)
        self.search_entry = e

        tip = tk.Label(frame, text="Enter 应用 · Esc 取消 · 可用输入法打中文", bg=C_BG, fg=C_DIM, font=FONT_S)
        tip.pack(fill="x", padx=10, pady=(0, 8))

        e.bind("<Return>", lambda ev: self.apply_search_box())
        e.bind("<Escape>", lambda ev: self.close_search_box())
        w.bind("<Escape>", lambda ev: self.close_search_box())

        pw, h, px, py = self._search_win_geom()
        w.geometry("%dx%d+%d+%d" % (pw, h, px, py))
        w.deiconify()
        w.lift()
        e.focus_force()
        e.icursor("end")

        def _refocus():
            try:
                if w.winfo_exists():
                    w.lift()
                    e.focus_force()
                    e.icursor("end")
            except Exception:
                pass

        w.after(60, _refocus)
        self._update_hit_rects([(px, py, pw, h)])
        log("search box opened at %dx%d+%d+%d" % (pw, h, px, py))

    def apply_search_box(self):
        try:
            text = self.search_var.get()
        except Exception:
            text = ""
        self.search_text = text
        self.close_search_box()
        self.refresh_list()
        self.selected_index = 0 if self.filtered else -1
        self._apply_selection()
        log("search applied: %r -> %d" % (text, len(self.filtered)))

    def close_search_box(self):
        self._search_open = False
        w = getattr(self, "search_win", None)
        self.search_win = None
        if w is not None:
            try:
                if w.winfo_exists():
                    w.destroy()
            except Exception:
                pass
        if self.panel_visible:
            self._update_hit_rects()

    def toggle_panel(self, hwnd=None):
        if self.panel and self.panel.winfo_viewable():
            self.hide_panel()
        else:
            self.show_panel(hwnd)

    def set_tab(self, key):
        if key not in TYPE_ORDER or key == self.active_tab:
            return
        self.active_tab = key
        self.store.data["active_tab"] = key
        self.store.save()
        self.refresh_list()

    def _update_tabs(self):
        counts = self.store.counts()
        for key in TYPE_ORDER:
            widgets = getattr(self, "tab_widgets", {}).get(key)
            if not widgets:
                continue
            lbl, bar = widgets
            active = (key == self.active_tab)
            lbl.configure(text="%s %d" % (TYPE_LABEL[key], counts.get(key, 0)),
                          fg=C_FG if active else C_DIM)
            bar.configure(bg=C_ACCENT if active else C_BG)

    def refresh_list(self):
        if not self.panel:
            return
        tk = self.tk
        kw = self.search_text.strip().lower()
        self.search_label.configure(
            text=self.search_text if self.search_text else "输入关键字筛选…（F2 打中文）",
            fg=C_FG if self.search_text else C_DIM)
        self._update_tabs()
        items = self.store.items()
        sort_on = bool(self.store.data.get("sort_by_letter", True))
        # 路径页按盘符分组；搜索（跨类型）与文本/图片页仍按首字母
        by_drive = (not kw) and self.active_tab == "path"
        color_mode = sort_on and self.store.data.get("group_style", "letter") == "color"

        if kw:
            cands = []
            for idx, it in enumerate(items):
                hay = it["value"].lower()
                if it["type"] == "image":
                    hay = ("图片 " + os.path.basename(it["value"])).lower() + " " + hay
                elif it["type"] == "text":
                    hay = it["value"].lower() + " 文本"
                if kw in hay:
                    cands.append((idx, it))
        else:
            cands = [(idx, it) for idx, it in enumerate(items) if it["type"] == self.active_tab]
        if sort_on:
            cands.sort(key=lambda pair: entry_sort_key_for(pair[1], by_drive))

        for child in self.list_frame.winfo_children():
            child.destroy()
        self.cards = []
        self._thumb_refs = []
        self.filtered = [idx for idx, _ in cands]
        self.selected_index = 0 if cands else -1

        last_letter = None
        self.card_styles = []
        for pos, (idx, it) in enumerate(cands):
            etype, value = it["type"], it["value"]
            name = entry_name(it)

            # 分组：默认显示盘符/首字母标题；颜色模式下不给文字，用整块底色区分
            gcolor = None
            new_group = False
            if sort_on:
                letter = entry_group(it, by_drive)[1]
                if letter != last_letter:
                    new_group = last_letter is not None
                    last_letter = letter
                    if not color_mode:
                        hdr = tk.Label(self.list_frame, text=letter, bg=C_BG, fg=C_DIM, anchor="w",
                                       font=("Microsoft YaHei UI", 9, "bold"), padx=4)
                        hdr.pack(fill="x", pady=((6 if new_group else 0), 2))
                if color_mode:
                    gcolor = group_color(letter)

            card_bg = mix_color(C_BG2, gcolor, 0.22) if gcolor else C_BG2
            card = tk.Frame(self.list_frame, bg=card_bg)
            card.pack(fill="x", pady=((9 if new_group else 0), 4))
            bar = tk.Frame(card, bg=(gcolor if gcolor else card_bg), width=(4 if gcolor else 3))
            bar.pack(side="left", fill="y")

            if etype == "image":
                try:
                    from PIL import Image, ImageTk
                    im = Image.open(value)
                    im.thumbnail((56, 56))
                    ph = ImageTk.PhotoImage(im)
                    self._thumb_refs.append(ph)
                    tk.Label(card, image=ph, bg=card_bg).pack(side="left", padx=(10, 8), pady=6)
                except Exception as e:
                    log("thumb failed: %r" % (e,))
                text = "图片 · %s%s" % (name, "" if os.path.isfile(value) else "（缺失）")
                show = tk.Label(card, text=text, bg=card_bg,
                                fg=C_FG, anchor="w", justify="left", font=FONT_S, wraplength=300)
            elif etype == "text":
                disp = value if len(value) <= 300 else value[:300] + "…"
                show = tk.Label(card, text=(disp if not kw else "文本 · " + disp),
                                bg=card_bg, fg=C_FG, anchor="w", justify="left",
                                font=FONT_S, wraplength=366, padx=10, pady=8)
            else:
                show = tk.Label(card, text=(value if not kw else "路径 · " + value),
                                bg=card_bg, fg=C_FG, anchor="w", justify="left",
                                font=FONT, wraplength=366, padx=10, pady=8)
            show.pack(side="left", fill="x", expand=True)

            for w in [card, bar] + list(card.winfo_children()):
                w.bind("<Button-1>", lambda e, p=pos: self.select_pos(p))
                w.bind("<Double-1>", lambda e: self.commit_selection(True))
            self.cards.append((card, bar, show))
            self.card_styles.append((card_bg, mix_color(C_SEL, gcolor, 0.22) if gcolor else C_SEL,
                                     gcolor if gcolor else card_bg))

        self._apply_selection()
        total = len(items)
        if kw:
            self.count_label.configure(text="匹配 %d / 共 %d 条" % (len(self.filtered), total))
        else:
            self.count_label.configure(text="共 %d 条" % len(self.filtered))
        try:
            self.panel.after_idle(self._draw_scrollbar)
        except Exception:
            pass

    def handle_hook_key(self, vk, shift):
        if vk == VK_F2:
            self.open_search_box()
            return
        if vk == VK_ESCAPE:
            self.hide_panel()
        elif vk == VK_RETURN:
            self.commit_selection()
        elif vk == VK_UP:
            self.move_selection(-1)
        elif vk == VK_DOWN:
            self.move_selection(1)
        elif vk == VK_BACK:
            if self.search_text:
                self.search_text = self.search_text[:-1]
                self.refresh_list()
        else:
            ch = vk_to_char(vk, shift)
            if ch:
                self.search_text += ch
                self.refresh_list()

    def _on_list_scroll(self, first, last):
        try:
            self._sb_first = float(first)
            self._sb_last = float(last)
        except Exception:
            return
        self._draw_scrollbar()

    _SB_ARROW = 17

    def _draw_scrollbar(self):
        sb = getattr(self, "sb", None)
        if not sb or not sb.winfo_exists():
            return
        sb.delete("all")
        h = sb.winfo_height()
        w = max(6, sb.winfo_width())
        if h <= 4:
            return
        aw = self._SB_ARROW
        first, last = self._sb_first, self._sb_last
        active = (last - first) < 0.995
        # 轨道
        sb.create_rectangle(0, 0, w, h, fill=C_BG2, outline="")
        # 上下箭头：细线条折角（Windows 10 深色滚动条观感）
        ac = C_DIM if active else "#5a5a5a"
        cx = w / 2.0
        lc = max(4.0, aw * 0.28)
        ty = aw / 2.0
        sb.create_line(cx - lc, ty + lc * 0.5, cx, ty - lc * 0.5, fill=ac, width=1)
        sb.create_line(cx, ty - lc * 0.5, cx + lc, ty + lc * 0.5, fill=ac, width=1)
        by = h - aw / 2.0
        sb.create_line(cx - lc, by - lc * 0.5, cx, by + lc * 0.5, fill=ac, width=1)
        sb.create_line(cx, by + lc * 0.5, cx + lc, by - lc * 0.5, fill=ac, width=1)
        if h < aw * 2 + 10 or not active:
            return
        y, thumb_h = self._sb_thumb_geom()
        x0, x1 = 1, w - 1
        fill = "#5e5e5e" if self._sb_hover else C_THUMB
        # 直角矩形滑块 + 1px 亮边，贴近系统滚动条
        sb.create_rectangle(x0, y, x1, y + thumb_h, fill=fill, outline="#6e6e6e")

    def _sb_hover_move(self, e):
        y, thumb_h = self._sb_thumb_geom()
        self._sb_set_hover(y <= e.y <= y + thumb_h)

    def _sb_set_hover(self, on):
        if on != self._sb_hover:
            self._sb_hover = on
            self._draw_scrollbar()

    def _sb_thumb_geom(self):
        h = max(1, self.sb.winfo_height())
        aw = self._SB_ARROW
        area = max(1, h - 2 * aw)
        thumb_h = max(24, int((self._sb_last - self._sb_first) * area))
        y = aw + int(self._sb_first * area)
        if y + thumb_h > h - aw:
            y = h - aw - thumb_h
        return y, thumb_h

    def _sb_press(self, e):
        h = max(1, self.sb.winfo_height())
        aw = self._SB_ARROW
        if e.y < aw:
            self.list_canvas.yview_scroll(-1, "pages")
            self._sb_dragging = False
            return
        if e.y > h - aw:
            self.list_canvas.yview_scroll(1, "pages")
            self._sb_dragging = False
            return
        y, thumb_h = self._sb_thumb_geom()
        if y <= e.y <= y + thumb_h:
            self._sb_grab = e.y - y
        else:
            self._sb_grab = thumb_h // 2
            self._sb_jump(e.y - self._sb_grab)
        self._sb_dragging = True

    def _sb_drag(self, e):
        if self._sb_dragging:
            self._sb_jump(e.y - self._sb_grab)

    def _sb_release(self, e):
        self._sb_dragging = False

    def _sb_jump(self, thumb_top):
        h = max(1, self.sb.winfo_height())
        aw = self._SB_ARROW
        area = max(1, h - 2 * aw)
        span = max(1e-6, self._sb_last - self._sb_first)
        frac = max(0.0, min(1.0 - span, float(thumb_top - aw) / area))
        self.list_canvas.yview_moveto(frac)

    def _on_canvas_resize(self, e):
        try:
            self.list_canvas.itemconfigure(self.list_window, width=e.width)
        except Exception:
            pass

    def _on_wheel(self, e):
        try:
            self.list_canvas.yview_scroll(int(-e.delta / 120) * 2, "units")
        except Exception:
            pass

    def select_pos(self, pos):
        if 0 <= pos < len(self.filtered):
            self.selected_index = pos
            self._apply_selection()

    def _apply_selection(self):
        for i, (card, bar, label) in enumerate(self.cards):
            style = self.card_styles[i] if i < len(self.card_styles) else (C_BG2, C_SEL, C_BG2)
            bg, sel_bg, bar_bg = style
            if i == self.selected_index:
                bg, bar_bg = sel_bg, C_ACCENT
            card.configure(bg=bg)
            bar.configure(bg=bar_bg)
            for w in card.winfo_children():
                if w is bar:
                    continue
                try:
                    w.configure(bg=bg)
                except Exception:
                    pass
        self._scroll_to_selected()

    def _scroll_to_selected(self):
        if self.selected_index < 0 or not self.cards:
            return
        try:
            self.panel.update_idletasks()
            card = self.cards[self.selected_index][0]
            total = max(1, self.list_frame.winfo_height())
            top = self.list_canvas.canvasy(0)
            view_h = self.list_canvas.winfo_height()
            y = card.winfo_y()
            h = card.winfo_height()
            if y < top:
                self.list_canvas.yview_moveto(float(y) / total)
            elif y + h > top + view_h:
                self.list_canvas.yview_moveto(float(y + h - view_h) / total)
        except Exception:
            pass

    def move_selection(self, delta):
        if not self.filtered:
            return "break"
        if self.selected_index < 0:
            self.selected_index = 0
        else:
            self.selected_index = max(0, min(len(self.filtered) - 1, self.selected_index + delta))
        self._apply_selection()
        return "break"

    def _current_entry(self):
        items = self.store.items()
        if 0 <= self.selected_index < len(self.filtered):
            idx = self.filtered[self.selected_index]
            if 0 <= idx < len(items):
                return items[idx]
        return None

    def should_confirm(self, hwnd):
        """双击是否追加回车：总开关开着就确认；关掉后只有白名单里的应用才确认。"""
        if self.store.data.get("double_confirm", True):
            return True
        wl = [x.lower() for x in (self.store.data.get("confirm_whitelist") or []) if x]
        if not wl:
            return False
        name = process_name_of_hwnd(hwnd)
        return bool(name and name in wl)

    def commit_selection(self, confirm=False):
        it = self._current_entry()
        if not it:
            return "break"
        self.paste_entry(it, confirm)
        return "break"

    def paste_entry(self, it, confirm=False):
        etype, value = it["type"], it["value"]
        if etype == "image":
            snap = clipboard_snapshot() if self.store.data.get("restore_clip", True) else None
            if not set_clipboard_image(value):
                self.notify("这张图放不进剪贴板：%s" % os.path.basename(value))
                return
            confirm = False  # 图片不需要回车确认
        elif etype == "text":
            snap = clipboard_snapshot() if self.store.data.get("restore_clip", True) else None
            if not set_clipboard_text(value):
                log("set clipboard failed")
                return
        else:
            if self.store.data.get("quote_path") and not value.startswith('"'):
                text = '"%s"' % value
            else:
                text = value
            snap = clipboard_snapshot() if self.store.data.get("restore_clip", True) else None
            if not set_clipboard_text(text):
                log("set clipboard failed")
                return
        # 我们自己写剪贴板，不要触发自动收录
        self._clip_seq = user32.GetClipboardSequenceNumber()
        hwnd = self.target_hwnd or foreground_hwnd()
        log("paste start target=%s fg=%s type=%s" % (hwnd, foreground_hwnd(), etype))
        # 先撤掉置顶，确保窗口真正消失后不压住目标控件
        try:
            self.panel.attributes("-topmost", False)
        except Exception:
            pass
        self.hide_panel()
        try:
            self.root.update_idletasks()
        except Exception:
            pass
        threading.Thread(target=self._paste_worker, args=(hwnd, self.target_focus, snap, confirm), daemon=True).start()

    def _paste_worker(self, hwnd, focus_hwnd, snap, confirm=False):
        try:
            # 面板从未激活，目标窗口应仍是前台；只有被切走时才兜底拉回
            time.sleep(0.12)
            fg = foreground_hwnd()
            if fg != hwnd:
                log("refocus target, fg=%s target=%s" % (fg, hwnd))
                if self._focus_stolen:
                    refocus_target_editing(hwnd, focus_hwnd)
                else:
                    activate_window(hwnd)
                time.sleep(0.10)
            elif self._focus_stolen:
                log("repair editing state after search")
                refocus_target_editing(hwnd, focus_hwnd)
                time.sleep(0.10)
            fg = foreground_hwnd()
            log("paste fg=%s target=%s focus=%s(%s) mode=%s" % (
                fg, hwnd, focus_hwnd, window_class(focus_hwnd), self.store.data.get("paste_mode")))
            send_paste(self.store.data.get("paste_mode", "ctrl_v"))
            if confirm and self.should_confirm(hwnd):
                time.sleep(0.18)
                _key(0x0D)
                _key(0x0D, True)
                log("confirm enter sent")
            if snap:
                # 等目标程序把剪贴板读完再还原，避免粘贴内容被截断
                time.sleep(1.0)
                restore_clipboard(snap)
                self._clip_seq = user32.GetClipboardSequenceNumber()
                log("clipboard restored (%s)" % snap[0])
            self._focus_stolen = False
        except Exception as e:
            log("paste worker failed: %r" % (e,))

    # ---------- 剪贴板添加
    def _wait_modifiers_up(self, timeout=0.7):
        """等用户松开 Win/Alt/Ctrl/Shift，避免 Ctrl+C 被修饰成别的组合。"""
        vks = (0x5B, 0x5C, 0x12, 0x11, 0x10)
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not any(user32.GetAsyncKeyState(v) & 0x8000 for v in vks):
                return True
            time.sleep(0.03)
        return False

    def _grab_selection(self):
        """向当前前台窗口发一次 Ctrl+C 取它眼下选中的内容，然后恢复原剪贴板。"""
        if not self._wait_modifiers_up():
            log("modifiers still down before copy")
        before_files = clipboard_files()
        before_txt = clipboard_text()
        snap = clipboard_snapshot()
        _key(0x11); _key(0x43); _key(0x43, True); _key(0x11, True)
        time.sleep(0.35)
        files = clipboard_files()
        txt = clipboard_text()
        img = clipboard_image()
        if not (files or txt):
            # 有些程序复制慢一拍，再给一次机会
            time.sleep(0.25)
            files = clipboard_files()
            txt = clipboard_text()
            img = clipboard_image()
        reused = (files == before_files and txt == before_txt)
        if snap:
            try:
                restore_clipboard(snap)
                self._clip_seq = user32.GetClipboardSequenceNumber()
            except Exception as e:
                log("restore after selection copy failed: %r" % (e,))
        return files, txt, img, reused

    def _items_from_content(self, files, txt, img, mode="dir"):
        """收录并自动分类：图片文件/位图 → 图片，路径 → 路径，其余文本 → 文本。"""
        return classify_items(files, txt, img, mode)

    def add_from_clipboard(self, silent=False):
        mode = self.store.data.get("copy_file_mode", "dir")
        # 优先取“当前选中”——在地址栏/文本框里选中并不等于已复制到剪贴板
        sel_files, sel_txt, sel_img, reused = self._grab_selection()
        items = self._items_from_content(sel_files, sel_txt, sel_img, mode)
        source = "selection"
        if not items:
            items = self._items_from_content(
                clipboard_files(), clipboard_text(), clipboard_image(), mode)
            source = "clipboard"
        log("add source=%s reused=%s files=%d img=%s text=%r items=%r" % (
            source, reused, len(sel_files), bool(sel_img), (sel_txt or "")[:60], items[:2]))
        if not items:
            self.notify("没取到内容：先在地址栏/文本框里选中，或复制文字、图片、文件")
            return 0
        added = self.store.add_many(items)
        first = items[0]
        label = first["value"]
        if first["type"] == "image":
            label = "图片 " + os.path.basename(first["value"])
        elif first["type"] == "text":
            label = first["value"].replace("\n", " ")
        if len(items) > 1:
            label = "%s 等 %d 条" % (label, len(items))
        if len(label) > 70:
            label = label[:67] + "..."
        tag = "已添加" if source == "selection" else "已添加（剪贴板）"
        if reused and source == "selection":
            tag = "已添加（没取到新选中，用的剪贴板）"
        if added:
            self.notify("%s：%s" % (tag, label))
        elif not silent:
            self.notify("已在列表中：%s" % label)
        if added:
            # 切到刚收录内容所在的分类页，并高亮定位
            self.active_tab = first["type"]
            self.store.data["active_tab"] = self.active_tab
        if self.settings_win and self.settings_win.winfo_exists():
            self.refresh_settings()
        if self.panel and self.panel.winfo_viewable():
            self.refresh_list()
            if added:
                self._select_entry(first)
        return added

    def _select_entry(self, it):
        for pos, idx in enumerate(self.filtered):
            e = self.store.items()[idx]
            if e["type"] == it["type"] and e["value"] == it["value"]:
                self.selected_index = pos
                self._apply_selection()
                return True
        return False

    def notify(self, msg):
        log("notify: %s" % msg)
        if self.tray:
            try:
                self.tray.notify(msg, APP_NAME)
            except Exception:
                pass

    # ---------- 设置窗口
    def open_settings(self):
        if self.settings_win and self.settings_win.winfo_exists():
            self.settings_win.deiconify()
            self.settings_win.lift()
            self.settings_win.focus_force()
            return
        tk = self.tk
        w = tk.Toplevel(self.root)
        self.settings_win = w
        w.title(APP_NAME + " 设置")
        w.configure(bg=C_BG)
        w.geometry("660x700")
        try:
            w.attributes("-topmost", True)
        except Exception:
            pass

        style = self.ttk.Style(w)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure("Set.Treeview", background=C_BG2, fieldbackground=C_BG2,
                        foreground=C_FG, borderwidth=0, rowheight=28, font=FONT)
        style.map("Set.Treeview", background=[("selected", C_SEL)], foreground=[("selected", "#ffffff")])
        style.configure("Set.Treeview.Heading", background=C_BG2, foreground=C_DIM, borderwidth=0, font=FONT_S)
        self._set_style = style
        # 去掉表头行（分类 / 序 / 内容 / 状态），只保留数据行
        try:
            style.layout("Set.Treeview", [("Set.Treeview.treearea", {"sticky": "nswe"})])
        except Exception:
            pass

        tk.Label(w, text="用标签切换分类；组内路径按盘符、文本/图片按首字母分组。",
                 bg=C_BG, fg=C_DIM, font=FONT_S, anchor="w").pack(fill="x", padx=14, pady=(10, 2))

        # 分类标签（路径 / 文本 / 图片）
        tabbar = tk.Frame(w, bg=C_BG)
        tabbar.pack(fill="x", padx=14, pady=(2, 6))
        self.set_active_tab = getattr(self, "set_active_tab", None) or "path"
        self.set_tab_widgets = {}
        for key in TYPE_ORDER:
            holder = tk.Frame(tabbar, bg=C_BG)
            holder.pack(side="left", padx=(0, 14))
            lbl = tk.Label(holder, text=TYPE_LABEL[key], bg=C_BG, fg=C_DIM, font=FONT_S, cursor="hand2")
            lbl.pack()
            ubar = tk.Frame(holder, bg=C_BG, height=2, width=36)
            ubar.pack(fill="x", pady=(2, 0))
            lbl.bind("<Button-1>", lambda e, k=key: self.set_settings_tab(k))
            self.set_tab_widgets[key] = (lbl, ubar)
        self._update_settings_tabs()

        wrap = tk.Frame(w, bg=C_BG)
        wrap.pack(fill="both", expand=True, padx=14)
        self.set_tree = self.ttk.Treeview(wrap, show="tree",
                                        style="Set.Treeview", selectmode="browse")
        self.set_tree.column("#0", width=560, anchor="w", stretch=True)
        self.set_tree.tag_configure("sub", foreground=C_DIM)
        vsb = self.ttk.Scrollbar(wrap, orient="vertical", command=self.set_tree.yview)
        self.set_tree.configure(yscrollcommand=vsb.set)
        self.set_tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")

        btns = tk.Frame(w, bg=C_BG)
        btns.pack(fill="x", padx=14, pady=8)

        def mkbtn(text, cmd):
            b = tk.Button(btns, text=text, command=cmd, bg=C_BG2, fg=C_FG,
                          activebackground=C_SEL, activeforeground="#ffffff",
                          relief="flat", bd=0, padx=12, pady=6, font=FONT, cursor="hand2")
            b.pack(side="left", padx=(0, 6))
            return b

        self.move_btns = [mkbtn("上移", lambda: self._settings_move(-1)),
                          mkbtn("下移", lambda: self._settings_move(1))]
        mkbtn("删除", self._settings_delete)
        mkbtn("添加文件夹", self._settings_add_dir)
        mkbtn("手动添加", self._settings_add_manual)
        mkbtn("从剪贴板添加", lambda: self.add_from_clipboard())

        opts = tk.Frame(w, bg=C_BG)
        opts.pack(fill="x", padx=14, pady=(6, 0))
        tk.Label(opts, text="选项", bg=C_BG, fg=C_FG, font=("Microsoft YaHei UI", 10, "bold"),
                 anchor="w").pack(fill="x", pady=(0, 6))

        self.var_sort = tk.BooleanVar(value=bool(self.store.data.get("sort_by_letter", True)))
        cbs = tk.Checkbutton(opts, text="自动排序（路径按盘符分组，文本/图片按首字母；中文取拼音）",
                             variable=self.var_sort, command=self._save_options,
                             bg=C_BG, fg=C_FG, selectcolor=C_BG2, activebackground=C_BG,
                             activeforeground=C_FG, font=FONT, anchor="w", bd=0,
                             highlightthickness=0)
        cbs.pack(fill="x", anchor="w")

        row_g = tk.Frame(opts, bg=C_BG)
        row_g.pack(fill="x", padx=24, pady=(3, 2))
        tk.Label(row_g, text="分组显示：", bg=C_BG, fg=C_DIM, font=FONT_S).pack(side="left", padx=(0, 8))
        self.var_group = tk.StringVar(value=self.store.data.get("group_style", "letter"))
        for val, txt in (("letter", "盘符/字母标题"), ("color", "彩色整块")):
            tk.Radiobutton(row_g, text=txt, variable=self.var_group, value=val,
                           command=self._save_options, bg=C_BG, fg=C_FG, selectcolor=C_BG2,
                           activebackground=C_BG, activeforeground=C_FG, font=FONT_S,
                           bd=0, highlightthickness=0).pack(side="left", padx=(0, 12))

        self.var_file_mode = tk.BooleanVar(value=True)
        cb1 = tk.Checkbutton(opts, text="记录所选内容本身（路径 / 文本 / 图片都原样收藏）",
                             variable=self.var_file_mode, command=self._save_options,
                             state="disabled", disabledforeground=C_FG,
                             bg=C_BG, fg=C_FG, selectcolor=C_BG2, activebackground=C_BG,
                             activeforeground=C_FG, font=FONT, anchor="w", bd=0,
                             highlightthickness=0)
        cb1.pack(fill="x", anchor="w")

        self.var_restore = tk.BooleanVar(value=bool(self.store.data.get("restore_clip", True)))
        cb2 = tk.Checkbutton(opts, text="粘贴后恢复原剪贴板（文本/文件列表）",
                             variable=self.var_restore, command=self._save_options,
                             bg=C_BG, fg=C_FG, selectcolor=C_BG2, activebackground=C_BG,
                             activeforeground=C_FG, font=FONT, anchor="w", bd=0,
                             highlightthickness=0)
        cb2.pack(fill="x", anchor="w")

        self.var_quote = tk.BooleanVar(value=bool(self.store.data.get("quote_path", False)))
        cb3 = tk.Checkbutton(opts, text="粘贴时给路径加引号（适合带空格的路径）",
                             variable=self.var_quote, command=self._save_options,
                             bg=C_BG, fg=C_FG, selectcolor=C_BG2, activebackground=C_BG,
                             activeforeground=C_FG, font=FONT, anchor="w", bd=0,
                             highlightthickness=0)
        cb3.pack(fill="x", anchor="w")

        self.var_double = tk.BooleanVar(value=bool(self.store.data.get("double_confirm", True)))
        cb4 = tk.Checkbutton(opts, text="双击条目时自动回车确认（地址栏跳转 / 表单提交）",
                             variable=self.var_double, command=self._save_options,
                             bg=C_BG, fg=C_FG, selectcolor=C_BG2, activebackground=C_BG,
                             activeforeground=C_FG, font=FONT, anchor="w", bd=0,
                             highlightthickness=0)
        cb4.pack(fill="x", anchor="w")

        wlbox = tk.Frame(opts, bg=C_BG)
        wlbox.pack(fill="x", padx=24, pady=(2, 0))
        self.var_whitelist = tk.StringVar(
            value=", ".join(self.store.data.get("confirm_whitelist") or []))
        tk.Label(wlbox, text="确认白名单", bg=C_BG, fg=C_DIM, font=FONT_S).pack(side="left", padx=(0, 6))
        ent = tk.Entry(wlbox, textvariable=self.var_whitelist, bg=C_BG2, fg=C_FG,
                       insertbackground=C_FG, relief="flat", font=FONT_S, highlightthickness=0)
        ent.pack(side="left", fill="x", expand=True, ipady=3, padx=(0, 6))
        ent.bind("<FocusOut>", lambda ev: self._save_options())
        ent.bind("<Return>", lambda ev: self._save_options())
        tk.Button(wlbox, text="加当前程序（5 秒）", command=self._add_fg_to_whitelist,
                  bg=C_BG2, fg=C_FG, activebackground=C_SEL, activeforeground="#ffffff",
                  relief="flat", bd=0, padx=8, pady=2, font=FONT_S, cursor="hand2").pack(side="left")
        tk.Label(opts, text="填 exe 名，逗号分隔（explorer.exe, Code.exe）。总开关关掉后只有这里的应用会回车。",
                 bg=C_BG, fg=C_DIM, font=FONT_S, anchor="w", justify="left").pack(fill="x")

        self.var_auto = tk.BooleanVar(value=bool(self.store.data.get("auto_collect", True)))
        cb5 = tk.Checkbutton(opts, text="自动收录复制到剪贴板的路径（不必再按 Win+Alt+C）",
                             variable=self.var_auto, command=self._save_options,
                             bg=C_BG, fg=C_FG, selectcolor=C_BG2, activebackground=C_BG,
                             activeforeground=C_FG, font=FONT, anchor="w", bd=0,
                             highlightthickness=0)
        cb5.pack(fill="x", anchor="w")

        row = tk.Frame(opts, bg=C_BG)
        row.pack(fill="x", pady=(6, 4))
        tk.Label(row, text="粘贴方式：", bg=C_BG, fg=C_FG, font=FONT).pack(side="left")
        self.var_paste = tk.StringVar(value=self.store.data.get("paste_mode", "ctrl_v"))
        om = tk.OptionMenu(row, self.var_paste, "ctrl_v", "ctrl_shift_v", "shift_insert",
                           command=lambda _v: self._save_options())
        om.configure(bg=C_BG2, fg=C_FG, activebackground=C_SEL, activeforeground="#ffffff",
                     relief="flat", bd=0, font=FONT, highlightthickness=0)
        om["menu"].configure(bg=C_BG2, fg=C_FG, activebackground=C_SEL, activeforeground="#ffffff", font=FONT)
        om.pack(side="left")
        tk.Label(row, text="（终端类窗口用 ctrl_shift_v 或 shift_insert）", bg=C_BG, fg=C_DIM, font=FONT_S).pack(side="left", padx=6)

        tk.Label(opts, text="热键：Win+Alt+V 呼出面板    Win+Alt+C 收录当前选中/剪贴板内容（路径·文本·图片）",
                 bg=C_BG, fg=C_DIM, font=FONT_S, anchor="w").pack(fill="x", pady=(6, 0))

        bottom = tk.Frame(w, bg=C_BG)
        bottom.pack(fill="x", padx=14, pady=10)
        tk.Button(bottom, text="关闭", command=self.close_settings, bg=C_ACCENT, fg="#ffffff",
                  activebackground="#c63a34", activeforeground="#ffffff", relief="flat", bd=0,
                  padx=20, pady=6, font=FONT, cursor="hand2").pack(side="right")

        self.refresh_settings()
        self._sync_move_buttons()
        w.protocol("WM_DELETE_WINDOW", self.close_settings)
        w.focus_force()

    def _sync_move_buttons(self):
        """首字母排序开启时，手动上移/下移没有意义，置灰。"""
        on = bool(self.store.data.get("sort_by_letter", True))
        for b in getattr(self, "move_btns", []):
            try:
                b.configure(state="disabled" if on else "normal",
                             fg=C_DIM if on else C_FG,
                             cursor="arrow" if on else "hand2")
            except Exception:
                pass

    def close_settings(self):
        if self.settings_win and self.settings_win.winfo_exists():
            self.settings_win.destroy()
        self.settings_win = None

    def set_settings_tab(self, key):
        if key not in TYPE_ORDER:
            return
        self.set_active_tab = key
        self.refresh_settings()

    def _update_settings_tabs(self):
        if not getattr(self, "set_tab_widgets", None):
            return
        counts = self.store.counts()
        for key, (lbl, ubar) in self.set_tab_widgets.items():
            active = key == self.set_active_tab
            lbl.configure(text="%s %d" % (TYPE_LABEL[key], counts.get(key, 0)),
                          fg=(C_FG if active else C_DIM))
            ubar.configure(bg=(C_ACCENT if active else C_BG))

    def refresh_settings(self):
        if not self.settings_win or not self.settings_win.winfo_exists():
            return
        tree = self.set_tree
        sel = tree.selection()
        keep = None
        if sel:
            try:
                keep = int(sel[0])
            except Exception:
                keep = None
        tree.delete(*tree.get_children())
        self._set_thumbs = []
        # 行高与分类列宽度固定，切换标签时窗口尺寸不再跳动
        try:
            self._set_style.configure("Set.Treeview", rowheight=40)
        except Exception:
            pass
        items = self.store.items()
        sort_on = bool(self.store.data.get("sort_by_letter", True))

        def item_row(idx, it, parent):
            etype, value = it["type"], it["value"]
            thumb = None
            if etype == "path":
                label = value if os.path.exists(value) else value + "（缺失）"
            elif etype == "text":
                label = value.replace("\n", " ")
                if len(label) > 120:
                    label = label[:120] + "…"
            else:
                label = os.path.basename(value)
                if os.path.isfile(value):
                    try:
                        from PIL import Image, ImageTk
                        im = Image.open(value)
                        im.thumbnail((34, 34))
                        thumb = ImageTk.PhotoImage(im)
                        self._set_thumbs.append(thumb)
                    except Exception as e:
                        log("settings thumb failed: %r" % (e,))
                else:
                    label = "（缺失）" + label
            tree.insert(parent, "end", iid=str(idx),
                        text=label[:200],
                        image=(thumb if thumb else ""))

        etype = self.set_active_tab
        rows = [(i, it) for i, it in enumerate(items) if it["type"] == etype]
        if rows:
            if sort_on:
                by_drive = etype == "path"
                rows.sort(key=lambda pair: entry_sort_key_for(pair[1], by_drive))
                last = None
                parent = ""
                for idx, it in rows:
                    lbl = entry_group(it, by_drive)[1]
                    if lbl != last:
                        sid = "gs_%s_%s" % (etype, lbl)
                        tree.insert("", "end", iid=sid, text=lbl, open=True, tags=("sub",))
                        parent = sid
                        last = lbl
                    item_row(idx, it, parent)
            else:
                for idx, it in rows:
                    item_row(idx, it, "")
        self._update_settings_tabs()
        if keep is not None and tree.exists(str(keep)):
            tree.selection_set(str(keep))
            tree.see(str(keep))
        elif keep is not None:
            # 条目被删/移动后，尽量保持父分组展开
            pass

    def _settings_selected(self):
        sel = self.set_tree.selection()
        if not sel:
            return None
        try:
            return int(sel[0])
        except Exception:
            return None

    def _settings_move(self, delta):
        if self.store.data.get("sort_by_letter", True):
            self.notify("首字母排序开启中，先关闭排序才能手动调整顺序")
            return
        idx = self._settings_selected()
        if idx is None:
            return
        new = self.store.move(idx, delta)
        self.refresh_settings()
        if self.set_tree.exists(str(new)):
            self.set_tree.selection_set(str(new))
            self.set_tree.see(str(new))

    def _settings_delete(self):
        idx = self._settings_selected()
        if idx is None:
            return
        it = self.store.items()[idx]
        label = it["value"] if it["type"] != "image" else "图片 · " + os.path.basename(it["value"])
        if not self.messagebox.askyesno("删除条目", "从收藏中移除？\n\n" + label[:200], parent=self.settings_win):
            return
        self.store.remove_at(idx)
        self.refresh_settings()
        if self.panel and self.panel.winfo_viewable():
            self.refresh_list()

    def _settings_add_dir(self):
        d = self.filedialog.askdirectory(parent=self.settings_win, title="选择要收藏的文件夹")
        if d:
            ok, msg = self.store.add(d)
            self.refresh_settings()
            if self.panel and self.panel.winfo_viewable():
                self.refresh_list()
            if not ok:
                self.notify(msg)

    def _settings_add_manual(self):
        s = self.simpledialog.askstring("添加路径", "输入文件或文件夹的完整路径：",
                                           parent=self.settings_win)
        if not s:
            return
        p = clean_path(s)
        if not p:
            return
        if not os.path.exists(p):
            if not self.messagebox.askyesno("路径不存在", "该路径当前不存在，仍要添加吗？\n\n" + p,
                                               parent=self.settings_win):
                return
        ok, msg = self.store.add(p)
        self.refresh_settings()
        if self.panel and self.panel.winfo_viewable():
            self.refresh_list()
        if not ok:
            self.notify(msg)

    def _whitelist_list(self):
        try:
            raw = self.var_whitelist.get()
        except Exception:
            raw = ""
        out = []
        for part in re.split(r"[,，;；\s]+", raw):
            p = part.strip().lower()
            if p and p not in out:
                out.append(p)
        return out

    def _add_fg_to_whitelist(self):
        if not (self.settings_win and self.settings_win.winfo_exists()):
            return
        self.settings_win.withdraw()
        self.notify("5 秒内切到要加入白名单的应用…")

        def grab():
            name = process_name_of_hwnd(foreground_hwnd())
            try:
                if name and name not in ("python.exe", "pythonw.exe"):
                    cur = self._whitelist_list()
                    if name not in cur:
                        cur.append(name)
                    self.var_whitelist.set(", ".join(cur))
                    self._save_options()
                    log("whitelist add: %s" % name)
            finally:
                if self.settings_win and self.settings_win.winfo_exists():
                    self.settings_win.deiconify()
                    self.settings_win.lift()

        self.root.after(5000, grab)

    def _save_options(self):
        self.store.data["copy_file_mode"] = "file"
        self.store.data["restore_clip"] = bool(self.var_restore.get())
        self.store.data["quote_path"] = bool(self.var_quote.get())
        self.store.data["paste_mode"] = self.var_paste.get()
        self.store.data["double_confirm"] = bool(self.var_double.get())
        self.store.data["confirm_whitelist"] = self._whitelist_list()
        self.store.data["auto_collect"] = bool(self.var_auto.get())
        self.store.data["sort_by_letter"] = bool(self.var_sort.get())
        self.store.data["group_style"] = self.var_group.get()
        self.store.save()
        self._sync_move_buttons()
        if self.panel and self.panel.winfo_viewable():
            self.refresh_list()

    def watch_clipboard(self):
        try:
            seq = user32.GetClipboardSequenceNumber()
            if seq != self._clip_seq:
                self._clip_seq = seq
                if self.store.data.get("auto_collect", True):
                    entries = clipboard_paths_as_entries(self.store.data.get("copy_file_mode", "dir"))
                    if entries:
                        added = self.store.add_many(entries)
                        if added:
                            log("auto collect %d" % added)
                            if self.panel and self.panel.winfo_viewable():
                                self.refresh_list()
                            if self.settings_win and self.settings_win.winfo_exists():
                                self.refresh_settings()
        except Exception as e:
            log("watch clipboard failed: %r" % (e,))
        try:
            self.root.after(700, self.watch_clipboard)
        except Exception:
            pass

    # ---------- 队列处理
    def poll(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "show":
                    self.toggle_panel(payload or None)
                elif kind == "add_clip":
                    self.add_from_clipboard()
                elif kind == "settings":
                    self.open_settings()
                elif kind == "toggle_autostart":
                    self.set_autostart(not self._autostart_enabled())
                elif kind == "toggle_double_confirm":
                    self.store.data["double_confirm"] = not self.store.data.get("double_confirm", True)
                    self.store.save()
                    if self.settings_win and self.settings_win.winfo_exists():
                        self.refresh_settings()
                    try:
                        self.tray.update_menu()
                    except Exception:
                        pass
                    log("double_confirm -> %s" % self.store.data["double_confirm"])
                    self.notify("双击自动回车确认：%s" % ("开" if self.store.data["double_confirm"] else "关"))
                elif kind == "hide":
                    self.hide_panel()
                elif kind == "key":
                    vk, shift = payload
                    self.handle_hook_key(vk, shift)
                elif kind == "hotkey_error":
                    self.notify("热键注册失败，可能被其他程序占用")
                elif kind == "quit":
                    self.shutdown()
                    return
        except queue.Empty:
            pass
        self.root.after(80, self.poll)

    def shutdown(self):
        log("shutdown")
        try:
            if self.keyboard:
                self.keyboard.stop()
        except Exception:
            pass
        try:
            if self.mousewatch:
                self.mousewatch.stop()
        except Exception:
            pass
        try:
            if self.hotkeys:
                self.hotkeys.stop()
        except Exception:
            pass
        try:
            if self.tray:
                self.tray.stop()
        except Exception:
            pass
        try:
            self.root.destroy()
        except Exception:
            pass

    # ---------- 运行
    def run(self):
        self.start_tray()
        self.keyboard = KeyboardHook(self)
        self.keyboard.start()
        self.mousewatch = OutsideClickWatch(self)
        self.mousewatch.start()
        self.hotkeys = HotkeyThread(self.q)
        self.hotkeys.start()
        self.hotkeys.ready.wait(2.0)
        self.root.protocol("WM_DELETE_WINDOW", self.shutdown)
        self._clip_seq = user32.GetClipboardSequenceNumber()
        self.root.after(80, self.poll)
        self.root.after(700, self.watch_clipboard)
        self.root.mainloop()


def main():
    ensure_dirs()
    if "--selftest" in sys.argv:
        print("data dir:", APP_DIR)
        s = Store()
        print("entries:", len(s.items()))
        print("clipboard files:", clipboard_files()[:3])
        print("clipboard text head:", repr(clipboard_text()[:60]))
        print("cursor workarea:", workarea_of_cursor())
        print("foreground:", foreground_hwnd())
        print("autostart:", App._autostart_enabled())
        return 0

    if not single_instance():
        log("another instance already running")
        try:
            ctypes.windll.user32.MessageBoxW(0, "路径面板已经在运行，请查看系统托盘。", APP_NAME, 0x40)
        except Exception:
            pass
        return 0

    set_dpi_aware()
    try:
        app = App()
        app.run()
    except Exception as e:
        log("fatal: %r" % (e,))
        import traceback
        log(traceback.format_exc())
        try:
            ctypes.windll.user32.MessageBoxW(0, "路径面板启动失败：\n\n%s\n\n详见 %s" % (e, LOG_FILE),
                                             APP_NAME, 0x10)
        except Exception:
            pass
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
