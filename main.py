"""跟跑浏览器 - V1.0.6
核心能力：用 Windows 原生 RegisterHotKey API 注册全局热键，
解决 Edge/WebView2 焦点下键盘钩子容易被吞的问题。
V1.0.4 优化：修复首次打开崩溃卡死问题（WebView2 异常保护+多实例互斥锁+原子写入）。
V1.0.5 重构：WebView2 缓存目录前置到 %LOCALAPPDATA%（exe 黑屏修复）、移除强制 GPU
参数（交给 WebView2 按显卡自动降级）、private_mode=False（关闭崩溃/登录持久化）。
V1.0.6 修复：首次启动（window_state 为空）时窗口从未收到尺寸变化，WebView2 渲染表面
停在创建时的旧尺寸 → 整窗只剩背景色的"白屏"（页面其实已加载）。现在无论有无保存状态
都会应用一次窗口几何，尺寸未变则抖动 2px 强制刷新；并把 pywebview 自身日志接入
debug.log（此前只写 stderr，打包后白屏没有任何线索）。"""
# ---- 环境初始化：必须在 import webview 之前执行（exe 打包黑屏修复）----
import os
import sys

# 1. 强制重定向 WebView2 用户数据目录到 AppData，
#    绕过 PyInstaller 解压临时目录（sys._MEIPASS）的权限黑洞
_WB2_DATA_DIR = os.path.join(
    os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
    "GenPaoBrowser_Data",
)
try:
    os.makedirs(_WB2_DATA_DIR, exist_ok=True)
except Exception:
    pass
os.environ["WEBVIEW2_USER_DATA_FOLDER"] = _WB2_DATA_DIR

# 2. 移除强制 GPU 光栅化/硬件解码参数：部分显卡驱动下会导致
#    exe 内 WebView2 渲染进程挂起 → 黑屏。交给 WebView2 自动降级。
os.environ.pop("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS", None)

# 3. 固定使用 EdgeChromium 后端
os.environ["PYWEBVIEW_GUI"] = "edgechromium"

# ---- 以下为标准库导入 ----
import ctypes
import ctypes.wintypes
import json
import logging
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

# win32 常量（避免额外依赖 pywin32）
WM_HOTKEY    = 0x0312
MB_OK               = 0x00000000
MB_OKCANCEL         = 0x00000001
MB_YESNO            = 0x00000004
MB_ICONERROR        = 0x00000010
MB_ICONQUESTION     = 0x00000020
MB_ICONWARNING      = 0x00000030
MB_ICONINFORMATION  = 0x00000040
MB_TOPMOST          = 0x00040000


def _messagebox_w(title, text, style=MB_OK | MB_ICONINFORMATION | MB_TOPMOST):
    """Win32 MessageBoxW — 不依赖 tkinter，可在主入口安全使用。"""
    return ctypes.windll.user32.MessageBoxW(None, text, title, style)


MOD_ALT      = 0x0001
MOD_CONTROL  = 0x0002
MOD_SHIFT    = 0x0004
MOD_WIN      = 0x0008
WS_EX_TRANSPARENT = 0x00000020

# 与主窗口共享"跟跑助手"字样的辅助窗口标题特征（设置/等待/无响应弹窗），
# 按标题取主窗口 HWND 时必须排除它们
_AUX_WINDOW_TITLE_MARKS = ("设置", "等待中", "无响应")

# 虚拟键码映射：配置键名 -> Windows VK_* 值
VK_MAP = {
    "a": 0x41, "b": 0x42, "c": 0x43, "d": 0x44, "e": 0x45,
    "f": 0x46, "g": 0x47, "h": 0x48, "i": 0x49, "j": 0x4A,
    "k": 0x4B, "l": 0x4C, "m": 0x4D, "n": 0x4E, "o": 0x4F,
    "p": 0x50, "q": 0x51, "r": 0x52, "s": 0x53, "t": 0x54,
    "u": 0x55, "v": 0x56, "w": 0x57, "x": 0x58, "y": 0x59, "z": 0x5A,
    "0": 0x30, "1": 0x31, "2": 0x32, "3": 0x33, "4": 0x34,
    "5": 0x35, "6": 0x36, "7": 0x37, "8": 0x38, "9": 0x39,
    "+": 0xBB, "=": 0xBB, "-": 0xBD,
    "[": 0xDB, "]": 0xDD, "\\": 0xDC,
    ";": 0xBA, "'": 0xDE, ",": 0xBC, ".": 0xBE, "/": 0xBF, "`": 0xC0,
    "plus": 0xBB, "minus": 0xBD, "equal": 0xBB,
    # 小键盘（独立于字母区键位）
    "numpad_0": 0x60, "numpad_1": 0x61, "numpad_2": 0x62, "numpad_3": 0x63,
    "numpad_4": 0x64, "numpad_5": 0x65, "numpad_6": 0x66, "numpad_7": 0x67,
    "numpad_8": 0x68, "numpad_9": 0x69, "numpad_plus": 0x6B, "numpad_minus": 0x6D,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "home": 0x24, "end": 0x23,
    "prior": 0x21, "next": 0x22, "pageup": 0x21, "pagedown": 0x22,
    "insert": 0x2D, "delete": 0x2E,
    "space": 0x20, "return": 0x0D, "enter": 0x0D,
    "backspace": 0x08, "tab": 0x09, "escape": 0x1B, "esc": 0x1B,
    "f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73, "f5": 0x74,
    "f6": 0x75, "f7": 0x76, "f8": 0x77, "f9": 0x78, "f10": 0x79,
    "f11": 0x7A, "f12": 0x7B,
}


def _is_frozen():
    return getattr(sys, "frozen", False)

_APP_DIR = os.path.dirname(sys.executable if _is_frozen() else __file__)


try:
    import webview as pywebview
    from pynput import keyboard  # 仅 HotkeyRecorder 使用
except ImportError as _ie:
    import traceback
    _err_path = os.path.join(os.path.dirname(sys.executable if getattr(sys, "frozen", False) else __file__), "debug.log")
    with open(_err_path, "a", encoding="utf-8") as _f:
        _f.write(f"[{time.strftime('%H:%M:%S')}] FATAL: 缺少必要依赖: {_ie}\n")
        _f.write(traceback.format_exc() + "\n")
    _messagebox_w("环境缺失", f"缺少必要依赖: {_ie}\n\n请确保已安装所有依赖。", MB_OK | MB_ICONERROR | MB_TOPMOST)
    sys.exit(1)

LOG_PATH = os.path.join(_APP_DIR, "debug.log")
_config_write_lock = threading.Lock()  # 配置写入互斥锁
_logger = logging.getLogger("跟跑浏览器")
_logger.setLevel(logging.DEBUG)
if not _logger.handlers:
    fh = logging.FileHandler(LOG_PATH, encoding="utf-8", mode="w")
    fh.setLevel(logging.DEBUG)
    sh = logging.StreamHandler()
    sh.setLevel(logging.DEBUG)
    fmt = logging.Formatter("[%(asctime)s] %(levelname)s: %(message)s", datefmt="%H:%M:%S")
    fh.setFormatter(fmt); sh.setFormatter(fmt)
    _logger.addHandler(fh); _logger.addHandler(sh)

    # pywebview 自身的日志（WebView2 初始化失败、导航/渲染异常等）过去只写
    # stderr，打包成窗口程序后 stderr 无处可去 → 出白屏时 debug.log 里一条
    # 线索都没有。这里复用同一个 FileHandler 接进来（不能各开一个句柄，
    # 两个文件对象各持写入位置会互相覆盖日志内容）。
    _pw_logger = logging.getLogger("pywebview")
    _pw_logger.setLevel(logging.DEBUG)
    _pw_logger.addHandler(fh)

def _log(msg): _logger.info(msg)
def _logd(msg): _logger.debug(msg)
DEFAULT_CONFIG = {
    "homepage": "https://www.bilibili.com",
    "width": 1280, "height": 720,   # 默认 16:9
    "window_state": None,
    "lock_aspect_ratio": True,      # 拖动窗口时锁定 16:9 比例
    "opacity_levels": [1.0, 0.9, 0.8, 0.7, 0.6, 0.5, 0.4, 0.3, 0.2, 0.1],
    "show_top_bar": True, "top_bar_auto_hide": True,
    "hotkeys": {
        "play_pause": "ctrl+space", "seek_forward": "ctrl+right", "seek_backward": "ctrl+left",
        "adjust_opacity_up": "ctrl+up", "adjust_opacity_down": "ctrl+down",
        "show_settings": "home",
        "speed_up": "ctrl++", "speed_down": "ctrl+-", "speed_reset": "ctrl+0",
        "browser_back": "ctrl+backspace", "toggle_top_bar": "insert",
        "toggle_click_through": "ctrl+t",
    }
}

_action_queue   = queue.Queue()
_hotkey_manager = None   # RegisterHotKey 监听器引用


# ====================== RegisterHotKey 全局热键实现 ======================
# 根因：pynput 的键盘钩子（WH_KEYBOARD_LL）在 Edge/WebView2 焦点下会被吞事件，
# 原因：Edge 的输入管道在更底层拦截了键盘事件，阻止钩子收到。
# 解决方案：改用 Windows RegisterHotKey API（工作于消息队列层，与键盘钩子完全独立）
# 限制：只支持 1 个修饰键组合 + 1 个主键（正好满足所有热键场景）


class RegisterHotkeyManager:
    """使用 Windows RegisterHotKey API 的全局热键管理器（独立线程消息循环）。"""

    def __init__(self):
        self.user32     = ctypes.windll.user32
        self._callbacks = {}   # {hotkey_id: (action_name, combo_str, app_ref_fn)}
        self._cb_lock = threading.Lock()  # _callbacks 线程安全锁
        self._next_id   = 1
        self._running   = False
        self._thread    = None

    @staticmethod
    def parse_combo(combo):
        """
        将 "ctrl+p"、"alt+right"、"ctrl++" 等字符串转为 (MOD_*, vk)。
        RegisterHotKey 只支持：修饰键（ALT/CTRL/SHIFT/WIN）+ 1 个主键。
        """
        if not combo:
            return None
        combo = combo.strip().lower()
        # "ctrl++" → split("+") 会得到 ["ctrl", "", ""]，需恢复末尾的 "+" 主键。
        # "ctrl+-" 过滤空字符串后已经是 ["ctrl", "-"]，不需要特殊处理。
        parts = [p.strip() for p in combo.split("+") if p.strip()]
        if combo.endswith("++"):
            parts.append("+")
        if not parts:
            return None

        mods = 0
        main = None
        for p in parts:
            if p == "alt":
                mods |= MOD_ALT
            elif p in ("ctrl", "control"):
                mods |= MOD_CONTROL
            elif p == "shift":
                mods |= MOD_SHIFT
            elif p in ("win", "cmd", "command"):
                mods |= MOD_WIN
            else:
                if main is not None:
                    return None   # 两个以上主键，不支持
                main = p

        if main is None:
            return None
        vk = VK_MAP.get(main)
        if vk is None:
            return None
        return (mods, vk)

    # ---- 注册必须在消息循环线程内调用（核心修复） ----

    def _register(self, action_name, combo, app_ref_fn):
        """在线程内执行 RegisterHotKey，确保消息投递到同一线程。"""
        hotkey_id = self._next_id
        self._next_id += 1
        parsed = self.parse_combo(combo)
        if parsed is None:
            _logd(f"热键无法注册（格式不支持）: {combo!r} ({action_name})")
            return
        mods, vk = parsed
        ok = self.user32.RegisterHotKey(None, hotkey_id, mods, vk)
        if ok:
            with self._cb_lock:
                self._callbacks[hotkey_id] = (action_name, combo, app_ref_fn)
            _logd(f"注册热键: {combo!r} -> id={hotkey_id} ({action_name})")
        else:
            _log(f"热键注册失败（可能被占用）: {combo!r} ({action_name})")

    def start(self, pending_registrations):
        """启动线程，在线程内完成注册+消息循环。"""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._run,
            args=(pending_registrations,),
            name="HotkeyMsgLoop",
            daemon=True
        )
        self._thread.start()

    def stop(self):
        self._running = False
        with self._cb_lock:
            for hid in list(self._callbacks.keys()):
                self.user32.UnregisterHotKey(None, hid)
            self._callbacks.clear()
        # 发送 WM_QUIT 让 GetMessageA 返回 0，线程正常退出
        if self._thread and self._thread.ident and self._thread.is_alive():
            WM_QUIT = 0x0012
            ctypes.windll.user32.PostThreadMessageW(self._thread.ident, WM_QUIT, 0, 0)

    def _run(self, pending):
        """线程入口：先注册热键，再进入消息循环。"""
        for action, combo, app_ref_fn in pending:
            self._register(action, combo, app_ref_fn)
        registered = len(self._callbacks)
        _log(f"热键监听已启动（RegisterHotKey），共注册 {registered} 个热键")
        self._loop()

    def _loop(self):
        """Windows 消息循环，使用 PeekMessage 避免阻塞。"""
        msg = ctypes.wintypes.MSG()
        PM_REMOVE = 0x0001
        while self._running:
            # PeekMessage 非阻塞，有消息才处理，无消息时 sleep 1ms 降低 CPU
            ret = self.user32.PeekMessageA(ctypes.byref(msg), None, 0, 0, PM_REMOVE)
            if ret == 0:
                time.sleep(0.001)
                continue
            if msg.message == WM_HOTKEY:
                hid = msg.wParam
                with self._cb_lock:
                    cb_info = self._callbacks.get(hid)
                if cb_info:
                    action_name, combo, app_ref_fn = cb_info
                    self._dispatch(action_name, combo, app_ref_fn)
            elif msg.message == 0x0012:  # WM_QUIT
                break
            self.user32.TranslateMessage(ctypes.byref(msg))
            self.user32.DispatchMessageA(ctypes.byref(msg))

    def _dispatch(self, action_name, combo, app_ref_fn):
        app = app_ref_fn()
        if app is None:
            return
        if action_name in ("show_settings", "open_settings") and app.is_settings_opened:
            return
        real_act = "open_settings" if action_name == "show_settings" else action_name
        # 队列上限保护：超过 50 个丢弃最早的，防止内存堆积
        if _action_queue.qsize() > 50:
            try:
                _action_queue.get_nowait()
            except queue.Empty:
                pass
        _action_queue.put(real_act)
        _log(f"热键触发: {combo!r} -> {real_act}")


