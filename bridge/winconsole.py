"""向已打开的 CC 控制台窗口注入键盘输入（AttachConsole + WriteConsoleInputW）
并支持回读屏幕文本（ReadConsoleOutputCharacterW）。

桥借此把微信提示词"打进"对应窗口：窗口里的交互 claude 亲自执行任务，
提示词/工具/回答全过程在窗口实时可见。注入直接写目标控制台的输入缓冲，
不需要把窗口切到前台、不抢焦点。

必须在短寿命子进程里执行（FreeConsole/AttachConsole 会折腾调用者自己的
控制台，桥本体有隐藏控制台不能被波及）：

    python -m bridge.winconsole inject <pid> <text_file>
    python -m bridge.winconsole escape <pid>          # 发 Esc 打断生成
    python -m bridge.winconsole read <pid>            # 打印当前屏幕文本

pid：目标控制台里任一进程（claude.exe 即可）。
"""
from __future__ import annotations

import ctypes
import re
import sys
from ctypes import wintypes
from pathlib import Path

KEY_EVENT = 0x0001
VK_RETURN = 0x0D
VK_ESCAPE = 0x1B


class KEY_EVENT_RECORD(ctypes.Structure):
    _fields_ = [("bKeyDown", wintypes.BOOL),
                ("wRepeatCount", wintypes.WORD),
                ("wVirtualKeyCode", wintypes.WORD),
                ("wVirtualScanCode", wintypes.WORD),
                ("UnicodeChar", wintypes.WCHAR),
                ("dwControlKeyState", wintypes.DWORD)]


class _EventUnion(ctypes.Union):
    _fields_ = [("KeyEvent", KEY_EVENT_RECORD),
                ("_pad", ctypes.c_byte * 16)]   # INPUT_RECORD 联合体按 16 字节对齐


class INPUT_RECORD(ctypes.Structure):
    _fields_ = [("EventType", wintypes.WORD),
                ("Event", _EventUnion)]


def _pair(vk: int, ch: str) -> list[INPUT_RECORD]:
    out = []
    for down in (True, False):
        r = INPUT_RECORD()
        r.EventType = KEY_EVENT
        r.Event.KeyEvent.bKeyDown = down
        r.Event.KeyEvent.wRepeatCount = 1
        r.Event.KeyEvent.wVirtualKeyCode = vk
        r.Event.KeyEvent.wVirtualScanCode = 0
        r.Event.KeyEvent.UnicodeChar = ch
        r.Event.KeyEvent.dwControlKeyState = 0
        out.append(r)
    return out


def build_text_events(text: str) -> list[INPUT_RECORD]:
    """文本 → 键事件序列。\\r\\n 归一成空格：TUI 里裸回车 = 直接提交，
    多行提示词必须压成一行再注入（换行结构对任务语义无影响）。"""
    events: list[INPUT_RECORD] = []
    for ch in text:
        if ch in "\r\n":
            ch = " "
        events.extend(_pair(0, ch))
    return events


def build_submit_events() -> list[INPUT_RECORD]:
    return _pair(VK_RETURN, "\r")


def build_escape_events() -> list[INPUT_RECORD]:
    return _pair(VK_ESCAPE, "\x1b")


GENERIC_READ_WRITE = 0xC0000000
GENERIC_READ = 0x80000000
FILE_SHARE_READ_WRITE = 0x00000003
OPEN_EXISTING = 3
INVALID_HANDLE_VALUE = 0xFFFFFFFFFFFFFFFF


class COORD(ctypes.Structure):
    _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]


class SMALL_RECT(ctypes.Structure):
    _fields_ = [("Left", ctypes.c_short), ("Top", ctypes.c_short),
                ("Right", ctypes.c_short), ("Bottom", ctypes.c_short)]


class CONSOLE_SCREEN_BUFFER_INFO(ctypes.Structure):
    _fields_ = [("dwSize", COORD),
                ("dwCursorPosition", COORD),
                ("wAttributes", wintypes.WORD),
                ("srWindow", SMALL_RECT),
                ("dwMaximumWindowSize", COORD)]


def _console_input_handle(k32) -> int:
    """AttachConsole 后取控制台输入缓冲句柄。

    不能用 GetStdHandle(STD_INPUT_HANDLE)：PEB 标准句柄在进程创建时定型，
    FreeConsole 后即成失效句柄（实测 WriteConsoleInputW 报 error 6
    ERROR_INVALID_HANDLE）。正确做法是打开 "CONIN$" 设备。"""
    return _open_device(k32, "CONIN$", GENERIC_READ_WRITE)


from contextlib import contextmanager


@contextmanager
def _console_input(pid: int):
    """Attach 到目标控制台并打开 CONIN$；退出时自动 CloseHandle/FreeConsole。"""
    k32 = ctypes.windll.kernel32
    k32.FreeConsole()
    if not k32.AttachConsole(wintypes.DWORD(pid)):
        raise OSError(ctypes.GetLastError(), f"AttachConsole(pid={pid}) 失败")
    try:
        handle = _console_input_handle(k32)
        try:
            yield k32, handle
        finally:
            k32.CloseHandle(handle)
    finally:
        k32.FreeConsole()


