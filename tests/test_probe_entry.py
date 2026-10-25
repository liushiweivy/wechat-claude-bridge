"""tools/bridge_probe.py 入口测试：mutex、state 持久化、单次巡检流程、
处置分支、dry-run（全打桩，不连微信）。"""
import ctypes
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import bridge_probe as bp
from bridge.probe import DeafnessDetector, MsgView, Verdict


class _Wx:
    def __init__(self):
        self.sent = []

    def SendMsg(self, text, who=None):
        self.sent.append(text)


class FakeClock:
    def __init__(self, now: float = 10_000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _user(content: str) -> MsgView:
    return MsgView(msg_type="self", content=content)


def _deaf() -> Verdict:
    return Verdict(True, unanswered=2, age_seconds=200.0, head_preview="任务A")


BRIDGE_PROC = (200, "python.exe", r".venv\Scripts\python.exe -m bridge")
BAT_PROC = (400, "cmd.exe",
            r"cmd /k E:\1workspace\wechat-claude-bridge\tools\autostart-run.bat")


# ---------- handle_deaf 处置分支（签名不变） ----------

def test_handle_deaf_kills_bridge_pids(monkeypatch):
    killed = []
    monkeypatch.setattr(bp, "scan_procs", lambda: [BRIDGE_PROC])
    monkeypatch.setattr(bp, "_still_bridge", lambda pid: True)
    monkeypatch.setattr(bp, "kill_tree", lambda pid: killed.append(pid))
    monkeypatch.setattr(bp, "send_notice", lambda *a: None)
    det = DeafnessDetector()
    bp.handle_deaf(_Wx(), "chat", _deaf(), det, False)
    assert killed == [200]
    assert det._cooldown_until > 0


def test_handle_deaf_skips_pid_reused_before_kill(monkeypatch):
    """扫描到击杀间隔着 send_notice 的 join（最长 15s）：桥若在此窗口内
    自然退出且 PID 被复用，杀前复核必须拦下，不杀无关进程。"""
    killed = []
    monkeypatch.setattr(bp, "scan_procs", lambda: [BRIDGE_PROC])
    monkeypatch.setattr(bp, "_still_bridge", lambda pid: False)
    monkeypatch.setattr(bp, "kill_tree", lambda pid: killed.append(pid))
    monkeypatch.setattr(bp, "send_notice", lambda *a: None)
    bp.handle_deaf(_Wx(), "chat", _deaf(), DeafnessDetector(), False)
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
    bp.handle_deaf(_Wx(), "chat", _deaf(), DeafnessDetector(), True)
    assert killed == []


def test_handle_deaf_waits_when_bat_alive(monkeypatch):
    relaunched = []
    monkeypatch.setattr(bp, "scan_procs", lambda: [BAT_PROC])
    monkeypatch.setattr(bp, "relaunch_bat", lambda: relaunched.append(1))
    monkeypatch.setattr(bp, "send_notice", lambda *a: None)
    bp.handle_deaf(_Wx(), "chat", _deaf(), DeafnessDetector(), False)
    assert relaunched == []


def test_handle_deaf_relaunches_when_all_gone(monkeypatch):
    relaunched = []
    monkeypatch.setattr(bp, "scan_procs", lambda: [])
    monkeypatch.setattr(bp, "relaunch_bat", lambda: relaunched.append(1))
    monkeypatch.setattr(bp, "send_notice", lambda *a: None)
    bp.handle_deaf(_Wx(), "chat", _deaf(), DeafnessDetector(), False)
    assert relaunched == [1]


def test_handle_deaf_enters_cooldown(monkeypatch):
    monkeypatch.setattr(bp, "scan_procs", lambda: [])
    monkeypatch.setattr(bp, "relaunch_bat", lambda: None)
    monkeypatch.setattr(bp, "send_notice", lambda *a: None)
    det = DeafnessDetector()
    bp.handle_deaf(_Wx(), "chat", _deaf(), det, False)
    assert det._cooldown_until > 0


# ---------- mutex 单例（替代旧的扫描式 singleton_check） ----------

class _FakeKernel32:
    def __init__(self, last_error: int = 0, handle: int | None = 1):
        self._last_error = last_error
        self._handle = handle
        self.created_names: list[str] = []
        self.closed: list[int | None] = []

    def CreateMutexW(self, attrs, initial_owner, name):
        self.created_names.append(name)
        ctypes.set_last_error(self._last_error)
        return self._handle

    def CloseHandle(self, handle) -> int:
        self.closed.append(handle)
        return 1


def test_mutex_acquired_when_free():
    fake = _FakeKernel32(last_error=0)
    assert bp.acquire_mutex(lib=fake) is not None
    assert fake.created_names == [bp.MUTEX_NAME]


def test_mutex_returns_none_when_already_exists():
    fake = _FakeKernel32(last_error=183, handle=77)
    assert bp.acquire_mutex(lib=fake) is None
    assert fake.closed == [77]          # 重复句柄已关闭，不留泄露


def test_mutex_raises_when_create_fails():
    with pytest.raises(OSError):
        bp.acquire_mutex(lib=_FakeKernel32(last_error=0, handle=None))


# ---------- state 文件 ----------

def test_load_state_missing_returns_none(monkeypatch, tmp_path):
    monkeypatch.setattr(bp, "STATE_PATH", tmp_path / "probe_state.json")
    assert bp.load_state() is None


def test_save_load_state_roundtrip_detects_across_instances(monkeypatch,
                                                            tmp_path):
    monkeypatch.setattr(bp, "STATE_PATH", tmp_path / "probe_state.json")
    clock = FakeClock()
    tail = [_user("任务A"), _user("任务B")]
    d1 = DeafnessDetector(clock=clock)
    assert not d1.observe(tail).deaf
    bp.save_state(d1)

    loaded = bp.load_state()
    assert loaded and loaded["version"] == 1
    clock.advance(3600)                 # 下一小时的全新进程
    d2 = DeafnessDetector(clock=clock, state=loaded)
    assert d2.observe(tail).deaf


def test_load_state_broken_json_returns_none(monkeypatch, tmp_path):
    path = tmp_path / "probe_state.json"
    path.write_text("{broken json", encoding="utf-8")
    monkeypatch.setattr(bp, "STATE_PATH", path)
    assert bp.load_state() is None


# ---------- run_once：单轮消息检测 + 二次确认 ----------

def test_run_once_no_block_does_not_wait(monkeypatch):
    monkeypatch.setattr(bp, "read_tail", lambda wx, chat: [])
    slept: list[float] = []
    v = bp.run_once(object(), "chat", DeafnessDetector(clock=FakeClock()),
                    False, sleeper=slept.append)
    assert slept == [] and not v.deaf


def test_run_once_confirms_deaf_after_wait(monkeypatch):
    clock = FakeClock()
    detector = DeafnessDetector(clock=clock)
    tail = [_user("任务A"), _user("任务B")]
    rounds = iter([tail, tail])
    monkeypatch.setattr(bp, "read_tail", lambda wx, chat: next(rounds))
    handled: list[Verdict] = []
    monkeypatch.setattr(bp, "handle_deaf",
                        lambda wx, chat, v, d, dry: handled.append(v))

    def sleep(seconds: float) -> None:
        clock.advance(seconds)

    v = bp.run_once(object(), "chat", detector, False, sleeper=sleep)
    assert v is not None and v.deaf
    assert handled and handled[0].unanswered == 2


def test_run_once_reply_during_wait_clears(monkeypatch):
    tail_block = [_user("任务A"), _user("任务B")]
    rounds = iter([tail_block, tail_block + [_user("✅ 完成")]])
    monkeypatch.setattr(bp, "read_tail", lambda wx, chat: next(rounds))
    handled: list = []
    monkeypatch.setattr(bp, "handle_deaf",
                        lambda *a, **k: handled.append(1))
    slept: list[float] = []
    v = bp.run_once(object(), "chat", DeafnessDetector(clock=FakeClock()),
                    False, sleeper=slept.append)
    assert slept == [bp.CONFIRM_WAIT_SECONDS]
    assert handled == [] and not v.deaf


def test_run_once_read_failure_returns_none(monkeypatch):
    monkeypatch.setattr(bp, "read_tail", lambda wx, chat: None)
    slept: list[float] = []
    assert bp.run_once(object(), "chat", DeafnessDetector(), False,
                       sleeper=slept.append) is None
    assert slept == []


def test_run_once_confirm_read_failure_returns_none(monkeypatch):
    """200s 等待后的二次读取失败：不能沿用等待前的旧判定记成'巡检正常'，
    返回 None（调用方退出码 1、不写 state），绝不走处置。"""
    tail = [_user("任务A"), _user("任务B")]
    rounds = iter([tail, None])
    monkeypatch.setattr(bp, "read_tail", lambda wx, chat: next(rounds))
    handled: list = []
    monkeypatch.setattr(bp, "handle_deaf",
                        lambda *a, **k: handled.append(1))
    slept: list[float] = []
    v = bp.run_once(object(), "chat", DeafnessDetector(clock=FakeClock()),
                    False, sleeper=slept.append)
    assert v is None and slept == [bp.CONFIRM_WAIT_SECONDS]
    assert handled == []


def test_run_once_already_deaf_skips_wait(monkeypatch):
    """state 带陈旧 first-seen（age 已超阈值）时首轮即判，不等 200s。"""
    clock = FakeClock()
    tail = [_user("任务A"), _user("任务B")]
    d1 = DeafnessDetector(clock=clock)
    d1.observe(tail)
    clock.advance(3600)
    detector = DeafnessDetector(clock=clock, state=d1.export_state())
    monkeypatch.setattr(bp, "read_tail", lambda wx, chat: tail)
    handled: list = []
    monkeypatch.setattr(bp, "handle_deaf",
                        lambda *a, **k: handled.append(1))
    slept: list[float] = []
    v = bp.run_once(object(), "chat", detector, False, sleeper=slept.append)
    assert v.deaf and slept == [] and len(handled) == 1


# ---------- run_once_main：任务计划入口全流程 ----------

def _patch_main(monkeypatch, tmp_path, procs):
    monkeypatch.setattr(bp, "setup_logging", lambda: None)
    monkeypatch.setattr(bp, "acquire_mutex", lambda: 1)
    monkeypatch.setattr(bp, "STATE_PATH", tmp_path / "probe_state.json")
    monkeypatch.setattr(bp, "scan_procs", lambda: procs)
    monkeypatch.setattr(bp, "load_config",
                        lambda: SimpleNamespace(listen_chat="文件传输助手"))


def test_once_main_mutex_busy_exits_without_action(monkeypatch, tmp_path):
    _patch_main(monkeypatch, tmp_path, procs=[])
    monkeypatch.setattr(bp, "acquire_mutex", lambda: None)
    relaunched: list = []
    monkeypatch.setattr(bp, "relaunch_bat", lambda: relaunched.append(1))
    assert bp.run_once_main(False, 2, 180.0) == 0
    assert relaunched == []


def test_once_main_relaunches_when_all_gone_and_writes_empty_state(
        monkeypatch, tmp_path):
    _patch_main(monkeypatch, tmp_path, procs=[])
    connected: list = []
    monkeypatch.setattr(bp, "connect_wx", lambda chat: connected.append(1))
    relaunched: list = []
    monkeypatch.setattr(bp, "relaunch_bat", lambda: relaunched.append(1))

    assert bp.run_once_main(False, 2, 180.0) == 0
    assert relaunched == [1] and connected == []   # 不连微信、本轮不判聋
    data = json.loads((tmp_path / "probe_state.json").read_text("utf-8"))
    assert data["first_seen"] == {}


def test_once_main_wait_bat_neither_relaunches_nor_checks_messages(
        monkeypatch, tmp_path):
    _patch_main(monkeypatch, tmp_path, procs=[BAT_PROC])
    relaunched: list = []
    connected: list = []
    monkeypatch.setattr(bp, "relaunch_bat", lambda: relaunched.append(1))
    monkeypatch.setattr(bp, "connect_wx", lambda chat: connected.append(1))

    assert bp.run_once_main(False, 2, 180.0) == 0
    assert relaunched == [] and connected == []
    assert not (tmp_path / "probe_state.json").exists()


def test_once_main_ambiguous_bat_waits_without_relaunch(monkeypatch, tmp_path):
    """cwd 不可读的相对路径 bat（基名命中、归属未知）：保守等待，不重复拉 bat。"""
    _patch_main(monkeypatch, tmp_path,
                procs=[(700, "cmd.exe", r"cmd /c tools\autostart-run.bat", None)])
    relaunched: list = []
    connected: list = []
    monkeypatch.setattr(bp, "relaunch_bat", lambda: relaunched.append(1))
    monkeypatch.setattr(bp, "connect_wx", lambda chat: connected.append(1))
    assert bp.run_once_main(False, 2, 180.0) == 0
    assert relaunched == [] and connected == []
    assert not (tmp_path / "probe_state.json").exists()


def test_youngest_bridge_age_warns_when_all_unreadable(monkeypatch, caplog):
    import logging
    import psutil

    def boom(pid):
        raise psutil.NoSuchProcess(pid)

    monkeypatch.setattr(psutil, "Process", boom)
    caplog.set_level(logging.WARNING, logger="bridge.probe")
    assert bp.youngest_bridge_age([200]) is None
    assert any("create_time" in r.getMessage() for r in caplog.records)


def test_once_main_alive_runs_check_and_saves_state(monkeypatch, tmp_path):
    _patch_main(monkeypatch, tmp_path, procs=[BRIDGE_PROC])
    monkeypatch.setattr(bp, "connect_wx", lambda chat: "WX")
    ran: list = []

    def fake_run_once(wx, chat, detector, dry_run, wait=bp.CONFIRM_WAIT_SECONDS,
                      sleeper=None):
        ran.append((wx, chat, dry_run))
        return Verdict(False, unanswered=0)

    monkeypatch.setattr(bp, "run_once", fake_run_once)
    assert bp.run_once_main(False, 2, 180.0) == 0
    assert ran and ran[0][0] == "WX"
    assert (tmp_path / "probe_state.json").exists()


def test_once_main_wechat_failure_returns_1(monkeypatch, tmp_path):
    _patch_main(monkeypatch, tmp_path, procs=[BRIDGE_PROC])

    def boom(chat):
        raise RuntimeError("no wechat")

    monkeypatch.setattr(bp, "connect_wx", boom)
    assert bp.run_once_main(False, 2, 180.0) == 1
    assert not (tmp_path / "probe_state.json").exists()


def test_once_main_read_failure_returns_1_and_skips_state(monkeypatch,
                                                          tmp_path):
    _patch_main(monkeypatch, tmp_path, procs=[BRIDGE_PROC])
    monkeypatch.setattr(bp, "connect_wx", lambda chat: "WX")
    monkeypatch.setattr(bp, "run_once", lambda *a, **k: None)
    assert bp.run_once_main(False, 2, 180.0) == 1
    assert not (tmp_path / "probe_state.json").exists()


def test_once_main_dry_run_relaunch_does_not_actually_launch(
        monkeypatch, tmp_path):
    _patch_main(monkeypatch, tmp_path, procs=[])
    relaunched: list = []
    monkeypatch.setattr(bp, "relaunch_bat", lambda: relaunched.append(1))
    assert bp.run_once_main(True, 2, 180.0) == 0
    assert relaunched == []


# ---------- 新桥宽限：刚启动的桥不吃跨进程陈旧 first-seen ----------

class _FakePsutilProc:
    """youngest_bridge_age 的 psutil.Process 打桩：pid→已启动秒数。"""
    ages: dict[int, float] = {}

    def __init__(self, pid: int):
        import psutil
        if pid not in type(self).ages:
            raise psutil.NoSuchProcess(pid)
        self._ct = 100_000.0 - type(self).ages[pid]

    def create_time(self) -> float:
        return self._ct


def _patch_psutil_ages(monkeypatch, ages: dict[int, float]) -> None:
    _FakePsutilProc.ages = ages
    import psutil
    monkeypatch.setattr(psutil, "Process", _FakePsutilProc)


def test_youngest_bridge_age_returns_minimum(monkeypatch):
    _patch_psutil_ages(monkeypatch, {200: 30.0, 201: 900.0})
    clock = FakeClock(100_000.0)
    assert bp.youngest_bridge_age([200, 201], clock=clock) == 30.0


def test_youngest_bridge_age_none_when_all_pids_gone(monkeypatch):
    _patch_psutil_ages(monkeypatch, {})
    assert bp.youngest_bridge_age([200], clock=FakeClock()) is None


def _stale_state_file(tmp_path) -> Path:
    path = tmp_path / "probe_state.json"
    path.write_text(json.dumps({
        "version": 1,
        "first_seen": {"self|任务A": 1.0, "self|任务B": 1.0},
        "cooldown_until": 0.0,
    }), encoding="utf-8")
    return path


def test_once_main_fresh_bridge_forgets_loaded_first_seen(monkeypatch,
                                                         tmp_path):
    """state 带着跨小时陈旧 first-seen，桥却是 10s 前才启动的：上一轮的
    时间戳属于上一代桥，run_once 拿到的 detector 必须已是干净基线。"""
    _stale_state_file(tmp_path)
    _patch_main(monkeypatch, tmp_path, procs=[BRIDGE_PROC])
    _patch_psutil_ages(monkeypatch, {200: 10.0})
    monkeypatch.setattr(bp.time, "time", lambda: 100_000.0)
    monkeypatch.setattr(bp, "connect_wx", lambda chat: "WX")
    seen: list[dict] = []

    def fake_run_once(wx, chat, detector, dry_run,
                      wait=bp.CONFIRM_WAIT_SECONDS, sleeper=None):
        seen.append(dict(detector._first_seen))
        return Verdict(False, 0)

    monkeypatch.setattr(bp, "run_once", fake_run_once)
    assert bp.run_once_main(False, 2, 180.0) == 0
    assert seen == [{}]


def test_once_main_old_bridge_keeps_loaded_first_seen(monkeypatch, tmp_path):
    """桥已运行 2 小时：state 里的陈旧 first-seen 就是这代桥攒下的，保留。"""
    _stale_state_file(tmp_path)
    _patch_main(monkeypatch, tmp_path, procs=[BRIDGE_PROC])
    _patch_psutil_ages(monkeypatch, {200: 7200.0})
    monkeypatch.setattr(bp.time, "time", lambda: 100_000.0)
    monkeypatch.setattr(bp, "connect_wx", lambda chat: "WX")
    seen: list[dict] = []

    def fake_run_once(wx, chat, detector, dry_run,
                      wait=bp.CONFIRM_WAIT_SECONDS, sleeper=None):
        seen.append(dict(detector._first_seen))
        return Verdict(False, 0)

    monkeypatch.setattr(bp, "run_once", fake_run_once)
    assert bp.run_once_main(False, 2, 180.0) == 0
    assert seen and seen[0]            # 历史时间戳原样保留


# ---------- mutex 获取本身异常（pythonw 下必须留文件日志痕迹） ----------

def test_once_main_mutex_error_returns_1(monkeypatch, tmp_path):
    _patch_main(monkeypatch, tmp_path, procs=[])

    def boom():
        raise OSError("CreateMutexW failed")

    monkeypatch.setattr(bp, "acquire_mutex", boom)
    relaunched: list = []
    monkeypatch.setattr(bp, "relaunch_bat", lambda: relaunched.append(1))
    assert bp.run_once_main(False, 2, 180.0) == 1
    assert relaunched == []


def test_run_watch_mutex_error_returns_1(monkeypatch):
    monkeypatch.setattr(bp, "setup_logging", lambda: None)

    def boom():
        raise OSError("CreateMutexW failed")

    monkeypatch.setattr(bp, "acquire_mutex", boom)
    assert bp.run_watch(False, 15.0, 2, 180.0) == 1


# ---------- run_watch（常驻排障模式） ----------

def test_run_watch_returns_1_when_wechat_unavailable(monkeypatch):
    monkeypatch.setattr(bp, "setup_logging", lambda: None)
    monkeypatch.setattr(bp, "acquire_mutex", lambda: 1)
    monkeypatch.setattr(bp, "load_config",
                        lambda: SimpleNamespace(listen_chat="x"))

    def boom(chat):
        raise RuntimeError("no wechat")

    monkeypatch.setattr(bp, "connect_wx", boom)
    assert bp.run_watch(False, 15.0, 2, 180.0) == 1


def test_run_watch_mutex_busy_exits(monkeypatch):
    monkeypatch.setattr(bp, "setup_logging", lambda: None)
    monkeypatch.setattr(bp, "acquire_mutex", lambda: None)
    assert bp.run_watch(False, 15.0, 2, 180.0) == 0
