"""app 层测试：消息类型闸门与指令分发（网关打桩，不连接微信）。"""
import logging
from pathlib import Path

import pytest

from bridge.app import BridgeApp, is_actionable, sessions_log
from bridge.config import Config
from bridge.runner import TaskStatus


class FakeGateway:
    def __init__(self):
        self.sent = []

    def send(self, text):
        self.sent.append(text)


def _app(tmp_path: Path) -> BridgeApp:
    cfg = Config(
        listen_chat="文件传输助手",
        workspace=str(tmp_path),
        allowed_dirs=(str(tmp_path),),
    )
    app = BridgeApp(cfg)
    app.gw = FakeGateway()
    return app


def test_only_self_and_friend_are_actionable():
    assert is_actionable("self")
    assert is_actionable("friend")
    assert not is_actionable("sys")
    assert not is_actionable("time")
    assert not is_actionable("recall")


def test_dispatch_help_and_status_without_wechat(tmp_path: Path):
    app = _app(tmp_path)
    app.dispatch("/help")
    app.dispatch("/status")
    assert any("指令" in s or "/sudo" in s for s in app.gw.sent)
    assert any("工作目录" in s for s in app.gw.sent)


def test_dispatch_unknown_command_gets_hint(tmp_path: Path):
    app = _app(tmp_path)
    app.dispatch("/frobnicate")
    assert app.gw.sent and "/help" in app.gw.sent[-1]


def test_dispatch_notify_message_is_ignored(tmp_path: Path):
    app = _app(tmp_path)
    app.dispatch("🌐 会话台公网地址\nhttp://x/?token=1")
    assert app.gw.sent == []
    assert not app.runner.status.is_busy


def test_new_resets_session(tmp_path: Path):
    app = _app(tmp_path)
    app.session_id = "sess-old"
    app.dispatch("/new")
    assert app.session_id == ""


def test_cd_rejects_escape(tmp_path: Path):
    app = _app(tmp_path)
    app.dispatch("/cd ../../..")
    assert app.gw.sent and "⚠️" in app.gw.sent[-1]


# ---- sessions.log 留痕 ----

class _ListHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record.getMessage())


def _capture_sessions_log():
    logger = sessions_log()
    handler = _ListHandler()
    logger.addHandler(handler)
    return handler


class _StubRunner:
    """假装成功接单的最小 runner（避免真起 claude 子进程）。"""
    def __init__(self):
        self.status = TaskStatus()
        self.calls = []

    def start(self, prompt, cwd, sudo, session_id, on_done=None):
        self.calls.append((prompt, cwd, sudo, session_id))
        return True


def test_start_task_writes_start_line(tmp_path: Path):
    app = _app(tmp_path)
    app.runner = _StubRunner()
    handler = _capture_sessions_log()
    app.session_id = "abcdef12-rest"
    app.dispatch("帮我看看项目结构")
    assert any(r.startswith("START") and "abcdef12" in r
               and "帮我看看项目结构" in r for r in handler.records)


def test_report_writes_done_line_with_jsonl_path(tmp_path: Path):
    app = _app(tmp_path)
    handler = _capture_sessions_log()
    st = TaskStatus(state="done", prompt="x", started_at=1.0, finished_at=3.0,
                    result="好了", session_id="sess-123")
    app._report(st)
    done = [r for r in handler.records if r.startswith("DONE")]
    assert done, "任务结束必须落一行 DONE"
    assert "sess-123" in done[0] and "sess-123.jsonl" in done[0]
    assert "elapsed=2s" in done[0]


def test_report_without_session_id_still_logs(tmp_path: Path):
    app = _app(tmp_path)
    handler = _capture_sessions_log()
    st = TaskStatus(state="canceled", started_at=1.0, finished_at=2.0)
    app._report(st)
    assert any(r.startswith("DONE state=canceled") and "jsonl=-" in r
               for r in handler.records)


# ---- 回复长度约束 ----

def test_start_task_prompt_carries_reply_hint(tmp_path: Path):
    app = _app(tmp_path)
    stub = _StubRunner()
    app.runner = stub
    app.dispatch("帮我看看项目结构")
    sent_prompt = stub.calls[0][0]
    assert sent_prompt.startswith("帮我看看项目结构")
    assert "200 字以内" in sent_prompt


def test_report_caps_long_result(tmp_path: Path):
    app = _app(tmp_path)
    st = TaskStatus(state="done", prompt="x", started_at=1.0, finished_at=2.0,
                    result="好" * 500, session_id="sess-1")
    app._report(st)
    body = app.gw.sent[-1].split("\n\n", 1)[1]
    assert len(body) <= 201 and body.endswith("…")


# ---- /open 开窗口 ----

