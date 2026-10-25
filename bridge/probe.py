"""探活检测纯逻辑：消息分类、尾部未响应块、first-seen 计时、判定、杀目标选择。

不 import wxauto；psutil 仅在杀树函数内延迟 import，全部可脱离微信单测。

背景：桥主循环只在 pull() 抛异常时重连；COM/UIA 调用挂死不返回时进程静默
冻结（"看着连着、其实聋了"），同进程内无解（进程级 COM 状态损坏），必须由
独立外部进程读聊天记录发现"有来无回"并杀桥换进程（2026-10-19 晨故障）。

部署形态（2026-10-21 起）：检测逻辑由 Windows 任务计划每小时拉起一个全新
短命进程跑一轮（tools/bridge_probe.py 默认单次模式），first-seen 与冷却经
export_state/_load_state 经 logs/probe_state.json 跨进程传递。常驻 pythonw
会无声蒸发、守护 bat 也会随 cmd 窗口一起死（当天三进程齐亡事故），系统任务
计划服务才是不会死的"监控者的监控者"。
"""
from __future__ import annotations

import os
import re
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
                 clock=time.monotonic,
                 state: dict | None = None) -> None:
        self.min_unanswered = min_unanswered
        self.min_age = min_age
        self.cooldown = cooldown
        self.clock = clock
        self._first_seen: dict[str, float] = {}
        self._cooldown_until = 0.0
        if state:
            self._load_state(state)

    def _load_state(self, state: dict) -> None:
        """从上一轮巡检进程导出的 state 恢复 first-seen/冷却（跨进程计时）。

        时间戳是入口注入时钟域的值（入口用 time.time 墙钟）。任何坏键/坏值
        一律忽略：state 文件损坏只退化为全新基线（可能晚一轮判聋），绝不能
        因 age 虚高误杀恢复中的桥。"""
        if not isinstance(state, dict):
            return
        raw = state.get("first_seen")
        if isinstance(raw, dict):
            for key, ts in raw.items():
                if isinstance(key, str) and isinstance(ts, (int, float)) \
                        and not isinstance(ts, bool):
                    self._first_seen[key] = float(ts)
        cooldown_until = state.get("cooldown_until", 0.0)
        if isinstance(cooldown_until, (int, float)) \
                and not isinstance(cooldown_until, bool):
            self._cooldown_until = float(cooldown_until)

    def export_state(self) -> dict:
        """导出给下一轮（每小时一次的全新进程）：first-seen 与冷却时间戳。"""
        return {
            "version": 1,
            "first_seen": dict(self._first_seen),
            "cooldown_until": self._cooldown_until,
        }

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

    def forget_first_seen(self) -> None:
        """丢弃历史 first-seen，保留冷却。巡检确认桥进程刚启动时调用：
        state 里的陈旧时间戳属于上一代桥，不能让恢复中的桥一上来就被
        跨小时 age 瞬间判死；reset 的冷却仍需继续生效。"""
        self._first_seen.clear()

    def reset(self) -> None:
        """处置（或无处置目标）后调用：清基线 + 冷却。"""
        self._first_seen.clear()
        self._cooldown_until = self.clock() + self.cooldown


def find_bridge_pids(procs: list[tuple], self_pid: int) -> list[int]:
    """从 (pid, name, cmdline) 列表选杀桥目标。

    条件：python/pythonw + cmdline 含 '-m bridge' + 不含 'bridge_probe'
    + 排除自身。探活入口是 tools\\bridge_probe.py 脚本而非 -m bridge.probe，
    两个条件天然互斥（双向防误杀）。
    """
    out: list[int] = []
    for row in procs:
        pid, name, cmdline = row[0], row[1], row[2]
        if pid == self_pid:
            continue
        if (name or "").lower() not in ("python.exe", "pythonw.exe"):
            continue
        cmd = cmdline or ""
        if "-m bridge" not in cmd or "bridge_probe" in cmd:
            continue
        out.append(pid)
    return out


