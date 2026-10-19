"""窗口任务执行器：把微信提示词注入已打开的 CC 窗口，由窗口里的交互
claude 亲自执行；桥轮询 transcript 把结果取回发微信。

与 ClaudeRunner（无头 claude -p）并列的另一条执行路径；app 在绑定了
窗口的会话上优先走这里。并发模型与 ClaudeRunner 相同：单任务串行 +
代际号防迟到回调。
"""
from __future__ import annotations

import logging
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import psutil

from .core import last_assistant_text, transcript_path, with_reply_hint
from .runner import TaskStatus
from .winconsole import done_count, extract_last_answer

log = logging.getLogger("bridge.winrunner")

POLL_SECONDS = 2.0
STABLE_POLLS = 2          # transcript 连续 2 次轮询无增长视为本轮写完
INJECT_TIMEOUT = 30
# 回车吞键补发：TUI 偶发吃掉注入的回车（文字留在输入框不提交，
# 2026-10-18 窗口 2188 实测，补发一次即提交）。注入后若迟迟看不到
# 「开始执行」的迹象（transcript 出现 / 屏幕 spinner / done 标记增加），
# 每隔 CONFIRM_RETRY_SECONDS 补发一个裸回车，最多 CONFIRM_RESENDS 次。
# 空输入上的裸回车是 no-op，误补发无害。
CONFIRM_RESENDS = 3
CONFIRM_RETRY_SECONDS = 3.0
CONFIRM_SECONDS = 15.0


@dataclass
class _WinTask:
    status: TaskStatus
    gen: int
    pid: int = 0


def _helper_cmd(python_bin: str, action: str, pid: int,
                text_file: str | None = None) -> list[str]:
    cmd = [python_bin, "-m", "bridge.winconsole", action, str(pid)]
    if text_file:
        cmd.append(text_file)
    return cmd


