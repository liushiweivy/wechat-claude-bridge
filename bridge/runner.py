"""Claude Code 子进程执行器：后台线程跑 claude -p，实时解析 stream-json。

并发模型：每个任务一个 _Task（status + proc + 代际号 gen）。工作线程只写
自己手里的任务对象，绝不回头读写 self.* —— 这样 /cancel 后立刻派新任务时，
旧线程收尾不会污染新任务状态；旧任务迟到的完成回调按 gen 丢弃。
"""
from __future__ import annotations

import ctypes
import logging
import os
import queue
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import psutil

from .core import (
    RECOVERY_NOTICE,
    StreamEvent,
    build_claude_args,
    clean_child_env,
    clean_stderr,
    last_assistant_text,
    parse_stream_line,
    transcript_path,
)

log = logging.getLogger("bridge.runner")

# CLI 进程退出后（但管道可能被残留的 .CMD/node 攥住写端）最多再等多久收尾
EXIT_GRACE_SECONDS = 3.0
_PUMP_POLL_SECONDS = 0.5


def _abort_pending_read(stream) -> None:
    """跨线程撤销 pump 线程在匿名管道上挂起的 ReadFile（仅 Windows）。

    claude.CMD 经 cmd.exe 多跳启动，残留子进程可能继承管道写端；CLI 本体
    退出后读端仍永远阻塞。CancelIoEx 可从另一线程取消该读，不依赖找到
    具体是哪个进程攥着句柄。
    """
    if os.name != "nt":
        return
    try:
        import msvcrt
        from ctypes import wintypes

        handle = msvcrt.get_osfhandle(stream.fileno())
        ctypes.windll.kernel32.CancelIoEx(wintypes.HANDLE(handle), None)
    except Exception:
        log.debug("CancelIoEx 跳过（非管道/句柄已关）", exc_info=True)


@dataclass
class TaskStatus:
    state: str = "idle"          # idle | running | done | error | canceled | timeout
    prompt: str = ""
    started_at: float = 0.0
    finished_at: float = 0.0
    result: str = ""
    tools: list[str] = field(default_factory=list)
    session_id: str = ""
    error: str = ""

    @property
    def is_busy(self) -> bool:
        return self.state == "running"


@dataclass
class _Task:
    status: TaskStatus
    gen: int
    proc: subprocess.Popen | None = None


