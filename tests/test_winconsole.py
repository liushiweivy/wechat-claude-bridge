"""winconsole 键事件构造与屏幕文本解析（纯数据，不触 AttachConsole）。"""
from bridge.winconsole import (KEY_EVENT, VK_ESCAPE, VK_RETURN,
                               build_escape_events, build_submit_events,
                               build_text_events, done_count,
                               extract_last_answer, find_session_id)


def _chars(events):
    return [(e.Event.KeyEvent.bKeyDown, e.Event.KeyEvent.UnicodeChar,
             e.Event.KeyEvent.wVirtualKeyCode) for e in events]


def test_text_events_pairs_and_newline_flattening():
    events = build_text_events("A\nB")
    assert len(events) == 6                      # 3 字符 × keydown/keyup
    chars = _chars(events)
    assert chars[0] == (True, "A", 0)
    assert chars[1] == (False, "A", 0)
    assert chars[2][1] == " "                    # \n 压成空格，防 TUI 提前提交
    assert all(e.EventType == KEY_EVENT for e in events)


def test_text_events_chinese():
    events = build_text_events("你好")
    assert [c for _, c, _ in _chars(events)] == ["你", "你", "好", "好"]


def test_submit_and_escape_events():
    sub = _chars(build_submit_events())
    assert sub == [(True, "\r", VK_RETURN), (False, "\r", VK_RETURN)]
    esc = _chars(build_escape_events())
    assert esc == [(True, "\x1b", VK_ESCAPE), (False, "\x1b", VK_ESCAPE)]


# ---- 屏幕文本解析 ----

def test_done_count():
    screen = ("✳ 思考 for 1s · done 13:00\n"
              "✳ 完成 for 3s · done 13:05\n"
              "✳ Brewing... (esc to interrupt · 12s)")   # 进行中的不算
    assert done_count(screen) == 2
    assert done_count("没有任何标记") == 0


def test_extract_last_answer_picks_last_turn():
    screen = "\n".join([
        "> 问题一",
        "● 第一个答案",
        "✳ 思考 for 2s · done 13:00",
        "> 问题二",
        "● 最终答案",
        "  答案续行",
        "✳ 完成 for 3s · done 13:05",
        "❯ ",
    ])
    assert extract_last_answer(screen) == "最终答案\n答案续行"


def test_extract_last_answer_stops_at_separator():
    screen = "● 答案正文\n────────────────────\n❯ "
    assert extract_last_answer(screen) == "答案正文"


def test_extract_last_answer_empty_without_marker():
    assert extract_last_answer("✻ Claude Code v2.1.283\n❯ ") == ""


def test_extract_last_answer_with_padded_scrape_rows():
    """真实抓屏：窗口改窄后旧宽渲染残留，行首带 100+ 空格、
    完成标记是 ✻（v2.1.283），完成行还可能折行。"""
    pad = " " * 100
    screen = "\n".join([
        "❯ 只回复两个字：在线",
        pad + "● 在线",
        pad + "✻ Churned f",
        "or 9s · done 14:34",
        "❯ 请只回复六个字：窗口任务成功了",
        pad + "● 窗口任务成功了",
        pad + "✻ Sautéed for 7s · done 14:45",
        "─" * 60,
        "❯ ",
    ])
    assert extract_last_answer(screen) == "窗口任务成功了"


def test_find_session_id():
    screen = ("\n".join([
        "Claude Code Status",
        " Session ID: 5a789ed1-c814-451b-a2e9-48c22fc281b7",
        " Model: ark-code-latest",
    ]))
    assert find_session_id(screen) == "5a789ed1-c814-451b-a2e9-48c22fc281b7"
    assert find_session_id("没有会话行") == ""
