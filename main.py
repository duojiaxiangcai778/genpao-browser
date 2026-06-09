"""
跟跑浏览器 - V1.0.3
核心能力：用 Windows 原生 RegisterHotKey API 注册全局热键，
解决 Edge/WebView2 焦点下键盘钩子容易被吞的问题。
本版优化：保存/恢复窗口位置、大小和最大化状态。
"""
import os, sys, json, logging, queue, threading, time, subprocess
import ctypes, ctypes.wintypes
import tkinter as tk
from tkinter import ttk, messagebox

# win32 常量（避免额外依赖 pywin32）
WM_HOTKEY    = 0x0312
MOD_ALT      = 0x0001
MOD_CONTROL  = 0x0002
MOD_SHIFT    = 0x0004
MOD_WIN      = 0x0008
WS_EX_TRANSPARENT = 0x00000020

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
    from pynput import keyboard   # 仅 HotkeyRecorder 使用
except ImportError:
    r = tk.Tk(); r.withdraw()
    messagebox.showerror("环境缺失", "缺少必要依赖")
    r.destroy(); sys.exit()

pywebview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = False

LOG_PATH = os.path.join(_APP_DIR, "debug.log")
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

def _log(msg): _logger.info(msg)
def _logd(msg): _logger.debug(msg)

