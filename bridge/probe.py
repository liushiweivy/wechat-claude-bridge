"""探活检测纯逻辑：消息分类、尾部未响应块、first-seen 计时、判定、杀目标选择。

不 import wxauto；psutil 仅在杀树函数内延迟 import，全部可脱离微信单测。

背景：桥主循环只在 pull() 抛异常时重连；COM/UIA 调用挂死不返回时进程静默
冻结（"看着连着、其实聋了"），同进程内无解（进程级 COM 状态损坏），必须由
独立外部进程读聊天记录发现"有来无回"并杀桥换进程（2026-10-19 晨故障）。
"""
from __future__ import annotations

import time
from dataclasses import dataclass

# 桥回复的首字符。含探活自己的 🔧 提醒与续段 ↳——否则它们落在文件助手历史里
# 会被当成"用户消息"，新桥刚拉起又被判聋，造成杀循环。
# 与 app.py 各 _safe_send 调用点保持同步。
# 注意必须剥掉变体选择符：classify 前 content 已 normalize（UIA 会丢 ️），
# 前缀里留着 ️ 会永远匹配不上（⚠️ → ⚠）。
BRIDGE_PREFIXES = tuple(p.replace("️", "").replace("︎", "") for p in (
    "🤖", "⏳", "✅", "❌", "🚫", "⏰", "⚠️", "🔄",
    "🆕", "📁", "🖥", "🔗", "🔧", "↳"))
# 本机自动通知（开机公网地址播报等），与 core.NOTIFY_PREFIXES 对齐。
IGNORE_PREFIXES = ("🌐",)
IGNORE_TYPES = frozenset({"sys", "time", "recall"})

MIN_UNANSWERED = 2              # 连续未响应用户消息条数
MIN_AGE_SECONDS = 180.0         # 最早一条的 first-seen 年龄
KILL_COOLDOWN_SECONDS = 300.0   # 处置后冷却，防 bat 拉起新桥期间连环误判
TAIL_N = 30                     # 每轮只分析窗口尾部 30 条


def normalize(text: str) -> str:
    """剥 emoji 变体选择符 + 折叠空白（UIA 会丢 ️，core.EchoFilter._norm 同款）。"""
    text = (text or "").replace("️", "").replace("︎", "")
    return " ".join(text.split())


def classify(msg_type: str, content: str) -> str:
    """→ 'bridge' | 'user' | 'ignore'。⏳ 忙回复也算响应（长任务期间不误杀）。"""
    if msg_type in IGNORE_TYPES:
        return "ignore"
    text = normalize(content)
    if not text:
        return "ignore"
    if text.startswith(IGNORE_PREFIXES):
        return "ignore"
    if text.startswith(BRIDGE_PREFIXES):
        return "bridge"
    return "user"


@dataclass(frozen=True)
class MsgView:
    msg_type: str
    content: str
    key: str = ""

    def __post_init__(self) -> None:
        # 同类型同内容消息共享 key。注意方向：共享导致 first_seen 偏早 = age
        # 偏大 = 判定更敏感（不是更迟钝！），所以时间戳只允许随"当前未响应块"
        # 存活——observe 每轮清掉不在块内的 key，被桥回复断块的旧消息不会把
        # 陈旧时间戳留给用户重发的同内容消息（2026-10-19 code review HIGH）。
        object.__setattr__(self, "key", f"{self.msg_type}|{normalize(self.content)}")


@dataclass(frozen=True)
class Verdict:
    deaf: bool
    unanswered: int = 0
    age_seconds: float = 0.0
    head_preview: str = ""        # 块首消息前 40 字，日志用


