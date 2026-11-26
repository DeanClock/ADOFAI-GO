from __future__ import annotations

import ctypes
import logging
import sys
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass

from PySide6.QtCore import QObject, Qt, Signal

logger = logging.getLogger(__name__)

_MODIFIER_KEYS = {"Ctrl", "Alt", "Shift", "Win"}
_SPECIAL_KEYS = [
    "Shift",
    "Ctrl",
    "Alt",
    "Win",
    "Space",
    "Enter",
    "Esc",
    "Tab",
    "Backspace",
    "Delete",
    "Insert",
    "Home",
    "End",
    "PageUp",
    "PageDown",
    "Up",
    "Down",
    "Left",
    "Right",
    "CapsLock",
    "NumLock",
    "ScrollLock",
    "PrintScreen",
    "Pause",
    "Menu",
    "NumpadAdd",
    "NumpadSubtract",
    "NumpadMultiply",
    "NumpadDivide",
    "NumpadDecimal",
]
_PUNCTUATION = {
    "`": "Backquote",
    "-": "Minus",
    "=": "Equal",
    "[": "BracketLeft",
    "]": "BracketRight",
    "\\": "Backslash",
    ";": "Semicolon",
    "'": "Quote",
    ",": "Comma",
    ".": "Period",
    "/": "Slash",
}
_DISPLAY = {
    "Backquote": "`",
    "Minus": "-",
    "Equal": "=",
    "BracketLeft": "[",
    "BracketRight": "]",
    "Backslash": "\\",
    "Semicolon": ";",
    "Quote": "'",
    "Comma": ",",
    "Period": ".",
    "Slash": "/",
}
_CANONICAL = set(_SPECIAL_KEYS)
_CANONICAL.update(chr(code) for code in range(ord("A"), ord("Z") + 1))
_CANONICAL.update(str(number) for number in range(10))
_CANONICAL.update(f"F{number}" for number in range(1, 25))
_CANONICAL.update(f"Numpad{number}" for number in range(10))
_CANONICAL.update(_PUNCTUATION.values())

_ALIASES: dict[str, str] = {key.casefold(): key for key in _CANONICAL}
_ALIASES.update(
    {
        "control": "Ctrl",
        "ctl": "Ctrl",
        "windows": "Win",
        "meta": "Win",
        "cmd": "Win",
        "command": "Win",
        "return": "Enter",
        "escape": "Esc",
        "del": "Delete",
        "ins": "Insert",
        "pgup": "PageUp",
        "pageup": "PageUp",
        "pgdn": "PageDown",
        "pagedown": "PageDown",
        "arrowup": "Up",
        "arrowdown": "Down",
        "arrowleft": "Left",
        "arrowright": "Right",
        "spacebar": "Space",
        "numpadplus": "NumpadAdd",
        "numpadminus": "NumpadSubtract",
        "numpadstar": "NumpadMultiply",
        "numpadslash": "NumpadDivide",
        "numpaddot": "NumpadDecimal",
    }
)
for _symbol, _name in _PUNCTUATION.items():
    _ALIASES[_name.casefold()] = _name
    _ALIASES[_symbol] = _name


def normalize_key_name(value: str) -> str:
    """把用户输入、Qt 键值或 Win32 键名归一成同一个名称。"""
    raw = str(value or "").strip()
    if not raw:
        return ""
    if len(raw) == 1:
        if raw.isalpha():
            return raw.upper()
        if raw.isdigit():
            return raw
    return _ALIASES.get(raw.casefold(), "")


def display_key_name(value: str) -> str:
    canonical = normalize_key_name(value)
    return _DISPLAY.get(canonical, canonical)


def _sort_key(value: str) -> tuple[int, str]:
    rank = {"Ctrl": 0, "Alt": 1, "Shift": 2, "Win": 3}
    return (rank.get(value, 20), value.casefold())


@dataclass(frozen=True)
class HotkeySpec:
    display: str
    keys: frozenset[str]
    double_key: str | None = None


def parse_hotkey(value: str) -> HotkeySpec:
    """解析 Shift+Shift、Ctrl+Shift+G、F8 这几种热键写法。"""
    parts = [part.strip() for part in str(value or "").replace("＋", "+").split("+")]
    parts = [part for part in parts if part]
    if not parts:
        raise ValueError("热键不能为空")
    keys = [normalize_key_name(part) for part in parts]
    if not all(keys):
        unknown = [part for part, key in zip(parts, keys) if not key]
        raise ValueError(f"无法识别按键：{', '.join(unknown)}")
    if len(keys) == 2 and keys[0] == keys[1]:
        return HotkeySpec(
            display=f"{display_key_name(keys[0])}+{display_key_name(keys[0])}",
            keys=frozenset(),
            double_key=keys[0],
        )
    if len(set(keys)) != len(keys):
        raise ValueError("组合键里有重复按键；双击请只写两个相同按键")
    if len(keys) == 1 and keys[0] in _MODIFIER_KEYS:
        raise ValueError("单个修饰键不能作为热键，请使用双击或组合键")
    ordered = sorted(keys, key=_sort_key)
    return HotkeySpec(
        display="+".join(display_key_name(key) for key in ordered),
        keys=frozenset(ordered),
    )


