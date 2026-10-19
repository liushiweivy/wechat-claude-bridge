"""桥探活看门狗（独立外部进程）。

持续只读「文件传输助手」聊天记录：连续 >=2 条用户消息没有桥的回复、且最早
一条超过 3 分钟 → 判定桥"聋了"（COM/UIA 挂死不返回，进程内无解，见
bridge/app.py MAX_RECONNECT_ROUNDS 注释），处置：

- 桥进程在 → 杀进程树，由 autostart bat 重试循环 ~20s 拉起新进程；
- 桥进程不在、守护 bat 在 → 不动作，等 bat 重试；
- 两者都不在（bat 30 次重试烧完放弃的场景，2026-10-19 实测桥死 10 小时）
  → 探活自己把 autostart-run.bat 拉回来。

读失败（微信未就绪/退出）只记日志不处置——区分不了"微信没了"与"桥聋"，
误杀无益；桥自身的重连/自杀逻辑会处理。

运行：.venv\\Scripts\\pythonw.exe tools\\bridge_probe.py [--dry-run] [--once]
环境变量：BRIDGE_PROBE_DRY_RUN=1 / BRIDGE_PROBE_MIN_UNANSWERED=2 /
          BRIDGE_PROBE_MIN_AGE=180 / BRIDGE_PROBE_POLL=15
日志：logs/probe.log（1MB x 3 滚动）

注意：必须用本脚本路径运行，不要 python -m bridge.probe——后者 cmdline 含
'-m bridge'，会被 autostart-visible.bat 的桥防重复检查误当本体，探活杀桥
时也会匹配到自己（bridge/probe.py find_bridge_pids 注释）。
"""
from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bridge.config import load_config
from bridge.probe import (KILL_COOLDOWN_SECONDS, MIN_AGE_SECONDS,
                          MIN_UNANSWERED, TAIL_N, DeafnessDetector, MsgView,
                          Verdict, find_autostart_pids, find_bridge_pids,
                          kill_tree)

log = logging.getLogger("bridge.probe")

