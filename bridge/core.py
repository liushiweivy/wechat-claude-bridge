"""纯逻辑：命令解析、长文分段、防回环、claude -p 参数组装与 stream-json 解析。

本文件不 import wxauto / subprocess，全部可脱离微信单测。
"""
from __future__ import annotations

import json
import re
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

HELP_TEXT = """\
🤖 本地 Claude 桥已连接
普通发消息 = 派任务（当前工作目录执行）
/new　重新开始一个独立对话
/cd　查看当前工作目录
/cd 目录名　切换到工作区下的子目录
/sudo 任务　以完全权限执行（可运行命令，谨慎）
/cancel　取消正在执行的任务
/status　查看桥状态
/open　打开电脑上的对应对话窗口（之后任务打进窗口执行）
/help　再次显示本帮助"""

COMMANDS = {"help", "new", "cd", "sudo", "cancel", "status", "open", "win"}

# 以这些前缀开头的消息是本机自动通知（如开机公网地址播报），不是用户指令：
# 桥直接忽略，不回复、不派任务。发送方见 claude-remote-web/tools/notify_public_url.py。
NOTIFY_PREFIXES = ("🌐",)


@dataclass
class Instruction:
    kind: str                 # task | help | new | cd | cancel | status | ignore | unknown
    text: str = ""            # task: 提示词；cd: 目标子目录
    sudo: bool = False


def parse_instruction(raw: str) -> Instruction:
    """解析微信来的一条消息。"""
    text = (raw or "").strip()
    if not text:
        return Instruction("unknown")
    if text.startswith(NOTIFY_PREFIXES):
        return Instruction("ignore")
    if not text.startswith("/"):
        return Instruction("task", text=text)

    head, _, arg = text.partition(" ")
    cmd = head[1:].lower()
    arg = arg.strip()
    if cmd == "sudo":
        if not arg:
            return Instruction("unknown", text="/sudo 后需要任务内容")
        return Instruction("task", text=arg, sudo=True)
    if cmd == "cd":
        return Instruction("cd", text=arg)
    if cmd in {"open", "win"}:
        return Instruction("open")
    if cmd in {"help", "new", "cancel", "status"}:
        return Instruction(cmd)
    return Instruction("unknown", text=f"未知命令 /{cmd}，发送 /help 查看用法")


# ---------- 回复长度约束 ----------
# 双层约束：提示层让模型自己写短（保留关键信息），截断层在模型没遵守时硬切兜底。
REPLY_HINT_TEMPLATE = (
    "\n\n【回复格式要求】你的最终回复会直接转发到手机微信，必须控制在 {limit} 字以内："
    "只保留关键信息（结论、成功或失败、必要的文件路径），不写执行过程，"
    "不用 Markdown 标题和代码块。"
)


def with_reply_hint(prompt: str, limit: int = 200) -> str:
    """给任务提示词追加"最终回复 ≤limit 字"的格式要求。"""
    return prompt.rstrip() + REPLY_HINT_TEMPLATE.format(limit=limit)


def cap_reply(text: str, limit: int = 200) -> str:
    """模型没遵守长度要求时的兜底截断：优先句子边界，否则硬切，末尾加省略号。"""
    text = text.strip()
    if len(text) <= limit:
        return text
    window = text[:limit]
    cut = max(window.rfind(p) for p in ("。", "！", "？", "\n", "；",
                                        ". ", "! ", "? "))
    if cut < limit // 2:
        cut = limit
    return window[:cut].rstrip() + "…"


def split_wechat(text: str, limit: int = 1500) -> list[str]:
    """把长结果切成微信可发送的片段：优先段落边界，其次句子，最后硬切。

    片段尾部加 (n/N) 页码（多于一片时）。
    """
    text = text.strip()
    if len(text) <= limit:
        return [text]

    # 先按段落贪心装箱
    chunks: list[str] = []
    buf = ""
    for para in text.split("\n"):
        unit = para + "\n"
        if len(unit) > limit:
            if buf:
                chunks.append(buf.rstrip())
                buf = ""
            chunks.extend(_hard_split(unit.rstrip("\n"), limit))
            buf = ""
            continue
        if len(buf) + len(unit) > limit:
            chunks.append(buf.rstrip())
            buf = unit
        else:
            buf += unit
    if buf.strip():
        chunks.append(buf.rstrip())

    total = len(chunks)
    return [f"{c}\n（{i + 1}/{total}）" for i, c in enumerate(chunks)]


