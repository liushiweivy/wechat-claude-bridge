"""主循环：微信消息 → 指令分发 → ClaudeRunner → 结果回微信。

wxauto(UIA) 的收发全部留在主线程；runner 线程只把完成事件放进队列。
"""
from __future__ import annotations

import logging
import queue
import subprocess
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .config import Config, load_config
from .core import (HELP_TEXT, cap_reply, clean_child_env, find_newer_session,
                   parse_instruction, resolve_cd, transcript_path,
                   with_reply_hint)
from .gateway import WxGateway
from .runner import ClaudeRunner, TaskStatus
from .winconsole import find_session_id
from .winrunner import WindowRunner

LOG_DIR = Path(__file__).resolve().parent.parent / "logs"

# 只有本人在文件传输助手发的消息(self)和普通好友消息(friend)可驱动任务；
# 时间提示、撤回提示、系统消息等一律忽略，防止把"以下为新消息"当成任务。
ACTIONABLE_TYPES = frozenset({"self", "friend"})

# /open 开新窗口后等用户在窗口里发首条消息（落盘 jsonl）的认领有效期
ADOPT_WINDOW_SECONDS = 1800.0
# 屏幕抓取认领（transcript 不落盘的兜底）：向待认领窗口注入 /status、
# 从弹层抠 Session ID 的最小间隔，避免每轮主循环都往窗口打字
STATUS_SCRAPE_INTERVAL = 30.0
# 注入 /status 后等弹层渲染的秒数
STATUS_SCRAPE_WAIT = 2.5
# 连续重连失败多少轮后自杀：UIA 断连有时是进程级 COM 状态损坏
# （2026-10-18 实测：reconnect 每次新建 WeChat() 仍连不上，同 venv 新
# 进程秒连），此时唯一解是换进程。autostart-run.bat 的 30 次重试循环
# 会在 ~20s 后自动拉起全新桥进程。
MAX_RECONNECT_ROUNDS = 3


def _console_url() -> str:
    """会话台本地地址（带 token）：任务执行时在那里可实时旁观桥会话。"""
    try:
        token = (Path(r"E:\1workspace\claude-remote-web\data\token.txt")
                 .read_text(encoding="utf-8").strip())
    except OSError:
        token = ""
    return (f"http://127.0.0.1:8765/?token={token}" if token
            else "http://127.0.0.1:8765")


def _spawn_claude_window(claude_bin: str, cwd: Path, session_id: str = "") -> None:
    """在电脑桌面上开一个新的可见 CC 窗口；有 session_id 则 resume 同一对话。

    必须经 wscript 起窗口：桥自身常是无窗口的隐藏进程，直接 start 出的
    窗口会继承隐藏状态（进程在、桌面看不到，2026-10-18 实测）。
    env 必须剥掉 CLAUDE* 标记，否则窗口被当成子会话不写 transcript。
    """
    vbs = Path(__file__).resolve().parent.parent / "tools" / "open-cc-window.vbs"
    subprocess.Popen(["wscript.exe", str(vbs), str(cwd), claude_bin,
                      session_id or ""], env=clean_child_env())


def is_actionable(msg_type: str) -> bool:
    return msg_type in ACTIONABLE_TYPES