class ClaudeRunner:
    """串行任务执行器：同一时间只跑一个任务。"""

    def __init__(self, claude_bin: str, default_mode: str, timeout: int,
                 extra_args: tuple[str, ...] = ()) -> None:
        self.claude_bin = self._resolve_bin(claude_bin)
        self.default_mode = default_mode
        self.timeout = timeout
        self.extra_args = extra_args
        self.status = TaskStatus()
        self._active: _Task | None = None
        self._gen = 0
        self._lock = threading.Lock()

    @staticmethod
    def _resolve_bin(start_bin: str) -> str:
        found = shutil.which(start_bin) or shutil.which(start_bin + ".cmd")
        if not found:
            raise FileNotFoundError(f"找不到 claude 可执行文件: {start_bin}（检查 PATH）")
        native = ClaudeRunner._native_claude_exe(found)
        return native or found

    @staticmethod
    def _native_claude_exe(resolved: str) -> str:
        """npm 全局 bin 目录里的 .CMD shim 旁通常就是原生 claude.exe。

        直连原生 exe 可少一跳 cmd.exe（消除批处理跳转导致的句柄泄漏面）；
        不存在则返回空串，继续走 .CMD shim（有泵+撤销读兜底，不影响可用性）。
        """
        p = Path(resolved)
        if p.suffix.lower() in (".cmd", ".bat"):
            cand = (p.parent / "node_modules" / "@anthropic-ai"
                    / "claude-code" / "bin" / "claude.exe")
            if cand.is_file():
                return str(cand)
        return ""

    def start(self, prompt: str, cwd: Path, sudo: bool, resume_id: str = "",
              on_event=None, on_done=None) -> bool:
        with self._lock:
            if self._active is not None and self._active.status.is_busy:
                return False
            self._gen += 1
            task = _Task(
                status=TaskStatus(state="running", prompt=prompt,
                                  started_at=time.time(), session_id=resume_id),
                gen=self._gen,
            )
            self._active = task
            self.status = task.status
            thread = threading.Thread(
                target=self._run,
                args=(task, prompt, cwd, sudo, resume_id, on_event, on_done),
                daemon=True,
            )
            thread.start()
            return True

    def _run(self, task: _Task, prompt, cwd: Path, sudo: bool, resume_id: str,
             on_event, on_done) -> None:
        st = task.status
        mode = "bypassPermissions" if sudo else self.default_mode
        args = [self.claude_bin] + build_claude_args(
            prompt, mode, resume_id or None, self.extra_args
        )
        try:
            # WECHAT_BRIDGE 标记：会话台凭此 env 给活会话打「微信桥」来源标签。
            # 其余 CLAUDE* 标记必须剥掉（clean_child_env）：桥若间接启动自
            # 某个 claude 会话终端，继承的 CLAUDE_CODE_CHILD_SESSION 会让
            # 子 claude 关闭 transcript 持久化，结果取不回（2026-10-18 定位）
            proc = subprocess.Popen(
                args, cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace",
                env={**clean_child_env(), "WECHAT_BRIDGE": "1"},
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as exc:
            self._finish(st, "error", error=f"启动 claude 失败: {exc}")
            self._deactivate(task)
            if self._is_current(task):
                on_done(st)
            return
        task.proc = proc

        # 整体兜底：任何意外异常都必须把任务置终态并清出 _active，否则
        # 单任务调度会永久卡在 running（比管道卡死更隐蔽的死局）。
        try:
            # stderr 独立线程排空：子进程 stderr 写满管道而主线程在读 stdout
            # 时会死锁；read() 在进程退出(EOF)后自然返回。
            stderr_chunks: list[str] = []

            def _drain_stderr() -> None:
                assert proc.stderr is not None
                data = proc.stderr.read()
                if data:
                    stderr_chunks.append(data)

            err_thread = threading.Thread(target=_drain_stderr, daemon=True)
            err_thread.start()

            # 真正的 wall-clock 看门狗：子进程长时间静默（无新行）也会被杀。
            timed_out = False

            def _on_timeout() -> None:
                nonlocal timed_out
                if st.state != "running":
                    return                      # 与正常收尾竞态时不抢结果
                timed_out = True
                self._kill_tree(proc)

            watchdog = threading.Timer(self.timeout, _on_timeout)
            watchdog.daemon = True
            watchdog.start()

            # stdout 用独立泵线程读，主线程按轮询消费：这样 CLI 已退出但管道
            # 被 .CMD 残留进程攥住（不 EOF）时，仍能靠 proc.poll() 收尾，而不是
            # 死等到 1800s 看门狗。
            assert proc.stdout is not None
            line_q: queue.Queue = queue.Queue()
            sentinel = object()
            aborted = False

            def _pump_stdout() -> None:
                try:
                    for line in proc.stdout:
                        line_q.put(line)
                except Exception:
                    log.debug("stdout 泵线程异常退出", exc_info=True)
                finally:
                    line_q.put(sentinel)

            pump_thread = threading.Thread(target=_pump_stdout, daemon=True)
            pump_thread.start()

            grace_deadline: float | None = None
            while True:
                try:
                    item = line_q.get(timeout=_PUMP_POLL_SECONDS)
                except queue.Empty:
                    item = None
                if item is sentinel:
                    break
                if item is not None:
                    grace_deadline = None
                    ev = parse_stream_line(item)
                    if ev:
                        self._apply(st, ev)
                        if on_event:
                            on_event(ev)
                    continue
                # 队列空：进程还在就继续等（总看门狗兜底静默挂起）
                if proc.poll() is None:
                    continue
                # 进程已退出：扫掉残留子进程（释放被继承的管道写端），再宽限
                # 片刻让泵线程排空已落管道的尾行；仍不 EOF 就强制撤销挂起读。
                if grace_deadline is None:
                    self._kill_tree(proc)
                    grace_deadline = time.time() + EXIT_GRACE_SECONDS
                elif time.time() >= grace_deadline:
                    _abort_pending_read(proc.stdout)
                    aborted = True
                    break

            watchdog.cancel()
            # 撤销读可能落在泵线程两次 ReadFile 的间隙：等一下再补一次撤销，
            # 避免关句柄时仍有读挂着、泵线程被游离写端拖到它退出。
            if aborted:
                pump_thread.join(timeout=1.0)
                if pump_thread.is_alive():
                    _abort_pending_read(proc.stdout)
                    pump_thread.join(timeout=1.0)
            code = proc.wait()
            err_thread.join(timeout=2)
            stderr_tail = "".join(stderr_chunks)[-800:]
            self._close_pipes(proc)

            if st.state == "canceled":
                self._finish(st, "canceled")
            elif timed_out:
                self._finish(st, "timeout", error=f"超过 {self.timeout}s 被终止")
            elif not st.result:
                # CLI 在第三方网关下偶发"答案已落盘但不发 result 事件"（模型
                # 目录校验在收尾期拒绝未登记模型）。先 transcript 兜底取回。
                recovered = ""
                if st.session_id:
                    recovered = last_assistant_text(
                        transcript_path(cwd, st.session_id)
                    )
                if recovered:
                    st.result = f"{recovered}\n\n{RECOVERY_NOTICE}"
                    self._finish(st, "done")
                    log.warning(
                        "任务 %s 缺少 result 事件，已从 transcript 兜底恢复答案"
                        "（exit=%s，%d 字符）；stderr 原文：%r",
                        st.session_id, code, len(recovered), stderr_tail,
                    )
                else:
                    detail = clean_stderr(stderr_tail)
                    # 完整 stderr（含良性警告）只进日志，微信只收清洗后的真错误
                    log.warning(
                        "任务 %s 无 result 且 transcript 兜底失败（exit=%s）；"
                        "完整 stderr：\n%s",
                        st.session_id, code, stderr_tail,
                    )
                    head = (f"claude 退出码 {code}" if code
                            else "claude 没有返回文本")
                    self._finish(st, "error", error=f"{head}\n{detail}".strip())
            elif st.error:
                # CLI 退出码 0 但 result.subtype != success（触限/权限/执行中断）
                log.warning("任务 %s result.subtype 非 success；stderr：%r",
                            st.session_id, stderr_tail)
                self._finish(st, "error", error=st.error)
            else:
                self._finish(st, "done")
        except Exception:
            log.exception("runner 内部异常，任务 %s 强制置失败", st.session_id)
            try:
                self._kill_tree(proc)
            except Exception:
                log.debug("异常收尾时 _kill_tree 失败", exc_info=True)
            if st.state == "running":
                self._finish(st, "error",
                             error="桥内部异常，任务被强制结束（详见日志）")
        finally:
            self._deactivate(task)
            # 代际过期（用户已开新任务）→ 迟到结果丢弃，不回微信
            if self._is_current(task) and on_done:
                on_done(st)

    @staticmethod
    def _apply(st: TaskStatus, ev: StreamEvent) -> None:
        if ev.kind == "session" and ev.session_id and not st.session_id:
            st.session_id = ev.session_id
        elif ev.kind == "tool" and ev.tool:
            if ev.tool not in st.tools:
                st.tools.append(ev.tool)
        elif ev.kind == "result":
            st.result = ev.text
            if ev.is_error:
                st.error = "claude 报告本轮未成功完成（可能触及权限/上限）"

    @staticmethod
    def _finish(st: TaskStatus, state: str, error: str = "") -> None:
        st.state = state
        st.finished_at = time.time()
        if error:
            st.error = error

    def cancel(self) -> bool:
        with self._lock:
            task = self._active
            if task is None or not task.status.is_busy:
                return False
            task.status.state = "canceled"
            if task.proc is not None:
                self._kill_tree(task.proc)
            return True

    def _is_current(self, task: _Task) -> bool:
        return task.gen == self._gen

    def _deactivate(self, task: _Task) -> None:
        with self._lock:
            if self._active is task:
                self._active = None

    @staticmethod
    def _close_pipes(proc: subprocess.Popen) -> None:
        for stream in (proc.stdout, proc.stderr):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass

    @staticmethod
    def _kill_tree(proc: subprocess.Popen) -> None:
        """claude 是 .cmd→node 多层进程，必须按进程树杀（只杀本任务的 PID 树）。

        根进程已退出时 psutil 无法再枚举其子代（孤儿孙进程由 CancelIoEx +
        3s 宽限路径兜底，不影响任务收尾）；任何 psutil 错误都只记录不抛出，
        绝不让收尾路径反过来把任务卡死在 running。
        """
        try:
            parent = psutil.Process(proc.pid)
            for child in parent.children(recursive=True):
                child.kill()
            parent.kill()
        except psutil.Error:
            log.debug("_kill_tree 跳过（pid=%s 已退出或访问被拒）",
                      proc.pid, exc_info=True)
