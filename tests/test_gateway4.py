# -*- coding: utf-8 -*-
"""gateway4（wxauto4 / 微信 4.x 后端）：回调→队列适配、发送、连接与降级。"""
from __future__ import annotations

import sys
import types

import pytest

from bridge import gateway4


class FakeResponse(dict):
    """模拟 wxauto4 的 WxResponse：dict，带 status 键。"""


class FakeMessage:
    def __init__(self, content, msg_type="text", sender="好友甲", attr="friend"):
        self.content = content
        self.type = msg_type
        self.sender = sender
        self.attr = attr


class FakeChat:
    def __init__(self):
        self.sent = []

    def SendMsg(self, msg, who=None, **kw):
        self.sent.append(msg)
        return FakeResponse(status="success", message="")


class FakeWeChat:
    instances = []

    def __init__(self, nickname=None):
        self.nickname = "我本人"
        self.listen = {}
        self.sent = []
        FakeWeChat.instances.append(self)

    def AddListenChat(self, nickname, callback):
        if nickname == "不存在的人":
            return FakeResponse(status="failure", message="找不到聊天窗口")
        chat = FakeChat()
        self.listen[nickname] = (chat, callback)
        return chat

    def SendMsg(self, msg, who=None, **kw):
        self.sent.append((who, msg))
        return FakeResponse(status="success", message="")

    def StopListening(self, remove=True):
        self.listen.clear()


def _install_fake(monkeypatch):
    mod = types.ModuleType("wxauto4")
    mod.WeChat = FakeWeChat
    monkeypatch.setitem(sys.modules, "wxauto4", mod)
    FakeWeChat.instances = []
    return mod


@pytest.fixture()
def gw4(monkeypatch):
    _install_fake(monkeypatch)
    g = gateway4.WxGatewayV4("文件传输助手")
    return g


def test_connect_binds_listen_chat_and_returns_nickname(gw4):
    assert gw4.connect() == "我本人"
    wx = FakeWeChat.instances[0]
    assert "文件传输助手" in wx.listen


def test_connect_unknown_chat_raises(gw4):
    gw4.listen_chat = "不存在的人"
    with pytest.raises(RuntimeError, match="无法监听会话"):
        gw4.connect()


def test_pull_drains_callback_queue_and_filters_echo(gw4):
    gw4.connect()
    wx = FakeWeChat.instances[0]
    chat, callback = wx.listen["文件传输助手"]
    # 模拟微信侧新消息进回调
    callback(FakeMessage("  你好 "), chat)
    callback(FakeMessage("   "), chat)          # 空白消息丢弃
    callback(FakeMessage("图片", "image"), chat)
    msgs = gw4.pull()
    assert [m.content for m in msgs] == ["你好", "图片"]
    assert msgs[0].msg_type == "text" and msgs[0].sender == "好友甲"
    assert msgs[1].msg_type == "image"
    # 第二次 pull 为空（队列已排空）
    assert gw4.pull() == []


def test_pull_ignores_own_echo(gw4):
    gw4.connect()
    gw4.send("我是桥发出的")
    wx = FakeWeChat.instances[0]
    chat, callback = wx.listen["文件传输助手"]
    callback(FakeMessage("我是桥发出的", sender="我本人", attr="self"), chat)
    assert gw4.pull() == []


def test_send_uses_listen_subwindow_and_splits(gw4):
    gw4.connect()
    long_text = "x" * 3001  # 超过 max_msg_chars=1500 → 分段
    gw4.send(long_text)
    wx = FakeWeChat.instances[0]
    chat, _ = wx.listen["文件传输助手"]
    assert len(chat.sent) == 3
    assert chat.sent[1].startswith("↳ ")
    assert wx.sent == []  # 走子窗口，不走主窗口 who=


def test_send_failure_status_raises(gw4, monkeypatch):
    gw4.connect()
    wx = FakeWeChat.instances[0]
    chat, _ = wx.listen["文件传输助手"]

    def fail(msg, who=None, **kw):
        return FakeResponse(status="failure", message="窗口丢失")

    monkeypatch.setattr(chat, "SendMsg", fail)
    with pytest.raises(RuntimeError, match="窗口丢失"):
        gw4.send("hello")


def test_reconnect_reinitializes(gw4):
    gw4.connect()
    assert gw4.reconnect() == "我本人"
    assert len(FakeWeChat.instances) == 2


def test_missing_wxauto4_package(monkeypatch):
    monkeypatch.delitem(sys.modules, "wxauto4", raising=False)
    import builtins
    real_import = builtins.__import__

    def no_wxauto4(name, *a, **kw):
        if name == "wxauto4":
            raise ImportError("No module named 'wxauto4'")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", no_wxauto4)
    g = gateway4.WxGatewayV4("文件传输助手")
    with pytest.raises(RuntimeError, match="wxauto4"):
        g.connect()
