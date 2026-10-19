"""配置加载与校验。"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.json"
EXAMPLE_PATH = Path(__file__).resolve().parent.parent / "config.example.json"


@dataclass(frozen=True)
class Config:
    listen_chat: str = "文件传输助手"
    workspace: str = r"E:\1workspace"
    allowed_dirs: tuple[str, ...] = (r"E:\1workspace",)
    poll_seconds: float = 2.5
    default_mode: str = "acceptEdits"          # acceptEdits 日常；/sudo 单次 bypassPermissions
    task_timeout_seconds: int = 1800
    max_msg_chars: int = 1500                  # 微信单条消息保守上限
    max_reply_chars: int = 200                 # AI 最终回复上限（提示+截断双层约束）
    claude_bin: str = "claude"
    extra_args: tuple[str, ...] = field(default_factory=tuple)

    @staticmethod
    def from_dict(data: dict) -> "Config":
        known = {f for f in Config.__dataclass_fields__}  # type: ignore[attr-defined]
        unknown = set(data) - known
        if unknown:
            raise ValueError(f"config.json 存在未知配置项: {sorted(unknown)}")
        cfg = Config(
            listen_chat=str(data.get("listen_chat", "文件传输助手")),
            workspace=str(data.get("workspace", r"E:\1workspace")),
            allowed_dirs=tuple(str(d) for d in data.get("allowed_dirs", [r"E:\1workspace"])),
            poll_seconds=float(data.get("poll_seconds", 2.5)),
            default_mode=str(data.get("default_mode", "acceptEdits")),
            task_timeout_seconds=int(data.get("task_timeout_seconds", 1800)),
            max_msg_chars=int(data.get("max_msg_chars", 1500)),
            max_reply_chars=int(data.get("max_reply_chars", 200)),
            claude_bin=str(data.get("claude_bin", "claude")),
            extra_args=tuple(str(a) for a in data.get("extra_args", [])),
        )
        cfg.validate()
        return cfg

    def validate(self) -> None:
        if self.poll_seconds < 1:
            raise ValueError("poll_seconds 不能小于 1（过于频繁会拖慢微信窗口）")
        if self.default_mode not in {"default", "acceptEdits", "plan", "bypassPermissions"}:
            raise ValueError(f"非法 default_mode: {self.default_mode}")
        if self.max_msg_chars < 200 or self.max_msg_chars > 2000:
            raise ValueError("max_msg_chars 应在 200–2000 之间")
        if self.max_reply_chars < 100 or self.max_reply_chars > 1000:
            raise ValueError("max_reply_chars 应在 100–1000 之间")
        ws = Path(self.workspace).resolve()
        for d in self.allowed_dirs:
            p = Path(d).resolve()
            if not p.is_dir():
                raise ValueError(f"allowed_dirs 目录不存在: {p}")
        if not any(ws == p or p in ws.parents for p in (Path(x).resolve() for x in self.allowed_dirs)):
            raise ValueError(f"workspace 必须位于 allowed_dirs 之内: {ws}")
        if not ws.is_dir():
            raise ValueError(f"workspace 目录不存在: {ws}")


def load_config() -> Config:
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(
            f"未找到 {CONFIG_PATH}，请复制 config.example.json 为 config.json 后按需修改"
        )
    data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    return Config.from_dict(data)