# ====================== 热键校验（配置加载时用） ======================

def _validate_hotkey(combo):
    if not combo or not isinstance(combo, str):
        return False
    combo = combo.strip().lower()
    if not combo:
        return False
    if any(ord(c) < 32 for c in combo):
        return False
    return RegisterHotkeyManager.parse_combo(combo) is not None


# ====================== 主程序 ======================

class BrowserApp:
    CONFIG_FILE = os.path.join(_APP_DIR, "hotkeys.json")

    def __init__(self):
        self.config = self._load_config()
        self.window = None
        self.is_ghost_mode = False
        self.opacity_levels = self.config.get("opacity_levels", DEFAULT_CONFIG["opacity_levels"])
        self.opacity_index = 0
        self.current_opacity = self.opacity_levels[0]
        self.is_settings_opened = False
        self._last_action_time = {}   # action 节流用
        self._shutting_down = False
        self._last_bar_inject = 0
        self._bar_visible = False     # 横条显隐状态
        self._seek_accum = 0          # 快进/退累计偏移（毫秒合并用）
        self._seek_flush_scheduled = None  # 下次刷新时间（time.time）
        self._click_through = False   # 鼠标穿透状态
        self._pending_actions = []    # 窗口就绪前缓存的热键动作
        self._pending_lock = threading.Lock()  # pending_actions 线程安全锁
        self._window_ready = False    # WebView2 窗口是否已就绪
        self._hwnd = None             # 缓存窗口句柄，避免每次 FindWindowW
        # 16:9 比例锁定状态（resized 事件 + 防抖校正实现，不碰 Win32 WndProc）
        self._lock_ratio = bool(self.config.get("lock_aspect_ratio", True))
        self._fix_timer = None        # 16:9 校正防抖定时器
        self._pending_size = None     # 待校正的 (width, height)

    def _load_config(self):
        if not os.path.exists(self.CONFIG_FILE):
            self._write_config(DEFAULT_CONFIG)
            return json.loads(json.dumps(DEFAULT_CONFIG))
        try:
            with open(self.CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            for k, v in DEFAULT_CONFIG.items():
                if k not in data:
                    data[k] = v
            for k, v in DEFAULT_CONFIG["hotkeys"].items():
                if "hotkeys" not in data or k not in data["hotkeys"]:
                    if "hotkeys" not in data:
                        data["hotkeys"] = {}
                    data["hotkeys"][k] = v
                else:
                    existing = data["hotkeys"][k]
                    if not _validate_hotkey(existing):
                        _log(f"热键 {k} 格式损坏({existing!r})，已重置")
                        data["hotkeys"][k] = v
            for k in ("show_top_bar", "top_bar_auto_hide"):
                if k not in data:
                    data[k] = DEFAULT_CONFIG[k]
            # 清理已删除的键
            data["hotkeys"].pop("toggle_ghost", None)
            return data
        except Exception:
            return json.loads(json.dumps(DEFAULT_CONFIG))

    def _write_config(self, cfg):
        """原子写入配置文件：先写临时文件再重命名，防止崩溃导致文件损坏。"""
        with _config_write_lock:
            tmp_path = self.CONFIG_FILE + ".tmp"
            try:
                with open(tmp_path, "w", encoding="utf-8") as f:
                    json.dump(cfg, f, indent=2, ensure_ascii=False)
                os.replace(tmp_path, self.CONFIG_FILE)
            except Exception:
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass

    def _apply_config(self, new_cfg):
        global _hotkey_manager
        self.config = new_cfg
        self._write_config(new_cfg)
        self.opacity_levels = new_cfg.get("opacity_levels", DEFAULT_CONFIG["opacity_levels"])
        self.opacity_index = 0
        self.current_opacity = self.opacity_levels[0]
        # 同步 16:9 比例锁定开关；从关闭切到开启时，立即把窗口调整为 16:9
        was_locked = self._lock_ratio
        self._lock_ratio = bool(new_cfg.get("lock_aspect_ratio", True))
        if self._lock_ratio and not was_locked:
            self._fit_ratio_now()
        if _hotkey_manager:
            old_mgr = _hotkey_manager
            old_mgr.stop()
            if old_mgr._thread:
                old_mgr._thread.join(timeout=2)
            _hotkey_manager = _build_hotkey_manager(self)
        self._inject_bar()
        _log("配置已应用")

    def get_data_dir(self):
        return os.path.join(_APP_DIR, "data")

    # ---- 悬浮横条 ----

    KEY_DISPLAY_MAP = {
        "up": "⬆️", "down": "⬇️", "left": "⬅️", "right": "➡️",
        "space": "空格", "return": "↵ 回车", "enter": "↵ 回车",
        "backspace": "⌫ 退格", "tab": "⇥ Tab", "escape": "Esc",
        "delete": "Del", "home": "↖ Home", "end": "↘ End",
        "pageup": "⇞ 上页", "pagedown": "⇟ 下页", "insert": "Ins",
        "ctrl": "Ctrl", "alt": "Alt", "shift": "⇧ Shift", "win": "⊞ Win",
        "f1": "F1", "f2": "F2", "f3": "F3", "f4": "F4", "f5": "F5",
        "f6": "F6", "f7": "F7", "f8": "F8", "f9": "F9", "f10": "F10",
        "f11": "F11", "f12": "F12",
    }

    # 顶部横条定义（高频实时控制 + 网址跳转）
    TOP_BAR_DEFS = [
        ("play_pause",        "⏯", "播放",   "播放/暂停视频"),
        ("seek_backward",     "⏪", "快退",   "快退 5 秒"),
        ("seek_forward",      "⏩", "快进",   "快进 5 秒"),
    ]

    # 左侧边栏定义（低频状态切换）
    LEFT_BAR_DEFS = [
        ("adjust_opacity_up",   "📈", "透明+", "提高窗口透明度"),
        ("adjust_opacity_down", "📉", "透明-", "降低窗口透明度"),
        ("toggle_click_through", "🔒", "穿透", "切换鼠标穿透模式（Ctrl+T）"),
        ("open_settings",     "⚙",  "设置",   "打开设置面板"),
    ]

    def _format_hotkey_display(self, combo):
        if not combo:
            return ""
        parts = combo.lower().split("+")
        mods, mains = [], []
        for p in parts:
            p = p.strip()
            if p in ("ctrl", "alt", "shift", "win"):
                mods.append(self.KEY_DISPLAY_MAP.get(p, p.title()))
            else:
                mains.append(self.KEY_DISPLAY_MAP.get(p, p.upper() if p.startswith("f") else p.title()))
        joined = " + ".join(mods + mains)
        if len(parts) == 1 and parts[0] in self.KEY_DISPLAY_MAP:
            return self.KEY_DISPLAY_MAP[parts[0]]
        return joined

    def _bar_css(self):
        return r"""
        /* ===== 顶部横条 ===== */
        #__gt_bar { position: fixed; top: 0; left: 0; right: 0; height: 40px; z-index: 2147483646;
            display: flex; align-items: center; justify-content: center;
            font-family: "Microsoft YaHei","PingFang SC",sans-serif; font-size: 14px;
            color: rgba(205,214,244,0.9); pointer-events: auto; user-select: none;
            transform: translateY(0); transition: opacity 0.35s ease, transform 0.35s ease;
            background: linear-gradient(180deg, rgba(30,30,46,0.70) 0%, rgba(30,30,46,0.40) 100%);
            backdrop-filter: blur(14px); -webkit-backdrop-filter: blur(14px);
            border-bottom: 1px solid rgba(137,180,250,0.15);
            box-shadow: 0 2px 20px rgba(0,0,0,0.35), inset 0 1px 0 rgba(255,255,255,0.05); }
        #__gt_bar.gt-hide { transform: translateY(-100%); opacity: 0; pointer-events: none; }
        .gt-bar-inner { display: flex; align-items: center; gap: 10px; padding: 0 14px; }
        .gt-bar-item { display: flex; align-items: center; gap: 6px; padding: 5px 12px;
            border-radius: 10px; cursor: pointer; transition: background 0.2s, color 0.2s;
            position: relative; white-space: nowrap; }
        .gt-bar-item:hover { background: rgba(137,180,250,0.18); color: #89b4fa; }
        .gt-bar-item:active { background: rgba(166,227,161,0.3); }
        .gt-icon { font-size: 15px; line-height: 1; }
        .gt-label { font-size: 12px; }
        .gt-key { font-size: 10px; color: rgba(108,112,134,0.9); background: rgba(49,50,68,0.8);
            padding: 1px 5px; border-radius: 3px; border: 1px solid rgba(108,112,134,0.3); }
        .gt-bar-item:hover .gt-key { color: rgba(137,180,250,0.8); border-color: rgba(137,180,250,0.4); }
        .gt-url-wrap { display: flex; align-items: center; gap: 6px; padding: 0 4px; flex-shrink: 0; }
        .gt-url-input { background: rgba(49,50,68,0.90); border: 1px solid rgba(137,180,250,0.45);
            border-radius: 6px; color: rgba(205,214,244,0.95); font-size: 12px;
            padding: 4px 12px; width: 280px; min-width: 180px; outline: none;
            font-family: "Microsoft YaHei","PingFang SC",sans-serif; }
        .gt-url-input:focus { border-color: rgba(137,180,250,0.8); background: rgba(49,50,68,0.95); box-shadow: 0 0 0 2px rgba(137,180,250,0.15); }
        .gt-url-input::placeholder { color: rgba(108,112,134,0.8); }
        .gt-url-btn { background: rgba(137,180,250,0.25); border: none; border-radius: 6px;
            color: #89b4fa; font-size: 12px; padding: 3px 10px; cursor: pointer;
            font-family: "Microsoft YaHei","PingFang SC",sans-serif; }
        .gt-url-btn:hover { background: rgba(137,180,250,0.4); }
        /* 穿透状态呼吸动画 */
        @keyframes gt-pulse { 0%,100% { opacity: 1; transform: scale(1); box-shadow: 0 0 0 0 rgba(166,227,161,0.4); } 50% { opacity: 0.65; transform: scale(1.08); box-shadow: 0 0 12px 3px rgba(166,227,161,0.35); } }
        .gt-clickthrough-active { animation: gt-pulse 1s infinite; }
        /* 响应式 */
        @media (max-width: 650px) { .gt-label, .gt-key { display: none; } .gt-bar-item { padding: 5px 10px; } }
        @media (max-width: 520px) { .gt-url-input { width: 140px; } }
        @media (max-width: 440px) { .gt-url-wrap { display: none; } }

        /* ===== 左侧边栏 ===== */
        #__gt_leftbar { position: fixed; top: 0; left: 0; bottom: 0; width: 44px; z-index: 2147483645;
            display: flex; flex-direction: column; align-items: center; justify-content: center;
            font-family: "Microsoft YaHei","PingFang SC",sans-serif; font-size: 14px;
            color: rgba(205,214,244,0.9); pointer-events: auto; user-select: none;
            transform: translateX(0); transition: opacity 0.35s ease, transform 0.35s ease, width 0.35s ease;
            background: linear-gradient(90deg, rgba(30,30,46,0.70) 0%, rgba(30,30,46,0.40) 100%);
            backdrop-filter: blur(14px); -webkit-backdrop-filter: blur(14px);
            border-right: 1px solid rgba(137,180,250,0.15);
            box-shadow: 2px 0 20px rgba(0,0,0,0.35), inset 0 1px 0 rgba(255,255,255,0.05); }
        #__gt_leftbar.gt-collapsed { transform: translateX(-100%); width: 44px; }
        #__gt_leftbar.gt-expanded { width: 80px; }
        .gt-leftbar-inner { display: flex; flex-direction: column; align-items: center; gap: 10px; padding: 14px 0; }
        .gt-leftbar-item { display: flex; flex-direction: column; align-items: center; gap: 2px; padding: 8px 2px;
            border-radius: 10px; cursor: pointer; transition: background 0.2s, color 0.2s;
            white-space: nowrap; width: 100%; justify-content: center; }
        .gt-leftbar-item:hover { background: rgba(137,180,250,0.18); color: #89b4fa; }
        .gt-leftbar-item:active { background: rgba(166,227,161,0.3); }
        .gt-leftbar-item .gt-icon { font-size: 15px; line-height: 1; }
        .gt-leftbar-item .gt-label { font-size: 11px; }
        .gt-leftbar-item .gt-key { font-size: 9px; }
        /* 边栏收起时只显示图标 */
        #__gt_leftbar.gt-collapsed .gt-label,
        #__gt_leftbar.gt-collapsed .gt-key { display: none; }
        #__gt_leftbar.gt-collapsed .gt-leftbar-item { padding: 8px 0; justify-content: center; }
        """

    def _inject_bar(self):
        if not self.window:
            return
        if not self.config.get("show_top_bar", True):
            self._remove_bar()
            return
        # 注入频率限制：1 秒内不重复注入
        now = time.time()
        if now - self._last_bar_inject < 1.0:
            return
        self._last_bar_inject = now

        # 因为当前是由 _on_loaded 事件触发注入，说明 DOM 已绝对就绪
        # 直接检查 _window_ready 标志即可，不需要循环等待
        if not self._window_ready:
            return

        hk_cfg     = self.config.get("hotkeys", {})
        auto_hide  = self.config.get("top_bar_auto_hide", True)

        # ---- 构建顶部横条 HTML ----
        top_html = (
            '<div class="gt-url-wrap" title="输入网址按回车或点击跳转">'
            '<input class="gt-url-input" type="text" placeholder="输入网址..." />'
            '<button class="gt-url-btn">跳转</button></div>'
        )
        for act, icon, label, title in self.TOP_BAR_DEFS:
            hotkey_raw  = hk_cfg.get(act, "")
            hotkey_disp = self._format_hotkey_display(hotkey_raw)
            top_html += (
                f'<div class="gt-bar-item" data-action="{act}" title="{title}">'
                f'<span class="gt-icon">{icon}</span>'
                f'<span class="gt-label">{label}</span>'
                f'<span class="gt-key">{hotkey_disp}</span></div>'
            )

        # ---- 构建左侧边栏 HTML ----
        left_html = ""
        for act, icon, label, title in self.LEFT_BAR_DEFS:
            hotkey_raw  = hk_cfg.get(act, "")
            hotkey_disp = self._format_hotkey_display(hotkey_raw)
            extra_cls = " gt-clickthrough-active" if (act == "toggle_click_through" and self._click_through) else ""
            left_html += (
                f'<div class="gt-leftbar-item{extra_cls}" data-action="{act}" title="{title}">'
                f'<span class="gt-icon">{icon}</span>'
                f'<span class="gt-label">{label}</span>'
                f'<span class="gt-key">{hotkey_disp}</span></div>'
            )

        import json as _json_mod
        css_json = _json_mod.dumps(self._bar_css())
        top_html_json = _json_mod.dumps(f'<div class="gt-bar-inner">{top_html}</div>')
        left_html_json = _json_mod.dumps(f'<div class="gt-leftbar-inner">{left_html}</div>')

        # 自动隐藏逻辑：不自动出现，只自动消失
        auto_hide_js = (
            r"""var gtHideTimer=null;
            function gtShow(){ bar.classList.remove('gt-hide'); lbar.classList.remove('gt-collapsed'); lbar.classList.add('gt-expanded'); }
            function gtHide(){ bar.classList.add('gt-hide'); lbar.classList.add('gt-collapsed'); lbar.classList.remove('gt-expanded'); }
            bar.addEventListener('mouseenter',function(){if(gtHideTimer){clearTimeout(gtHideTimer);gtHideTimer=null;}});
            bar.addEventListener('mouseleave',function(){gtHideTimer=setTimeout(gtHide,3000);});
            lbar.addEventListener('mouseenter',function(){if(gtHideTimer){clearTimeout(gtHideTimer);gtHideTimer=null;}});
            lbar.addEventListener('mouseleave',function(){gtHideTimer=setTimeout(gtHide,3000);});
            document.addEventListener('mousemove',function(e){
                if(e.clientY<=3 || e.clientX<=4){
                    gtShow();
                    if(gtHideTimer){clearTimeout(gtHideTimer);}
                    gtHideTimer=setTimeout(gtHide,3000);
                }
            });"""
            if auto_hide else ""
        )

        js = rf"""(function(){{
            function inject(){{
                if(!document.body){{setTimeout(inject,100);return;}}
                // 清理旧元素
                ['__gt_bar','__gt_leftbar','__gt_bar_style'].forEach(function(id){{
                    var el=document.getElementById(id);if(el)el.remove();
                }});
                var style=document.createElement('style');style.id='__gt_bar_style';style.textContent={css_json};
                document.head.appendChild(style);

                // 顶部横条
                var bar=document.createElement('div');bar.id='__gt_bar';
                bar.innerHTML={top_html_json};document.body.appendChild(bar);
                bar.querySelectorAll('.gt-bar-item').forEach(function(btn){{
                    btn.addEventListener('click',function(){{
                        var act=btn.dataset.action;
                        if(window.pywebview&&pywebview.api)pywebview.api.trigger_action(act);
                        btn.style.background='rgba(166,227,161,0.35)';
                        setTimeout(function(){{btn.style.background='';}},200);
                    }});
                }});
                var urlInput=bar.querySelector('.gt-url-input');
                var urlBtn=bar.querySelector('.gt-url-btn');
                if(urlInput&&urlBtn){{
                    urlBtn.addEventListener('click',function(){{
                        var url=urlInput.value.trim();
                        if(url){{
                            if(!url.match(/^https?:\/\//i))url='https://'+url;
                            if(window.pywebview&&pywebview.api)pywebview.api.navigate(url);
                        }}
                        urlBtn.style.background='rgba(166,227,161,0.35)';
                        setTimeout(function(){{urlBtn.style.background='';}},200);
                    }});
                    urlInput.addEventListener('keydown',function(e){{
                        if(e.key==='Enter'){{e.preventDefault();urlBtn.click();}}
                    }});
                }}
                bar.classList.add('gt-hide');

                // 左侧边栏
                var lbar=document.createElement('div');lbar.id='__gt_leftbar';
                lbar.innerHTML={left_html_json};document.body.appendChild(lbar);
                lbar.querySelectorAll('.gt-leftbar-item').forEach(function(btn){{
                    btn.addEventListener('click',function(){{
                        var act=btn.dataset.action;
                        if(window.pywebview&&pywebview.api)pywebview.api.trigger_action(act);
                        btn.style.background='rgba(166,227,161,0.35)';
                        setTimeout(function(){{btn.style.background='';}},200);
                    }});
                }});
                // 穿透按钮初始颜色
                var ctBtn=lbar.querySelector('.gt-leftbar-item[data-action="toggle_click_through"]');
                if(ctBtn)ctBtn.style.color='#f38ba8';
                // 默认收起（只露细边暗示存在）
                lbar.classList.add('gt-collapsed');
                {auto_hide_js}
            }}
            inject();
        }})();"""
        try:
            self.window.evaluate_js(js)
            self._bar_visible = False
            _logd("_inject_bar JS 已投递（顶部横条+左侧边栏）")
        except Exception as e:
            _logd(f"_inject_bar error: {e}")

    def _toggle_bar(self):
        """切换横条显隐（Insert 键触发），左侧边栏同步联动。"""
        if not self.window:
            return
        self._bar_visible = not self._bar_visible
        try:
            self.window.evaluate_js(
                f"""var bar=document.getElementById('__gt_bar');
                var lbar=document.getElementById('__gt_leftbar');
                if(bar){{bar.classList.toggle('gt-hide', {str(not self._bar_visible).lower()});}}
                if(lbar){{
                    if({str(not self._bar_visible).lower()}){{
                        lbar.classList.add('gt-collapsed');
                        lbar.classList.remove('gt-expanded');
                    }}else{{
                        lbar.classList.remove('gt-collapsed');
                        lbar.classList.add('gt-expanded');
                    }}
                }}"""
            )
            _log(f"横条 {'显示' if self._bar_visible else '隐藏'}")
        except Exception as e:
            _logd(f"_toggle_bar error: {e}")

    def _remove_bar(self):
        if not self.window:
            return
        try:
            self.window.evaluate_js(
                "var b=document.getElementById('__gt_bar');if(b)b.remove();"
                "var l=document.getElementById('__gt_leftbar');if(l)l.remove();"
                "var s=document.getElementById('__gt_bar_style');if(s)s.remove();"
            )
        except Exception as e:
            _logd(f"_remove_bar error: {e}")

    # ---- 动作分发 ----

    def _window_alive(self):
        hwnd = self._get_hwnd()
        return hwnd is not None and ctypes.windll.user32.IsWindow(hwnd)

    def _dispatch(self, action):
        if self._shutting_down:
            return
        now = time.time()
        # 尝试获取 HWND（带缓存），用于判断是否可执行非 JS 操作
        hwnd = self._get_hwnd()

        # 依赖 JS 注入的操作
        if action in ("play_pause", "seek_forward", "seek_backward",
                       "speed_up", "speed_down", "speed_reset",
                       "browser_back", "toggle_top_bar"):
            if not self.window or not self._window_ready:
                with self._pending_lock:
                    self._pending_actions.append(action)
                _logd(f"热键缓存（JS 未就绪）: {action}")
                return

        # 仅依赖 HWND 的操作：有 HWND 就执行，不取 _window_ready
        elif action in ("adjust_opacity_up", "adjust_opacity_down",
                        "toggle_click_through"):
            if not hwnd:
                with self._pending_lock:
                    self._pending_actions.append(action)
                _logd(f"热键缓存（HWND 未就绪）: {action}")
                return
            # HWND 存在 → 立即执行，不依赖窗口就绪标志

        # 快进/退：高频合并（通过 tkinter 线程调度，避免在 Timer 线程调 evaluate_js）
        if action in ("seek_forward", "seek_backward"):
            delta = 5 if action == "seek_forward" else -5
            self._seek_accum += delta
            self._seek_flush_scheduled = time.time() + 0.1
            return

        # 播放/暂停：节流 150ms
        if action == "play_pause":
            if now - self._last_action_time.get(action, 0) < 0.15:
                return
            self._js("if(v.paused)v.play();else v.pause();")
            self._osd("播放 / 暂停")
            self._last_action_time[action] = now
            return

        # 其他：节流 200ms
        if now - self._last_action_time.get(action, 0) < 0.2:
            return
        self._last_action_time[action] = now

        try:
            if action == "open_settings":
                # 重复触发保护：设置窗口已打开时直接忽略，避免连点按钮/重复热键
                # 导致队列里堆两份 open_settings，第二次在 _build_settings 尚未把
                # settings_win 赋值前被 tkinter 线程取出，从而再建一个空白窗口。
                if self.is_settings_opened:
                    _logd("设置窗口已打开，忽略重复 open_settings")
                    return
                self.is_settings_opened = True
            elif action == "close_settings":
                self.is_settings_opened = False
            elif action == "adjust_opacity_up":
                self._adj_opacity(1)
            elif action == "adjust_opacity_down":
                self._adj_opacity(-1)
            elif action == "quit":
                self._quit()
            elif action == "speed_up":
                self._speed(0.25)
            elif action == "speed_down":
                self._speed(-0.25)
            elif action == "speed_reset":
                self._js("v.playbackRate=1;")
                self._osd("倍速重置 1.0x")
            elif action == "browser_back":
                if self.window:
                    try:
                        self.window.evaluate_js("history.back()")
                        self._osd("浏览器后退")
                    except Exception as e:
                        _logd(f"browser_back error: {e}")
            elif action == "toggle_top_bar":
                self._toggle_bar()
            elif action == "toggle_click_through":
                self._toggle_click_through()
        except Exception as e:
            _log(f"_dispatch error ({action}): {e}")

    def _flush_seek(self):
        """将累计的快进/退偏移一次性应用到视频。"""
        if not self.window or self._seek_accum == 0:
            self._seek_accum = 0
            self._seek_flush_scheduled = None
            return
        offset = self._seek_accum
        self._seek_accum = 0
        self._seek_flush_scheduled = None
        js = f"""(function(){{
            var vs=document.querySelectorAll('video');if(!vs.length)return;
            var v=Array.from(vs).sort(function(a,b){{
                var ap=!a.paused,bp=!b.paused;
                if(ap&&!bp)return-1;if(!ap&&bp)return 1;
                return(b.videoWidth*b.videoHeight)-(a.videoWidth*a.videoHeight);
            }})[0];
            if(v){{v.currentTime=Math.max(0,Math.min(v.currentTime+{offset},v.duration||9999));}}
        }})();"""
        self._js_safe(js)
        _logd(f"seek 合并执行: {offset:+d}s")
        direction = "快进" if offset > 0 else "快退"
        self._osd(f"{direction} {abs(offset)}s")

    # ---- 视频控制 JS ----

    def _js(self, expr):
        if not self.window:
            return
        try:
            self.window.evaluate_js(f"""(function(){{
                var vs=document.querySelectorAll('video');if(!vs.length)return;
                var v=Array.from(vs).sort(function(a,b){{
                    var ap=!a.paused,bp=!b.paused;
                    if(ap&&!bp)return-1;if(!ap&&bp)return 1;
                    return(b.videoWidth*b.videoHeight)-(a.videoWidth*a.videoHeight);
                }})[0];
                if(v){{{expr}}}
            }})();""")
        except Exception as e:
            _logd(f"_js error: {e}")

    def _js_safe(self, js_code):
        """带超时保护的 evaluate_js 包装，防止 WebView2 阻塞卡死主线程。"""
        if not self.window:
            return
        import threading as _th
        result = [None]
        def _run():
            try:
                self.window.evaluate_js(js_code)
                result[0] = True
            except Exception as e:
                result[0] = e
        t = _th.Thread(target=_run, daemon=True)
        t.start()
        t.join(timeout=2.0)
        if t.is_alive():
            _logd("_js_safe 超时: WebView2 evaluate_js 阻塞超过 2s")
        elif isinstance(result[0], Exception):
            _logd(f"_js_safe error: {result[0]}")

    def _speed(self, delta):
        if not self.window:
            return
        oid = "_gtosd"
        js = f"""(function(){{
            var vs=document.querySelectorAll('video');if(!vs.length)return;
            var v=Array.from(vs).sort(function(a,b){{
                var ap=!a.paused,bp=!b.paused;
                if(ap&&!bp)return-1;if(!ap&&bp)return 1;
                return(b.videoWidth*b.videoHeight)-(a.videoWidth*a.videoHeight);
            }})[0];
            if(!v)return;
            v.playbackRate=Math.max(0.25,Math.min(16,v.playbackRate+{delta}));
            var s=v.playbackRate.toFixed(2);
            var o=document.getElementById('{oid}');
            if(!o){{o=document.createElement('div');o.id='{oid}';
                Object.assign(o.style,{{position:'fixed',top:'50%',left:'50%',
                transform:'translate(-50%,-50%)',background:'rgba(20,20,30,0.92)',
                color:'#fff',padding:'10px 28px',borderRadius:'12px',
                fontSize:'20px',fontFamily:'微软雅黑,sans-serif',
                zIndex:2147483647,pointerEvents:'none',
                border:'1px solid rgba(137,180,250,0.5)',
                boxShadow:'0 4px 24px rgba(0,0,0,0.6)',
                textAlign:'center',minWidth:'120px'}});
                document.body.appendChild(o);}}
            o.textContent='倍速: '+s+'x';o.style.opacity='1';
            clearTimeout(o._t);o._t=setTimeout(function(){{o.style.opacity='0';}},1000);
        }})();"""
        self._js_safe(js)

    def _osd(self, text):
        if not self.window:
            return
        oid = "_gtosd"
        esc_text = json.dumps(text)[1:-1]  # JSON 安全编码，自动转义引号/换行/反斜杠
        js = f"""(function(){{
            var o=document.getElementById('{oid}');
            if(!o){{o=document.createElement('div');o.id='{oid}';
                Object.assign(o.style,{{position:'fixed',top:'50%',left:'50%',
                transform:'translate(-50%,-50%)',background:'rgba(20,20,30,0.92)',
                color:'#fff',padding:'10px 28px',borderRadius:'12px',
                fontSize:'20px',fontFamily:'微软雅黑,sans-serif',
                zIndex:2147483647,pointerEvents:'none',
                border:'1px solid rgba(137,180,250,0.5)',
                boxShadow:'0 4px 24px rgba(0,0,0,0.6)',
                textAlign:'center',minWidth:'120px'}});
                var fs=document.fullscreenElement||document.webkitFullscreenElement;
                (fs||document.body).appendChild(o);}}
            else{{
                var fs=document.fullscreenElement||document.webkitFullscreenElement;
                var parent=fs||document.body;
                if(o.parentNode!==parent)parent.appendChild(o);
            }}
            o.textContent='{esc_text}';o.style.opacity='1';
            clearTimeout(o._t);o._t=setTimeout(function(){{o.style.opacity='0';}},1000);
        }})();"""
        self._js_safe(js)

    # ---- 窗口透明度（Windows API） ----

    def _get_hwnd(self):
        """获取 pywebview 窗口的 HWND（带缓存，多方式兜底）。"""
        if self._hwnd and ctypes.windll.user32.IsWindow(self._hwnd):
            return self._hwnd
        if not self.window:
            return None
        # 方式1：pywebview 5.3+ native.Handle
        try:
            native = self.window.native
            if native:
                for hattr in ("Handle", "handle", "hwnd", "native"):
                    hwnd = getattr(native, hattr, None)
                    if hwnd and isinstance(hwnd, int) and hwnd > 0:
                        self._hwnd = hwnd
                        return hwnd
        except Exception:
            pass
        # 方式2：pywebview 内部属性（旧版本）
        for attr in ("_gui", "gui", "_window"):
            obj = getattr(self.window, attr, None)
            if obj:
                for hattr in ("hwnd", "handle", "native"):
                    hwnd = getattr(obj, hattr, None)
                    if hwnd and isinstance(hwnd, int) and hwnd > 0:
                        self._hwnd = hwnd
                        return hwnd
        # 方式3：FindWindowW 按标题精确查找
        try:
            hwnd = ctypes.windll.user32.FindWindowW(None, "跟跑助手")
            if hwnd:
                self._hwnd = hwnd
                return hwnd
        except Exception:
            pass
        # 方式4：EnumWindows 枚举所有窗口模糊匹配。
        # 注意必须排除自身辅助窗口："跟跑助手 · 设置" / "跟跑助手 - 等待中"
        # / "跟跑助手 - 无响应" 都含"跟跑助手"，一旦被认成主窗口，
        # 透明度、鼠标穿透、窗口几何都会打到错误的窗口上。
        # 多个候选时取面积最大的（主窗口），避免 result[0] 的随机性。
        result = []
        EnumWindowsProc = ctypes.WINFUNCTYPE(
            ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM
        )
        def cb(hwnd, _):
            buf = ctypes.create_unicode_buffer(256)
            ctypes.windll.user32.GetWindowTextW(hwnd, buf, 256)
            title = buf.value
            if "跟跑助手" in title and not any(m in title for m in _AUX_WINDOW_TITLE_MARKS):
                rect = self._get_window_rect(hwnd)
                area = (rect[2] * rect[3]) if rect else 0
                result.append((area, hwnd))
            return True
        ctypes.windll.user32.EnumWindows(EnumWindowsProc(cb), 0)
        if result:
            result.sort(reverse=True)
            self._hwnd = result[0][1]
            return self._hwnd
        return None

    def _get_window_placement(self):
        """读取窗口普通位置、大小和最大化状态，用于下次启动恢复。"""
        hwnd = self._get_hwnd()
        if not hwnd:
            return None

        class POINT(ctypes.Structure):
            _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

        class RECT(ctypes.Structure):
            _fields_ = [
                ("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long),
            ]

        class WINDOWPLACEMENT(ctypes.Structure):
            _fields_ = [
                ("length", ctypes.c_uint), ("flags", ctypes.c_uint),
                ("showCmd", ctypes.c_uint), ("ptMinPosition", POINT),
                ("ptMaxPosition", POINT), ("rcNormalPosition", RECT),
            ]

        placement = WINDOWPLACEMENT()
        placement.length = ctypes.sizeof(WINDOWPLACEMENT)
        if not ctypes.windll.user32.GetWindowPlacement(hwnd, ctypes.byref(placement)):
            return None
        rect = placement.rcNormalPosition
        width = max(400, rect.right - rect.left)
        height = max(300, rect.bottom - rect.top)
        return {
            "x": int(rect.left), "y": int(rect.top),
            "width": int(width), "height": int(height),
            "maximized": placement.showCmd == 3,
        }

    def _save_window_state(self):
        state = self._get_window_placement()
        if not state:
            return
        self.config["window_state"] = state
        self.config["width"] = state["width"]
        self.config["height"] = state["height"]
        self._write_config(self.config)
        _logd(f"窗口状态已保存: {state}")

    def _get_window_rect(self, hwnd):
        """返回窗口当前 (x, y, width, height)，失败返回 None。"""
        class RECT(ctypes.Structure):
            _fields_ = [
                ("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long),
            ]
        rect = RECT()
        if not ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return None
        return (int(rect.left), int(rect.top),
                int(rect.right - rect.left), int(rect.bottom - rect.top))

    def _restore_window_state(self):
        """应用窗口几何，并保证 WebView2 收到一次真实的尺寸变化。

        为什么"没有保存状态也要动一次窗口"：WebView2 的渲染表面是跟随窗口
        尺寸建立的，若窗口显示后从未收到过尺寸变化，表面会停在创建时的旧
        尺寸，整窗只剩默认底色（白屏），而页面其实已加载、JS 与热键都正常，
        表现出来就是"白屏卡死"。window_state 为空（首次启动、配置被重置）
        恰好会跳过这一步，所以首次打开最容易白屏。因此这里不再提前 return：
        没有保存状态就沿用当前位置与配置尺寸，尺寸确实没变时额外做一次
        2px 抖动，强制 WebView2 重建渲染表面。
        """
        hwnd = self._get_hwnd()
        if not hwnd:
            return
        state = self.config.get("window_state")
        try:
            cur = self._get_window_rect(hwnd)
            if isinstance(state, dict):
                x = int(state.get("x", cur[0] if cur else 100))
                y = int(state.get("y", cur[1] if cur else 100))
                width = max(400, int(state.get("width", self.config.get("width", 1280))))
                height = max(300, int(state.get("height", self.config.get("height", 720))))
                maximized = bool(state.get("maximized"))
            else:
                # 首次启动：保持当前位置，套用配置里的默认尺寸
                x, y = (cur[0], cur[1]) if cur else (100, 100)
                width = max(400, int(self.config.get("width", DEFAULT_CONFIG["width"])))
                height = max(300, int(self.config.get("height", DEFAULT_CONFIG["height"])))
                maximized = False
                _logd("无窗口状态（首次启动），按默认尺寸应用几何")
            if self._lock_ratio and not maximized:
                width, height = self._ratio_size(width, height)
            ctypes.windll.user32.MoveWindow(hwnd, x, y, width, height, True)
            if cur and (width, height) == (cur[2], cur[3]):
                # 尺寸没变时 WebView2 不会重建渲染表面，抖动 2px 强制刷新
                ctypes.windll.user32.MoveWindow(hwnd, x, y, width, height + 2, True)
                ctypes.windll.user32.MoveWindow(hwnd, x, y, width, height, True)
                _logd("窗口尺寸未变化，已做 2px 抖动以强制刷新 WebView2 表面")
            if maximized:
                ctypes.windll.user32.ShowWindow(hwnd, 3)  # SW_MAXIMIZE
            _logd(f"窗口几何已应用: x={x} y={y} {width}x{height}"
                  + (f"（按16:9修正）" if self._lock_ratio and not maximized else ""))
        except Exception as e:
            _logd(f"_restore_window_state error: {e}")

    # ---- 16:9 比例锁定（resized 事件校正法，不碰 Win32 WndProc） ----

    def _ratio_size(self, w, h):
        """把窗口尺寸修正为 16:9（以宽为基准），最小不低于 533x300。

        533x300 是 16:9 且满足原窗口 400x300 下限的最小尺寸。
        """
        MINW, MINH = 533, 300
        RATIO = 16.0 / 9.0
        w = max(int(w), MINW)
        nh = int(round(w / RATIO))
        if nh < MINH:
            nh = MINH
            w = int(round(nh * RATIO))
        return w, nh

    def _on_window_resized(self, width, height):
        """窗口尺寸变化后，防抖 300ms 再按 16:9 校正（安全方案）。

        不实时校正（resize 会再触发 resized 形成高频振荡），而是：
        每次 resized 记录最新尺寸并重置定时器 → 用户停止拖动 300ms 后
        校正一次，实现"松手吸附 16:9"。全程用 Win32 MoveWindow（线程安全），
        完全不碰 WndProc 消息链，WebView2 渲染不受影响。
        """
        if not self._lock_ratio or not self.window:
            return
        self._pending_size = (int(width), int(height))
        if self._fix_timer:
            try:
                self._fix_timer.cancel()
            except Exception:
                pass
        self._fix_timer = threading.Timer(0.3, self._do_fix_ratio)
        self._fix_timer.daemon = True
        self._fix_timer.start()

    def _do_fix_ratio(self):
        """防抖到期后执行校正。Timer 线程调用，只用 Win32 API（线程安全）。"""
        if not self._lock_ratio or not self.window:
            return
        try:
            w, h = self._pending_size or (0, 0)
            expected_h = int(round(w * 9 / 16))
            if abs(h - expected_h) <= 3 or expected_h < 300:
                return  # 已在 16:9 容差内
            hwnd = self._get_hwnd()
            if not hwnd:
                return
            if ctypes.windll.user32.IsZoomed(hwnd):
                return  # 最大化不校正
            class _R(ctypes.Structure):
                _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                            ("right", ctypes.c_long), ("bottom", ctypes.c_long)]
            rect = _R()
            if not ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                return
            ctypes.windll.user32.MoveWindow(
                hwnd, rect.left, rect.top, w, expected_h, True
            )
            _log(f"16:9 校正(松手吸附): {w}x{h} -> {w}x{expected_h}")
        except Exception as e:
            _logd(f"_do_fix_ratio error: {e}")

    def _fit_ratio_now(self):
        """把当前窗口立即调整为 16:9（保留左上角位置；最大化时不处理）。"""
        hwnd = self._get_hwnd()
        if not hwnd:
            return
        user32 = ctypes.windll.user32
        class _R(ctypes.Structure):
            _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                        ("right", ctypes.c_long), ("bottom", ctypes.c_long)]
        rect = _R()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return
        if user32.IsZoomed(hwnd):
            return  # 最大化状态不调整
        w = rect.right - rect.left
        h = rect.bottom - rect.top
        nw, nh = self._ratio_size(w, h)
        if (nw, nh) != (w, h):
            user32.MoveWindow(hwnd, rect.left, rect.top, nw, nh, True)
            _log(f"已按 16:9 调整当前窗口: {w}x{h} -> {nw}x{nh}")

    def _set_window_alpha(self, ratio):
        """设置窗口整体透明度 0.0~1.0（SetLayeredWindowAttributes）。"""
        hwnd = self._get_hwnd()
        if not hwnd:
            return
        GWL_EXSTYLE = -20
        WS_EX_LAYERED = 0x00080000
        ex_style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        if not (ex_style & WS_EX_LAYERED):
            ctypes.windll.user32.SetWindowLongW(
                hwnd, GWL_EXSTYLE, ex_style | WS_EX_LAYERED
            )
        alpha = max(0, min(255, int(ratio * 255)))
        ctypes.windll.user32.SetLayeredWindowAttributes(hwnd, 0, alpha, 2)

    def _set_click_through(self, enabled):
        """设置窗口穿透。WS_EX_TRANSPARENT 需要有 WS_EX_LAYERED 配
        合才能在焦点状态下生效，否则点击会被窗口吞掉。"""
        hwnd = self._get_hwnd()
        if not hwnd:
            return
        GWL_EXSTYLE = -20
        WS_EX_LAYERED = 0x00080000
        ex_style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        if enabled:
            ex_style |= WS_EX_TRANSPARENT | WS_EX_LAYERED
        else:
            ex_style &= ~WS_EX_TRANSPARENT
        ctypes.windll.user32.SetWindowLongW(hwnd, GWL_EXSTYLE, ex_style)
        ctypes.windll.user32.SetWindowPos(
            hwnd, 0, 0, 0, 0, 0,
            0x0001 | 0x0002 | 0x0004 | 0x0020  # NOSIZE|NOMOVE|NOZORDER|FRAMECHANGED
        )

    def _toggle_click_through(self):
        """切换鼠标穿透状态。"""
        self._click_through = not self._click_through
        self._set_click_through(self._click_through)
        status = "开启" if self._click_through else "关闭"
        self._osd(f"鼠标穿透：{status}")
        _log(f"鼠标穿透 {status}")
        # 开启穿透时把焦点推回桌面，让透传立即生效
        if self._click_through:
            # 用多种方式强制失焦
            ctypes.windll.user32.AllowSetForegroundWindow(0xFFFFFFFF)
            ctypes.windll.user32.SetForegroundWindow(
                ctypes.windll.user32.GetDesktopWindow()
            )
            hwnd = self._get_hwnd()
            if hwnd:
                ctypes.windll.user32.SendMessageW(hwnd, 0x0008, 0, 0)  # WM_KILLFOCUS
        self._update_clickthrough_ui()

    def _update_clickthrough_ui(self):
        """同步左侧边栏穿透按钮的颜色状态。"""
        if not self.window:
            return
        color = "#a6e3a1" if self._click_through else "#f38ba8"  # 绿=穿透，红=关闭
        try:
            self.window.evaluate_js(f"""(function(){{
                var items=document.querySelectorAll('.gt-leftbar-item[data-action=\"toggle_click_through\"]');
                items.forEach(function(btn){{
                    btn.style.color='{color}';
                    if({str(self._click_through).lower()}){{
                        btn.classList.add('gt-clickthrough-active');
                    }}else{{
                        btn.classList.remove('gt-clickthrough-active');
                    }}
                }});
            }})();""")
        except Exception as e:
            _logd(f"_update_clickthrough_ui error: {e}")

    def _adj_opacity(self, direction):
        if not self.window:
            return
        self.opacity_index = (self.opacity_index + direction) % len(self.opacity_levels)
        self.current_opacity = self.opacity_levels[self.opacity_index]
        self._set_window_alpha(self.current_opacity)
        pct = int(self.current_opacity * 100)
        self._osd(f"透明度：{pct}%")

    def _navigate(self, url):
        if not self.window:
            return
        try:
            self.window.load_url(url)
            _log(f"跳转: {url}")
        except Exception as e:
            _logd(f"_navigate error: {e}")

    def _quit(self):
        self._shutting_down = True
        self._seek_accum = 0
        self._seek_flush_scheduled = None
        global _hotkey_manager
        if _hotkey_manager:
            try:
                _hotkey_manager.stop()
            except Exception:
                pass
        if self.window:
            try:
                self._save_window_state()
                self.window.destroy()
            except Exception:
                pass

    def _on_closing(self):
        self._save_window_state()

    def _inject_js(self, *args):
        if not self.window:
            return
        try:
            self.window.evaluate_js("""(function(){
                if(window.__gt_injected)return;
                window.__gt_injected=true;
                document.addEventListener('click',function(e){
                    var a=e.target.closest('a');
                    if(a&&a.href&&!a.href.startsWith('#')&&!a.href.startsWith('mailto:')){
                        e.preventDefault();window.location.href=a.href;
                    }},true);
                window.open=function(u){if(u){window.location.href=u;return null;}return null;};
                // ===== 默认倍速保护：视频加载时自动重置为 1.0x =====
                function resetVideoSpeed(){
                    var vs=document.querySelectorAll('video');
                    for(var i=0;i<vs.length;i++){
                        var v=vs[i];
                        if(v.playbackRate!==1.0){
                            v.playbackRate=1.0;
                            //console.log('[跟跑浏览器] 倍速已重置为 1.0x');
                        }
                    }
                }
                resetVideoSpeed();
                // 监听新视频元素和播放状态变化
                var obs=new MutationObserver(function(){resetVideoSpeed();});
                obs.observe(document.body,{childList:true,subtree:true});
                setInterval(resetVideoSpeed,3000);
                document.addEventListener('loadedmetadata',function(e){
                    if(e.target.tagName==='VIDEO')resetVideoSpeed();
                },true);
            })();""")
            _logd("_inject_js 成功（含默认倍速保护）")
        except Exception as e:
            _logd(f"_inject_js error: {e}")

    def _on_new_window(self, url):
        _log(f"new_window 拦截: {url}")
        try:
            self.window.load_url(url)
        except Exception:
            pass
        return False

    def _on_loaded(self):
        """页面加载完成后重新注入横条。"""
        if hasattr(self, '_page_timer') and self._page_timer:
            self._page_timer.cancel()
            self._page_timer = None

        # 此时页面已经是主页，直接注入即可
        self._inject_js()
        self._inject_bar()
        _logd("页面加载完成，横条已重新注入")

    def start(self):
        data_dir = self.get_data_dir()
        if not os.path.exists(data_dir):
            os.makedirs(data_dir)
        # 注：WEBVIEW2_USER_DATA_FOLDER 已在模块顶部（import webview 之前）
        # 统一指向 %LOCALAPPDATA%\GenPaoBrowser_Data，这里不再覆盖；
        # 也不再加 WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS 强制 GPU 加速
        # （部分显卡驱动下会导致 exe 内 WebView2 渲染挂起 → 黑屏）。
        _log(f"WebView2 数据目录: {os.environ.get('WEBVIEW2_USER_DATA_FOLDER')}")

        js_api = JsApi()
        try:
            # 直接获取主页 URL
            _homepage = self.config.get("homepage", DEFAULT_CONFIG["homepage"])

            # 默认窗口尺寸：无保存状态且锁定比例时，按 16:9 兜底
            _init_w = int(self.config.get("width", DEFAULT_CONFIG["width"]))
            _init_h = int(self.config.get("height", DEFAULT_CONFIG["height"]))
            if self._lock_ratio and not self.config.get("window_state"):
                _init_w, _init_h = self._ratio_size(_init_w, _init_h)

            # 直接传入目标网址，使用深色背景替代加载页（防止白屏闪烁）
            self.window = pywebview.create_window(
                "跟跑助手",
                url=_homepage,              # 直接传入目标网址
                background_color='#1e1e2e', # 设置深色背景，防止白屏闪烁
                width=_init_w,
                height=_init_h,
                min_size=(400, 300),
                on_top=True,                # 置顶：构造参数在 Form 初始化(GUI线程)设 TopMost，安全且持久
                js_api=js_api,
            )
        except Exception as e:
            _log(f"创建 WebView2 窗口失败: {e}")
            _messagebox_w("启动失败",
                f"无法创建 WebView2 窗口。\n请确保已安装 WebView2 Runtime。\n\n错误: {e}",
                MB_OK | MB_ICONERROR | MB_TOPMOST)
            sys.exit(1)
        try:
            self.window.events.new_window += self._on_new_window
        except AttributeError:
            _log("当前 pywebview 版本不支持 new_window 事件")
        self.window.events.loaded += self._on_loaded
        try:
            # 16:9 锁比：监听窗口尺寸变化后校正（安全方案，不碰 WndProc）
            self.window.events.resized += self._on_window_resized
        except AttributeError:
            _log("当前 pywebview 版本不支持 resized 事件，16:9 拖动锁比不可用")
        try:
            self.window.events.closing += self._on_closing
        except AttributeError:
            _log("当前 pywebview 版本不支持 closing 事件")

        # 绑定 shown 事件：强制任务栏显示图标
        def _on_shown():
            """窗口显示后，强制在任务栏显示图标（解决无边框窗口不显示任务栏的问题）"""
            _log("窗口 shown 事件触发，尝试强制任务栏显示")
            try:
                hwnd = self._get_hwnd()
                if hwnd:
                    user32 = ctypes.windll.user32
                    GWL_EXSTYLE = -20
                    WS_EX_APPWINDOW = 0x00040000
                    WS_EX_TOOLWINDOW = 0x00000080

                    style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
                    # 移除 ToolWindow，添加 AppWindow
                    style = (style & ~WS_EX_TOOLWINDOW) | WS_EX_APPWINDOW
                    user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)

                    # 修改扩展样式后，必须调用 SetWindowPos 触发重绘（FRAMECHANGED），否则WebView2必定白屏或假死
                    user32.SetWindowPos(
                        hwnd, 0, 0, 0, 0, 0,
                        0x0001 | 0x0002 | 0x0004 | 0x0020  # NOSIZE|NOMOVE|NOZORDER|FRAMECHANGED
                    )
                    _log("已强制添加 WS_EX_APPWINDOW 样式并刷新帧，任务栏应显示图标")
            except Exception as e:
                _logd(f"强制任务栏显示失败: {e}")

        try:
            self.window.events.shown += _on_shown
        except AttributeError:
            _log("当前 pywebview 版本不支持 shown 事件")

        _start_timeout = [None]  # 闭包可写引用

        def _on_start_timeout():
            """pywebview.start 超过 20 秒仍未调用 on_start → 白屏超时保护"""
            _log("启动超时：WebView2 窗口初始化超过 20 秒")
            _messagebox_w("启动超时",
                "跟跑助手启动时间过长。\n\n"
                "可能原因：\n"
                "1. WebView2 Runtime 未安装或损坏\n"
                "2. 网络连接问题\n"
                "3. 系统资源不足\n\n"
                "请检查后重试。",
                MB_OK | MB_ICONWARNING | MB_TOPMOST)
            os._exit(1)

        def on_start():
            # 取消启动超时定时器
            if _start_timeout[0]:
                _start_timeout[0].cancel()
                _start_timeout[0] = None

            _log("on_start 回调已触发")

            # 等待 HWND 出现（最多 2 秒）
            hwnd = None
            for attempt in range(20):  # 20 * 100ms = 2s
                hwnd = self._get_hwnd()
                if hwnd:
                    _log(f"窗口 HWND 已获取: 0x{hwnd:X}（尝试 {attempt+1} 次）")
                    break
                time.sleep(0.1)

            if not hwnd:
                _log("警告：获取 HWND 失败，但继续运行（依赖 loaded 事件）")

            # 标记窗口就绪（非 JS 操作可用）
            self._window_ready = True
            _log("窗口已就绪 (_window_ready = True)")

            # 恢复窗口状态（锁定比例开启时按 16:9 修正尺寸）
            self._restore_window_state()

            # 置顶：已通过 create_window(on_top=True) 在 Form 初始化(GUI线程)设置，
            # 这里不再用 Win32 SetWindowPos 或 window.on_top（后者跨线程会死锁，
            # 前者设置的 WS_EX_TOPMOST 会被 .NET 按 TopMost=False 清掉）。

            # 重放缓存中的非 JS 操作（穿透/透明度）
            with self._pending_lock:
                pending = self._pending_actions[:]
                self._pending_actions.clear()
            for action in pending:
                if action in ("adjust_opacity_up", "adjust_opacity_down",
                              "toggle_click_through"):
                    _log(f"重放（HWND 就绪）: {action}")
                    self._dispatch(action)

            # 跳转已移至 _on_loaded，等待深色加载页完全渲染后再跳转
            _log("on_start 完成（跳转已移至 _on_loaded）")

        # 启动超时保护：20 秒后如果 on_start 还没被调用则提示并退出
        _start_timeout[0] = threading.Timer(20.0, _on_start_timeout)
        _start_timeout[0].daemon = True
        _start_timeout[0].start()

        _log("=" * 40)
        _log(f"启动 | 热键：{self.config.get('hotkeys')}")
        try:
            # private_mode=False + storage_path：修复打包后关闭崩溃（pywebview 5.4 的
            # clear_user_data 在 private_mode=True 时访问已释放的 CoreWebView2 崩溃），
            # 同时让 cookies/登录状态持久化、数据目录固定在项目 data。
            pywebview.start(
                on_start,
                private_mode=False,
                storage_path=_WB2_DATA_DIR,
            )
        except Exception as e:
            # 取消超时定时器（如果有的话）
            if _start_timeout[0]:
                _start_timeout[0].cancel()
            _log(f"WebView2 启动失败: {e}")
            _messagebox_w("启动失败",
                f"WebView2 启动时发生错误。\n请确保已安装 WebView2 Runtime。\n\n错误: {e}",
                MB_OK | MB_ICONERROR | MB_TOPMOST)
            sys.exit(1)