class WindowRunner:
    def __init__(self, python_bin: str | None = None, timeout: int = 1800) -> None:
        self.python_bin = python_bin or sys.executable
        self.timeout = timeout
        self.status = TaskStatus()
        self._active: _WinTask | None = None
        self._gen = 0
        self._lock = threading.Lock()

    def start(self, prompt: str, cwd: Path, session_id: str, pid: int,
              reply_limit: int = 200, on_done=None) -> bool:
        with self._lock:
            if self._active is not None and self._active.status.is_busy:
                return False
            self._gen += 1
            task = _WinTask(
                status=TaskStatus(state="running", prompt=prompt,
                                  started_at=time.time(),
                                  session_id=session_id),
                gen=self._gen, pid=pid,
            )
            self._active = task
            self.status = task.status
            threading.Thread(
                target=self._run,
                args=(task, prompt, Path(cwd), reply_limit, on_done),
                daemon=True,
            ).start()
            return True

    def cancel(self) -> bool:
        with self._lock:
            task = self._active
            if task is None or not task.status.is_busy:
                return False
            task.status.state = "canceled"
            return True

    # ---------- 内部 ----------
    def _inject(self, pid: int, text: str) -> None:
        with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", suffix=".txt", delete=False) as f:
            f.write(text)
            tmp = f.name
        try:
            proc = subprocess.run(
                _helper_cmd(self.python_bin, "inject", pid, tmp),
                capture_output=True, text=True, timeout=INJECT_TIMEOUT,
            )
            if proc.returncode != 0:
                raise RuntimeError(f"注入子进程失败: {proc.stderr.strip()[:200]}")
        finally:
            Path(tmp).unlink(missing_ok=True)

    def _escape(self, pid: int) -> None:
        try:
            subprocess.run(_helper_cmd(self.python_bin, "escape", pid),
                           capture_output=True, timeout=INJECT_TIMEOUT)
        except Exception:
            log.debug("发送 Esc 失败", exc_info=True)

    def _read_screen(self, pid: int) -> str:
        """抓窗口当前屏幕文本；失败返回 ""（不抛异常，轮询循环继续）。"""
        try:
            proc = subprocess.run(
                _helper_cmd(self.python_bin, "read", pid),
                capture_output=True, timeout=INJECT_TIMEOUT)
            if proc.returncode != 0:
                return ""
            return proc.stdout.decode("utf-8", errors="replace")
        except Exception:
            log.debug("屏幕抓取失败", exc_info=True)
            return ""

    def _run(self, task: _WinTask, prompt: str, cwd: Path,
             reply_limit: int, on_done) -> None:
        st = task.status
        path = transcript_path(cwd, st.session_id)
        before = last_assistant_text(path)
        # 兜底基线：v2.1.283 出现过新窗口不写 transcript 的异常，
        # transcript 不存在时改用屏幕抓取判定完成、抠取回答。
        screen_before = self._read_screen(task.pid)
        try:
            self._inject(task.pid, with_reply_hint(prompt, reply_limit))
        except Exception as exc:
            self._finish(st, "error", error=f"注入窗口失败: {exc}")
            self._deactivate(task)
            if self._is_current(task) and on_done:
                on_done(st)
            return

        last_sig: tuple[int, int] | None = None
        stable = 0
        screen_last = ""
        screen_stable = 0
        confirmed = False
        enters_resent = 0
        next_resend_at = time.time() + CONFIRM_RETRY_SECONDS
        confirm_until = time.time() + CONFIRM_SECONDS
        deadline = time.time() + self.timeout
        try:
            while True:
                if st.state == "canceled":
                    self._escape(task.pid)
                    self._finish(st, "canceled")
                    break
                if time.time() > deadline:
                    self._escape(task.pid)
                    self._finish(st, "timeout",
                                 error=f"窗口任务超过 {self.timeout}s 被终止")
                    break
                if not psutil.pid_exists(task.pid):
                    self._finish(st, "error", error="CC 窗口已关闭，任务中断")
                    break
                transcript_ready = path.exists()
                cur = ""
                if not transcript_ready:
                    cur = self._read_screen(task.pid)
                if not confirmed:
                    # 提交确认：transcript 出现、spinner 转起或 done 标记
                    # 增加都算「回车生效了」；否则按节奏补发裸回车。
                    if (transcript_ready or "esc to interrupt" in cur
                            or done_count(cur) > done_count(screen_before)):
                        confirmed = True
                    elif (time.time() < confirm_until
                            and enters_resent < CONFIRM_RESENDS
                            and time.time() >= next_resend_at):
                        self._inject(task.pid, "")
                        enters_resent += 1
                        next_resend_at = time.time() + CONFIRM_RETRY_SECONDS
                if transcript_ready:
                    # 正常路径：transcript 轮询
                    stat = path.stat()
                    sig = (stat.st_size, stat.st_mtime_ns)
                    current = last_assistant_text(path)
                    if current and current != before:
                        if sig == last_sig:
                            stable += 1
                            if stable >= STABLE_POLLS:
                                st.result = current
                                self._finish(st, "done")
                                break
                        else:
                            stable = 0
                    last_sig = sig
                else:
                    # 兜底路径：transcript 始终不出现（v2.1.283 会话注册异常）
                    # → 屏幕抓取。完成判定 = 「· done」标记数增加 + 画面稳定。
                    if (cur and cur != screen_before
                            and done_count(cur) > done_count(screen_before)):
                        if cur == screen_last:
                            screen_stable += 1
                            if screen_stable >= STABLE_POLLS:
                                answer = extract_last_answer(cur)
                                if answer:
                                    st.result = answer
                                    self._finish(st, "done")
                                else:
                                    self._finish(
                                        st, "error",
                                        error="屏幕抓取完成但未抠到回答文本")
                                break
                        else:
                            screen_stable = 0
                    screen_last = cur
                time.sleep(POLL_SECONDS)
        except Exception:
            log.exception("winrunner 内部异常")
            if st.state == "running":
                self._finish(st, "error", error="窗口任务异常结束（详见日志）")
        finally:
            self._deactivate(task)
            if self._is_current(task) and on_done:
                on_done(st)

    @staticmethod
    def _finish(st: TaskStatus, state: str, error: str = "") -> None:
        st.state = state
        st.finished_at = time.time()
        if error:
            st.error = error

    def _is_current(self, task: _WinTask) -> bool:
        return task.gen == self._gen

    def _deactivate(self, task: _WinTask) -> None:
        with self._lock:
            if self._active is task:
                self._active = None
