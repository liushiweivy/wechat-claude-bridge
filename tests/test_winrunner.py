"""WindowRunner：注入→轮询 transcript→回传结果（注入/进程打桩，不起真窗口）。"""
import json
import threading
import time
from pathlib import Path

import pytest

from bridge import winrunner
from bridge.core import transcript_path
from bridge.winrunner import WindowRunner


@pytest.fixture(autouse=True)
def fast_poll(monkeypatch):
    monkeypatch.setattr(winrunner, "POLL_SECONDS", 0.05)
    monkeypatch.setattr(winrunner, "STABLE_POLLS", 2)
    monkeypatch.setattr(winrunner, "CONFIRM_RETRY_SECONDS", 0.05)
    monkeypatch.setattr(winrunner, "CONFIRM_SECONDS", 0.6)
    # 测试用的假 PID（1234）默认不存在，桩成"窗口活着"
    monkeypatch.setattr(winrunner.psutil, "pid_exists", lambda pid: True)


def _write_assistant(path: Path, text: str) -> None:
    line = json.dumps({"type": "assistant", "message": {
        "content": [{"type": "text", "text": text}]}})
    with path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def _runner(tmp_path: Path, monkeypatch):
    path = transcript_path(tmp_path, "sess-win")
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_assistant(path, "之前的答案")
    r = WindowRunner(python_bin="unused", timeout=5)
    monkeypatch.setattr(r, "_escape", lambda pid: None)
    return r, path


def test_done_when_new_answer_stabilizes(tmp_path: Path, monkeypatch):
    r, path = _runner(tmp_path, monkeypatch)
    monkeypatch.setattr(
        r, "_inject",
        lambda pid, text: threading.Timer(
            0.1, _write_assistant, args=(path, "新答案")).start())
    done = []
    assert r.start("任务", tmp_path, "sess-win", pid=1234,
                   on_done=lambda st: done.append(st))
    deadline = time.time() + 5
    while not done and time.time() < deadline:
        time.sleep(0.05)
    assert done and done[0].state == "done"
    assert done[0].result == "新答案"


def test_timeout_when_nothing_written(tmp_path: Path, monkeypatch):
    r, _ = _runner(tmp_path, monkeypatch)
    r.timeout = 0.3
    monkeypatch.setattr(r, "_inject", lambda pid, text: None)
    done = []
    r.start("任务", tmp_path, "sess-win", pid=1234,
            on_done=lambda st: done.append(st))
    deadline = time.time() + 5
    while not done and time.time() < deadline:
        time.sleep(0.05)
    assert done and done[0].state == "timeout"


def test_cancel_sends_escape(tmp_path: Path, monkeypatch):
    r, _ = _runner(tmp_path, monkeypatch)
    escaped = []
    monkeypatch.setattr(r, "_inject", lambda pid, text: None)
    monkeypatch.setattr(r, "_escape", lambda pid: escaped.append(pid))
    r.timeout = 5
    done = []
    r.start("任务", tmp_path, "sess-win", pid=1234,
            on_done=lambda st: done.append(st))
    time.sleep(0.15)
    assert r.cancel()
    deadline = time.time() + 5
    while not done and time.time() < deadline:
        time.sleep(0.05)
    assert done and done[0].state == "canceled"
    assert escaped == [1234]


def test_inject_failure_reports_error(tmp_path: Path, monkeypatch):
    r, _ = _runner(tmp_path, monkeypatch)

    def _boom(pid, text):
        raise RuntimeError("AttachConsole 失败")

    monkeypatch.setattr(r, "_inject", _boom)
    done = []
    r.start("任务", tmp_path, "sess-win", pid=1234,
            on_done=lambda st: done.append(st))
    deadline = time.time() + 5
    while not done and time.time() < deadline:
        time.sleep(0.05)
    assert done and done[0].state == "error"
    assert "注入窗口失败" in done[0].error


# ---- 屏幕抓取兜底（transcript 永不落盘的异常期，2026-10-18 v2.1.283 实测）----

_BASELINE = "✻ Claude Code\n● 旧答案\n✳ 思考 for 1s · done 13:00\n❯ "
_WORKING = ("✻ Claude Code\n> 任务\n"
            "✳ Brewing... (esc to interrupt · 3s)\n● 旧答案\n"
            "✳ 思考 for 1s · done 13:00")
_DONE_SCREEN = ("✻ Claude Code\n> 任务\n● 新答案\n"
                "✳ 完成 for 4s · done 13:05\n"
                "✳ 思考 for 1s · done 13:00\n❯ ")


def _screen_runner(tmp_path: Path, monkeypatch, screens: list[str]):
    """transcript 永不创建（不落盘异常），_read_screen 按剧本返回。"""
    r = WindowRunner(python_bin="unused", timeout=5)
    monkeypatch.setattr(r, "_escape", lambda pid: None)
    it = iter(screens)
    monkeypatch.setattr(r, "_read_screen", lambda pid: next(it, screens[-1]))
    return r


def _wait_done(r, tmp_path, monkeypatch):
    monkeypatch.setattr(r, "_inject", lambda pid, text: None)
    done = []
    r.start("任务", tmp_path, "sess-win", pid=1234,
            on_done=lambda st: done.append(st))
    deadline = time.time() + 5
    while not done and time.time() < deadline:
        time.sleep(0.05)
    return done


def test_screen_fallback_done_when_marker_appears(tmp_path: Path, monkeypatch):
    r = _screen_runner(tmp_path, monkeypatch,
                       [_BASELINE, _WORKING, _DONE_SCREEN])
    done = _wait_done(r, tmp_path, monkeypatch)
    assert done and done[0].state == "done"
    assert done[0].result == "新答案"


def test_screen_fallback_error_when_no_answer_marker(tmp_path: Path,
                                                     monkeypatch):
    no_answer = ("✻ Claude Code\n> 任务\n"
                 "✳ 完成 for 4s · done 13:05\n"
                 "✳ 思考 for 1s · done 13:00\n❯ ")
    r = _screen_runner(tmp_path, monkeypatch, [_BASELINE, _WORKING, no_answer])
    done = _wait_done(r, tmp_path, monkeypatch)
    assert done and done[0].state == "error"
    assert "未抠到回答文本" in done[0].error


def test_screen_fallback_keeps_polling_without_new_marker(
        tmp_path: Path, monkeypatch):
    """完成标记数没增加时不能误判完成（画面稳定也不行）。"""
    r = _screen_runner(tmp_path, monkeypatch, [_BASELINE])
    r.timeout = 0.4
    done = _wait_done(r, tmp_path, monkeypatch)
    assert done and done[0].state == "timeout"


def test_resends_bare_enter_until_submit_confirmed(tmp_path: Path,
                                                   monkeypatch):
    """回车被 TUI 吞掉时（文字留在输入框）按节奏补发裸回车，
    直到看到执行迹象（2026-10-18 窗口 2188 实测补发一次即提交）。"""
    # 基线 + 两拍无动静 + 任务完成
    r = _screen_runner(tmp_path, monkeypatch,
                       [_BASELINE, _BASELINE, _BASELINE, _DONE_SCREEN])
    injects = []
    monkeypatch.setattr(r, "_inject",
                        lambda pid, text: injects.append(text))
    done = []
    r.start("任务", tmp_path, "sess-win", pid=1234,
            on_done=lambda st: done.append(st))
    deadline = time.time() + 5
    while not done and time.time() < deadline:
        time.sleep(0.05)
    assert done and done[0].state == "done"
    assert done[0].result == "新答案"
    assert injects[0] != ""                    # 第一次是正式注入
    assert "" in injects[1:]                   # 之后补发过裸回车