# ====================== JS API（暴露给网页） ======================

class JsApi:
    def open_settings(self):
        _action_queue.put("open_settings")

    def quit_app(self):
        _action_queue.put("quit")

    def trigger_action(self, action_name):
        if _action_queue.qsize() > 50:
            return
        if action_name == "show_settings":
            action_name = "open_settings"
        _action_queue.put(action_name)

    def navigate(self, url):
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            _log(f"navigate 拒绝非 HTTP URL: {url!r}")
            return
        _action_queue.put(("navigate", url))


# ====================== Tkinter 子线程 ======================


def tkinter_thread(app):
    # 初始化 COM 为 STA，与 WebView2 的 COM 线程模型兼容
    COINIT_APARTMENTTHREADED = 2
    try:
        ctypes.windll.ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED)
    except Exception:
        pass
    root = tk.Tk()
    root.withdraw()
    settings_win = None
    _last_open_settings = [0.0]  # 防抖：记录上次 open_settings 的时间戳

    # 超时退出按钮：如果窗口长时间未就绪，显示一个退出按钮
    _timeout_btn = [None]
    _window_check_count = [0]

    def _poll():
        nonlocal settings_win

        # 检测窗口是否长时间未就绪，显示退出按钮
        if not app._window_ready:
            _window_check_count[0] += 1
            # 10 秒后显示退出按钮（25ms * 400 = 10s）
            if _window_check_count[0] == 400 and _timeout_btn[0] is None:
                _log("窗口初始化超时，显示退出按钮")
                try:
                    # 创建一个小型退出窗口
                    _timeout_win = tk.Toplevel(root)
                    _timeout_win.title("跟跑助手 - 等待中")
                    _timeout_win.geometry("300x100")
                    _timeout_win.attributes("-topmost", True)
                    _timeout_win.configure(bg="#1e1e2e")
                    _timeout_win.protocol("WM_DELETE_WINDOW", lambda: None)  # 禁止关闭

                    tk.Label(_timeout_win, text="窗口初始化中...",
                             bg="#1e1e2e", fg="#cdd6f4",
                             font=("微软雅黑", 10)).pack(pady=(15, 10))

                    def _force_exit():
                        _log("用户点击强制退出按钮")
                        try:
                            app._quit()
                        except:
                            pass
                        os._exit(1)

                    _btn = tk.Button(_timeout_win, text="强制退出",
                                     command=_force_exit,
                                     bg="#f38ba8", fg="#1e1e2e",
                                     font=("微软雅黑", 10, "bold"),
                                     padx=20, pady=5)
                    _btn.pack(pady=5)
                    _timeout_btn[0] = _timeout_win
                except Exception as e:
                    _logd(f"创建退出按钮失败: {e}")

        # 窗口就绪后关闭退出按钮
        if app._window_ready and _timeout_btn[0] is not None:
            try:
                _timeout_btn[0].destroy()
            except:
                pass
            _timeout_btn[0] = None

        # 检测窗口是否无响应（僵尸状态）
        if app._window_ready and app._window_alive():
            try:
                # 检查窗口是否响应（通过 SendMessage 发送 WM_NULL）
                hwnd = app._get_hwnd()
                if hwnd:
                    # 使用 SendMessageTimeout 检测窗口响应
                    SMTO_ABORTIFHUNG = 0x0002
                    result = ctypes.windll.user32.SendMessageTimeoutW(
                        hwnd, 0x0000, 0, 0, SMTO_ABORTIFHUNG, 1000, None
                    )
                    if result == 0:
                        # 窗口无响应
                        _log("检测到窗口无响应，可能已卡死")
                        if _timeout_btn[0] is None:
                            try:
                                _timeout_win = tk.Toplevel(root)
                                _timeout_win.title("跟跑助手 - 无响应")
                                _timeout_win.geometry("300x100")
                                _timeout_win.attributes("-topmost", True)
                                _timeout_win.configure(bg="#1e1e2e")
                                _timeout_win.protocol("WM_DELETE_WINDOW", lambda: None)

                                tk.Label(_timeout_win, text="窗口无响应",
                                         bg="#1e1e2e", fg="#f38ba8",
                                         font=("微软雅黑", 10)).pack(pady=(15, 10))

                                def _force_exit2():
                                    _log("用户点击强制退出按钮（窗口无响应）")
                                    os._exit(1)

                                _btn = tk.Button(_timeout_win, text="强制退出",
                                                 command=_force_exit2,
                                                 bg="#f38ba8", fg="#1e1e2e",
                                                 font=("微软雅黑", 10, "bold"),
                                                 padx=20, pady=5)
                                _btn.pack(pady=5)
                                _timeout_btn[0] = _timeout_win
                            except Exception as e:
                                _logd(f"创建退出按钮失败: {e}")
            except Exception as e:
                _logd(f"检测窗口响应失败: {e}")

        # 主动探测 HWND：一旦 pywebview 创建窗口就尝试获取，不等 on_start
        if not app._window_ready and app.window:
            hwnd = app._get_hwnd()
            if hwnd and app._pending_actions:
                pending_now = []
                with app._pending_lock:
                    pending_now = [a for a in app._pending_actions
                                   if a in ("adjust_opacity_up", "adjust_opacity_down",
                                            "toggle_click_through")]
                    app._pending_actions = [a for a in app._pending_actions
                                            if a not in pending_now]
                for a in pending_now:
                    _log(f"HWND 就绪，提前执行: {a}")
                    app._dispatch(a)
        # 处理热键队列
        processed = 0
        while processed < 20:
            try:
                action = _action_queue.get_nowait()
            except queue.Empty:
                break
            processed += 1
            try:
                if action == "open_settings":
                    # 防抖：1 秒内重复触发（webview 双击/事件重复派发）直接忽略
                    _now = time.time()
                    if _now - _last_open_settings[0] < 1.0:
                        _logd("设置窗口触发过于频繁，忽略（防抖）")
                        continue
                    _last_open_settings[0] = _now
                    if settings_win is not None:
                        try:
                            if settings_win.winfo_exists():
                                settings_win.lift()
                                settings_win.focus_force()
                                continue
                        except Exception:
                            settings_win = None
                    # 双路径兜底清理：销毁所有"跟跑助手 · 设置"窗口（保留 keep_win）
                    # 路径 1: tk 优雅销毁；路径 2: Win32 DestroyWindow 强制销毁
                    # （tk 状态混乱时 title()/destroy() 抛异常，Win32 路径仍能清理）
                    _purged = _purge_settings_windows(settings_win, root)
                    if _purged:
                        _log(f"已清 {_purged} 个跟跑助手 · 设置 窗口")
                    if app.is_settings_opened:
                        _logd("tkinter: 设置窗口已打开，跳过本次创建")
                        continue
                    app.is_settings_opened = True
                    try:
                        settings_win = _build_settings(
                            root, app,
                            lambda: _action_queue.put("close_settings")
                        )
                    except Exception as e:
                        # _build_settings 可能在 widget 创建中途抛异常，但 Toplevel
                        # 已经创建出来——必须销毁并清状态，避免留下孤儿窗口
                        _log(f"创建设置窗口失败: {e}")
                        app.is_settings_opened = False
                        settings_win = None
                        raise
                elif action == "close_settings":
                    app.is_settings_opened = False
                    if settings_win is not None:
                        try:
                            if settings_win.winfo_exists():
                                settings_win.destroy()
                        except Exception:
                            pass
                        settings_win = None
                elif action == "quit":
                    app._quit()
                    try:
                        root.quit()
                    except Exception:
                        pass
                elif isinstance(action, tuple) and action[0] == "navigate":
                    app._navigate(action[1])
                else:
                    app._dispatch(action)
            except Exception as e:
                _log(f"_poll 处理 action 异常: {e}")
        # 检查 seek 合并是否到期（在 tkinter 线程执行，避免在 Timer 线程调 evaluate_js）
        if app._seek_flush_scheduled and time.time() >= app._seek_flush_scheduled:
            app._flush_seek()
        root.after(25, _poll)

    root.after(25, _poll)
    root.mainloop()


