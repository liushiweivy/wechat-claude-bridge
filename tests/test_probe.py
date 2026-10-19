"""探活纯逻辑测试：分类、tail 未响应块、first-seen 计时、判定、目标选择。"""
from types import SimpleNamespace

import pytest

from bridge.probe import (BRIDGE_PREFIXES, DeafnessDetector, MsgView,
                          classify, find_autostart_pids, find_bridge_pids,
                          normalize)


def msg(content: str, msg_type: str = "self") -> MsgView:
    return MsgView(msg_type=msg_type, content=content)


class FakeClock:
    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _detector(clock: FakeClock, **kw) -> DeafnessDetector:
    return DeafnessDetector(clock=clock, **kw)


# ---------- normalize / classify ----------

def test_normalize_strips_variation_selector_and_whitespace():
    assert normalize("  🖥️ 已打开\n窗口 ") == "🖥 已打开 窗口"


@pytest.mark.parametrize("prefix", BRIDGE_PREFIXES)
def test_classify_all_bridge_prefixes(prefix):
    assert classify("self", f"{prefix} 某某回复") == "bridge"


def test_classify_ignore_notify_and_nonchat_types():
    assert classify("self", "🌐 会话台公网地址\nhttp://x") == "ignore"
    assert classify("sys", "anything") == "ignore"
    assert classify("time", "上午 10:00") == "ignore"
    assert classify("recall", "撤回了一条消息") == "ignore"
    assert classify("self", "   ") == "ignore"


def test_classify_user_messages():
    assert classify("self", "帮我看看项目结构") == "user"
    assert classify("self", "/status") == "user"
    assert classify("friend", "你好") == "user"


def test_classify_variation_selector_emoji_still_bridge():
    # UIA 回报会丢 ️：带选择符的 🖥️ 也必须分类为 bridge
    assert classify("self", "🖥️ 已打开窗口") == "bridge"


# ---------- tail 未响应块 ----------

def test_busy_reply_counts_as_response():
    d = _detector(FakeClock())
    v = d.observe([msg("任务A"), msg("⏳ 已开工【窗口】")])
    assert not v.deaf and v.unanswered == 0


def test_ignore_rows_do_not_break_block():
    d = _detector(FakeClock())
    v = d.observe([msg("任务A"), msg("上午 10:00", "time"),
                   msg("🌐 自动通知"), msg("任务B")])
    assert v.unanswered == 2


def test_continuation_piece_counts_as_bridge():
    d = _detector(FakeClock())
    v = d.observe([msg("任务A"), msg("✅ 完成\n（1/2）"),
                   msg("↳ 后续内容\n（2/2）")])
    assert v.unanswered == 0


# ---------- first-seen 计时与阈值 ----------

def test_two_unanswered_trigger_after_min_age():
    clock = FakeClock()
    d = _detector(clock)
    tail = [msg("任务A"), msg("任务B")]
    assert not d.observe(tail).deaf          # 首轮登记基线
    clock.advance(179)
    assert not d.observe(tail).deaf
    clock.advance(1)
    v = d.observe(tail)
    assert v.deaf and v.unanswered == 2 and v.age_seconds >= 180
    assert v.head_preview == "任务A"


def test_single_unanswered_never_triggers():
    clock = FakeClock()
    d = _detector(clock)
    tail = [msg("任务A")]
    assert not d.observe(tail).deaf
    clock.advance(3600)
    assert not d.observe(tail).deaf


def test_startup_baseline_does_not_trigger_on_history():
    # 探活启动时历史里已有 4 条未回复消息：首轮全部记为 now，不立即触发
    d = _detector(FakeClock())
    tail = [msg(f"任务{i}") for i in range(4)]
    assert not d.observe(tail).deaf


def test_bridge_reply_resets_block():
    clock = FakeClock()
    d = _detector(clock)
    tail = [msg("任务A"), msg("任务B")]
    d.observe(tail)
    clock.advance(179)
    # 桥在 179s 时回复了：块被断，之后即使再挂 3 分钟也不应触发
    assert not d.observe(tail + [msg("✅ 完成")]).deaf
    clock.advance(300)
    assert not d.observe(tail + [msg("✅ 完成")]).deaf


def test_resent_same_content_after_reply_gets_fresh_clock():
    """HIGH 回归（2026-10-19 review）：被桥回复过的消息，其 first_seen 必须
    随块清除，不能留给用户重发的同内容消息。桥断连恢复后自己提示"重发最后
    一条指令"——重发一字不差的内容若命中陈旧时间戳，age 瞬间虚高，180s
    宽限被绕过，恢复中的桥被立即误杀。"""
    clock = FakeClock()
    d = _detector(clock)
    # 10:00 用户发"任务A"，桥还没回：单独在块里，登记 first_seen
    assert not d.observe([msg("任务A")]).deaf
    # 桥回复了：块断开，"任务A" 的时间戳应被清除
    assert not d.observe([msg("任务A"), msg("✅ 完成")]).deaf
    # 10:10（600s 后）用户按桥提示重发同内容 + 追问：必须按全新计时
    clock.advance(600)
    tail = [msg("任务A"), msg("✅ 完成"), msg("任务A"), msg("在吗")]
    assert not d.observe(tail).deaf
    # 但新块自身满 180s 后仍要判聋（真实计时不丢）
    clock.advance(180)
    assert d.observe(tail).deaf


def test_cooldown_after_reset():
    clock = FakeClock()
    d = _detector(clock)
    tail = [msg("任务A"), msg("任务B")]
    d.observe(tail)
    clock.advance(200)
    assert d.observe(tail).deaf
    d.reset()                               # 处置后冷却
    assert not d.observe(tail).deaf         # 同样的 tail 立即重喂不触发
    clock.advance(299)
    assert not d.observe(tail).deaf
    clock.advance(1)
    assert not d.observe(tail).deaf         # 冷却结束但基线已清，重新计 180s
    clock.advance(180)
    assert d.observe(tail).deaf


# ---------- 杀目标选择 ----------

PROCS = [
    (100, "pythonw.exe", r"C:\Python\pythonw.exe tools\bridge_probe.py"),
    (200, "python.exe", r"E:\1workspace\wechat-claude-bridge\.venv\Scripts\python.exe -m bridge"),
    (300, "python.exe", r"C:\Python\python.exe other_project.py"),
    (400, "cmd.exe", r"cmd /k E:\1workspace\wechat-claude-bridge\tools\autostart-visible.bat"),
    (500, "cmd.exe", r"cmd /c unrelated.bat"),
]


def test_find_bridge_pids_selects_only_bridge():
    assert find_bridge_pids(PROCS, self_pid=100) == [200]


def test_find_bridge_pids_excludes_self_pid():
    procs = PROCS + [(600, "python.exe", "python.exe -m bridge")]
    assert find_bridge_pids(procs, self_pid=600) == [200]


def test_find_autostart_pids_selects_only_bridge_bat():
    assert find_autostart_pids(PROCS, self_pid=100) == [400]