class DeafnessDetector:
    """每轮喂窗口尾部消息，判定桥是否"有来无回"。

    消息本身无可靠时间戳，用探活自己的 first-seen 时钟：首轮观察=全量历史
    基线（全部记为 now），天然兼容桥正常重启期间 <3min 的消息等待。
    """

    def __init__(self, min_unanswered: int = MIN_UNANSWERED,
                 min_age: float = MIN_AGE_SECONDS,
                 cooldown: float = KILL_COOLDOWN_SECONDS,
                 clock=time.monotonic) -> None:
        self.min_unanswered = min_unanswered
        self.min_age = min_age
        self.cooldown = cooldown
        self.clock = clock
        self._first_seen: dict[str, float] = {}
        self._cooldown_until = 0.0

    def observe(self, tail: list[MsgView]) -> Verdict:
        now = self.clock()
        if now < self._cooldown_until:
            return Verdict(deaf=False)

        # 从尾部向前收连续 USER 块。IGNORE（sys/time/recall/🌐/空）穿插不断块：
        # 时间分隔行常夹在两条用户消息之间，断了反而漏判。
        block: list[MsgView] = []
        for m in reversed(tail):
            kind = classify(m.msg_type, m.content)
            if kind == "user":
                block.insert(0, m)
            elif kind == "ignore":
                continue
            else:
                break                     # 遇到桥回复（含 ⏳/🔧/↳）即断块

        for m in block:
            self._first_seen.setdefault(m.key, now)

        # 时间戳只随当前未响应块存活：被桥回复断块的消息视为已处理，其
        # first_seen 一并清除。否则"重发同内容"会命中陈旧时间戳、age 瞬间
        # 虚高，绕过 min_age 宽限立即误杀——而桥自己正提示用户"重发最后一条
        # 指令"（断连恢复后），两条设计撞在一起必踩。块内消息每轮都会重新
        # setdefault，真实计时不丢。
        block_keys = {m.key for m in block}
        for k in [k for k in self._first_seen if k not in block_keys]:
            del self._first_seen[k]

        unanswered = len(block)
        if not block:
            return Verdict(False)
        if unanswered >= self.min_unanswered:
            age = now - self._first_seen[block[0].key]
            if age >= self.min_age:
                return Verdict(True, unanswered, age,
                               normalize(block[0].content)[:40])
        return Verdict(False, unanswered)

    def reset(self) -> None:
        """处置（或无处置目标）后调用：清基线 + 冷却。"""
        self._first_seen.clear()
        self._cooldown_until = self.clock() + self.cooldown


def find_bridge_pids(procs: list[tuple[int, str, str]],
                     self_pid: int) -> list[int]:
    """从 (pid, name, cmdline) 列表选杀桥目标。

    条件：python/pythonw + cmdline 含 '-m bridge' + 不含 'bridge_probe'
    + 排除自身。探活入口是 tools\\bridge_probe.py 脚本而非 -m bridge.probe，
    两个条件天然互斥（双向防误杀）。
    """
    out: list[int] = []
    for pid, name, cmdline in procs:
        if pid == self_pid:
            continue
        if (name or "").lower() not in ("python.exe", "pythonw.exe"):
            continue
        cmd = cmdline or ""
        if "-m bridge" not in cmd or "bridge_probe" in cmd:
            continue
        out.append(pid)
    return out


def find_autostart_pids(procs: list[tuple[int, str, str]],
                        self_pid: int) -> list[int]:
    """找桥的守护 bat 宿主 cmd：cmdline 含项目路径 + autostart。

    用于"桥进程不在"时决定谁来拉：守护循环在就等它重试，不在（30 次重试
    烧完放弃的场景，2026-10-19 实测桥死 10 小时）才由探活自己拉起。
    """
    out: list[int] = []
    for pid, name, cmdline in procs:
        if pid == self_pid:
            continue
        if (name or "").lower() != "cmd.exe":
            continue
        cmd = cmdline or ""
        if "wechat-claude-bridge" in cmd and "autostart" in cmd:
            out.append(pid)
    return out


def kill_tree(pid: int) -> None:
    """psutil 树杀（runner._kill_tree 同款）：claude 子进程在树内一并带走。"""
    import psutil
    try:
        proc = psutil.Process(pid)
        for child in proc.children(recursive=True):
            try:
                child.kill()
            except psutil.Error:
                pass
        proc.kill()
    except psutil.Error:
        pass
