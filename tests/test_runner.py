"""ClaudeRunner 测试：用假 Popen 喂 stream-json，验证状态流转，不依赖真 claude。"""
import threading
import time
from pathlib import Path

from bridge.runner import ClaudeRunner


OK_LINES = [
    '{"type":"system","session_id":"sess-xyz"}\n',
    '{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Read"}]}}\n',
    '{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Bash"}]}}\n',
    '{"type":"result","subtype":"success","result":"做完了"}\n',
]


class FakeStdout:
    def __init__(self, lines):
        self._lines = list(lines)

    def __iter__(self):
        return iter(self._lines)

    def read(self):
        return "".join(self._lines)

    def close(self):
        pass


class FakePopen:
    instances: list["FakePopen"] = []
    SCENARIOS = {"ok": OK_LINES, "empty": ['{"type":"result","subtype":"success","result":""}\n']}

    def __init__(self, args, cwd, stdout, stderr, text, encoding, errors, creationflags):
        self.args = args
        self.cwd = cwd
        name = getattr(self, "_scenario", "ok")
        self.stdout = FakeStdout(self.SCENARIOS[name])
        self.stderr = FakeStdout([""])
        self.pid = 90000 + len(FakePopen.instances)
        self.returncode = 0
        FakePopen.instances.append(self)

    def wait(self):
        return self.returncode

    def kill(self):
        self.returncode = -9


class GatedPopen:
    """stdout 在 gate 打开前阻塞，用于 cancel / 竞态测试。"""
    instances: list["GatedPopen"] = []
    by_pid: dict[int, "GatedPopen"] = {}

    def __init__(self, args, cwd, stdout, stderr, text, encoding, errors, creationflags):
        self.args = args
        self.cwd = cwd
        self.gate = threading.Event()
        self.stdout = self
        self.stderr = self
        self.pid = 95000 + len(GatedPopen.instances)
        self.returncode = 0
        GatedPopen.instances.append(self)
        GatedPopen.by_pid[self.pid] = self
        if args[2] == "任务B":
            self.gate.set()                 # 新任务正常产出；旧任务(A)等 cancel 才放行

    def __iter__(self):
        self.gate.wait(5)
        return iter(OK_LINES if self.args[2] == "任务B" else [])

    def read(self):
        self.gate.wait(5)
        return ""

    def wait(self):
        return self.returncode

    def kill(self):
        self.gate.set()
        self.returncode = -9

    def close(self):
        pass


class FakePsutilParent:
    def __init__(self, pid):
        self.pid = pid

    def children(self, recursive=False):
        return []

    def kill(self):
        proc = GatedPopen.by_pid.get(self.pid)
        if proc:
            proc.kill()


def _patch(monkeypatch, popen_cls):
    monkeypatch.setattr("bridge.runner.shutil.which", lambda name: r"C:\fake\claude.cmd")
    monkeypatch.setattr("bridge.runner.subprocess.Popen", popen_cls)
    monkeypatch.setattr("bridge.runner.psutil.Process", FakePsutilParent)


def _wait_state(runner, states, timeout=5):
    deadline = time.time() + timeout
    while runner.status.state not in states and time.time() < deadline:
        time.sleep(0.02)
    return runner.status.state


def test_runner_success_flow(monkeypatch, tmp_path: Path):
    FakePopen.instances = []
    _patch(monkeypatch, FakePopen)
    r = ClaudeRunner("claude", "acceptEdits", timeout=600)
    done = []
    assert r.start("干活", tmp_path, False, "", on_done=done.append)
    assert _wait_state(r, {"done", "error"}) == "done"

    assert r.status.result == "做完了"
    assert r.status.session_id == "sess-xyz"
    assert r.status.tools == ["Read", "Bash"]
    assert done and done[0].state == "done"
    proc = FakePopen.instances[0]
    assert proc.cwd == str(tmp_path)
    assert "acceptEdits" in proc.args
    assert "--verbose" in proc.args and "--resume" not in proc.args


def test_runner_rejects_second_task_while_busy(monkeypatch, tmp_path: Path):
    class Blocking(FakePopen):
        def wait(self):
            time.sleep(0.5)
            return 0

    FakePopen.instances = []
    _patch(monkeypatch, Blocking)
    r = ClaudeRunner("claude", "acceptEdits", timeout=600)
    assert r.start("慢任务", tmp_path, False)
    time.sleep(0.05)
    assert r.status.is_busy
    assert not r.start("插队任务", tmp_path, False)


def test_runner_empty_result_is_error(monkeypatch, tmp_path: Path):
    FakePopen.instances = []
    monkeypatch.setattr(FakePopen, "_scenario", "empty", raising=False)
    _patch(monkeypatch, FakePopen)
    r = ClaudeRunner("claude", "acceptEdits", timeout=600)
    r.start("空", tmp_path, False)
    assert _wait_state(r, {"done", "error"}) == "error"
    assert "没有返回文本" in r.status.error


def test_sudo_uses_bypass_mode(monkeypatch, tmp_path: Path):
    FakePopen.instances = []
    _patch(monkeypatch, FakePopen)
    r = ClaudeRunner("claude", "acceptEdits", timeout=600)
    r.start("装包", tmp_path, True)
    _wait_state(r, {"done", "error"})
    assert "bypassPermissions" in FakePopen.instances[0].args


def test_cancel_marks_task_canceled(monkeypatch, tmp_path: Path):
    GatedPopen.instances = []
    GatedPopen.by_pid = {}
    _patch(monkeypatch, GatedPopen)
    r = ClaudeRunner("claude", "acceptEdits", timeout=600)
    r.start("长任务", tmp_path, False)
    time.sleep(0.1)
    assert r.cancel() is True
    assert _wait_state(r, {"canceled"}) == "canceled"