def _record_popen(monkeypatch):
    calls = []

    class _FakePopen:
        def __init__(self, cmd, cwd=None, shell=False, env=None, **kw):
            calls.append({"cmd": cmd, "cwd": cwd, "shell": shell, "env": env})

    monkeypatch.setattr("bridge.app.subprocess.Popen", _FakePopen)
    return calls


def test_spawn_window_strips_claude_env_markers(tmp_path: Path, monkeypatch):
    """带 CLAUDE_CODE_CHILD_SESSION 标记开出的窗口会被当成子会话，
    不写 transcript（2026-10-18 窗口 2188 实测），spawn 必须剥掉 CLAUDE*。"""
    app = _app(tmp_path)
    calls = _record_popen(monkeypatch)
    monkeypatch.setenv("CLAUDE_CODE_CHILD_SESSION", "1")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "parent-sess")
    monkeypatch.setenv("CLAUDECODE", "1")
    app.dispatch("/open")
    env = calls[0]["env"]
    assert env is not None
    assert not any(k.upper().startswith("CLAUDE") for k in env)
    assert "PATH" in env or "Path" in env          # 其余环境保留


def test_open_with_session_spawns_resume_window(tmp_path: Path, monkeypatch):
    app = _app(tmp_path)
    calls = _record_popen(monkeypatch)
    app.session_id = "sess-123-rest"
    app.dispatch("/open")
    cmd = calls[0]["cmd"]
    assert isinstance(cmd, list) and "wscript" in cmd[0]
    assert "open-cc-window.vbs" in cmd[1]
    assert "sess-123-rest" in cmd              # 会话 id 传给 vbs 做 --resume
    assert "sess-123" in app.gw.sent[-1]
    assert app._pending_open_since == 0.0      # 已有会话无需认领


def test_open_without_session_marks_pending(tmp_path: Path, monkeypatch):
    app = _app(tmp_path)
    calls = _record_popen(monkeypatch)
    app.dispatch("/open")
    assert calls and calls[0]["cmd"][-1] == ""   # 空会话 → vbs 开全新窗口
    assert app._pending_open_since > 0


def test_open_refused_while_busy(tmp_path: Path, monkeypatch):
    app = _app(tmp_path)
    calls = _record_popen(monkeypatch)
    app.runner.status = TaskStatus(state="running", started_at=1.0)
    app.dispatch("/open")
    assert calls == []
    assert "任务执行中" in app.gw.sent[-1]


def test_adopt_window_session(tmp_path: Path, monkeypatch):
    import time as _time
    app = _app(tmp_path)
    app._pending_open_since = _time.time()
    monkeypatch.setattr("bridge.app.find_newer_session",
                        lambda cwd, since, exclude=frozenset(): "adopted-sess")
    app._maybe_adopt_window_session()
    assert app.session_id == "adopted-sess"
    assert app._pending_open_since == 0.0
    assert "已认领" in app.gw.sent[-1]


def test_adopt_skipped_when_expired_or_has_session(tmp_path: Path, monkeypatch):
    import time as _time
    app = _app(tmp_path)
    monkeypatch.setattr("bridge.app.find_newer_session",
                        lambda cwd, since, exclude=frozenset(): "should-not-use")
    app._pending_open_since = _time.time() - 99999   # 过期
    app._maybe_adopt_window_session()
    assert app.session_id == "" and app._pending_open_since == 0.0
    app._pending_open_since = _time.time()
    app.session_id = "have-one"                       # 已有会话不认领
    app._maybe_adopt_window_session()
    assert app.session_id == "have-one"


def test_adopt_skipped_while_task_running(tmp_path: Path, monkeypatch):
    """任务运行中它的 jsonl 是最新文件，此时认领必错。"""
    import time as _time
    app = _app(tmp_path)
    called = []
    monkeypatch.setattr("bridge.app.find_newer_session",
                        lambda *a, **k: called.append(1) or "x")
    app._pending_open_since = _time.time()
    app.runner.status = TaskStatus(state="running", started_at=1.0)
    app._maybe_adopt_window_session()
    assert called == [] and app.session_id == ""
    assert app._pending_open_since > 0               # 待认领保留


def test_adopt_excludes_own_task_sessions(tmp_path: Path, monkeypatch):
    import time as _time
    app = _app(tmp_path)
    seen = {}

    def _fake_find(cwd, since, exclude=frozenset()):
        seen["ex"] = exclude
        return "win-sess"

    monkeypatch.setattr("bridge.app.find_newer_session", _fake_find)
    app._own_session_ids.add("own-task-sess")
    app._pending_open_since = _time.time()
    app._maybe_adopt_window_session()
    assert app.session_id == "win-sess"
    assert "own-task-sess" in seen["ex"]


def test_drain_done_records_own_session(tmp_path: Path):
    app = _app(tmp_path)
    st = TaskStatus(state="done", started_at=1.0, finished_at=2.0,
                    result="好", session_id="own-1")
    app.done_q.put(st)
    app._drain_done()
    assert "own-1" in app._own_session_ids


