"""核心纯逻辑测试：命令解析 / 分段 / 回环 / 目录约束 / 参数与流解析。"""
import json
import time
from pathlib import Path

import pytest

from bridge.core import (
    EchoFilter,
    build_claude_args,
    clean_stderr,
    last_assistant_text,
    parse_instruction,
    parse_stream_line,
    resolve_cd,
    split_wechat,
    transcript_path,
)


# ---------- parse_instruction ----------
def test_plain_text_is_task():
    ins = parse_instruction("帮我看看这个报错")
    assert ins.kind == "task"
    assert ins.text == "帮我看看这个报错"
    assert ins.sudo is False


def test_commands():
    assert parse_instruction("/help").kind == "help"
    assert parse_instruction("/new").kind == "new"
    assert parse_instruction("/status").kind == "status"
    assert parse_instruction("/cancel").kind == "cancel"


def test_sudo_sets_flag_and_keeps_text():
    ins = parse_instruction("/sudo 跑一下 pytest")
    assert ins.kind == "task"
    assert ins.sudo is True
    assert ins.text == "跑一下 pytest"


def test_sudo_without_arg_is_unknown():
    assert parse_instruction("  /sudo  ").kind == "unknown"


def test_unknown_command_and_blank():
    ins = parse_instruction("/frobnicate x")
    assert ins.kind == "unknown"
    assert "/frobnicate" in ins.text
    assert parse_instruction("   ").kind == "unknown"


def test_cd_carries_arg():
    assert parse_instruction("/cd demo").text == "demo"
    assert parse_instruction("/cd").text == ""


def test_command_prefix_only_when_starts_at_head():
    # 行内的 /help 只是普通文本
    ins = parse_instruction("请解释一下 /help 是什么")
    assert ins.kind == "task"


# ---------- split_wechat ----------
def test_short_text_single_piece():
    assert split_wechat("短结果", 1500) == ["短结果"]


def test_long_text_split_with_page_numbers():
    text = "段落。" * 1000  # 3000 字
    pieces = split_wechat(text, 500)
    assert 6 <= len(pieces) <= 9
    assert all(len(p) <= 500 + 10 for p in pieces)  # 页码少量超出
    assert pieces[0].endswith("（1/%d）" % len(pieces))
    assert pieces[-1].endswith("（%d/%d）" % (len(pieces), len(pieces)))


def test_hard_split_giant_paragraph_without_newlines():
    text = "abc" * 1000  # 3000 字单段
    pieces = split_wechat(text, 400)
    assert all(len(p) <= 410 for p in pieces)
    assert "".join(p.split("\n（")[0] for p in pieces).startswith("abc")


# ---------- EchoFilter ----------
def test_echo_matches_recent_sent():
    f = EchoFilter(window_seconds=15)
    f.mark_sent("✅ 完成（3s）\n结果内容")
    assert f.is_echo("✅ 完成（3s）\n结果内容")


def test_echo_expires():
    f = EchoFilter(window_seconds=0.01, exact_window_seconds=0.01)
    f.mark_sent("某条回复")
    time.sleep(0.05)
    assert not f.is_echo("某条回复")


def test_echo_consumed_on_match_wont_swallow_resend():
    f = EchoFilter()
    f.mark_sent("同一句话")
    assert f.is_echo("同一句话")          # 第一次回声被过滤
    assert not f.is_echo("同一句话")      # 用户稍后主动重发同文，不再吞


def test_exact_echo_survives_long_multipart_send():
    # 回归：长结果第 1 段发出后，几十秒才被 pull 回报，仍须识别为回声
    f = EchoFilter(window_seconds=0.05, exact_window_seconds=300)
    f.mark_sent("（1/20）很长的结果开头……")
    time.sleep(0.2)
    assert f.is_echo("（1/20）很长的结果开头……")


def test_echo_does_not_swallow_new_command():
    f = EchoFilter()
    f.mark_sent("✅ 完成\n好的")
    assert not f.is_echo("继续下一个任务")


def test_echo_matches_through_newline_normalization():
    # 回归 2026-10-15：UIA 回报的多行消息把 \n 变成 \r\n，
    # 精确匹配失败 → 桥把自己发出的失败通知当成新任务（回环）。
    f = EchoFilter()
    f.mark_sent("❌ 执行失败（147s）\nclaude 没有返回文本\n模型目录错误")
    assert f.is_echo("❌ 执行失败（147s）\r\nclaude 没有返回文本\r\n模型目录错误")


def test_echo_matches_when_uia_duplicates_blank_lines():
    f = EchoFilter()
    f.mark_sent("第一段\n第二段")
    assert f.is_echo("第一段\r\n\r\n第二段\r\n")  # UIA 多余空行/尾部换行不影响识别