def test_cancel_idle_returns_false(tmp_path: Path):
    r = ClaudeRunner.__new__(ClaudeRunner)  # 不走 __init__（无需真 claude 解析）
    r._active = None
    r._lock = threading.Lock()
    assert r.cancel() is False


def test_stale_task_callback_dropped_after_new_task(monkeypatch, tmp_path: Path):
    """HIGH1 回归：A 取消后立刻派 B，A 迟到的完成事件不得污染/上报 B。"""
    GatedPopen.instances = []
    GatedPopen.by_pid = {}
    _patch(monkeypatch, GatedPopen)
    r = ClaudeRunner("claude", "acceptEdits", timeout=600)

    cb_a, cb_b = [], []
    r.start("任务A", tmp_path, False, on_done=cb_a.append)
    time.sleep(0.1)
    r.cancel()                              # A 置 canceled 并打开 A 的 gate
    assert r.start("任务B", tmp_path, False, on_done=cb_b.append)  # 不等 A 收尾

    assert _wait_state(r, {"done", "error"}) == "done"
    assert r.status.result == "做完了"       # 这是 B 的结果
    assert cb_b and cb_b[0].state == "done"
    assert cb_a == []                        # A 的迟到回调被代际丢弃


def test_wallclock_timeout_kills_silent_process(monkeypatch, tmp_path: Path):
    # 子进程一直不输出任何行（旧实现只在读到行时比对时间 → 永久挂死）
    class SilentStdout:
        def __init__(self, evt):
            self._evt = evt

        def __iter__(self):
            self._evt.wait(10)
            return iter([])

        def read(self):
            self._evt.wait(10)
            return ""

        def close(self):
            pass

    killed = threading.Event()

    class SilentPopen(FakePopen):
        def __init__(self, args, cwd, stdout, stderr, text, encoding,
                     errors, creationflags):
            self.args = args
            self.cwd = cwd
            self.stdout = SilentStdout(killed)
            self.stderr = SilentStdout(killed)
            self.pid = 88000 + len(FakePopen.instances)
            self.returncode = 0
            FakePopen.instances.append(self)
            GatedPopen.by_pid[self.pid] = self

        def wait(self):
            killed.wait(10)
            return -9

        def kill(self):
            killed.set()
            self.returncode = -9

    FakePopen.instances = []
    _patch(monkeypatch, SilentPopen)
    r = ClaudeRunner("claude", "acceptEdits", timeout=1)
    r.start("静默长任务", tmp_path, False)
    assert _wait_state(r, {"timeout", "error"}, timeout=8) == "timeout"
    assert killed.is_set()


def test_runner_recovers_answer_from_transcript_when_result_missing(
        monkeypatch, tmp_path: Path):
    """回归 2026-10-15：CLI 长任务收尾期被模型目录校验拒绝，answer 已落盘但
    不发 result 事件 → 从 transcript jsonl 兜底取回答案，按完成上报。"""
    import json

    recovered = tmp_path / "sid-recover.jsonl"
    recovered.write_text(
        json.dumps({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "兜底取回的答案"},
        ]}}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    lines = [
        '{"type":"system","session_id":"sid-recover"}\n',
        '{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Read"}]}}\n',
    ]
    FakePopen.instances = []
    FakePopen.SCENARIOS["norecover"] = lines
    monkeypatch.setattr(FakePopen, "_scenario", "norecover", raising=False)
    _patch(monkeypatch, FakePopen)
    monkeypatch.setattr("bridge.runner.transcript_path",
                        lambda cwd, sid: recovered)
    r = ClaudeRunner("claude", "acceptEdits", timeout=600)
    r.start("长任务", tmp_path, False)
    assert _wait_state(r, {"done", "error"}) == "done"
    assert "兜底取回的答案" in r.status.result
    assert "transcript" in r.status.result
    FakePopen.SCENARIOS.pop("norecover", None)


def test_runner_strips_benign_model_warning_from_error(monkeypatch, tmp_path: Path):
    class WarnPopen(FakePopen):
        def __init__(self, args, cwd, stdout, stderr, text, encoding,
                     errors, creationflags):
            super().__init__(args, cwd, stdout, stderr, text, encoding,
                             errors, creationflags)
            self.stderr = FakeStdout([
                '[claude-code:unrecognized_model] {"model":"ark-code-latest"}\n',
            ])

    FakePopen.instances = []
    monkeypatch.setattr(FakePopen, "_scenario", "empty", raising=False)
    _patch(monkeypatch, WarnPopen)
    r = ClaudeRunner("claude", "acceptEdits", timeout=600)
    r.start("空", tmp_path, False)
    assert _wait_state(r, {"done", "error"}) == "error"
    assert "unrecognized_model" not in r.status.error


def test_error_subtype_result_surfaces_as_error(monkeypatch, tmp_path: Path):
    # CLI 退出码 0 但 result.subtype=error：不能汇报"完成"
    err_lines = ['{"type":"result","subtype":"error","result":"部分输出"}\n']
    FakePopen.instances = []
    FakePopen.SCENARIOS["err"] = err_lines
    monkeypatch.setattr(FakePopen, "_scenario", "err", raising=False)
    _patch(monkeypatch, FakePopen)
    r = ClaudeRunner("claude", "acceptEdits", timeout=600)
    r.start("触限任务", tmp_path, False)
    assert _wait_state(r, {"done", "error"}) == "error"
    assert r.status.error
    FakePopen.SCENARIOS.pop("err", None)