# 守护 bat 宿主识别：cmdline 形态有绝对路径、带引号绝对路径、从项目根交互式
# 拉起的相对路径 'cmd /c tools\\autostart-run.bat'（不含项目目录名，
# 2026-10-21 19:39 真机漏判）。按 bat 基名命中后还要确认归属本项目：
# 绝对路径看 cmdline 里的项目目录名，相对路径看进程 cwd（scan_procs 填的
# 第 4 元素）。必须排除姊妹项目——claude-remote-web 有同名 bat 且常驻，
# 只按基名会让桥真死时永远误判 wait_bat 不恢复（当天真机并存实测）。
PROJECT_DIR_NAME = "wechat-claude-bridge"
AUTOSTART_BAT_RE = re.compile(r"autostart-(?:run|visible)\.bat",
                              re.IGNORECASE)


def find_autostart_pids(procs: list[tuple], self_pid: int) -> list[int]:
    """找桥的守护 bat 宿主 cmd：基名命中且归属本项目（路径或 cwd）。

    行形态 (pid, name, cmdline) 或 (pid, name, cmdline, cwd)。
    用于"桥进程不在"时决定谁来拉：守护循环在就等它重试，不在（30 次重试
    烧完放弃的场景，2026-10-19 实测桥死 10 小时）才由探活自己拉起。
    """
    out: list[int] = []
    for row in procs:
        pid, name, cmdline = row[0], row[1], row[2]
        cwd = row[3] if len(row) > 3 else ""
        if pid == self_pid:
            continue
        if (name or "").lower() != "cmd.exe":
            continue
        cmd = cmdline or ""
        if not AUTOSTART_BAT_RE.search(cmd):
            continue
        cwd_base = os.path.basename((cwd or "").rstrip("\\/"))
        if PROJECT_DIR_NAME in cmd or cwd_base == PROJECT_DIR_NAME:
            out.append(pid)
    return out


def find_ambiguous_autostart_pids(procs: list[tuple], self_pid: int) -> list[int]:
    """基名命中但归属未知的守护 cmd：相对路径 cmdline（不含项目目录名）且
    cwd 读不到（scan_procs 对 AccessDenied 填 None）。

    典型场景：守护 bat 由更高权限 shell 拉起、探活以普通用户跑，p.cwd()
    被拒。此时无法区分它是本项目 bat 还是姊妹项目同名 bat——保守按
    wait_bat 处理（最坏晚一轮恢复），绝不 relaunch 拉第二个 bat 造成双桥。
    cwd 能读到（空串=其他读错或旧 3 元组）时归属已知，不算 ambiguous。
    """
    out: list[int] = []
    for row in procs:
        pid, name, cmdline = row[0], row[1], row[2]
        cwd = row[3] if len(row) > 3 else ""
        if pid == self_pid:
            continue
        if (name or "").lower() != "cmd.exe":
            continue
        cmd = cmdline or ""
        if not AUTOSTART_BAT_RE.search(cmd):
            continue
        if PROJECT_DIR_NAME in cmd:
            continue                        # 绝对路径形态：归属已知
        if cwd is None:
            out.append(pid)
    return out


def recovery_action(procs: list[tuple], self_pid: int) -> str:
    """巡检入口的桥存活决策：

    - "alive"    桥进程在，继续消息无回复检测；
    - "wait_bat" 桥没了但守护 bat 还在重试（含 cwd 读不到、归属未知的
      保守分支），本轮不动作；
    - "relaunch" 桥和 bat 都没了（bat 30 次烧完/宿主 cmd 被杀），调用方拉 bat。
    """
    if find_bridge_pids(procs, self_pid):
        return "alive"
    if find_autostart_pids(procs, self_pid):
        return "wait_bat"
    if find_ambiguous_autostart_pids(procs, self_pid):
        return "wait_bat"
    return "relaunch"


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
