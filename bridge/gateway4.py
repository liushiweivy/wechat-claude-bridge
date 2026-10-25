# -*- coding: utf-8 -*-
"""wxauto4（微信 4.x）后端：与 gateway.WxGateway 同接口的 V4 变体。

为什么单独一个文件而不是改 gateway.py：
- 桥上所有在用流程（3.9）继续走 gateway.py，一行不动；
- 4.x 的 wxauto4 是**推模式**（AddListenChat 必须给回调、会弹出独立子窗口），
  和 3.9 wxauto 的拉模式（GetListenMessage 轮询）模型不同，硬塞进一个类
  只会让两边都别扭。这里用「回调→线程安全队列」把推模式适配回桥上
  主循环的 pull() 轮询语义，接口与 WxGateway 完全一致，由 config.json 的
  gateway_backend=v4 切换。

wxauto4 来源：原作者 cluic 的仓库已停更（README 只剩"停止更新"），
本桥以 fly000001/wxauto4 的可用源码为准（本地参考副本在 E:\\1workspace\\wxauto4）。
"""
from __future__ import annotations

import logging
import queue
import time
from typing import Any

from .core import EchoFilter, split_wechat
from .gateway import Incoming

log = logging.getLogger("bridge.v4")


class WxGatewayV4:
    """与 WxGateway 相同接口：connect / reconnect / pull / send。"""

    def __init__(self, listen_chat: str, max_msg_chars: int = 1500,
                 send_pause: float = 1.2) -> None:
        self.listen_chat = listen_chat
        self.max_msg_chars = max_msg_chars
        self.send_pause = send_pause
        self._wx: Any = None
        self._chat: Any = None          # 监听子窗口（SendMsg 在子窗口上 who 无效）
        self._queue: queue.Queue[Any] = queue.Queue()
        self.echo = EchoFilter()

    # ---- 连接 --------------------------------------------------------------

    def _on_message(self, msg: Any, chat: Any) -> None:
        """wxauto4 监听回调（推模式）→ 入队，由主循环 pull() 取走。"""
        self._queue.put(msg)

    def connect(self) -> str:
        """初始化并绑定监听会话；返回登录账号昵称。失败抛 RuntimeError。"""
        try:
            from wxauto4 import WeChat
        except ImportError as exc:
            raise RuntimeError(
                "未安装 wxauto4（在桥 .venv 里 pip install E:\\1workspace\\wxauto4）"
            ) from exc

        try:
            self._wx = WeChat()
        except Exception as exc:
            raise RuntimeError(
                "无法连接 PC 微信，请确认：微信 4.x 已启动并完成登录"
            ) from exc

        # AddListenChat 成功返回 Chat 子窗口对象，失败返回 WxResponse(dict)
        result = self._wx.AddListenChat(self.listen_chat, callback=self._on_message)
        if isinstance(result, dict) or result is None:
            reason = result.get("message", "未知错误") if isinstance(result, dict) else "返回空"
            raise RuntimeError(f"无法监听会话「{self.listen_chat}」: {reason}")
        self._chat = result
        return str(getattr(self._wx, "nickname", ""))

    def reconnect(self) -> str:
        try:
            if self._wx is not None:
                self._wx.StopListening()
        except Exception:  # 停监听失败不阻塞重连
            log.warning("StopListening 异常，继续重连", exc_info=True)
        self._wx = None
        self._chat = None
        return self.connect()

    # ---- 收 -----------------------------------------------------------------

    def pull(self) -> list[Incoming]:
        """取监听会话的新消息（排空回调队列）；wxauto 异常向上抛由主循环重连。"""
        if self._wx is None:
            raise RuntimeError("gateway 未连接")
        messages: list[Any] = []
        while True:
            try:
                messages.append(self._queue.get_nowait())
            except queue.Empty:
                break
        log.debug("raw pull: %d 条 [%s]", len(messages),
                  " | ".join(f"{getattr(m, 'type', '?')}:{(getattr(m, 'content', '') or '')[:40]!r}"
                             for m in messages))
        out: list[Incoming] = []
        for m in messages:
            content = getattr(m, "content", "") or ""
            if not str(content).strip():
                continue
            content = str(content).strip()
            if self.echo.is_echo(content):
                log.debug("忽略自己发出的回声消息: %.40r", content)
                continue
            out.append(Incoming(
                content=content,
                msg_type=str(getattr(m, "type", "unknown")),
                sender=str(getattr(m, "sender", "")),
            ))
        return out

    # ---- 发 -----------------------------------------------------------------

    def _send_piece(self, piece: str) -> None:
        """优先走监听子窗口发送（who 在子窗口无效）；失败抛 RuntimeError。"""
        if self._chat is not None:
            resp = self._chat.SendMsg(piece)
        else:
            resp = self._wx.SendMsg(piece, who=self.listen_chat)
        if isinstance(resp, dict) and resp.get("status") != "success":
            raise RuntimeError(f"发送失败: {resp.get('message', '未知错误')}")

    def send(self, text: str) -> None:
        """分段发送；每段都记入回环过滤器（↳ 前缀语义同 V3，见 gateway.py）。"""
        if self._wx is None:
            raise RuntimeError("gateway 未连接")
        pieces = split_wechat(text, self.max_msg_chars)
        if len(pieces) > 1:
            pieces = [pieces[0]] + ["↳ " + p for p in pieces[1:]]
        for piece in pieces:
            self.echo.mark_sent(piece)
            self._send_piece(piece)
            if len(pieces) > 1:
                time.sleep(self.send_pause)