class HotkeyMatcher:
    """纯状态机，便于在无窗口环境测试热键匹配。"""

    def __init__(self, spec: HotkeySpec, double_interval: float = 0.45) -> None:
        self.spec = spec
        self.double_interval = max(0.1, float(double_interval))
        self._pressed: set[str] = set()
        self._combo_fired = False
        self._last_double_key = ""
        self._last_double_time = 0.0

    def reset(self) -> None:
        self._pressed.clear()
        self._combo_fired = False
        self._last_double_key = ""
        self._last_double_time = 0.0

    def key_down(self, name: str, now: float | None = None) -> bool:
        key = normalize_key_name(name)
        if not key or key in self._pressed:
            return False
        current = time.monotonic() if now is None else float(now)
        self._pressed.add(key)
        if self.spec.double_key:
            if key != self.spec.double_key or len(self._pressed) > 1:
                self._last_double_key = ""
                return False
            if (
                self._last_double_key == key
                and current - self._last_double_time <= self.double_interval
            ):
                self._last_double_key = ""
                self._last_double_time = 0.0
                return True
            self._last_double_key = key
            self._last_double_time = current
            return False
        if self.spec.keys.issubset(self._pressed) and not self._combo_fired:
            self._combo_fired = True
            return True
        return False

    def key_up(self, name: str) -> None:
        key = normalize_key_name(name)
        if not key:
            return
        self._pressed.discard(key)
        if not self.spec.keys.issubset(self._pressed):
            self._combo_fired = False


def qt_key_name(key: int, text: str = "") -> str:
    """把 QKeyEvent 的 key/text 转成统一键名。"""
    key = int(key)
    special = {
        int(Qt.Key.Key_Shift): "Shift",
        int(Qt.Key.Key_Control): "Ctrl",
        int(Qt.Key.Key_Alt): "Alt",
        int(Qt.Key.Key_Meta): "Win",
        int(Qt.Key.Key_Space): "Space",
        int(Qt.Key.Key_Return): "Enter",
        int(Qt.Key.Key_Enter): "Enter",
        int(Qt.Key.Key_Escape): "Esc",
        int(Qt.Key.Key_Tab): "Tab",
        int(Qt.Key.Key_Backspace): "Backspace",
        int(Qt.Key.Key_Delete): "Delete",
        int(Qt.Key.Key_Insert): "Insert",
        int(Qt.Key.Key_Home): "Home",
        int(Qt.Key.Key_End): "End",
        int(Qt.Key.Key_PageUp): "PageUp",
        int(Qt.Key.Key_PageDown): "PageDown",
        int(Qt.Key.Key_Up): "Up",
        int(Qt.Key.Key_Down): "Down",
        int(Qt.Key.Key_Left): "Left",
        int(Qt.Key.Key_Right): "Right",
        int(Qt.Key.Key_CapsLock): "CapsLock",
        int(Qt.Key.Key_NumLock): "NumLock",
        int(Qt.Key.Key_ScrollLock): "ScrollLock",
        int(Qt.Key.Key_Print): "PrintScreen",
        int(Qt.Key.Key_Pause): "Pause",
        int(Qt.Key.Key_Menu): "Menu",
        int(Qt.Key.Key_Comma): "Comma",
        int(Qt.Key.Key_Period): "Period",
        int(Qt.Key.Key_Slash): "Slash",
        int(Qt.Key.Key_Semicolon): "Semicolon",
        int(Qt.Key.Key_Apostrophe): "Quote",
        int(Qt.Key.Key_BracketLeft): "BracketLeft",
        int(Qt.Key.Key_BracketRight): "BracketRight",
        int(Qt.Key.Key_Backslash): "Backslash",
        int(Qt.Key.Key_Minus): "Minus",
        int(Qt.Key.Key_Equal): "Equal",
        int(Qt.Key.Key_QuoteLeft): "Backquote",
    }
    if key in special:
        return special[key]
    if int(Qt.Key.Key_A) <= key <= int(Qt.Key.Key_Z):
        return chr(key)
    if int(Qt.Key.Key_0) <= key <= int(Qt.Key.Key_9):
        return chr(key)
    if int(Qt.Key.Key_F1) <= key <= int(Qt.Key.Key_F24):
        return f"F{key - int(Qt.Key.Key_F1) + 1}"
    return normalize_key_name(text)


