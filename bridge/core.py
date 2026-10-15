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
/help　再次显示本帮助"""

COMMANDS = {"help", "new", "cd", "sudo", "cancel", "status"}


@dataclass
class Instruction:
    kind: str                 # task | help | new | cd | cancel | status | unknown
    text: str = ""            # task: 提示词；cd: 目标子目录
    sudo: bool = False


def parse_instruction(raw: str) -> Instruction:
    """解析微信来的一条消息。"""
    text = (raw or "").strip()
    if not text:
        return Instruction("unknown")
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
    if cmd in {"help", "new", "cancel", "status"}:
        return Instruction(cmd)
    return Instruction("unknown", text=f"未知命令 /{cmd}，发送 /help 查看用法")


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
        # UIA 回报会把 \n 变成 \r\n、偶尔插入空行；折叠所有空白后再比
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
            if exact or prefix:
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