def _write(k32, handle: int, events: list[INPUT_RECORD]) -> None:
    if not events:
        return
    arr = (INPUT_RECORD * len(events))(*events)
    written = wintypes.DWORD(0)
    if not k32.WriteConsoleInputW(handle, arr, len(events),
                                  ctypes.byref(written)):
        raise OSError(ctypes.GetLastError(), "WriteConsoleInputW 失败")


# 文本写完到发回车之间的间隔：长文本与同批回车曾被 TUI 一起吞掉回车
# （文字完整留在输入框但没提交，2026-10-18 窗口 2188 实测）
SUBMIT_DELAY_MS = 400


def inject(pid: int, text: str) -> None:
    """文本与回车分两批写入，中间留 SUBMIT_DELAY_MS 给 TUI 消化文本。

    text 为空串时等于只发一个回车（用于提交窗口里已存在的草稿）。"""
    with _console_input(pid) as (k32, handle):
        _write(k32, handle, build_text_events(text))
        k32.Sleep(SUBMIT_DELAY_MS)
        _write(k32, handle, build_submit_events())


def send_escape(pid: int) -> None:
    with _console_input(pid) as (k32, handle):
        _write(k32, handle, build_escape_events())


def _open_device(k32, name: str, access: int) -> int:
    k32.CreateFileW.restype = wintypes.HANDLE
    handle = k32.CreateFileW(name, access, FILE_SHARE_READ_WRITE,
                             None, OPEN_EXISTING, 0, None)
    if not handle or handle == INVALID_HANDLE_VALUE:
        raise OSError(ctypes.GetLastError(), f"打开 {name} 失败")
    return handle


def read_screen(pid: int) -> str:
    """读取目标控制台当前屏幕文本（含 TUI 交替屏），按行拼接返回。

    交互 claude 在 2026-10-18 出现过"新窗口不写 transcript jsonl"的
    异常（v2.1.283 会话注册失败），屏幕抓取是窗口模式取回结果的兜底通道。"""
    k32 = ctypes.windll.kernel32
    k32.FreeConsole()
    if not k32.AttachConsole(wintypes.DWORD(pid)):
        raise OSError(ctypes.GetLastError(), f"AttachConsole(pid={pid}) 失败")
    try:
        handle = _open_device(k32, "CONOUT$", GENERIC_READ_WRITE)
        try:
            info = CONSOLE_SCREEN_BUFFER_INFO()
            if not k32.GetConsoleScreenBufferInfo(handle, ctypes.byref(info)):
                raise OSError(ctypes.GetLastError(),
                              "GetConsoleScreenBufferInfo 失败")
            cols, rows = info.dwSize.X, info.dwSize.Y
            total = cols * rows
            buf = ctypes.create_unicode_buffer(total + 1)
            read = wintypes.DWORD(0)
            if not k32.ReadConsoleOutputCharacterW(
                    handle, buf, total, COORD(0, 0), ctypes.byref(read)):
                raise OSError(ctypes.GetLastError(),
                              "ReadConsoleOutputCharacterW 失败")
            raw = buf.value[:read.value]
            lines = [raw[y * cols:(y + 1) * cols].rstrip()
                     for y in range(rows)]
            # 去掉尾部全空行
            while lines and not lines[-1]:
                lines.pop()
            return "\n".join(lines)
        finally:
            k32.CloseHandle(handle)
    finally:
        k32.FreeConsole()


# ---- 屏幕文本解析（纯函数，供 winrunner/app 复用与单测）----

# 完成标记：TUI 每完成一轮会落一行「✳ <verb> for Ns · done HH:MM」
_DONE_RE = re.compile(r"· done \d{1,2}:\d{2}")
# /status 弹层里的会话行
_SID_RE = re.compile(r"Session ID:\s*([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})")


def done_count(screen: str) -> int:
    """屏幕里「· done HH:MM」完成标记的个数（每完成一轮 +1）。"""
    return len(_DONE_RE.findall(screen))


def find_session_id(screen: str) -> str:
    """从 /status 屏幕里抠 Session ID；没有返回 ""。
    /status 是全覆盖弹层，读取完成后需要向窗口发 Esc 关闭。"""
    m = _SID_RE.search(screen)
    return m.group(1) if m else ""


def extract_last_answer(screen: str) -> str:
    """抠出最后一轮回答文本：最后一个「● 」起、到完成行（✳/✻）或
    分隔线止。完成标记的符号版本间会变（v2.1.283 用 ✻），两个都要认。

    抓屏行可能带大段前导空格（窗口被改窄后 TUI 旧宽渲染残留，
    2026-10-18 实测行首有 100+ 空格），所以判定前一律 strip。
    抠不到返回 ""。"""
    lines = [line.strip() for line in screen.splitlines()]
    start = -1
    for i, line in enumerate(lines):
        if line.startswith("● "):
            start = i
    if start < 0:
        return ""
    out: list[str] = []
    for line in lines[start:]:
        if line and line[0] in "✳✻─":
            break
        out.append(line)
    out[0] = out[0][2:]          # 去掉首行「● 」前缀
    return "\n".join(out).strip()


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print(__doc__)
        return 2
    action, pid = argv[1], int(argv[2])
    if action == "inject":
        text = Path(argv[3]).read_text(encoding="utf-8")
        inject(pid, text)
    elif action == "escape":
        send_escape(pid)
    elif action == "read":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        print(read_screen(pid))
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