def _hard_split(text: str, limit: int) -> list[str]:
    """超过单帧上限的段落：优先在句号/分号处断，仍不行再硬切。"""
    out: list[str] = []
    rest = text
    while len(rest) > limit:
        window = rest[:limit]
        cut = max(window.rfind("。"), window.rfind("；"), window.rfind("！"),
                  window.rfind("？"), window.rfind(". "), window.rfind("\n"))
        if cut < limit * 0.5:
            cut = limit
        out.append(rest[:cut].strip())
        rest = rest[cut:].strip()
    if rest:
        out.append(rest)
    return out


@dataclass
class EchoFilter:
    """识别桥自己发出的消息，防止回环把回复当成新指令。

    文件传输助手里所有消息 sender 都是自己，无法靠发送者区分，只能按内容匹配。
    长结果会被切成很多段逐段发送（每段秒级间隔），从第 1 段 mark_sent 到主循环
    重新 pull 可能超过 15s，因此精确匹配给宽窗；匹配命中后立即把记录消费掉，
    避免误吞用户稍后主动发出的同文消息。
    """
    # 前缀兜底：UIA 回报多行消息实测有 57s 延迟，窗口给到 120s
    window_seconds: float = 120.0
    exact_window_seconds: float = 300.0   # 精确回声：覆盖多段长发送全程
    max_records: int = 100
    _sent: deque = field(default_factory=deque, repr=False)

    def __post_init__(self) -> None:
        self._sent = deque(maxlen=self.max_records)

    @staticmethod
    def _norm(text: str) -> str:
        # UIA 回报会把 \n 变成 \r\n、偶尔插入空行；折叠所有空白后再比。
        # 还会丢 emoji 变体选择符（🖥️ → 🖥），两边统一剥掉再比（2026-10-18
        # /open 的 🖥️ 消息因此漏判成任务）。
        text = text.replace("️", "").replace("︎", "")
        return " ".join(text.split())

    def mark_sent(self, text: str) -> None:
        self._sent.append((text.strip(), time.time()))

    def is_echo(self, content: str) -> bool:
        content = self._norm(content.strip())
        now = time.time()
        for i, (text, ts) in enumerate(self._sent):
            age = now - ts
            sent = self._norm(text)
            exact = content == sent and age <= self.exact_window_seconds
            prefix = age <= self.window_seconds and content.startswith(sent[:80])
            # UIA 偶尔把桥连续发的多条合成一条回报（2026-10-18 /open 三连消息
            # 合成一条被当成任务）：已发消息作为子串出现在其中也算回声。
            merged = (age <= self.window_seconds and len(sent) >= 12
                      and sent in content)
            if exact or prefix or merged:
                del self._sent[i]         # 消费即弹出
                return True
        return False


def resolve_cd(sub: str, current: Path, allowed_dirs: list[str]) -> Path:
    """把 /cd 子目录解析为 allowed_dirs 之内的真实目录，越界则拒绝。

    支持：子目录名/多级相对路径；/cd .. 回到根；绝对路径只接受白名单内。
    """
    roots = [Path(d).resolve() for d in allowed_dirs]
    if not sub:
        return current
    if sub in {"..", "/", "~"}:
        return roots[0]
    candidate = Path(sub)
    target = candidate.resolve() if candidate.is_absolute() else (current / candidate).resolve()
    if not any(target == r or r in target.parents for r in roots):
        raise ValueError(f"目录 {target} 不在允许范围内")
    if not target.is_dir():
        raise ValueError(f"目录不存在: {target}")
    return target


def build_claude_args(
    prompt: str,
    mode: str,
    resume_id: str | None = None,
    extra_args: tuple[str, ...] = (),
) -> list[str]:
    """组装 claude -p 命令参数（不含可执行文件本身）。"""
    # stream-json 在 -p 模式下必须带 --verbose，否则 CLI 直接报错退出
    args = ["-p", prompt, "--verbose", "--output-format", "stream-json",
            "--permission-mode", mode]
    if resume_id:
        args += ["--resume", resume_id]
    args += list(extra_args)
    return args


@dataclass
class StreamEvent:
    kind: str                 # session | tool | text | result | error
    session_id: str = ""
    text: str = ""
    tool: str = ""
    is_error: bool = False


