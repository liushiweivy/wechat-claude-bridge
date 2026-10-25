"""桥探活看门狗（独立外部进程）。

持续只读「文件传输助手」聊天记录：连续 >=2 条用户消息没有桥的回复、且最早
一条超过 3 分钟 → 判定桥"聋了"（COM/UIA 挂死不返回，进程内无解，见
bridge/app.py MAX_RECONNECT_ROUNDS 注释），处置：

- 桥进程在 → 杀进程树，由 autostart bat 重试循环 ~20s 拉起新进程；
- 桥进程不在、守护 bat 在 → 不动作，等 bat 重试；
- 两者都不在（bat 30 次重试烧完/宿主 cmd 被关，2026-10-21 三进程齐亡事故）
  → 探活自己把 autostart-run.bat 拉回来。

读失败（微信未就绪/退出）只记日志退出码 1——区分不了"微信没了"与"桥聋"，
误杀无益；任务计划下一小时还会再拉起一轮。

默认 = 单次巡检（Windows 任务计划 WxBridgeHourlyProbe 每小时调一次，跑完
即退，系统服务托管，不会像常驻 pythonw 那样无声蒸发）：
  mutex → 读 state → 进程巡检（双灭直接拉 bat，本轮结束）→ 连微信读尾部
  → 已有未达龄块则等 200s 二次确认 → 判聋走上面处置 → 写 state 退出。
first-seen/冷却经 logs/probe_state.json 在每小时的全新进程之间传递。

--watch = 常驻 15s 循环（人工排障用），纯内存状态，不读不写 state 文件。

运行：.venv\\Scripts\\pythonw.exe tools\\bridge_probe.py [--dry-run]
      .venv\\Scripts\\python.exe  tools\\bridge_probe.py --watch
环境变量：BRIDGE_PROBE_DRY_RUN=1 / BRIDGE_PROBE_MIN_UNANSWERED=2 /
          BRIDGE_PROBE_MIN_AGE=180 / BRIDGE_PROBE_POLL=15
日志：logs/probe.log（1MB x 3 滚动）

注意：必须用本脚本路径运行，不要 python -m bridge.probe——后者 cmdline 含
'-m bridge'，会被 autostart-visible.bat 的桥防重复检查误当本体，探活杀桥
时也会匹配到自己（bridge/probe.py find_bridge_pids 注释）。
"""
from __future__ import annotations

import argparse
import ctypes
import json
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
                          Verdict, find_ambiguous_autostart_pids,
                          find_bridge_pids, kill_tree, recovery_action)

log = logging.getLogger("bridge.probe")

NOTICE_KILL = "🔧 桥无响应（{n} 条消息超 {age:.0f}s 未回复），自动重启中…"
NOTICE_REVIVE = "🔧 桥已停止且守护循环不在，正在拉起…"

MUTEX_NAME = "Global\\WxBridgeProbe"
ERROR_ALREADY_EXISTS = 183
STATE_PATH = ROOT / "logs" / "probe_state.json"
# 首轮看到未达龄块后，等多久做二次确认：跨过 MIN_AGE_SECONDS(180s) 阈值即可，
# 留 20s 余量给微信渲染/UIA 读取延迟。
CONFIRM_WAIT_SECONDS = 200.0
# 桥进程启动宽限：年龄小于 MIN_AGE + 此余量时丢弃 state 里的跨进程
# first-seen（那些时间戳属于上一代桥），防巡检落在新桥上线横幅前的窄窗误杀。
FRESH_GRACE_MARGIN = 20.0


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


def acquire_mutex(lib=None, name: str = MUTEX_NAME):
    """持有 Global 命名互斥量，进程退出时系统自动释放；已被占用返回 None。

    任务计划 IgnoreNew 之外的进程内硬单例（手动 schtasks /run、--watch 撞车
    也互斥）。lib 参数仅供测试注入 fake kernel32。句柄无需显式 CloseHandle。
    """
    if lib is None:
        lib = ctypes.WinDLL("kernel32", use_last_error=True)
        lib.CreateMutexW.restype = ctypes.c_void_p
        lib.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool,
                                     ctypes.c_wchar_p]
        lib.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = lib.CreateMutexW(None, True, name)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        lib.CloseHandle(handle)
        return None
    return handle


def load_state() -> dict | None:
    """读上一轮巡检持久化的 first-seen/冷却；无文件返回 None，损坏记日志 None
    （退化为全新基线，可能晚一轮判聋，绝不因脏数据 age 虚高误杀）。"""
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("state 文件不可读，按全新基线: %s", exc)
        return None
    return data if isinstance(data, dict) else None


def save_state(detector: DeafnessDetector) -> None:
    """原子写 state（tmp + replace）；失败只记日志——state 丢了最坏晚一轮判聋。"""
    try:
        tmp = STATE_PATH.with_name("probe_state.json.tmp")
        tmp.write_text(json.dumps(detector.export_state(), ensure_ascii=False),
                       encoding="utf-8")
        os.replace(tmp, STATE_PATH)
    except OSError:
        log.exception("写 state 文件失败（不影响本轮处置）")