# ---------- resolve_cd ----------
def test_resolve_cd_subdir_and_root(tmp_path: Path):
    sub = tmp_path / "proj"
    sub.mkdir()
    assert resolve_cd("proj", tmp_path, [str(tmp_path)]) == sub.resolve()
    assert resolve_cd("..", sub, [str(tmp_path)]) == tmp_path.resolve()


def test_resolve_cd_blocks_escape(tmp_path: Path):
    current = tmp_path / "proj"
    current.mkdir()
    with pytest.raises(ValueError):
        resolve_cd("../../..", current, [str(tmp_path)])


def test_resolve_cd_nonexistent(tmp_path: Path):
    with pytest.raises(ValueError):
        resolve_cd("nope", tmp_path, [str(tmp_path)])


# ---------- build_claude_args ----------
def test_build_args_basic():
    args = build_claude_args("任务", "acceptEdits")
    assert args[:2] == ["-p", "任务"]
    assert "--verbose" in args  # stream-json 在 -p 下的硬性要求
    assert "--output-format" in args and "stream-json" in args
    assert "--resume" not in args


def test_build_args_with_resume_and_sudo():
    args = build_claude_args("x", "bypassPermissions", resume_id="sess123",
                             extra_args=("--verbose",))
    i = args.index("--resume")
    assert args[i + 1] == "sess123"
    assert args[args.index("--permission-mode") + 1] == "bypassPermissions"
    assert args[-1] == "--verbose"


# ---------- parse_stream_line ----------
def test_parse_session_and_tool_and_result():
    ev = parse_stream_line('{"type":"system","session_id":"abc-123"}')
    assert ev.kind == "session" and ev.session_id == "abc-123"

    ev = parse_stream_line(
        '{"type":"assistant","message":{"content":['
        '{"type":"tool_use","name":"Bash"},{"type":"text","text":"x"}]}}'
    )
    assert ev.kind == "tool" and ev.tool == "Bash"

    ev = parse_stream_line('{"type":"result","subtype":"success","result":"答案"}')
    assert ev.kind == "result" and ev.text == "答案" and not ev.is_error


def test_parse_stream_error_subtype_and_garbage():
    ev = parse_stream_line('{"type":"result","subtype":"error","result":"x"}')
    assert ev.is_error
    assert parse_stream_line("not json") is None
    assert parse_stream_line("") is None
    assert parse_stream_line('{"type":"user"}') is None


# ---------- transcript 兜底 ----------
def test_transcript_path_matches_claude_slug(tmp_path: Path):
    p = transcript_path(r"E:\1workspace", "sid-1", home=tmp_path)
    assert p == tmp_path / ".claude" / "projects" / "E--1workspace" / "sid-1.jsonl"


def test_last_assistant_text_returns_final_text_block(tmp_path: Path):
    lines = [
        {"type": "system", "session_id": "sid-1"},
        {"type": "assistant", "message": {"content": [
            {"type": "thinking", "thinking": "想一下"},
            {"type": "tool_use", "name": "Read"},
        ]}},
        {"type": "user", "message": {"content": [{"type": "tool_result"}]}},
        {"type": "assistant", "message": {"content": [
            {"type": "thinking", "thinking": "最终思考"},
            {"type": "text", "text": "这是被 CLI 吞掉的最终答案"},
        ]}},
    ]
    f = tmp_path / "s.jsonl"
    f.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in lines),
                 encoding="utf-8")
    assert last_assistant_text(f) == "这是被 CLI 吞掉的最终答案"


def test_last_assistant_text_skips_empty_and_tool_only(tmp_path: Path):
    lines = [
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "早期答案"},
        ]}},
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Bash"},
        ]}},
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "   "},
        ]}},
    ]
    f = tmp_path / "s.jsonl"
    f.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in lines),
                 encoding="utf-8")
    assert last_assistant_text(f) == "早期答案"


def test_last_assistant_text_missing_or_broken_file(tmp_path: Path):
    assert last_assistant_text(tmp_path / "nope.jsonl") == ""
    f = tmp_path / "bad.jsonl"
    f.write_text("not json\n\n" + json.dumps({"type": "assistant", "message": {}}),
                 encoding="utf-8")
    assert last_assistant_text(f) == ""


# ---------- clean_stderr ----------
def test_clean_stderr_strips_unrecognized_model_warning():
    raw = '[claude-code:unrecognized_model] {"model":"ark-code-latest"}\n真错误内容'
    out = clean_stderr(raw)
    assert "unrecognized_model" not in out
    assert "真错误内容" in out


def test_clean_stderr_keeps_catalog_error():
    raw = '"ark-code-latest" isn\'t described by this version\'s model catalog'
    assert clean_stderr(raw) == raw