DEFAULT_CONFIG = {
    "homepage": "https://www.bilibili.com",
    "width": 800, "height": 600,
    "window_state": None,
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
        self._callbacks[hotkey_id] = (action_name, combo, app_ref_fn)
        ok = self.user32.RegisterHotKey(None, hotkey_id, mods, vk)
        if ok:
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
                if hid in self._callbacks:
                    action_name, combo, app_ref_fn = self._callbacks[hid]
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
        self._seek_timer = None       # 合并定时器
        self._click_through = False   # 鼠标穿透状态
        self._pending_actions = []    # 窗口就绪前缓存的热键动作
        self._window_ready = False    # WebView2 窗口是否已就绪
        self._hwnd = None             # 缓存窗口句柄，避免每次 FindWindowW

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
        try:
            with open(self.CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=2, ensure_ascii=False)
        except Exception:
            pass

    def _apply_config(self, new_cfg):
        global _hotkey_manager
        self.config = new_cfg
        self._write_config(new_cfg)
        self.opacity_levels = new_cfg.get("opacity_levels", DEFAULT_CONFIG["opacity_levels"])
        self.opacity_index = 0
        self.current_opacity = self.opacity_levels[0]
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

        js = f"""(function(){{
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
                self._pending_actions.append(action)
                _logd(f"热键缓存（JS 未就绪）: {action}")
                return

        # 仅依赖 HWND 的操作：有 HWND 就执行，不取 _window_ready
        elif action in ("adjust_opacity_up", "adjust_opacity_down",
                        "toggle_click_through"):
            if not hwnd:
                self._pending_actions.append(action)
                _logd(f"热键缓存（HWND 未就绪）: {action}")
                return
            # HWND 存在 → 立即执行，不依赖窗口就绪标志

        # 快进/退：高频合并
        if action in ("seek_forward", "seek_backward"):
            delta = 5 if action == "seek_forward" else -5
            self._seek_accum += delta
            if self._seek_timer:
                self._seek_timer.cancel()
            self._seek_timer = threading.Timer(0.1, self._flush_seek)
            self._seek_timer.start()
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
            return
        offset = self._seek_accum
        self._seek_accum = 0
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
        esc_text = text.replace("'", "\\'").replace("\\", "\\\\")
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
        # 方式4：EnumWindows 枚举所有窗口模糊匹配
        result = []
        EnumWindowsProc = ctypes.WINFUNCTYPE(
            ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM
        )
        def cb(hwnd, _):
            buf = ctypes.create_unicode_buffer(256)
            ctypes.windll.user32.GetWindowTextW(hwnd, buf, 256)
            if "跟跑助手" in buf.value:
                result.append(hwnd)
            return True
        ctypes.windll.user32.EnumWindows(EnumWindowsProc(cb), 0)
        if result:
            self._hwnd = result[0]
            return result[0]
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

    def _restore_window_state(self):
        state = self.config.get("window_state")
        if not isinstance(state, dict):
            return
        hwnd = self._get_hwnd()
        if not hwnd:
            return
        try:
            x = int(state.get("x", 100))
            y = int(state.get("y", 100))
            width = max(400, int(state.get("width", self.config.get("width", 800))))
            height = max(300, int(state.get("height", self.config.get("height", 600))))
            ctypes.windll.user32.MoveWindow(hwnd, x, y, width, height, True)
            if state.get("maximized"):
                ctypes.windll.user32.ShowWindow(hwnd, 3)  # SW_MAXIMIZE
            _logd(f"窗口状态已恢复: {state}")
        except Exception as e:
            _logd(f"_restore_window_state error: {e}")

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
        self._inject_js()
        self._inject_bar()

    def start(self):
        data_dir = self.get_data_dir()
        if not os.path.exists(data_dir):
            os.makedirs(data_dir)
        os.environ["PYWEBVIEW_GUI"] = "edgechromium"
        os.environ["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = \
            "--ignore-gpu-blocklist --enable-gpu-rasterization --enable-hw-video-decode"
        os.environ["WEBVIEW2_USER_DATA_FOLDER"] = data_dir
        _log(f"WebView2 数据目录: {data_dir}")

        js_api = JsApi()
        self.window = pywebview.create_window(
            "跟跑助手", self.config.get("homepage"),
            width=self.config.get("width", 800),
            height=self.config.get("height", 600),
            min_size=(400, 300),
            js_api=js_api,
        )
        try:
            self.window.events.new_window += self._on_new_window
        except AttributeError:
            _log("当前 pywebview 版本不支持 new_window 事件")
        self.window.events.loaded += self._on_loaded
        try:
            self.window.events.closing += self._on_closing
        except AttributeError:
            _log("当前 pywebview 版本不支持 closing 事件")

        def on_start():
            # 精确认 HWND 存在（窗口刚创建，FindWindowW 立即可查到）
            for attempt in range(5):
                hwnd = self._get_hwnd()
                if hwnd:
                    _log(f"窗口 HWND 已获取: 0x{hwnd:X}（尝试 {attempt+1} 次）")
                    break
                time.sleep(0.02)
            else:
                _log("警告：获取 HWND 失败，非 JS 操作可能延迟")
            # HWND 就绪 → 非 JS 操作（穿透/透明度）立即可用
            self._window_ready = True
            self._restore_window_state()
            try:
                self.window.on_top = True
            except Exception as e:
                _logd(f"设置 on_top 失败: {e}")
            # 立即重放缓存中的非 JS 操作（穿透/透明度）
            pending = self._pending_actions[:]
            self._pending_actions.clear()
            for action in pending:
                if action in ("adjust_opacity_up", "adjust_opacity_down",
                              "toggle_click_through"):
                    _log(f"快速重放（HWND 已就绪）: {action}")
                    self._dispatch(action)
            # JS 注入（视频控制类操作需要）
            try:
                self._inject_bar()
                _log("JS 注入完成，所有热键已就绪")
            except Exception as e:
                _log(f"on_start 注入横条失败: {e}")
            # 重放剩余的 JS 依赖操作
            for action in pending:
                if action not in ("adjust_opacity_up", "adjust_opacity_down",
                                  "toggle_click_through"):
                    _log(f"重放 JS 热键: {action}")
                    self._dispatch(action)

        _log("=" * 40)
        _log(f"启动 | 热键：{self.config.get('hotkeys')}")
        pywebview.start(on_start)


# ====================== JS API（暴露给网页） ======================

class JsApi:
    def open_settings(self):
        _action_queue.put("open_settings")

    def quit_app(self):
        _action_queue.put("quit")

    def trigger_action(self, action_name):
        if action_name == "show_settings":
            action_name = "open_settings"
        _action_queue.put(action_name)

    def navigate(self, url):
        _action_queue.put(("navigate", url))


# ====================== Tkinter 子线程 ======================

def tkinter_thread(app):
    # 初始化 COM 为 STA，与 WebView2 的 COM 线程模型兼容
    COINIT_APARTMENTTHREADED = 2
    ctypes.windll.ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED)
    root = tk.Tk()
    root.withdraw()
    settings_win = None

    def _poll():
        nonlocal settings_win
        # 主动探测 HWND：一旦 pywebview 创建窗口就尝试获取，不等 on_start
        if not app._window_ready and app.window:
            hwnd = app._get_hwnd()
            if hwnd and app._pending_actions:
                # HWND 已存在但 on_start 还没触发 → 释放非 JS 操作
                pending_now = []
                still_pending = []
                for a in app._pending_actions:
                    if a in ("adjust_opacity_up", "adjust_opacity_down",
                             "toggle_click_through"):
                        pending_now.append(a)
                    else:
                        still_pending.append(a)
                app._pending_actions = still_pending
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
                    if settings_win is not None:
                        try:
                            if settings_win.winfo_exists():
                                settings_win.lift()
                                settings_win.focus_force()
                                continue
                        except Exception:
                            settings_win = None
                    app.is_settings_opened = True
                    settings_win = _build_settings(
                        root, app,
                        lambda: _action_queue.put("close_settings")
                    )
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
                    root.quit()
                elif isinstance(action, tuple) and action[0] == "navigate":
                    app._navigate(action[1])
                else:
                    app._dispatch(action)
            except Exception as e:
                _log(f"_poll 处理 action 异常: {e}")
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
        0xBD: "-", 0xBB: "=", 0xDB: "[", 0xDD: "]", 0xDC: "\\",
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
            relief="flat", cursor="hand2",
            font=("微软雅黑", 9), padx=8
        )
        self.btn.pack(side="left", padx=(6, 0))

    def set_window_ref(self, window):
        self._win_ref = window

    def _key_name(self, key):
        name = str(key).replace("Key.", "")
        if name in self.MOD_MAP:
            return self.MOD_MAP[name]
        try:
            if key.char:
                # 检测小键盘键（通过 vk 判断）
                if hasattr(key, "vk") and key.vk is not None:
                    nk = self._numpad_map.get(key.vk)
                    if nk:
                        return nk
                return key.char.lower()
        except AttributeError:
            pass
        if hasattr(key, "vk") and key.vk is not None:
            nk = self._numpad_map.get(key.vk)
            if nk:
                return nk
            return self.VK_MAP.get(key.vk, f"vk{key.vk}")
        return name.lower()

    # 小键盘 VK → 配置名映射（避免 ↔ 字母区同名键混淆）
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

def _build_settings(tk_root, app, on_close_callback):
    win = tk.Toplevel(tk_root)
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
    gen_vars["show_top_bar"]       = var_show_bar
    gen_vars["top_bar_auto_hide"] = var_auto_hide

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
    def is_admin():
        try:
            return ctypes.windll.shell32.IsUserAnAdmin()
        except Exception:
            return False

    if not is_admin():
        r = tk.Tk()
        r.withdraw()
        if messagebox.askyesno("权限", "建议管理员运行，热键更稳定"):
            ctypes.windll.shell32.ShellExecuteW(
                None, "runas", sys.executable, subprocess.list2cmdline(sys.argv[1:]), None, 1
            )
            r.destroy()
            sys.exit()
        r.destroy()

    app = BrowserApp()

    tk_thr = threading.Thread(target=tkinter_thread, args=(app,), name="TkThread", daemon=True)
    tk_thr.start()

    # 启动 RegisterHotKey 全局热键（独立线程消息循环）
    _hotkey_manager = _build_hotkey_manager(app)

    # pywebview 在主线程阻塞
    app.start()