# ---- 窗口执行路径 ----

class _StubWindowRunner:
    def __init__(self):
        self.status = TaskStatus()
        self.calls = []
        self.canceled = False

    def start(self, prompt, cwd, session_id, pid, reply_limit=200, on_done=None):
        self.calls.append((prompt, session_id, pid))
        return True

    def cancel(self):
        self.canceled = True
        return True


def test_task_routes_to_window_when_bound(tmp_path: Path, monkeypatch):
    app = _app(tmp_path)
    headless = _StubRunner()
    app.runner = headless
    app.window = _StubWindowRunner()
    monkeypatch.setattr(app, "_window_pid_for", lambda sid: 4321)
    app.session_id = "sess-win"
    app.dispatch("在窗口里跑这个")
    assert app.window.calls == [("在窗口里跑这个", "sess-win", 4321)]
    assert headless.calls == []                     # 无头路径没被触发
    assert "【窗口" in app.gw.sent[-1]


def test_task_falls_back_headless_without_window(tmp_path: Path, monkeypatch):
    app = _app(tmp_path)
    headless = _StubRunner()
    app.runner = headless
    app.window = _StubWindowRunner()
    monkeypatch.setattr(app, "_window_pid_for", lambda sid: 0)
    app.session_id = "sess-nowin"
    app.dispatch("无窗口任务")
    assert app.window.calls == []
    assert headless.calls and "【窗口" not in app.gw.sent[-1]


def test_cancel_prefers_window_task(tmp_path: Path):
    app = _app(tmp_path)
    app.window = _StubWindowRunner()
    app.window.status = TaskStatus(state="running", started_at=1.0)
    app.dispatch("/cancel")
    assert app.window.canceled
    assert "已取消" in app.gw.sent[-1]


# ---- 屏幕抓取认领（transcript 不落盘异常的兜底，2026-10-18 v2.1.283 实测）----

_SCRAPE_SID = "5a789ed1-c814-451b-a2e9-48c22fc281b7"


class _ScrapeStubWindow(_StubWindowRunner):
    """记录 /status 注入与抓屏、按剧本返回屏幕文本的窗口桩。"""

    def __init__(self, screen: str):
        super().__init__()
        self.screen = screen
        self.injected = []
        self.escaped = []

    def _inject(self, pid, text):
        self.injected.append((pid, text))

    def _read_screen(self, pid):
        return self.screen

    def _escape(self, pid):
        self.escaped.append(pid)


def _scrape_app(tmp_path: Path, monkeypatch, screen: str):
    import time as _time
    app = _app(tmp_path)
    app.window = _ScrapeStubWindow(screen)
    app._pending_open_since = _time.time()
    monkeypatch.setattr("bridge.app.find_newer_session",
                        lambda cwd, since, exclude=frozenset(): "")
    monkeypatch.setattr(BridgeApp, "_find_fresh_window_pid",
                        staticmethod(lambda since: 4321))
    monkeypatch.setattr("bridge.app.time.sleep", lambda s: None)
    return app


def test_scrape_adopt_when_no_jsonl(tmp_path: Path, monkeypatch):
    app = _scrape_app(tmp_path, monkeypatch,
                      f"Claude Code Status\n Session ID: {_SCRAPE_SID}\n")
    app._maybe_adopt_window_session()
    assert app.session_id == _SCRAPE_SID
    assert app._pending_open_since == 0.0
    assert app.window.injected == [(4321, "/status")]
    assert app.window.escaped == [4321]           # 弹层必须关掉
    assert "已认领" in app.gw.sent[-1]


def test_scrape_adopt_throttled(tmp_path: Path, monkeypatch):
    import time as _time
    app = _scrape_app(tmp_path, monkeypatch, "没有会话行")
    app._maybe_adopt_window_session()
    assert app.session_id == ""                    # 抠不到 → 不认领
    first = len(app.window.injected)
    assert first == 1
    app._maybe_adopt_window_session()              # 间隔内不再往窗口打字
    assert len(app.window.injected) == 1
    app._last_status_scrape = _time.time() - 999   # 过节流期后可重试
    app._maybe_adopt_window_session()
    assert len(app.window.injected) == 2


def test_scrape_adopt_rejects_own_session(tmp_path: Path, monkeypatch):
    app = _scrape_app(tmp_path, monkeypatch,
                      f" Session ID: {_SCRAPE_SID}\n")
    app._own_session_ids.add(_SCRAPE_SID)          # 桥自己任务的会话不能认领
    app._maybe_adopt_window_session()
    assert app.session_id == ""
    assert app._pending_open_since > 0             # 待认领保留


