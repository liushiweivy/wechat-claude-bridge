"""tools/bridge_probe.py 入口脚本测试：处置分支、单例、dry-run（全打桩）。"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import bridge_probe as bp
from bridge.probe import Verdict


class _Wx:
    def __init__(self):
        self.sent = []

    def SendMsg(self, text, who=None):
        self.sent.append(text)


def _deaf() -> Verdict:
    return Verdict(True, unanswered=2, age_seconds=200.0, head_preview="任务A")


BRIDGE_PROC = (200, "python.exe", r".venv\Scripts\python.exe -m bridge")
BAT_PROC = (400, "cmd.exe",
            r"cmd /k E:\1workspace\wechat-claude-bridge\tools\autostart-run.bat")


def test_handle_deaf_kills_bridge_pids(monkeypatch):
    killed = []
    monkeypatch.setattr(bp, "scan_procs", lambda: [BRIDGE_PROC])
    monkeypatch.setattr(bp, "_still_bridge", lambda pid: True)
    monkeypatch.setattr(bp, "kill_tree", lambda pid: killed.append(pid))
    monkeypatch.setattr(bp, "send_notice", lambda *a: None)
    bp.handle_deaf(_Wx(), "chat", _deaf(), bp.DeafnessDetector(), False)
    assert killed == [200]


def test_handle_deaf_skips_pid_reused_before_kill(monkeypatch):
    """扫描到击杀间隔着 send_notice 的 join（最长 15s）：桥若在此窗口内
    自然退出且 PID 被复用，杀前复核必须拦下，不杀无关进程。"""
    killed = []
    monkeypatch.setattr(bp, "scan_procs", lambda: [BRIDGE_PROC])
    monkeypatch.setattr(bp, "_still_bridge", lambda pid: False)
    monkeypatch.setattr(bp, "kill_tree", lambda pid: killed.append(pid))
    monkeypatch.setattr(bp, "send_notice", lambda *a: None)
    bp.handle_deaf(_Wx(), "chat", _deaf(), bp.DeafnessDetector(), False)
    assert killed == []


def test_still_bridge_rejects_current_process():
    """复核用的是真 psutil：本 pytest 进程 cmdline 不含 '-m bridge'，
    必须被判为非桥。"""
    import os
    assert not bp._still_bridge(os.getpid())


def test_handle_deaf_dry_run_only_logs(monkeypatch):
    killed = []
    monkeypatch.setattr(bp, "scan_procs", lambda: [BRIDGE_PROC])
    monkeypatch.setattr(bp, "kill_tree", lambda pid: killed.append(pid))
    monkeypatch.setattr(bp, "send_notice", lambda *a: None)
    bp.handle_deaf(_Wx(), "chat", _deaf(), bp.DeafnessDetector(), True)
    assert killed == []


def test_handle_deaf_waits_when_bat_alive(monkeypatch):
    relaunched = []
    monkeypatch.setattr(bp, "scan_procs", lambda: [BAT_PROC])
    monkeypatch.setattr(bp, "relaunch_bat", lambda: relaunched.append(1))
    monkeypatch.setattr(bp, "send_notice", lambda *a: None)
    bp.handle_deaf(_Wx(), "chat", _deaf(), bp.DeafnessDetector(), False)
    assert relaunched == []


def test_handle_deaf_relaunches_when_all_gone(monkeypatch):
    relaunched = []
    monkeypatch.setattr(bp, "scan_procs", lambda: [])
    monkeypatch.setattr(bp, "relaunch_bat", lambda: relaunched.append(1))
    monkeypatch.setattr(bp, "send_notice", lambda *a: None)
    bp.handle_deaf(_Wx(), "chat", _deaf(), bp.DeafnessDetector(), False)
    assert relaunched == [1]


def test_handle_deaf_enters_cooldown(monkeypatch):
    monkeypatch.setattr(bp, "scan_procs", lambda: [])
    monkeypatch.setattr(bp, "relaunch_bat", lambda: None)
    monkeypatch.setattr(bp, "send_notice", lambda *a: None)
    det = bp.DeafnessDetector()
    bp.handle_deaf(_Wx(), "chat", _deaf(), det, False)
    assert det._cooldown_until > 0


def test_singleton_exits_when_already_running(monkeypatch):
    monkeypatch.setattr(bp.os, "getpid", lambda: 999)
    monkeypatch.setattr(bp.os, "getppid", lambda: 1)
    monkeypatch.setattr(bp, "scan_procs",
                        lambda: [(123, "pythonw.exe",
                                  r"pythonw.exe tools\bridge_probe.py")])
    with pytest.raises(SystemExit):
        bp.singleton_check()


def test_singleton_passes_when_alone(monkeypatch):
    monkeypatch.setattr(bp.os, "getpid", lambda: 999)
    monkeypatch.setattr(bp.os, "getppid", lambda: 1)
    monkeypatch.setattr(bp, "scan_procs",
                        lambda: [(999, "pythonw.exe",
                                  r"pythonw.exe tools\bridge_probe.py")])
    bp.singleton_check()      # 不抛


def test_singleton_ignores_shell_wrappers(monkeypatch):
    """bash/timeout/powershell 的 cmdline 也带 bridge_probe.py 字样，
    只有 python/pythonw 才算探活实例（否则被自己背后的壳误判秒退）。"""
    monkeypatch.setattr(bp.os, "getpid", lambda: 999)
    monkeypatch.setattr(bp.os, "getppid", lambda: 1)
    monkeypatch.setattr(bp, "scan_procs",
                        lambda: [(520, "timeout.exe",
                                  r"timeout.exe 60 .venv/Scripts/python.exe tools\bridge_probe.py"),
                                 (30080, "bash.exe",
                                  r"bash.exe -c python tools\bridge_probe.py"),
                                 (8888, "powershell.exe",
                                  r"powershell Start-Process pythonw tools\bridge_probe.py")])
    bp.singleton_check()      # 不抛


def test_singleton_ignores_venv_redirector_parent(monkeypatch):
    """venv pythonw 重定向器父进程 cmdline 同样含 bridge_probe.py，
    不能把它当成"已有探活"（否则探活启动即自杀）。"""
    monkeypatch.setattr(bp.os, "getpid", lambda: 999)
    monkeypatch.setattr(bp.os, "getppid", lambda: 555)
    monkeypatch.setattr(bp, "scan_procs",
                        lambda: [(555, "pythonw.exe",
                                  r".venv\Scripts\pythonw.exe tools\bridge_probe.py"),
                                 (999, "pythonw.exe",
                                  r"C:\Python312\pythonw.exe tools\bridge_probe.py")])
    bp.singleton_check()      # 不抛
