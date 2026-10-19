"""wxauto 薄封装：所有 wxauto 依赖都关在这里，便于打桩测试与断网开发。"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from .core import EchoFilter, split_wechat

log = logging.getLogger("bridge")


@dataclass
class Incoming:
    content: str
    msg_type: str          # self / friend / sys / time / recall ...
    sender: str


class WxGateway:
    def __init__(self, listen_chat: str, max_msg_chars: int = 1500,
                 send_pause: float = 1.2) -> None:
        self.listen_chat = listen_chat
        self.max_msg_chars = max_msg_chars
        self.send_pause = send_pause     # 多条分段发送之间的间隔，避免微信限频
        self._wx = None
        self.echo = EchoFilter()

    def connect(self) -> str:
        """初始化并绑定监听会话；返回登录账号昵称。失败抛 RuntimeError。"""
        try:
            from wxauto import WeChat
        except ImportError as exc:
            raise RuntimeError("未安装 wxauto（请用项目 .venv 运行）") from exc

        try:
            self._wx = WeChat()
        except Exception as exc:
            raise RuntimeError(
                "无法连接 PC 微信，请确认：微信 3.9.x 已启动并完成登录"
            ) from exc

        try:
            self._wx.AddListenChat(self.listen_chat)
        except Exception as exc:
            raise RuntimeError(f"无法监听会话「{self.listen_chat}」: {exc}") from exc
        nickname = getattr(self._wx, "nickname", "")
        return nickname

    def reconnect(self) -> str:
        self._wx = None
        return self.connect()

    def pull(self) -> list[Incoming]:
        """取监听会话的新消息；wxauto 异常向上抛（由主循环决定重连）。"""
        if self._wx is None:
            raise RuntimeError("gateway 未连接")
        raw = self._wx.GetListenMessage(who=self.listen_chat)
        out: list[Incoming] = []
        # who 指定时返回 list；不指定时返回 {chat: [...]}
        messages = raw if isinstance(raw, list) else []
        if isinstance(raw, dict):
            messages = [m for lst in raw.values() for m in lst]
        log.debug("raw pull: %d 条 [%s]", len(messages),
                  " | ".join(f"{getattr(m, 'type', '?')}:{(getattr(m, 'content', '') or '')[:40]!r}"
                             for m in messages))
        for m in messages:
            content = getattr(m, "content", "") or ""
            if not content.strip():
                continue
            if self.echo.is_echo(content):
                log.debug("忽略自己发出的回声消息: %.40r", content)
                continue
            out.append(Incoming(
                content=content.strip(),
                msg_type=getattr(m, "type", "unknown"),
                sender=str(getattr(m, "sender", "")),
            ))
        return out

    def send(self, text: str) -> None:
        """分段发送；每段都记入回环过滤器。

        第 2 段起加 ↳ 续段前缀：文件传输助手里无法靠 sender 区分桥与用户，
        探活（bridge/probe.py）按首字符归类消息，裸续段会被误判成用户消息。
        mark_sent 记录加前缀后的实际发送内容，回声一致性天然保持。"""
        if self._wx is None:
            raise RuntimeError("gateway 未连接")
        pieces = split_wechat(text, self.max_msg_chars)
        if len(pieces) > 1:
            pieces = [pieces[0]] + ["↳ " + p for p in pieces[1:]]
        for piece in pieces:
            self.echo.mark_sent(piece)
            self._wx.SendMsg(piece, who=self.listen_chat)
            if len(pieces) > 1:
                time.sleep(self.send_pause)