def scan_procs() -> list[tuple]:
    """(pid, name, cmdline, cwd) 快照；cwd 三态：字符串 / None / ""。

    None = AccessDenied（更高权限 shell 拉起的进程，普通用户探活读不到
    cwd，find_ambiguous_autostart_pids 据此走保守 wait_bat）；
    "" = 其他读错或取不到。cwd 供 find_autostart_pids 识别相对路径拉起的
    守护 bat（cmdline 不含项目目录名），并排除 claude-remote-web 同名 bat。
    """
    import psutil
    out: list[tuple] = []
    for p in psutil.process_iter(["name"]):
        try:
            cwd = p.cwd() or ""
        except psutil.AccessDenied:
            cwd = None
        except psutil.Error:
            cwd = ""
        try:
            out.append((p.pid, p.name() or "",
                        " ".join(p.cmdline() or []), cwd))
        except psutil.Error:
            continue
    return out


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


def youngest_bridge_age(pids: list[int], clock=None) -> float | None:
    """桥进程里最年轻者的年龄（秒）；PID 全部取不到返回 None。

    用于新桥宽限：任务计划每小时一个全新进程，它载入的 probe_state.json
    可能装着上一代桥的未回复块时间戳，巡检若落在 bat 刚拉起新桥、上线
    横幅（🤖，本身即断块回复）尚未发出的窄窗里，陈旧 age 会让新桥首轮
    就判死。2026-10-21 code review LOW：年龄 < min_age + 20s 的桥按
    干净基线对待。
    """
    if clock is None:
        clock = time.time
    import psutil
    ages: list[float] = []
    for pid in pids:
        try:
            ages.append(clock() - psutil.Process(pid).create_time())
        except psutil.Error:
            continue
    if pids and not ages:
        # 桥 PID 在但 create_time 全部读不到：宽限静默失效，陈旧 state 直接
        # 生效会重开误杀窗口，必须留日志痕迹（二轮复审 LOW）。
        log.warning("全部桥进程(pid=%s) create_time 读取失败，新桥宽限本轮失效",
                    pids)
    return min(ages) if ages else None


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
    elif recovery_action(scan_procs(), me) == "wait_bat":
        log.warning("桥进程不在，但守护循环在，等它重试")
    else:
        send_notice(wx, chat, NOTICE_REVIVE)
        if dry_run:
            log.warning("DRY-RUN: would relaunch autostart-run.bat")
        else:
            relaunch_bat()
    detector.reset()        # 无论是否处置都进冷却，防刷日志/连环误判


def connect_wx(chat: str):
    """初始化 wxauto 并绑定监听会话；失败由调用方记日志退出。"""
    from wxauto import WeChat
    wx = WeChat()
    wx.AddListenChat(chat)          # 复用桥已打开的独立 ChatWnd，只读不消费
    return wx


def read_tail(wx, chat: str) -> list[MsgView] | None:
    """尾部 TAIL_N 条消息；读取失败返回 None（调用方按读失败处理，不处置）。"""
    try:
        msgs = wx.listen[chat].GetAllMessage()[-TAIL_N:]
    except Exception as exc:
        log.warning("读取聊天记录失败（不处置）: %s", exc)
        return None
    return [MsgView(msg_type=getattr(m, "type", "unknown"),
                    content=getattr(m, "content", "") or "")
            for m in msgs]


def run_once(wx, chat: str, detector: DeafnessDetector, dry_run: bool,
             wait: float = CONFIRM_WAIT_SECONDS,
             sleeper=time.sleep) -> Verdict | None:
    """桥存活时的一轮消息检测（假定已连微信）。

    首轮已见未达龄块（>=MIN_UNANSWERED 但 age 不够）→ wait 秒后二次确认，
    让同一轮任务就能跨 180s 阈值判定，不必等下一小时。返回 None=读取失败。
    """
    tail = read_tail(wx, chat)
    if tail is None:
        return None
    v = detector.observe(tail)
    if not v.deaf and v.unanswered >= detector.min_unanswered:
        log.info("发现 %d 条未回复（首条约 %.0fs），%.0fs 后二次确认",
                 v.unanswered, v.age_seconds, wait)
        sleeper(wait)
        tail = read_tail(wx, chat)
        if tail is None:
            # 不能沿用等待前的旧判定记成"巡检正常"：返回 None 让调用方
            # 退出码 1、不写 state（不杀、不刷新时间戳，下一小时重新观察）。
            log.warning("二次确认读取失败，本轮不判定，退出")
            return None
        v = detector.observe(tail)
    if v.deaf:
        handle_deaf(wx, chat, v, detector, dry_run)
    else:
        log.info("巡检正常：尾部未回复块=%d 条，退出", v.unanswered)
    return v