def parse_stream_line(line: str) -> StreamEvent | None:
    """解析一行 stream-json。无法识别返回 None。"""
    line = line.strip()
    if not line:
        return None
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return None

    t = obj.get("type")
    if t == "system":
        sid = obj.get("session_id") or ""
        return StreamEvent("session", session_id=sid) if sid else None
    if t == "assistant":
        for block in (obj.get("message") or {}).get("content", []):
            if block.get("type") == "tool_use":
                return StreamEvent("tool", tool=str(block.get("name", "?")))
        return None
    if t == "result":
        return StreamEvent(
            "result",
            text=obj.get("result") or "",
            is_error=obj.get("subtype") not in (None, "success"),
        )
    return None


# ---------- transcript 兜底 ----------
# CLI 在第三方网关（如火山 Ark）下偶发：答案完整落盘但不发 stream-json 的
# result 事件（收尾期模型目录校验拒绝未登记模型，见 2026-10-15 故障）。
# transcript jsonl 仍在，可从那里把最终答案取回来。

def transcript_path(cwd: str | Path, session_id: str,
                    home: Path | None = None) -> Path:
    """cwd → ~/.claude/projects/<slug>/<sid>.jsonl（slug 规则同 CC 本体）。"""
    base = Path.home() if home is None else home
    slug = re.sub(r"[^A-Za-z0-9]", "-", str(Path(cwd).resolve()))
    return base / ".claude" / "projects" / slug / f"{session_id}.jsonl"


def find_newer_session(cwd: str | Path, since_ts: float,
                       home: Path | None = None,
                       exclude: frozenset[str] = frozenset()) -> str:
    """找 cwd 对应 projects 目录里**创建时间**晚于 since_ts 的最新 transcript。

    /open 开了新 CC 窗口后，用户在窗口里发出第一条消息时 CLI 才落盘 jsonl；
    桥靠这个函数把那个新会话认领成自己的 session_id。找不到返回 ""。

    必须用创建时间（Windows 上 st_ctime）而非 mtime：老会话被别的窗口
    碰一下 mtime 就更新，会被误认领（2026-10-18 实测 759d5e12 被误领）。
    exclude：桥自己任务产生的 session_id 必须排除，否则 /open 后的头一条
    微信任务会被误认成"窗口会话"（2026-10-18 实测认领错）。"""
    directory = transcript_path(cwd, "placeholder", home=home).parent
    best, best_ctime = "", since_ts
    try:
        files = list(directory.glob("*.jsonl"))
    except OSError:
        return ""
    for f in files:
        if f.stem in exclude:
            continue
        try:
            ctime = f.stat().st_ctime
        except OSError:
            continue
        if ctime > best_ctime:
            best, best_ctime = f.stem, ctime
    return best


def last_assistant_text(path: Path) -> str:
    """读 transcript，返回最后一条含非空 text 块的 assistant 消息文本。

    从文件尾部向前找：跳过纯工具轮和空文本轮；文件缺失/损坏返回 ""。
    """
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    for line in reversed(lines):
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if obj.get("type") != "assistant":
            continue
        blocks = (obj.get("message") or {}).get("content") or []
        texts = [b.get("text", "") for b in blocks
                 if isinstance(b, dict) and b.get("type") == "text"
                 and str(b.get("text", "")).strip()]
        if texts:
            return "\n".join(texts).strip()
    return ""


# ---------- stderr 清洗 ----------
# 第三方模型每次调用都打这条良性警告，混进失败文案里只会误导用户。
_BENIGN_STDERR_TAGS = ("[claude-code:unrecognized_model]",)

RECOVERY_NOTICE = "⚠️ CLI 未正常返回 result 事件，本答案从本地 transcript 兜底恢复"


def clean_stderr(stderr: str) -> str:
    """剥掉每条任务都会出现的良性模型警告，留下真正的错误。"""
    kept = [ln for ln in (stderr or "").splitlines()
            if not any(tag in ln for tag in _BENIGN_STDERR_TAGS)]
    return "\n".join(kept).strip()


# ---------- 子进程环境 ----------
def clean_child_env() -> dict[str, str]:
    """剥掉所有 CLAUDE* 环境变量的子进程环境。

    桥若是从某个 claude 会话终端里（间接）启动的，会继承
    CLAUDE_CODE_CHILD_SESSION=1 等标记；带着它起 claude（窗口或 -p）
    会被当成子会话：窗口屏幕警告「Transcript saving is off — inherited
    CLAUDE_CODE_CHILD_SESSION marker」，不写 transcript、不注册会话，
    认领/取结果全断（2026-10-18 窗口 2188 实测定位）。"""
    import os
    return {k: v for k, v in os.environ.items()
            if not k.upper().startswith("CLAUDE")}
