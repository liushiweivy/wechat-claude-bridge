"""主循环：微信消息 → 指令分发 → ClaudeRunner → 结果回微信。

wxauto(UIA) 的收发全部留在主线程；runner 线程只把完成事件放进队列。
"""
from __future__ import annotations

import logging
import queue
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .config import Config, load_config
from .core import HELP_TEXT, parse_instruction, resolve_cd
from .gateway import WxGateway
from .runner import ClaudeRunner, TaskStatus

LOG_DIR = Path(__file__).resolve().parent.parent / "logs"

# 只有本人在文件传输助手发的消息(self)和普通好友消息(friend)可驱动任务；
# 时间提示、撤回提示、系统消息等一律忽略，防止把"以下为新消息"当成任务。
ACTIONABLE_TYPES = frozenset({"self", "friend"})


def is_actionable(msg_type: str) -> bool:
    return msg_type in ACTIONABLE_TYPES


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
            # 2) 拉取新消息（网关异常 → 重连）
            try:
                messages = self.gw.pull()
            except Exception as exc:
                log.warning("拉取消息失败，10s 后重连: %s", exc)
                self._reconnect_loop(log)
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

    def _reconnect_loop(self, log: logging.Logger) -> None:
        for delay in (10, 20, 30):
            time.sleep(delay)
            try:
                nickname = self.gw.reconnect()
                log.info("微信重连成功：%s", nickname)
                self._safe_send("🔄 微信连接已恢复\n断连期间发的消息可能已丢失，请重发最后一条指令")
                return
            except Exception as exc:
                log.warning("重连失败(%ss): %s", delay, exc)

    # ---------- 指令 ----------
    def dispatch(self, raw: str) -> None:
        ins = parse_instruction(raw)
        log = logging.getLogger("bridge")

        if ins.kind == "task":
            self._start_task(ins.text, ins.sudo)
        elif ins.kind == "help":
            self._safe_send(HELP_TEXT)
        elif ins.kind == "new":
            self.session_id = ""
            self._safe_send("🆕 已开启新对话（下次任务不携带历史上下文）")
        elif ins.kind == "cancel":
            self._safe_send("🚫 已取消" if self.runner.cancel() else "当前没有执行中的任务")
        elif ins.kind == "status":
            self._send_status()
        elif ins.kind == "cd":
            self._change_cwd(ins.text)
        else:
            self._safe_send(f"⚠️ {ins.text or '无法识别的指令'}，发送 /help 查看用法")
        log.info("dispatch: %s%s <- %.80s", ins.kind,
                 "(sudo)" if ins.sudo else "", raw.replace("\n", "\\n"))

    def _start_task(self, prompt: str, sudo: bool) -> None:
        if self.runner.status.is_busy:
            elapsed = int(time.time() - self.runner.status.started_at)
            self._safe_send(f"⏳ 上一个任务仍在执行（已 {elapsed}s），发送 /cancel 可取消后重发")
            return
        preview = prompt.replace("\n", " ")[:60]
        full_access = sudo or self.cfg.default_mode == "bypassPermissions"
        mode = "完全权限(全盘)" if full_access else "常规(可改文件)"
        ok = self.runner.start(
            prompt, self.cwd, sudo, self.session_id,
            on_done=lambda st: self.done_q.put(st),
        )
        if ok:
            self._safe_send(f"⏳ 已开工【{mode}】\n{preview}")

    def _change_cwd(self, sub: str) -> None:
        if not sub:
            self._safe_send(f"当前工作目录：{self.cwd}")
            return
        try:
            target = resolve_cd(sub, self.cwd, list(self.cfg.allowed_dirs))
        except ValueError as exc:
            self._safe_send(f"⚠️ {exc}")
            return
        if target != self.cwd:
            self.cwd = target
            self.session_id = ""    # 换目录 = 换上下文
            self._safe_send(f"📁 工作目录切换到：{target}\n（已重置对话上下文）")
        else:
            self._safe_send(f"📁 仍在：{target}")

    def _send_status(self) -> None:
        st = self.runner.status
        state = {"idle": "空闲", "running": "执行中", "done": "上次完成",
                 "error": "上次出错", "canceled": "已取消", "timeout": "超时"}.get(st.state, st.state)
        lines = [f"状态：{state}", f"工作目录：{self.cwd}"]
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

    def _report(self, st: TaskStatus) -> None:
        elapsed = int(st.finished_at - st.started_at) if st.finished_at else 0
        if st.state == "done":
            tool_info = f"，工具：{'、'.join(st.tools)}" if st.tools else ""
            head = f"✅ 完成（{elapsed}s{tool_info}）"
            self._safe_send(f"{head}\n\n{st.result}" if st.result else head)
            if not st.result.strip():
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
