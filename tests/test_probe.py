"""探活纯逻辑测试：分类、tail 未响应块、first-seen 计时、判定、目标选择。"""
from types import SimpleNamespace

import pytest

from bridge.probe import (BRIDGE_PREFIXES, DeafnessDetector, MsgView,
                          classify, find_ambiguous_autostart_pids,
                          find_autostart_pids, find_bridge_pids, normalize,
                          recovery_action)


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


# ---------- 跨进程状态持久化（每小时短命巡检之间共享 first-seen） ----------

def test_state_roundtrip_keeps_first_seen():
    clock = FakeClock(10_000.0)
    d1 = _detector(clock)
    tail = [msg("任务A"), msg("任务B")]
    assert not d1.observe(tail).deaf          # 首轮只登记基线
    state = d1.export_state()
    assert set(state["first_seen"].keys()) == {m.key for m in tail}
    assert state["cooldown_until"] == 0.0

    # 一小时后（任务计划拉起的全新 Python 进程）载入旧 state，同块仍无回复 → 判聋
    clock.advance(3600)
    d2 = _detector(clock, state=state)
    v = d2.observe(tail)
    assert v.deaf and v.unanswered == 2


def test_exported_state_drops_keys_outside_current_block():
    """被桥回复断块的旧消息不带进新进程：否则用户按桥提示重发同文会命中
    陈旧时间戳、age 虚高、绕过 180s 宽限误杀恢复中的桥（同 HIGH 回归）。"""
    d1 = _detector(FakeClock())
    d1.observe([msg("任务A")])
    assert not d1.observe([msg("任务A"), msg("✅ 完成")]).deaf
    assert d1.export_state()["first_seen"] == {}


def test_cooldown_persists_across_instances():
    """reset 后的 300s 冷却也跨进程：否则新进程首轮就把刚重启的桥误判。"""
    clock = FakeClock()
    d1 = _detector(clock)
    tail = [msg("任务A"), msg("任务B")]
    d1.observe(tail)
    clock.advance(200)
    assert d1.observe(tail).deaf
    d1.reset()

    d2 = _detector(clock, state=d1.export_state())
    assert not d2.observe(tail).deaf          # 冷却期内不判
    clock.advance(300)
    assert not d2.observe(tail).deaf          # 冷却结束、基线已清，重新累计
    clock.advance(180)
    assert d2.observe(tail).deaf


def test_forget_first_seen_clears_history_but_keeps_cooldown():
    """桥进程确认刚启动时：state 里的陈旧 first-seen 属于上一代桥，必须丢弃；
    但冷却时间戳保留（reset 后的冷却仍要跨进程生效）。"""
    clock = FakeClock()
    d1 = _detector(clock)
    d1.observe([msg("任务A"), msg("任务B")])
    clock.advance(200)
    d1.observe([msg("任务A"), msg("任务B")])
    d1.reset()
    cooldown_until = d1.export_state()["cooldown_until"]

    clock.advance(301)                               # 先走出冷却期
    d2 = _detector(clock, state=d1.export_state())
    d2.observe([msg("任务A"), msg("任务B")])
    assert d2.export_state()["first_seen"]          # 又登记了新块
    d2.forget_first_seen()
    state = d2.export_state()
    assert state["first_seen"] == {}
    assert state["cooldown_until"] == cooldown_until


@pytest.mark.parametrize("bad", [
    "x", 42, ["no"], {"first_seen": "x"},
    {"first_seen": {"k": "late"}}, {"first_seen": {123: 1.0}},
    {"cooldown_until": "soon"}, {"first_seen": None, "cooldown_until": None},
])
def test_broken_state_falls_back_to_fresh_baseline(bad):
    d = _detector(FakeClock(), state=bad)
    state = d.export_state()
    assert state["first_seen"] == {}
    assert state["cooldown_until"] == 0.0


def test_missing_and_empty_state_is_fresh_baseline():
    assert _detector(FakeClock()).export_state()["first_seen"] == {}
    assert _detector(FakeClock(), state={}).export_state()["first_seen"] == {}


# ---------- 进程巡检决策（桥 / bat 存活组合） ----------