# ====================== 热键录制组件（pynput，仅在 Tk 设置窗口内使用） ======================

class HotkeyRecorder(tk.Frame):
    MOD_MAP = {
        "alt_l": "alt", "alt_r": "alt", "alt": "alt",
        "ctrl_l": "ctrl", "ctrl_r": "ctrl", "ctrl": "ctrl",
        "shift_l": "shift", "shift_r": "shift", "shift": "shift",
        "cmd_l": "win", "cmd_r": "win", "cmd": "win",
    }
    VK_MAP = {
        0x20: "space", 0x0D: "return", 0x08: "backspace", 0x09: "tab", 0x1B: "escape",
        0x2E: "delete", 0x21: "pageup", 0x22: "pagedown", 0x23: "end", 0x24: "home",
        0x25: "left", 0x26: "up", 0x27: "right", 0x28: "down", 0x2D: "insert",
        0x30: "0", 0x31: "1", 0x32: "2", 0x33: "3", 0x34: "4",
        0x35: "5", 0x36: "6", 0x37: "7", 0x38: "8", 0x39: "9",
        0x41: "a", 0x42: "b", 0x43: "c", 0x44: "d", 0x45: "e",
        0x46: "f", 0x47: "g", 0x48: "h", 0x49: "i", 0x4A: "j",
        0x4B: "k", 0x4C: "l", 0x4D: "m", 0x4E: "n", 0x4F: "o",
        0x50: "p", 0x51: "q", 0x52: "r", 0x53: "s", 0x54: "t",
        0x55: "u", 0x56: "v", 0x57: "w", 0x58: "x", 0x59: "y", 0x5A: "z",
        0x70: "f1", 0x71: "f2", 0x72: "f3", 0x73: "f4", 0x74: "f5", 0x75: "f6",
        0x76: "f7", 0x77: "f8", 0x78: "f9", 0x79: "f10", 0x7A: "f11", 0x7B: "f12",
        0xBD: "-", 0xBB: "=", 0xDB: "[", 0xDD: "]", 0xDC: "\\\\",
        0xBA: ";", 0xDE: "'", 0xBC: ",", 0xBE: ".", 0xBF: "/", 0xC0: "`",
        0x6B: "+", 0x6D: "-",  # numpad +/-
    }

    def __init__(self, parent, initial_value="", width=18):
        super().__init__(parent, bg="#1e1e2e")
        self.value    = tk.StringVar(value=initial_value)
        self._original   = initial_value
        self._recording  = False
        self._pressed    = set()
        self._listener   = None
        self._timeout_id = None
        self._win_ref    = None

        self.entry = tk.Entry(
            self, textvariable=self.value, width=width, state="readonly",
            readonlybackground="#313244", foreground="#cdd6f4", insertbackground="#cdd6f4",
            relief="flat", highlightthickness=1, highlightbackground="#45475a",
            highlightcolor="#89b4fa",
            font=("微软雅黑", 10), justify="center"
        )
        self.entry.pack(side="left")
        self.entry.bind("<Double-Button-1>", self._enable_manual_edit)

        self.btn = tk.Button(
            self, text="修改", command=self._toggle_record,
            bg="#45475a", fg="#cdd6f4",
            activebackground="#585b70", activeforeground="#cdd6f4",
            font=("微软雅黑", 10), cursor="hand2",
            relief="flat", borderwidth=0, padx=10, pady=2
        )
        self.btn.pack(side="left", padx=(6, 0))

    def set_window_ref(self, win):
        self._win_ref = win

    def _key_name(self, key):
        name = getattr(key, "name", str(key)).lower()
        if name in self.MOD_MAP:
            return self.MOD_MAP[name]
        try:
            vk = getattr(key, "vk", None)
        except AttributeError:
            pass
        if hasattr(key, "vk") and key.vk is not None:
            nk = self._numpad_map.get(key.vk)
            if nk:
                return nk
            return self.VK_MAP.get(key.vk, f"vk{key.vk}")
        return name.lower()

    _numpad_map = {
        0x60: "numpad_0", 0x61: "numpad_1", 0x62: "numpad_2", 0x63: "numpad_3",
        0x64: "numpad_4", 0x65: "numpad_5", 0x66: "numpad_6", 0x67: "numpad_7",
        0x68: "numpad_8", 0x69: "numpad_9",
        0x6A: "numpad_multiply", 0x6B: "numpad_plus", 0x6D: "numpad_minus",
        0x6E: "numpad_decimal", 0x6F: "numpad_divide",
    }

    def _enable_manual_edit(self, event=None):
        self._do_stop()
        self.entry.config(state="normal", foreground="#a6e3a1")
        self.entry.focus_set()
        self.entry.select_range(0, "end")
        self.btn.config(text="确定", command=self._confirm_manual)

    def _confirm_manual(self):
        self._original = self.value.get().strip().lower()
        self.entry.config(state="readonly", foreground="#cdd6f4")
        self.btn.config(text="修改", command=self._toggle_record)

    def _toggle_record(self):
        if self._recording:
            self._do_stop()
        else:
            self._start_record()

    def _start_record(self):
        self._recording = True
        self._pressed.clear()
        self._original = self.value.get()
        self.value.set("请按快捷键...")
        self.entry.config(readonlybackground="#313244", fg="#89b4fa")
        self.btn.config(text="取消", bg="#f38ba8", fg="#1e1e2e")
        self._listener = keyboard.Listener(
            on_press=self._on_keypress, on_release=self._on_keyrelease,
            suppress=False, daemon=True
        )
        self._listener.start()
        if self._win_ref and self._win_ref.winfo_exists():
            self._timeout_id = self._win_ref.after(10000, self._do_stop)

    def _do_stop(self):
        if not self._recording:
            return
        self._recording = False
        if self._timeout_id and self._win_ref and self._win_ref.winfo_exists():
            self._win_ref.after_cancel(self._timeout_id)
            self._timeout_id = None
        if self._listener:
            self._listener.stop()
            self._listener = None
        self.value.set(self._original)
        self.entry.config(readonlybackground="#313244", fg="#cdd6f4")
        self.btn.config(text="修改", bg="#45475a", fg="#cdd6f4")

    def _finish(self, combo):
        if not self._recording:
            return
        self._original = combo
        self.value.set(combo)
        self._recording = False
        if self._timeout_id and self._win_ref and self._win_ref.winfo_exists():
            self._win_ref.after_cancel(self._timeout_id)
            self._timeout_id = None
        if self._listener:
            self._listener.stop()
            self._listener = None
        if self._win_ref and self._win_ref.winfo_exists():
            self._win_ref.after(0, self._update_ui_after_record)

    def _update_ui_after_record(self):
        self.entry.config(readonlybackground="#313244", fg="#cdd6f4")
        self.btn.config(text="修改", bg="#45475a", fg="#cdd6f4")

    def _on_keypress(self, key):
        if not self._recording:
            return
        k = self._key_name(key)
        if not k:
            return
        self._pressed.add(k)
        combo = self._format_combo(self._pressed)
        if combo and self._win_ref and self._win_ref.winfo_exists():
            self._win_ref.after(0, lambda c=combo: self.value.set(c))

    def _on_keyrelease(self, key):
        if not self._recording:
            return
        k = self._key_name(key)
        if k in self._pressed:
            combo = self._format_combo(self._pressed)
            if combo:
                self._finish(combo)
                return
        self._pressed.discard(k)

    @staticmethod
    def _format_combo(pressed):
        order = ("ctrl", "alt", "shift", "win")
        mods  = [m for m in order if m in pressed]
        mains = pressed - set(order)
        if not mains:
            return None
        main = sorted(mains)[0]
        if not mods:
            return main
        return "+".join(mods + [main])

    def get(self):
        return self.value.get().strip().lower()