NOTICE_KILL = "🔧 桥无响应（{n} 条消息超 {age:.0f}s 未回复），自动重启中…"
NOTICE_REVIVE = "🔧 桥已停止且守护循环不在，正在拉起…"


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def setup_logging() -> None:
    path = ROOT / "logs" / "probe.log"
    path.parent.mkdir(exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    fh = RotatingFileHandler(path, maxBytes=1 << 20, backupCount=3,
                             encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    log.setLevel(logging.INFO)
    log.addHandler(fh)
    log.addHandler(sh)


def scan_procs() -> list[tuple[int, str, str]]:
    """(pid, name, cmdline) 快照；取不到的进程跳过。"""
    import psutil
    out: list[tuple[int, str, str]] = []
    for p in psutil.process_iter(["name"]):
        try:
            out.append((p.pid, p.name() or "",
                        " ".join(p.cmdline() or [])))
        except psutil.Error:
            continue
    return out


def singleton_check() -> None:
    # venv 的 pythonw.exe 是重定向器：它拉起真正的解释器子进程后自己留在
    # 原地等待，父子 cmdline 都含 bridge_probe.py——不排除父进程的话，探活
    # 会把自己的重定向器父亲当成"已有探活"秒退（2026-10-19 实测：bat 起的
    # 探活日志只有一行 pid=<父> 然后消失）。
    me, parent = os.getpid(), os.getppid()
    # 只看 python/pythonw：bash/timeout/powershell 等壳进程的 cmdline 里同样
    # 带着 bridge_probe.py 字样，不过滤会把自己背后的壳当成"已有探活"秒退
    # （2026-10-19 实测：timeout.exe / bash -c / Start-Process 的 powershell
    # 全部命中）。venv 重定向器父进程见下方 parent 排除。
    for pid, name, cmd in scan_procs():
        if (name or "").lower() not in ("python.exe", "pythonw.exe"):
            continue
        if pid not in (me, parent) and "bridge_probe.py" in cmd:
            log.info("已有探活进程在运行 (pid=%d)，退出", pid)
            raise SystemExit(0)


def send_notice(wx, chat: str, text: str) -> None:
    """发微信提醒；daemon 线程 + join(15)：判聋的根因可能是 UIA 整体损坏，
    SendMsg 自己也会挂，探活本体绝不能被拖死（杀桥不等提醒）。"""
    def _send() -> None:
        try:
            wx.SendMsg(text, who=chat)
        except Exception as exc:
            log.warning("发送提醒失败: %s", exc)

    t = threading.Thread(target=_send, daemon=True)
    t.start()
    t.join(15)


def _still_bridge(pid: int) -> bool:
    """杀前复核：扫描到击杀之间隔着 send_notice 的 join（最长 15s），桥若
    在此窗口内自然退出且 PID 被 Windows 复用，不复核会杀到无关进程。"""
    import psutil
    try:
        p = psutil.Process(pid)
        snap = [(pid, p.name() or "", " ".join(p.cmdline() or []))]
    except psutil.Error:
        return False
    return bool(find_bridge_pids(snap, self_pid=-1))


def relaunch_bat() -> None:
    bat = ROOT / "tools" / "autostart-run.bat"
    flags = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS
    subprocess.Popen(["cmd", "/c", str(bat)], cwd=str(ROOT),
                     creationflags=flags)
    log.warning("已拉起守护循环: %s", bat)


def handle_deaf(wx, chat: str, v: Verdict, detector: DeafnessDetector,
                dry_run: bool) -> None:
    log.warning("判定桥聋: %d 条未响应, 最早 %.0fs, 首条 %r",
                v.unanswered, v.age_seconds, v.head_preview)
    me = os.getpid()
    pids = find_bridge_pids(scan_procs(), me)
    if pids:
        send_notice(wx, chat, NOTICE_KILL.format(n=v.unanswered,
                                                 age=v.age_seconds))
        for pid in pids:
            if dry_run:
                log.warning("DRY-RUN: would kill bridge pid=%d", pid)
            elif not _still_bridge(pid):
                log.warning("pid=%d 已不再是桥进程（退出后 PID 被复用？），跳过",
                            pid)
            else:
                log.warning("杀桥进程树 pid=%d", pid)
                kill_tree(pid)
    else:
        bats = find_autostart_pids(scan_procs(), me)
        if bats:
            log.warning("桥进程不在，但守护循环在 (pids=%s)，等它重试", bats)
        else:
            send_notice(wx, chat, NOTICE_REVIVE)
            if dry_run:
                log.warning("DRY-RUN: would relaunch autostart-run.bat")
            else:
                relaunch_bat()
    detector.reset()        # 无论是否处置都进冷却，防刷日志/连环误判


def main() -> None:
    ap = argparse.ArgumentParser(description="桥探活看门狗")
    ap.add_argument("--dry-run", action="store_true",
                    help="只记录'会杀'不真杀，上线观察期用")
    ap.add_argument("--once", action="store_true",
                    help="只检查一轮就退出（冒烟测试用）")
    args = ap.parse_args()

    dry_run = args.dry_run or os.environ.get("BRIDGE_PROBE_DRY_RUN") == "1"
    # 钳制下限：MIN_UNANSWERED=0 会让 observe 在空块上取下标直接崩进程
    # （pythonw 下无声死亡），POLL<=0 则变忙轮询。
    min_unanswered = max(1, _env_int("BRIDGE_PROBE_MIN_UNANSWERED",
                                     MIN_UNANSWERED))
    min_age = max(1.0, _env_float("BRIDGE_PROBE_MIN_AGE", MIN_AGE_SECONDS))
    poll = max(1.0, _env_float("BRIDGE_PROBE_POLL", 15.0))

    setup_logging()
    singleton_check()
    chat = load_config().listen_chat

    try:
        from wxauto import WeChat
        wx = WeChat()
        wx.AddListenChat(chat)  # 复用桥已打开的独立 ChatWnd，只读不消费
    except Exception:
        # 微信未登录/COM 未就绪：pythonw 无控制台，不留日志就无痕蒸发了。
        # 守护 bat 的探活保活下一轮会重拉，功能自愈。
        log.exception("连接微信失败，退出（等守护循环重拉）")
        raise SystemExit(1)

    detector = DeafnessDetector(min_unanswered=min_unanswered, min_age=min_age,
                                cooldown=KILL_COOLDOWN_SECONDS)
    log.info("探活启动: chat=%r dry_run=%s 阈值=%d条/%.0fs 轮询=%.0fs pid=%d",
             chat, dry_run, min_unanswered, min_age, poll, os.getpid())

    while True:
        try:
            msgs = wx.listen[chat].GetAllMessage()[-TAIL_N:]
            tail = [MsgView(msg_type=getattr(m, "type", "unknown"),
                            content=getattr(m, "content", "") or "")
                    for m in msgs]
        except Exception as exc:
            log.warning("读取聊天记录失败（不处置）: %s", exc)
            tail = None

        if tail is not None:
            v = detector.observe(tail)
            if v.deaf:
                handle_deaf(wx, chat, v, detector, dry_run)

        if args.once:
            break
        time.sleep(poll)


if __name__ == "__main__":
    main()
