"""端到端验证：微信 /open 开窗 -> 注入建会话 -> 桥认领绑定 -> 微信任务打进窗口执行。

用 wxauto 模拟用户操作（真发微信消息），断言：
1. /open 后桌面出现「Claude (wechat)」窗口
2. 向窗口注入首条消息后，桥日志出现「认领」「绑定全新窗口」
3. 微信再发任务，微信里收到「已开工【窗口"」和「✅ 完成」回复
4. 被认领会话的 jsonl 里出现该任务的用户轮

跑完打印 PASS/FAIL。需桥已在线、微信已登录。
"""
import subprocess
import sys
import time
from pathlib import Path

import psutil
from wxauto import WeChat

BRIDGE = Path(r"E:\1workspace\wechat-claude-bridge")
PY = str(BRIDGE / ".venv/Scripts/python.exe")
SLUG_DIR = Path.home() / ".claude/projects/E--1workspace"
LOG = BRIDGE / "logs/bridge.log"
CHAT = "文件传输助手"
TASK_TEXT = "窗口任务成功了"


def find_wechat_window():
    """最新一个 title 为「Claude (wechat)」且有 claude.exe 子进程的 cmd 窗口。"""
    best = None
    for p in psutil.process_iter(["pid", "name", "cmdline", "create_time"]):
        try:
            if p.info["name"] != "cmd.exe":
                continue
            cmdline = " ".join(p.info["cmdline"] or [])
            if "title Claude (wechat)" not in cmdline:
                continue
            ctime = p.info["create_time"] or 0
            for child in psutil.Process(p.info["pid"]).children():
                if child.name() == "claude.exe":
                    if best is None or ctime > best[2]:
                        best = (p.info["pid"], child.pid, ctime)
        except (psutil.Error, TypeError):
            continue
    return best


def log_tail(since_pos: int) -> str:
    # since_pos 是字节偏移（stat().st_size），必须按字节切再解码：
    # 日志含中文，read_text() 后按字符切片会越过 EOF 永远拿到 ""
    return LOG.read_bytes()[since_pos:].decode("utf-8", errors="replace")


def wechat_tail(wx: WeChat, n: int = 15) -> str:
    wx.ChatWith(CHAT)
    msgs = wx.GetAllMessage()
    return "\n".join(getattr(m, "content", "") or "" for m in msgs[-n:])


def main() -> int:
    wx = WeChat()
    t0 = time.time()
    log_pos = LOG.stat().st_size
    step = "发 /new + /open"
    wx.SendMsg("/new", who=CHAT)
    time.sleep(3)
    wx.SendMsg("/open", who=CHAT)

    step = "等窗口出现"
    win = None
    deadline = time.time() + 30
    while time.time() < deadline:
        win = find_wechat_window()
        if win and win[2] >= t0 - 5:
            break
        win = None
        time.sleep(1)
    if not win:
        print(f"FAIL[{step}]: 未找到新窗口")
        return 1
    cmd_pid, claude_pid, _ = win
    print(f"OK 窗口出现: cmd={cmd_pid} claude={claude_pid}")

    step = "注入首条消息建会话"
    time.sleep(10)  # 等 TUI 启动
    tmp = BRIDGE / "logs/_e2e_first.txt"
    tmp.write_text("只回复两个字：在线", encoding="utf-8")
    proc = subprocess.run(
        [PY, "-m", "bridge.winconsole", "inject", str(claude_pid), str(tmp)],
        capture_output=True, text=True, timeout=40, cwd=str(BRIDGE))
    if proc.returncode != 0:
        print(f"FAIL[{step}]: {proc.stderr[:200]}")
        return 1
    print("OK 注入成功，等窗口 claude 落盘 + 桥认领")

    step = "桥认领+绑定窗口"
    deadline = time.time() + 120
    adopted_sid = ""
    while time.time() < deadline:
        if "绑定全新窗口" in log_tail(log_pos):
            break
        time.sleep(2)
    else:
        print(f"FAIL[{step}]: 120s 内桥未绑定\n{log_tail(log_pos)[-500:]}")
        return 1
    # 认领的会话 = t0 后新建的 jsonl
    news = [f for f in SLUG_DIR.glob("*.jsonl")
            if f.stat().st_ctime >= t0 - 5]
    adopted_sid = max(news, key=lambda f: f.stat().st_ctime).stem if news else ""
    print(f"OK 桥已认领绑定: session={adopted_sid[:8] if adopted_sid else '?'}")

    step = "微信发任务"
    wx.SendMsg(f"请只回复六个字：{TASK_TEXT}", who=CHAT)

    step = "等窗口路由回复"
    saw_route = saw_done = False
    deadline = time.time() + 240
    while time.time() < deadline and not (saw_route and saw_done):
        try:
            tail = wechat_tail(wx)
            saw_route = saw_route or "【窗口" in tail
            saw_done = saw_done or ("✅ 完成" in tail and TASK_TEXT in tail)
        except Exception as exc:  # UIA 偶发抖动，重试
            print(f"  (读微信抖动: {exc})")
        time.sleep(5)
    print(f"OK 路由回执={saw_route} 完成回执={saw_done}")
    if not (saw_route and saw_done):
        print(f"FAIL[{step}]: route={saw_route} done={saw_done}")
        return 1

    step = "jsonl 出现任务轮"
    if adopted_sid:
        text = (SLUG_DIR / f"{adopted_sid}.jsonl").read_text(
            encoding="utf-8", errors="replace")
        if TASK_TEXT not in text:
            print(f"FAIL[{step}]: jsonl 中无任务文本")
            return 1
    print("PASS: 微信提示词已打进窗口执行并取回结果")
    return 0


if __name__ == "__main__":
    sys.exit(main())