# ====================== 设置窗口 ======================

def _purge_settings_windows(keep_win, tk_root, title="跟跑助手 · 设置"):
    """销毁所有名为 title 的设置窗口（保留 keep_win）。

    双路径兜底：
    1) tk 路径：遍历 root.winfo_children() 里的 Toplevel，destroy() 优雅销毁
    2) Win32 路径：EnumWindows 找本进程同标题可见窗口，DestroyWindow 强制销毁
    （tk 状态混乱导致 title()/destroy() 抛异常时，路径 2 仍能兜底清理）
    """
    purged = 0
    keep_hwnd = 0
    if keep_win is not None:
        try:
            keep_hwnd = keep_win.winfo_id()
        except Exception:
            keep_hwnd = 0
    # 路径 1：tk 优雅销毁
    try:
        children = list(tk_root.winfo_children())
    except Exception:
        children = []
    for w in children:
        try:
            if not isinstance(w, tk.Toplevel) or w is keep_win:
                continue
        except Exception:
            continue
        try:
            if not w.winfo_exists() or w.title() != title:
                continue
        except Exception:
            continue
        try:
            w.destroy()
            purged += 1
        except Exception:
            pass
    # 路径 2：Win32 强制销毁（本进程内、同标题、可见窗口）
    try:
        my_pid = ctypes.windll.kernel32.GetCurrentProcessId()
        hwnds = []

        @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)
        def _cb(hwnd, _):
            pid = ctypes.wintypes.DWORD()
            ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value != my_pid or not ctypes.windll.user32.IsWindowVisible(hwnd):
                return True
            buf = ctypes.create_unicode_buffer(256)
            ctypes.windll.user32.GetWindowTextW(hwnd, buf, 256)
            if buf.value == title:
                hwnds.append(hwnd)
            return True

        ctypes.windll.user32.EnumWindows(_cb, 0)
        for h in hwnds:
            if h == keep_hwnd:
                continue
            try:
                if ctypes.windll.user32.IsWindow(h):
                    ctypes.windll.user32.DestroyWindow(h)
                    purged += 1
            except Exception:
                pass
    except Exception:
        pass
    return purged