def sessions_log() -> logging.Logger:
    """任务↔会话留痕（logs/sessions.log）：每条任务一行 START、一行 DONE，
    带 session_id 与 transcript jsonl 路径，事后可回查哪条微信消息开了哪个会话。"""
    logger = logging.getLogger("bridge.sessions")
    if not logger.handlers:
        LOG_DIR.mkdir(exist_ok=True)
        handler = RotatingFileHandler(LOG_DIR / "sessions.log",
                                      maxBytes=500_000, backupCount=3,
                                      encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        logger.addHandler(handler)
        logger.propagate = False
        logger.setLevel(logging.INFO)
    return logger


def setup_logging() -> None:
    import os
    LOG_DIR.mkdir(exist_ok=True)
    # Windows 控制台默认 GBK，中文日志会触发 UnicodeEncodeError
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
    debug = os.environ.get("BRIDGE_DEBUG") == "1"
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[
            RotatingFileHandler(LOG_DIR / "bridge.log", maxBytes=1_000_000,
                                backupCount=3, encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )
    for noisy in ("comtypes", "uiautomation"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


class BridgeApp:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.cwd = Path(cfg.workspace).resolve()
        self.session_id = ""
        self.gw = WxGateway(cfg.listen_chat, cfg.max_msg_chars)
        self.runner = ClaudeRunner(
            cfg.claude_bin, cfg.default_mode, cfg.task_timeout_seconds, cfg.extra_args
        )
        self.done_q: queue.Queue[TaskStatus] = queue.Queue()
        # /open 开新窗口后等待认领其会话的时间点（0 = 无待认领）
        self._pending_open_since = 0.0
        # 桥自己任务产生过的 session_id（认领窗口会话时排除，防误认领）
        self._own_session_ids: set[str] = set()
        # 窗口执行路径：session_id → 窗口里 claude.exe 的 PID
        self.window = WindowRunner(timeout=cfg.task_timeout_seconds)
        self._window_pids: dict[str, int] = {}
        # 上次向待认领窗口注入 /status 抠会话的时间点（节流用）
        self._last_status_scrape = 0.0
        # 连续重连失败轮数（达到 MAX_RECONNECT_ROUNDS 就自杀换进程）
        self._reconnect_fail_rounds = 0

    # ---------- 生命周期 ----------
    def run(self) -> None:
        log = logging.getLogger("bridge")
        nickname = self.gw.connect()
        log.info("已连接微信：%s，监听「%s」，工作目录 %s",
                 nickname, self.cfg.listen_chat, self.cwd)
        self._safe_send(f"🤖 Claude 桥已上线\n工作目录：{self.cwd}\n发送 /help 查看用法")

        while True:
            # 1) 完成事件（在主线程回微信，避免跨线程操作 UIA）
            self._drain_done()
            # 1.5) /open 的新窗口落盘了会话就认领
            self._maybe_adopt_window_session()
            # 2) 拉取新消息（网关异常 → 重连）
            try:
                messages = self.gw.pull()
            except Exception as exc:
                log.warning("拉取消息失败，10s 后重连: %s", exc)
                if self._reconnect_loop(log):
                    self._reconnect_fail_rounds = 0
                else:
                    self._reconnect_fail_rounds += 1
                    if self._reconnect_fail_rounds >= MAX_RECONNECT_ROUNDS:
                        log.error(
                            "连续 %d 轮重连失败，进程级 COM/UIA 状态已损坏；"
                            "主动退出，由自启重试循环拉起全新进程",
                            self._reconnect_fail_rounds)
                        raise SystemExit(1)
                continue
            # 3) 分发
            for m in messages:
                if not is_actionable(m.msg_type):
                    continue
                try:
                    self.dispatch(m.content)
                except Exception:
                    log.exception("处理消息异常: %s", m.content[:80])
                    self._safe_send("⚠️ 处理这条消息时出错，详情见 logs/bridge.log")
            time.sleep(self.cfg.poll_seconds)

    def _reconnect_loop(self, log: logging.Logger) -> bool:
        """按 10/20/30s 退避重连；成功返回 True，三次全败返回 False。"""
        for delay in (10, 20, 30):
            time.sleep(delay)
            try:
                nickname = self.gw.reconnect()
                log.info("微信重连成功：%s", nickname)
                self._safe_send("🔄 微信连接已恢复\n断连期间发的消息可能已丢失，请重发最后一条指令")
                return True
            except Exception as exc:
                log.warning("重连失败(%ss): %s", delay, exc)
        return False

    # ---------- 指令 ----------
    def dispatch(self, raw: str) -> None:
        ins = parse_instruction(raw)
        log = logging.getLogger("bridge")

        if ins.kind == "ignore":
            log.info("忽略自动通知消息 <- %.80s", raw.replace("\n", "\\n"))
            return
        if ins.kind == "task":
            self._start_task(ins.text, ins.sudo)
        elif ins.kind == "help":
            self._safe_send(HELP_TEXT)
        elif ins.kind == "new":
            self.session_id = ""
            self._safe_send("🆕 已开启新对话（下次任务不携带历史上下文）")
        elif ins.kind == "cancel":
            canceled = self.window.cancel() or self.runner.cancel()
            self._safe_send("🚫 已取消" if canceled else "🚫 当前没有执行中的任务")
        elif ins.kind == "status":
            self._send_status()
        elif ins.kind == "open":
            self._open_window()
        elif ins.kind == "cd":
            self._change_cwd(ins.text)
        else:
            self._safe_send(f"⚠️ {ins.text or '无法识别的指令'}，发送 /help 查看用法")
        log.info("dispatch: %s%s <- %.80s", ins.kind,
                 "(sudo)" if ins.sudo else "", raw.replace("\n", "\\n"))

    def _is_busy(self) -> bool:
        return self.runner.status.is_busy or self.window.status.is_busy

    def _window_pid_for(self, session_id: str) -> int:
        """找该会话对应窗口里 claude.exe 的 PID。

        WMI 实测运行中的 claude.exe 命令行不含参数（launcher 抹掉了），
        --resume <sid> 只能从窗口宿主 cmd.exe 的 /k 命令行里看到，
        所以扫 cmd.exe 命中后再取它正在跑的 claude.exe 子进程
        （注入与存活检测都盯 claude 本体；cmd 还在但 claude 已退出 = 未绑定）。
        先查缓存，再全量扫描；桥重启后窗口还在，靠扫描重新绑定。找不到返回 0。"""
        if not session_id:
            return 0
        cached = self._window_pids.get(session_id, 0)
        if cached and self._pid_matches(cached, session_id):
            return cached
        pid = self._scan_window_pid(f"--resume {session_id}")
        if pid:
            self._window_pids[session_id] = pid
        else:
            self._window_pids.pop(session_id, None)
        return pid

    @staticmethod
    def _scan_window_pid(needle: str) -> int:
        """扫 cmd.exe 窗口命令行，命中 needle 则返回其 claude.exe 子进程 PID。"""
        import psutil
        for proc in psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                if proc.info["name"] != "cmd.exe":
                    continue
                cmdline = " ".join(proc.info["cmdline"] or [])
            except (psutil.Error, TypeError):
                continue
            if needle not in cmdline:
                continue
            try:
                for child in psutil.Process(proc.info["pid"]).children():
                    if child.name() == "claude.exe":
                        return child.pid
            except psutil.Error:
                continue
        return 0

    @staticmethod
    def _pid_matches(pid: int, session_id: str) -> bool:
        """缓存 PID 仍有效：claude 活着且父 cmd 仍是对应窗口。

        绑定时已严格匹配过，复核只需确认没换进程：resume 窗口看
        --resume <sid>，全新窗口（无 --resume）看 /open 专用标题。"""
        import psutil
        try:
            proc = psutil.Process(pid)
            if proc.name() != "claude.exe":
                return False
            parent = proc.parent()
            if parent is None or parent.name() != "cmd.exe":
                return False
            cmdline = " ".join(parent.cmdline())
            return (f"--resume {session_id}" in cmdline
                    or "title Claude (wechat)" in cmdline)
        except psutil.Error:
            return False

    def _start_task(self, prompt: str, sudo: bool) -> None:
        if self._is_busy():
            busy = self.window.status if self.window.status.is_busy \
                else self.runner.status
            elapsed = int(time.time() - busy.started_at)
            self._safe_send(f"⏳ 上一个任务仍在执行（已 {elapsed}s），发送 /cancel 可取消后重发")
            return
        preview = prompt.replace("\n", " ")[:60]
        full_access = sudo or self.cfg.default_mode == "bypassPermissions"
        mode = "完全权限(全盘)" if full_access else "常规(可改文件)"
        sessions_log().info("START cwd=%s resume=%s sudo=%s prompt=%s",
                            self.cwd, self.session_id[:8] or "-",
                            int(sudo), prompt.replace("\n", " ")[:80])

        # 有绑定窗口的会话：提示词打进窗口，由窗口里的交互 claude 执行
        win_pid = self._window_pid_for(self.session_id)
        if win_pid:
            ok = self.window.start(
                prompt, self.cwd, self.session_id, win_pid,
                reply_limit=self.cfg.max_reply_chars,
                on_done=lambda st: self.done_q.put(st),
            )
            if ok:
                self._safe_send(f"⏳ 已开工【窗口·{mode}】\n{preview}")
                return
            self._safe_send("⚠️ 窗口任务启动失败，改走无头执行")

        ok = self.runner.start(
            with_reply_hint(prompt, self.cfg.max_reply_chars),
            self.cwd, sudo, self.session_id,
            on_done=lambda st: self.done_q.put(st),
        )
        if ok:
            self._safe_send(f"⏳ 已开工【{mode}】\n{preview}")

    def _change_cwd(self, sub: str) -> None:
        if not sub:
            self._safe_send(f"📁 当前工作目录：{self.cwd}")
            return
        try:
            target = resolve_cd(sub, self.cwd, list(self.cfg.allowed_dirs))
        except ValueError as exc:
            self._safe_send(f"⚠️ {exc}")
            return
        if target != self.cwd:
            self.cwd = target
            self.session_id = ""    # 换目录 = 换上下文
            self._pending_open_since = 0.0   # 待认领窗口属于旧目录，作废
            self._safe_send(f"📁 工作目录切换到：{target}\n（已重置对话上下文）")
        else:
            self._safe_send(f"📁 仍在：{target}")

    def _open_window(self) -> None:
        """/open：在电脑桌面开一个与桥当前对话对应的可见 CC 窗口。"""
        if self._is_busy():
            self._safe_send("⏳ 任务执行中，完成后再 /open（窗口与任务共用同一对话，避免并发冲突）")
            return
        sid = self.session_id
        try:
            _spawn_claude_window(self.cfg.claude_bin, self.cwd, sid)
        except OSError as exc:
            self._safe_send(f"⚠️ 打开窗口失败: {exc}")
            return
        if sid:
            self._safe_send(
                f"🖥️ 已打开对应对话的 CC 窗口（会话 {sid[:8]}…）\n"
                "之后微信派的任务会直接打进这个窗口执行，过程实时可见；"
                f"网页旁观：{_console_url()}")
        else:
            self._pending_open_since = time.time()
            self._safe_send(
                "🖥️ 已打开新的 CC 窗口，在窗口里发出第一条消息后我会自动认领；"
                f"网页旁观：{_console_url()}")

    def _maybe_adopt_window_session(self) -> None:
        """认领 /open 新窗口里产生的会话（用户在窗口发出首条消息后才落盘）。"""
        if not self._pending_open_since or self.session_id:
            return
        if self._is_busy():
            # 任务运行中它的 jsonl 就是最新文件，此时认领必错（2026-10-18 实测）
            return
        if time.time() - self._pending_open_since > ADOPT_WINDOW_SECONDS:
            self._pending_open_since = 0.0
            return
        pending_since = self._pending_open_since
        sid = find_newer_session(self.cwd, pending_since,
                                 exclude=frozenset(self._own_session_ids))
        if not sid:
            # 兜底：v2.1.283 出现过新窗口不写 transcript 的异常，
            # jsonl 认领不到时改为向窗口注入 /status、从屏幕抠 Session ID
            sid = self._scrape_adopt_fresh_window(pending_since)
        if sid:
            self.session_id = sid
            self._pending_open_since = 0.0
            self._bind_fresh_window(sid, pending_since)
            logging.getLogger("bridge").info("认领 PC 窗口会话: %s", sid)
            self._safe_send(f"🔗 已认领电脑窗口的对话（会话 {sid[:8]}…）\n之后微信任务与它同一上下文")

    def _scrape_adopt_fresh_window(self, pending_since: float) -> str:
        """屏幕抓取认领：往待认领窗口打 /status，从弹层文本抠 Session ID。

        只在 jsonl 认领失败时兜底（transcript 异常不落盘期间）。
        注入/抓屏子进程复用 WindowRunner 的同包助手；抠完发 Esc 关掉弹层。
        节流 STATUS_SCRAPE_INTERVAL 秒一次，找不到返回 ""。"""
        if time.time() - self._last_status_scrape < STATUS_SCRAPE_INTERVAL:
            return ""
        pid = self._find_fresh_window_pid(pending_since)
        if not pid:
            return ""
        self._last_status_scrape = time.time()
        try:
            self.window._inject(pid, "/status")
            time.sleep(STATUS_SCRAPE_WAIT)
            sid = find_session_id(self.window._read_screen(pid))
        except Exception:
            logging.getLogger("bridge").debug("/status 抓取认领失败",
                                              exc_info=True)
            sid = ""
        finally:
            self.window._escape(pid)   # 关掉 /status 弹层（无论成败）
        if sid and sid in self._own_session_ids:
            return ""
        if sid:
            logging.getLogger("bridge").info(
                "屏幕抓取认领到会话: %s (pid=%s)", sid, pid)
        return sid

    def _bind_fresh_window(self, session_id: str, pending_since: float) -> None:
        """认领全新窗口（无 --resume）的会话后，把窗口 claude 进程绑上。"""
        pid = self._find_fresh_window_pid(pending_since)
        if pid:
            self._window_pids[session_id] = pid
            logging.getLogger("bridge").info(
                "绑定全新窗口: session=%s claude_pid=%s", session_id, pid)

    @staticmethod
    def _find_fresh_window_pid(pending_since: float) -> int:
        """按 /open 专用标题「Claude (wechat)」+ 创建时间晚于 /open 时刻，
        找最新一个匹配 cmd 窗口的 claude.exe 子进程 PID；没有返回 0。

        全新窗口的 cmd 命令行没有 --resume，_window_pid_for 扫不到。"""
        import psutil
        best_pid, best_ctime = 0, pending_since
        for proc in psutil.process_iter(["pid", "name", "cmdline", "create_time"]):
            try:
                if proc.info["name"] != "cmd.exe":
                    continue
                cmdline = " ".join(proc.info["cmdline"] or [])
                if "title Claude (wechat)" not in cmdline:
                    continue
                ctime = proc.info["create_time"] or 0
            except (psutil.Error, TypeError):
                continue
            if ctime <= best_ctime:
                continue
            try:
                for child in psutil.Process(proc.info["pid"]).children():
                    if child.name() == "claude.exe":
                        best_pid, best_ctime = child.pid, ctime
            except psutil.Error:
                continue
        return best_pid

    def _send_status(self) -> None:
        st = self.window.status if self.window.status.is_busy else self.runner.status
        state = {"idle": "空闲", "running": "执行中", "done": "上次完成",
                 "error": "上次出错", "canceled": "已取消", "timeout": "超时"}.get(st.state, st.state)
        lines = [f"🤖 状态：{state}", f"工作目录：{self.cwd}"]
        if self.session_id and self._window_pid_for(self.session_id):
            lines.append("窗口：已绑定（任务打进窗口执行）")
        if self.session_id:
            lines.append(f"会话：{self.session_id[:8]}…（/new 重置）")
        if st.is_busy:
            lines.append(f"当前任务已执行 {int(time.time() - st.started_at)}s")
            if st.tools:
                lines.append("已用工具：" + "、".join(st.tools))
        elif st.result:
            lines.append(f"上次用时：{int(st.finished_at - st.started_at)}s")
        self._safe_send("\n".join(lines))

    # ---------- 结果回传 ----------
    def _drain_done(self) -> None:
        drained = False
        while True:
            try:
                st = self.done_q.get_nowait()
            except queue.Empty:
                break
            drained = True
            self._report(st)
        if drained:
            # 保留可续接的会话 ID（成功/部分成功都保留；取消则不留）
            if st.state in {"done", "error", "timeout"} and st.session_id:
                self.session_id = st.session_id
                self._own_session_ids.add(st.session_id)

    def _report(self, st: TaskStatus) -> None:
        elapsed = int(st.finished_at - st.started_at) if st.finished_at else 0
        sessions_log().info(
            "DONE state=%s elapsed=%ss session=%s jsonl=%s",
            st.state, elapsed, st.session_id or "-",
            transcript_path(self.cwd, st.session_id) if st.session_id else "-")
        if st.state == "done":
            tool_info = f"，工具：{'、'.join(st.tools)}" if st.tools else ""
            head = f"✅ 完成（{elapsed}s{tool_info}）"
            body = cap_reply(st.result, self.cfg.max_reply_chars)
            self._safe_send(f"{head}\n\n{body}" if body else head)
            if not body:
                self._safe_send("✅ 任务结束，但 Claude 没有返回文本")
        elif st.state == "canceled":
            self._safe_send(f"🚫 任务已取消（{elapsed}s）")
        elif st.state == "timeout":
            self._safe_send(f"⏰ {st.error}")
        else:
            self._safe_send(f"❌ 执行失败（{elapsed}s）\n{st.error or '未知错误'}")

    def _safe_send(self, text: str) -> None:
        try:
            self.gw.send(text)
        except Exception:
            logging.getLogger("bridge").exception("发送微信失败")


def main() -> None:
    setup_logging()
    log = logging.getLogger("bridge")
    cfg = load_config()
    try:
        BridgeApp(cfg).run()
    except KeyboardInterrupt:
        log.info("收到 Ctrl+C，桥已停止")


if __name__ == "__main__":
    main()