def run_once_main(dry_run: bool, min_unanswered: int,
                  min_age: float) -> int:
    """任务计划入口：单次巡检全流程。"""
    setup_logging()
    try:
        mutex = acquire_mutex()
    except OSError:
        # pythonw 无控制台：异常必须落文件日志，否则失败无任何痕迹。
        log.exception("获取互斥量失败，退出")
        return 1
    if mutex is None:
        log.info("已有巡检实例在运行（mutex 占用），退出")
        return 0
    detector = DeafnessDetector(
        min_unanswered=min_unanswered, min_age=min_age,
        cooldown=KILL_COOLDOWN_SECONDS, clock=time.time,
        state=load_state())

    procs = scan_procs()
    me = os.getpid()
    action = recovery_action(procs, me)
    if action == "relaunch":
        log.warning("桥与守护 bat 均不在，拉起 autostart-run.bat，本轮不做判聋")
        if dry_run:
            log.warning("DRY-RUN: would relaunch autostart-run.bat")
        else:
            relaunch_bat()
        # 刚拉起的桥给完整宽限：清掉历史 first-seen，防其来不及回复/横幅丢失
        # 时下一轮用陈旧时间戳误判。
        save_state(DeafnessDetector(clock=time.time))
        return 0
    if action == "wait_bat":
        ambiguous = find_ambiguous_autostart_pids(procs, me)
        if ambiguous:
            log.warning("桥不在；守护 bat 基名命中但 cwd 不可读 pid=%s"
                        "（更高权限 shell 拉起？），保守等待不重复拉起 bat",
                        ambiguous)
        else:
            log.warning("桥不在但守护 bat 在重试，本轮退出（不判聋、不写 state）")
        return 0

    # 新桥宽限：state 的陈旧时间戳属于上一代桥时，巡检若落在上线横幅前的
    # 窄窗会首轮判死。桥进程年龄 < min_age+20s 一律丢弃历史 first-seen。
    age = youngest_bridge_age(find_bridge_pids(procs, me))
    if age is not None and age < min_age + FRESH_GRACE_MARGIN:
        log.warning("桥进程仅启动 %.0fs（宽限 %.0fs），丢弃跨进程历史 "
                    "first-seen，按新基线", age, min_age + FRESH_GRACE_MARGIN)
        detector.forget_first_seen()

    chat = load_config().listen_chat
    try:
        wx = connect_wx(chat)
    except Exception:
        # 微信未登录/COM 未就绪：pythonw 无控制台，文件日志是唯一痕迹。
        log.exception("连接微信失败，退出（下一小时巡检再来）")
        return 1
    log.info("单次巡检启动: chat=%r dry_run=%s 阈值=%d条/%.0fs pid=%d",
             chat, dry_run, min_unanswered, min_age, os.getpid())
    v = run_once(wx, chat, detector, dry_run)
    if v is None:
        return 1
    save_state(detector)
    return 0


def run_watch(dry_run: bool, poll: float, min_unanswered: int,
              min_age: float) -> int:
    """常驻 15s 循环（人工排障）：纯内存状态，不碰 state 文件。"""
    setup_logging()
    try:
        mutex = acquire_mutex()
    except OSError:
        log.exception("获取互斥量失败，退出")
        return 1
    if mutex is None:
        log.info("已有巡检实例在运行（mutex 占用），退出")
        return 0
    chat = load_config().listen_chat
    try:
        wx = connect_wx(chat)
    except Exception:
        log.exception("连接微信失败，退出")
        return 1
    detector = DeafnessDetector(min_unanswered=min_unanswered,
                                min_age=min_age,
                                cooldown=KILL_COOLDOWN_SECONDS,
                                clock=time.monotonic)
    log.info("探活常驻启动: chat=%r dry_run=%s 阈值=%d条/%.0fs 轮询=%.0fs pid=%d",
             chat, dry_run, min_unanswered, min_age, poll, os.getpid())
    while True:
        tail = read_tail(wx, chat)
        if tail is not None:
            v = detector.observe(tail)
            if v.deaf:
                handle_deaf(wx, chat, v, detector, dry_run)
        time.sleep(poll)


def main() -> None:
    ap = argparse.ArgumentParser(description="桥探活看门狗")
    ap.add_argument("--dry-run", action="store_true",
                    help="只记录'会杀'不真杀，上线观察期用")
    ap.add_argument("--once", action="store_true",
                    help="单次巡检后退出（默认行为，任务计划用）")
    ap.add_argument("--watch", action="store_true",
                    help="常驻 15s 轮询循环（人工排障用，纯内存状态）")
    args = ap.parse_args()

    dry_run = args.dry_run or os.environ.get("BRIDGE_PROBE_DRY_RUN") == "1"
    # 钳制下限：MIN_UNANSWERED=0 会让 observe 在空块上取下标直接崩进程
    # （pythonw 下无声死亡），POLL<=0 则变忙轮询。
    min_unanswered = max(1, _env_int("BRIDGE_PROBE_MIN_UNANSWERED",
                                     MIN_UNANSWERED))
    min_age = max(1.0, _env_float("BRIDGE_PROBE_MIN_AGE", MIN_AGE_SECONDS))
    poll = max(1.0, _env_float("BRIDGE_PROBE_POLL", 15.0))

    if args.watch:
        code = run_watch(dry_run, poll, min_unanswered, min_age)
    else:
        code = run_once_main(dry_run, min_unanswered, min_age)
    raise SystemExit(code)


if __name__ == "__main__":
    main()