def _build_settings(tk_root, app, on_close_callback):
    win = tk.Toplevel(tk_root)
    try:
        _populate_settings(win, tk_root, app, on_close_callback)
    except Exception:
        # widget 构建中途失败：销毁半成品 Toplevel，避免留下空白孤儿窗口
        # —— 下次再点设置就不会出现"一个完整 + 一个空白"的脏状态
        try:
            win.destroy()
        except Exception:
            pass
        raise
    return win


def _populate_settings(win, tk_root, app, on_close_callback):
    win.title("跟跑助手 · 设置")
    win.resizable(False, False)
    win.attributes("-topmost", True)
    win.protocol("WM_DELETE_WINDOW", on_close_callback)
    try:
        ico = os.path.join(_APP_DIR, "icon.ico")
        if os.path.exists(ico):
            win.iconbitmap(ico)
    except Exception:
        pass
    win.configure(bg="#1e1e2e")

    s = ttk.Style(win)
    s.theme_use("clam")
    s.configure("TFrame", background="#1e1e2e")
    s.configure("TLabel", background="#1e1e2e", foreground="#cdd6f4", font=("微软雅黑", 10))
    s.configure("Head.TLabel", background="#1e1e2e", foreground="#89b4fa",
                 font=("微软雅黑", 11, "bold"))
    s.configure("TEntry", fieldbackground="#313244", foreground="#cdd6f4",
                 insertcolor="#cdd6f4", borderwidth=0, relief="flat")
    s.configure("TButton", background="#89b4fa", foreground="#1e1e2e",
                 font=("微软雅黑", 10, "bold"), borderwidth=0, relief="flat", padding=6)
    s.map("TButton", background=[("active", "#74c7ec"), ("pressed", "#585b70")])
    s.configure("Save.TButton", background="#a6e3a1", foreground="#1e1e2e",
                font=("微软雅黑", 11, "bold"), padding=8)
    s.map("Save.TButton", background=[("active", "#94e2d5"), ("pressed", "#585b70")])
    s.configure("TNotebook", background="#1e1e2e", borderwidth=0)
    s.configure("TNotebook.Tab", background="#313244", foreground="#cdd6f4",
                font=("微软雅黑", 10), padding=[12, 6])
    s.map("TNotebook.Tab",
          background=[("selected", "#89b4fa")],
          foreground=[("selected", "#1e1e2e")])

    outer = ttk.Frame(win, padding=20)
    outer.pack(fill="both", expand=True)

    nb = ttk.Notebook(outer)
    nb.pack(fill="both", expand=True)

    tab_gen = ttk.Frame(nb, padding=16)
    tab_hk  = ttk.Frame(nb, padding=16)
    nb.add(tab_gen, text="  常规  ")
    nb.add(tab_hk,  text="  热键  ")

    # 常规
    ttk.Label(tab_gen, text="常规设置", style="Head.TLabel").grid(
        row=0, column=0, columnspan=2, sticky="w", pady=(0, 14))

    rows = [
        ("启动主页",   "homepage",       36),
        ("窗口宽度",   "width",          10),
        ("窗口高度",   "height",         10),
        ("透明度级别", "opacity_levels", 28),
    ]
    gen_vars = {}
    for i, (label, key, w) in enumerate(rows, 1):
        ttk.Label(tab_gen, text=label).grid(row=i, column=0, sticky="w", pady=4)
        val = app.config.get(key, DEFAULT_CONFIG[key])
        if key == "opacity_levels":
            val = ", ".join(str(v) for v in val)
        elif key in ("width", "height"):
            val = str(val)
        var = tk.StringVar(value=val)
        gen_vars[key] = var
        ttk.Entry(tab_gen, textvariable=var, width=w).grid(
            row=i, column=1, sticky="w", padx=(12, 0), pady=4)
    tab_gen.columnconfigure(1, weight=1)

    sep_row = len(rows) + 2
    ttk.Separator(tab_gen, orient="horizontal").grid(
        row=sep_row, column=0, columnspan=2, sticky="ew", pady=(12, 8))

    var_show_bar  = tk.BooleanVar(value=app.config.get("show_top_bar", True))
    var_auto_hide = tk.BooleanVar(value=app.config.get("top_bar_auto_hide", True))
    var_lock_ratio = tk.BooleanVar(value=app.config.get("lock_aspect_ratio", True))

    def _chk(parent, text, v):
        return tk.Checkbutton(
            parent, text=text, variable=v,
            bg="#1e1e2e", fg="#cdd6f4", selectcolor="#313244",
            activebackground="#1e1e2e", activeforeground="#cdd6f4",
            font=("微软雅黑", 10), cursor="hand2"
        )

    _chk(tab_gen, "显示顶部快捷栏", var_show_bar).grid(
        row=sep_row + 1, column=0, columnspan=2, sticky="w", pady=2)
    _chk(tab_gen, "自动隐藏快捷栏（鼠标离开 3 秒后）", var_auto_hide).grid(
        row=sep_row + 2, column=0, columnspan=2, sticky="w", pady=2)
    _chk(tab_gen, "锁定窗口比例 16:9（拖动窗口大小时保持宽屏）", var_lock_ratio).grid(
        row=sep_row + 3, column=0, columnspan=2, sticky="w", pady=2)
    ttk.Label(tab_gen,
        text="关闭后可自由拉伸窗口大小",
        foreground="#6c7086", background="#1e1e2e",
        font=("微软雅黑", 9)
    ).grid(row=sep_row + 4, column=0, columnspan=2, sticky="w", padx=(22, 0), pady=(0, 4))
    gen_vars["show_top_bar"]        = var_show_bar
    gen_vars["top_bar_auto_hide"]   = var_auto_hide
    gen_vars["lock_aspect_ratio"]   = var_lock_ratio

    # 热键
    ttk.Label(tab_hk, text="热键绑定", style="Head.TLabel").grid(
        row=0, column=0, columnspan=2, sticky="w", pady=(0, 14))

    hk_labels = {
        "play_pause":          "播放/暂停",
        "seek_forward":        "快进5秒",
        "seek_backward":       "快退5秒",
        "adjust_opacity_up":   "透明度+",
        "adjust_opacity_down": "透明度-",
        "show_settings":       "打开设置",
        "speed_up":            "倍速+0.25",
        "speed_down":          "倍速-0.25",
        "speed_reset":         "倍速重置(ctrl+0)",
        "browser_back":        "浏览器后退",
        "toggle_top_bar":      "显示/隐藏横条",
        "toggle_click_through": "切换鼠标穿透",
    }
    hk_recorders = {}
    hk_cfg = app.config.get("hotkeys", DEFAULT_CONFIG["hotkeys"])
    for i, (key, label) in enumerate(hk_labels.items(), 1):
        ttk.Label(tab_hk, text=label).grid(row=i, column=0, sticky="w", pady=3)
        recorder = HotkeyRecorder(
            tab_hk,
            initial_value=hk_cfg.get(key, DEFAULT_CONFIG["hotkeys"][key]),
        )
        recorder.set_window_ref(win)
        recorder.grid(row=i, column=1, sticky="w", padx=(12, 0), pady=3)
        hk_recorders[key] = recorder

    ttk.Label(tab_hk,
        text="提示：点击「修改」后按组合键录制；双击输入框可手动输入",
        foreground="#6c7086", background="#1e1e2e",
        font=("微软雅黑", 9)
    ).grid(row=len(hk_labels) + 1, column=0, columnspan=2, sticky="w", pady=(10, 0))
    tab_hk.columnconfigure(1, weight=1)

    btn_frame = ttk.Frame(outer)
    btn_frame.pack(fill="x", pady=(14, 0))
    ttk.Button(btn_frame, text="取消", command=on_close_callback).pack(side="right", padx=(8, 0))
    ttk.Button(btn_frame, text="保存并应用", style="Save.TButton",
        command=lambda: _save_settings(win, gen_vars, hk_recorders, app, on_close_callback)
    ).pack(side="right")

    win.update_idletasks()
    sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
    ww, wh = win.winfo_width(), win.winfo_height()
    win.geometry(f"{ww}x{wh}+{(sw - ww) // 2}+{(sh - wh) // 2}")
    win.lift()
    win.focus_force()
    _log("创建设置窗口")
    return win