def test_scrape_adopt_skipped_without_window(tmp_path: Path, monkeypatch):
    import time as _time
    app = _app(tmp_path)
    app.window = _ScrapeStubWindow(f" Session ID: {_SCRAPE_SID}\n")
    app._pending_open_since = _time.time()
    monkeypatch.setattr("bridge.app.find_newer_session",
                        lambda cwd, since, exclude=frozenset(): "")
    monkeypatch.setattr(BridgeApp, "_find_fresh_window_pid",
                        staticmethod(lambda since: 0))    # 窗口已不在
    app._maybe_adopt_window_session()
    assert app.session_id == ""
    assert app.window.injected == []


# ---- 重连自救：进程级 COM 损坏时自杀，靠自启循环换进程 ----

class _FlappingGateway(FakeGateway):
    """pull 永远报 UIA 故障；reconnect 按 reconnect_ok 剧本成败。"""

    def __init__(self, reconnect_ok: bool):
        super().__init__()
        self.reconnect_ok = reconnect_ok
        self.reconnect_calls = 0

    def connect(self):
        return "simple"

    def pull(self):
        if self.reconnect_ok and self.reconnect_calls:
            raise KeyboardInterrupt      # 重连成功后借此跳出主循环
        raise RuntimeError("UIA dead")

    def reconnect(self):
        self.reconnect_calls += 1
        if not self.reconnect_ok:
            raise RuntimeError("still dead")
        return "simple"


def test_reconnect_suicide_after_max_rounds(tmp_path: Path, monkeypatch):
    """连续 N 轮（每轮 10/20/30s 三次）重连全败 → SystemExit 自杀，
    由 autostart-run.bat 重试循环拉起全新进程（2026-10-18 实测进程级
    COM 损坏时 reconnect 每次新建 WeChat() 仍连不上，新进程秒连）。"""
    app = _app(tmp_path)
    gw = _FlappingGateway(reconnect_ok=False)
    app.gw = gw
    monkeypatch.setattr("bridge.app.time.sleep", lambda s: None)
    monkeypatch.setattr("bridge.app.MAX_RECONNECT_ROUNDS", 2)
    with pytest.raises(SystemExit):
        app.run()
    assert gw.reconnect_calls == 2 * 3
    assert app._reconnect_fail_rounds == 2


def test_reconnect_recovery_continues_normally(tmp_path: Path, monkeypatch):
    """重连成功则清零计数、发恢复通知、继续主循环（不自杀）。"""
    app = _app(tmp_path)
    gw = _FlappingGateway(reconnect_ok=True)
    app.gw = gw
    monkeypatch.setattr("bridge.app.time.sleep", lambda s: None)
    with pytest.raises(KeyboardInterrupt):
        app.run()
    assert gw.reconnect_calls == 1
    assert app._reconnect_fail_rounds == 0
    assert any("连接已恢复" in s for s in gw.sent)


def test_command_replies_carry_bridge_prefix(tmp_path: Path):
    """探活（bridge/probe.py）按首字符区分桥回复与用户消息：
    /cancel 空闲、/cd 无参、/status 的回复必须带桥前缀，否则会被误判成
    未响应的用户消息。"""
    from bridge.probe import BRIDGE_PREFIXES, normalize
    app = _app(tmp_path)
    app.dispatch("/cancel")      # 空闲：🚫 当前没有执行中的任务
    app.dispatch("/cd")          # 无参：📁 当前工作目录：…
    app.dispatch("/status")      # 🤖 状态：…
    assert len(app.gw.sent) == 3
    for text in app.gw.sent:
        assert normalize(text).startswith(BRIDGE_PREFIXES), text


def test_all_safe_send_literals_carry_bridge_prefix():
    """防漂移源码扫描：探活（bridge/probe.py）与桥的唯一耦合约束是"所有
    _safe_send 文本必须带 BRIDGE_PREFIXES 内 emoji"。未来新增裸回复会在
    这里被拦下，而不是上线后杀掉健康桥（2026-10-19 code review 建议）。"""
    import re
    from bridge.probe import BRIDGE_PREFIXES, normalize
    src = (Path(__file__).resolve().parent.parent
           / "bridge" / "app.py").read_text(encoding="utf-8")
    firsts = re.findall(r'_safe_send\(\s*f?["\'](.)', src)
    assert firsts, "应至少匹配到若干字面量 _safe_send 调用"
    # 跳过两种动态组合形态（正则无法追踪，由行为测试覆盖）：
    #   "\" —— "\n".join(lines) 的 joiner，首行 🤖 见上面的行为测试
    #   "{" —— f"{head}…" 占位符，head 在源码里是带 ✅ 的字面量
    firsts = [c for c in firsts if c not in ("\\", "{")]
    bad = [c for c in firsts if not normalize(c).startswith(BRIDGE_PREFIXES)]
    assert not bad, f"_safe_send 裸回复首字符不在桥前缀表: {bad}"