def vk_key_name(vk: int) -> str:
    vk = int(vk)
    if 0x41 <= vk <= 0x5A:
        return chr(vk)
    if 0x30 <= vk <= 0x39:
        return chr(vk)
    if 0x70 <= vk <= 0x87:
        return f"F{vk - 0x6F}"
    if 0x60 <= vk <= 0x69:
        return f"Numpad{vk - 0x60}"
    mapping = {
        0x08: "Backspace",
        0x09: "Tab",
        0x0D: "Enter",
        0x10: "Shift",
        0x11: "Ctrl",
        0x12: "Alt",
        0x13: "Pause",
        0x14: "CapsLock",
        0x1B: "Esc",
        0x20: "Space",
        0x21: "PageUp",
        0x22: "PageDown",
        0x23: "End",
        0x24: "Home",
        0x25: "Left",
        0x26: "Up",
        0x27: "Right",
        0x28: "Down",
        0x2C: "PrintScreen",
        0x2D: "Insert",
        0x2E: "Delete",
        0x5B: "Win",
        0x5C: "Win",
        0x5D: "Menu",
        0x6A: "NumpadMultiply",
        0x6B: "NumpadAdd",
        0x6D: "NumpadSubtract",
        0x6E: "NumpadDecimal",
        0x6F: "NumpadDivide",
        0x90: "NumLock",
        0x91: "ScrollLock",
        0xA0: "Shift",
        0xA1: "Shift",
        0xA2: "Ctrl",
        0xA3: "Ctrl",
        0xA4: "Alt",
        0xA5: "Alt",
        0xBA: "Semicolon",
        0xBB: "Equal",
        0xBC: "Comma",
        0xBD: "Minus",
        0xBE: "Period",
        0xBF: "Slash",
        0xC0: "Backquote",
        0xDB: "BracketLeft",
        0xDC: "Backslash",
        0xDD: "BracketRight",
        0xDE: "Quote",
    }
    return mapping.get(vk, "")


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", wintypes.DWORD),
        ("scanCode", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


if sys.platform == "win32":
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _HOOKPROC = ctypes.WINFUNCTYPE(
        ctypes.c_ssize_t,
        ctypes.c_int,
        ctypes.c_size_t,
        ctypes.c_ssize_t,
    )
    _user32.SetWindowsHookExW.argtypes = [
        ctypes.c_int,
        _HOOKPROC,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    _user32.SetWindowsHookExW.restype = ctypes.c_void_p
    _user32.CallNextHookEx.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_size_t,
        ctypes.c_ssize_t,
    ]
    _user32.CallNextHookEx.restype = ctypes.c_ssize_t
    _user32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
    _user32.UnhookWindowsHookEx.restype = wintypes.BOOL
    _user32.GetMessageW.argtypes = [
        ctypes.POINTER(wintypes.MSG),
        ctypes.c_void_p,
        wintypes.UINT,
        wintypes.UINT,
    ]
    _user32.GetMessageW.restype = ctypes.c_int
    _user32.PostThreadMessageW.argtypes = [
        wintypes.DWORD,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
    ]
    _user32.PostThreadMessageW.restype = wintypes.BOOL
    _kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    _kernel32.GetModuleHandleW.restype = ctypes.c_void_p


class GlobalHotkey(QObject):
    activated = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._thread: threading.Thread | None = None
        self._thread_id = 0
        self._hook = ctypes.c_void_p(0) if sys.platform == "win32" else 0
        self._callback = None
        self._matcher: HotkeyMatcher | None = None
        self._ready = threading.Event()
        self._paused = threading.Event()
        self._error = ""

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self, hotkey: str) -> tuple[bool, str]:
        if sys.platform != "win32":
            return False, "全局热键仅支持 Windows"
        try:
            spec = parse_hotkey(hotkey)
        except ValueError as exc:
            return False, str(exc)
        self.stop()
        self._matcher = HotkeyMatcher(spec)
        self._error = ""
        self._ready.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="ADOFAI-GO-Hotkey",
            daemon=True,
        )
        self._thread.start()
        if not self._ready.wait(2.0):
            self.stop()
            return False, "热键监听启动超时"
        if self._error:
            message = self._error
            self.stop()
            return False, message
        return True, f"已启用 · {spec.display}"

    def stop(self) -> None:
        thread = self._thread
        if thread is not None and thread.is_alive():
            if self._thread_id:
                try:
                    _user32.PostThreadMessageW(self._thread_id, 0x0012, 0, 0)
                except Exception:
                    logger.debug("停止热键监听线程失败", exc_info=True)
            thread.join(1.0)
        self._thread = None
        self._thread_id = 0
        self._hook = ctypes.c_void_p(0) if sys.platform == "win32" else 0
        self._callback = None
        self._matcher = None
        self._ready.clear()

    def set_paused(self, paused: bool) -> None:
        if paused:
            self._paused.set()
        else:
            self._paused.clear()
        if self._matcher is not None:
            self._matcher.reset()

    def _run(self) -> None:
        self._thread_id = int(_kernel32.GetCurrentThreadId())
        self._callback = _HOOKPROC(self._hook_proc)
        module = _kernel32.GetModuleHandleW(None)
        self._hook = _user32.SetWindowsHookExW(
            13,
            self._callback,
            module,
            0,
        )
        if not self._hook:
            self._error = f"安装键盘钩子失败：{ctypes.WinError(ctypes.get_last_error())}"
            self._ready.set()
            return
        self._ready.set()
        msg = wintypes.MSG()
        try:
            while _user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                _user32.TranslateMessage(ctypes.byref(msg))
                _user32.DispatchMessageW(ctypes.byref(msg))
        finally:
            if self._hook:
                _user32.UnhookWindowsHookEx(self._hook)
            self._hook = ctypes.c_void_p(0)

    def _hook_proc(self, n_code: int, w_param: int, l_param: int) -> int:
        if n_code >= 0 and not self._paused.is_set():
            try:
                message = int(w_param)
                if message in (0x0100, 0x0104, 0x0101, 0x0105):
                    data = ctypes.cast(
                        l_param,
                        ctypes.POINTER(KBDLLHOOKSTRUCT),
                    ).contents
                    if not (data.flags & 0x10):
                        key = vk_key_name(data.vkCode)
                        if key and self._matcher is not None:
                            if message in (0x0100, 0x0104):
                                if self._matcher.key_down(key):
                                    self.activated.emit()
                            else:
                                self._matcher.key_up(key)
            except Exception:
                logger.exception("处理全局热键事件失败")
        return int(
            _user32.CallNextHookEx(
                self._hook,
                n_code,
                w_param,
                l_param,
            )
        )


class FLASHWINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.UINT),
        ("hwnd", wintypes.HWND),
        ("dwFlags", wintypes.DWORD),
        ("uCount", wintypes.UINT),
        ("dwTimeout", wintypes.DWORD),
    ]


def bring_window_to_front(hwnd: int) -> bool:
    """把指定窗口恢复到前台；失败时闪烁任务栏。"""
    if sys.platform != "win32" or not hwnd:
        return False
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32.IsIconic.argtypes = [wintypes.HWND]
    user32.IsIconic.restype = wintypes.BOOL
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.ShowWindow.restype = wintypes.BOOL
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.BringWindowToTop.argtypes = [wintypes.HWND]
    user32.BringWindowToTop.restype = wintypes.BOOL
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = [
        wintypes.HWND,
        ctypes.POINTER(wintypes.DWORD),
    ]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    kernel32.GetCurrentThreadId.restype = wintypes.DWORD
    user32.AttachThreadInput.argtypes = [
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.BOOL,
    ]
    user32.AttachThreadInput.restype = wintypes.BOOL

    handle = wintypes.HWND(hwnd)
    if user32.IsIconic(handle):
        user32.ShowWindow(handle, 9)
    else:
        user32.ShowWindow(handle, 5)
    user32.BringWindowToTop(handle)
    user32.SetForegroundWindow(handle)

    if int(user32.GetForegroundWindow() or 0) != hwnd:
        foreground = user32.GetForegroundWindow()
        foreground_thread = user32.GetWindowThreadProcessId(foreground, None)
        current_thread = kernel32.GetCurrentThreadId()
        attached = bool(
            foreground_thread
            and foreground_thread != current_thread
            and user32.AttachThreadInput(current_thread, foreground_thread, True)
        )
        try:
            user32.ShowWindow(handle, 9)
            user32.BringWindowToTop(handle)
            user32.SetForegroundWindow(handle)
        finally:
            if attached:
                user32.AttachThreadInput(current_thread, foreground_thread, False)

    if int(user32.GetForegroundWindow() or 0) != hwnd:
        info = FLASHWINFO(
            ctypes.sizeof(FLASHWINFO),
            handle,
            0x00000003,
            3,
            0,
        )
        user32.FlashWindowEx(ctypes.byref(info))
        return False
    return True