def _save_settings(win, gen_vars, hk_recorders, app, on_close_callback):
    try:
        width = int(gen_vars["width"].get().strip())
        height = int(gen_vars["height"].get().strip())
        opacity_levels = [float(v.strip()) for v in gen_vars["opacity_levels"].get().split(",") if v.strip()]
    except ValueError as e:
        messagebox.showerror("格式错误", f"数值错误：{e}", parent=win)
        return
    if width < 400 or height < 300:
        messagebox.showerror("格式错误", "窗口宽度至少 400，高度至少 300。", parent=win)
        return
    if not opacity_levels or any(v < 0.1 or v > 1.0 for v in opacity_levels):
        messagebox.showerror("格式错误", "透明度级别必须是 0.1 到 1.0 之间的数字，且不能为空。", parent=win)
        return
    hotkeys = {k: v.get() for k, v in hk_recorders.items()}
    invalid_hotkeys = [k for k, v in hotkeys.items() if not _validate_hotkey(v)]
    if invalid_hotkeys:
        messagebox.showerror("格式错误", f"存在不支持的热键：{', '.join(invalid_hotkeys)}", parent=win)
        return
    new_cfg = {
        "homepage":       gen_vars["homepage"].get().strip() or DEFAULT_CONFIG["homepage"],
        "width":          width,
        "height":         height,
        "window_state":   app.config.get("window_state"),
        "lock_aspect_ratio": bool(gen_vars["lock_aspect_ratio"].get()),
        "opacity_levels": opacity_levels,
        "show_top_bar":      bool(gen_vars["show_top_bar"].get()),
        "top_bar_auto_hide":  bool(gen_vars["top_bar_auto_hide"].get()),
        "hotkeys": hotkeys,
    }
    app._apply_config(new_cfg)
    messagebox.showinfo("已保存", "设置已保存！", parent=win)
    on_close_callback()
    _log("配置已保存")