def test_recovery_action_alive_when_bridge_running():
    assert recovery_action(PROCS, self_pid=100) == "alive"


def test_recovery_waits_when_bat_alive_but_bridge_missing():
    procs = [p for p in PROCS if p[0] != 200]   # 桥没了，bat 还在重试
    assert recovery_action(procs, self_pid=100) == "wait_bat"


def test_recovery_relaunches_when_bridge_and_bat_both_gone():
    procs = [p for p in PROCS if p[0] not in (200, 400)]
    assert recovery_action(procs, self_pid=100) == "relaunch"


def test_recovery_waits_when_bat_match_has_unreadable_cwd():
    """相对路径守护 bat 由更高权限 shell 拉起、探活读不到 cwd(None)：
    基名命中但归属未知。保守 wait_bat（最坏晚一轮恢复），绝不 relaunch
    拉第二个 bat 造成双桥抢会话（2026-10-21 二轮复审 MEDIUM）。"""
    ambiguous = [(700, "cmd.exe", r"cmd /c tools\autostart-run.bat", None)]
    assert recovery_action(ambiguous, self_pid=100) == "wait_bat"
    assert find_autostart_pids(ambiguous, self_pid=100) == []
    assert find_ambiguous_autostart_pids(ambiguous, self_pid=100) == [700]


def test_ambiguous_empty_when_sibling_cwd_readable():
    """cwd 能读到且是姊妹项目：归属已知不是我们，不算 ambiguous，
    桥真死时必须正常 relaunch（不能被姊妹 bat 永久挡住）。"""
    procs = [(701, "cmd.exe", r"cmd /c tools\autostart-run.bat",
              r"E:\1workspace\claude-remote-web")]
    assert find_ambiguous_autostart_pids(procs, self_pid=100) == []
    assert find_autostart_pids(procs, self_pid=100) == []
    assert recovery_action(procs, self_pid=100) == "relaunch"


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


def test_find_autostart_pids_matches_relative_launch_path():
    """从项目根交互式拉起时 cmdline 是相对路径 'cmd /c tools\\autostart-run.bat'
    （2026-10-21 19:39 真机形态）：不含项目目录名，旧匹配器漏判，桥一旦死掉
    探活会误走 relaunch 拉第二个 bat → 双桥抢同一会话。相对形态靠第 4 元素
    cwd 归属项目根来识别（scan_procs 用 psutil 填 cwd）。"""
    procs = [
        (100, "pythonw.exe", r"C:\Python\pythonw.exe tools\bridge_probe.py"),
        (700, "cmd.exe", r"C:\Windows\system32\cmd.exe /c tools\autostart-run.bat",
         r"E:\1workspace\wechat-claude-bridge"),
        (500, "cmd.exe", r"cmd /c unrelated.bat", r"E:\1workspace"),
    ]
    assert find_autostart_pids(procs, self_pid=100) == [700]


def test_find_autostart_pids_ignores_sibling_project_same_bat_name():
    """claude-remote-web 也有同名 tools\\autostart-run.bat（常驻服务，
    2026-10-21 真机并存）：只按基名会把它认成桥守护，桥真死时永远 wait_bat
    不恢复。绝对路径看路径、相对路径看 cwd，两种形态都必须排除姊妹项目。"""
    procs = [
        (100, "pythonw.exe", r"pythonw.exe tools\bridge_probe.py"),
        (701, "cmd.exe",
         r"cmd /c E:\1workspace\claude-remote-web\tools\autostart-run.bat"),
        (702, "cmd.exe", r"cmd /c tools\autostart-run.bat",
         r"E:\1workspace\claude-remote-web"),
    ]
    assert find_autostart_pids(procs, self_pid=100) == []


def test_find_autostart_pids_matches_quoted_absolute_run_bat():
    procs = [
        (100, "pythonw.exe", r"pythonw.exe tools\bridge_probe.py"),
        (800, "cmd.exe",
         r'cmd /c "E:\1workspace\wechat-claude-bridge\tools\autostart-run.bat"'),
    ]
    assert find_autostart_pids(procs, self_pid=100) == [800]