# ====================== 热键管理器工厂 ======================

def _build_hotkey_manager(app):
    """用 RegisterHotKey API 注册全局热键。"""
    manager = RegisterHotkeyManager()
    hk_cfg  = app.config.get("hotkeys", DEFAULT_CONFIG["hotkeys"])
    pending = []
    for action, combo in hk_cfg.items():
        if not _validate_hotkey(combo):
            _logd(f"热键跳过（格式无效）: {action} = {combo!r}")
            continue
        pending.append((action, combo, lambda ap=app: ap))
    if not pending:
        _log("警告：没有有效热键可注册")
        return manager
    manager.start(pending)
    return manager


# ====================== 主入口 ======================

if __name__ == "__main__":
    # 安装全局未捕获异常处理器（用日志 + MessageBoxW，不碰 tkinter）
    def _global_excepthook(exc_type, exc_value, exc_traceback):
        import traceback
        err_msg = "".join(traceback.format_exception(exc_type, exc_value, exc_traceback))
        try:
            _log(f"未捕获异常: {err_msg}")
        except Exception:
            pass
        try:
            _messagebox_w("程序异常",
                f"跟跑助手遇到未捕获的异常:\\n\\n{exc_value}\\n\\n请查看 debug.log 获取详情。",
                MB_OK | MB_ICONERROR | MB_TOPMOST)
        except Exception:
            pass
    sys.excepthook = _global_excepthook

    def is_admin():
        try:
            return ctypes.windll.shell32.IsUserAnAdmin()
        except Exception:
            return False

    if not is_admin():
        ret = _messagebox_w("权限", "建议管理员运行，热键更稳定。是否以管理员身份重新启动？",
                           MB_YESNO | MB_ICONQUESTION | MB_TOPMOST)
        if ret == 6:  # IDYES
            result = ctypes.windll.shell32.ShellExecuteW(
                None, "runas", sys.executable, subprocess.list2cmdline(sys.argv[1:]), None, 1
            )
            if result <= 32:  # ShellExecuteW 失败（如用户取消UAC、组策略阻止等）
                _log(f"管理员提权未成功（返回值 {result}），继续以当前权限运行")
            else:
                sys.exit()

    # 全局互斥锁：使用系统级 Mutex 精准判断程序是否真正在运行
    # 崩溃的进程会自动释放内核对象，不会导致新实例误判
    _app_mutex = ctypes.windll.kernel32.CreateMutexW(None, False, "GenpaoBrowser_Mutex_Unique")
    _last_error = ctypes.windll.kernel32.GetLastError()

    # ERROR_ALREADY_EXISTS = 183，说明已有实例在运行
    if _last_error == 183:
        _log("检测到已有实例在运行，退出")
        _messagebox_w("已运行", "跟跑助手已在运行中，不能同时打开多个实例。",
                      MB_OK | MB_ICONWARNING | MB_TOPMOST)
        sys.exit(0)

    # 必须保持对 mutex 的引用，防止被 Python 垃圾回收机制销毁
    _ = _app_mutex

    app = BrowserApp()

    tk_thr = threading.Thread(target=tkinter_thread, args=(app,), name="TkThread", daemon=True)
    tk_thr.start()

    # 启动 RegisterHotKey 全局热键（独立线程消息循环）
    _hotkey_manager = _build_hotkey_manager(app)

    # pywebview 在主线程阻塞
    app.start()
