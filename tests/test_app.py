"""app 层测试：消息类型闸门与指令分发（网关打桩，不连接微信）。"""
from pathlib import Path

from bridge.app import BridgeApp, is_actionable
from bridge.config import Config


class FakeGateway:
    def __init__(self):
        self.sent = []

    def send(self, text):
        self.sent.append(text)


def _app(tmp_path: Path) -> BridgeApp:
    cfg = Config(
        listen_chat="文件传输助手",
        workspace=str(tmp_path),
        allowed_dirs=(str(tmp_path),),
    )
    app = BridgeApp(cfg)
    app.gw = FakeGateway()
    return app


def test_only_self_and_friend_are_actionable():
    assert is_actionable("self")
    assert is_actionable("friend")
    assert not is_actionable("sys")
    assert not is_actionable("time")
    assert not is_actionable("recall")


def test_dispatch_help_and_status_without_wechat(tmp_path: Path):
    app = _app(tmp_path)
    app.dispatch("/help")
    app.dispatch("/status")
    assert any("指令" in s or "/sudo" in s for s in app.gw.sent)
    assert any("工作目录" in s for s in app.gw.sent)


def test_dispatch_unknown_command_gets_hint(tmp_path: Path):
    app = _app(tmp_path)
    app.dispatch("/frobnicate")
    assert app.gw.sent and "/help" in app.gw.sent[-1]


def test_new_resets_session(tmp_path: Path):
    app = _app(tmp_path)
    app.session_id = "sess-old"
    app.dispatch("/new")
    assert app.session_id == ""


def test_cd_rejects_escape(tmp_path: Path):
    app = _app(tmp_path)
    app.dispatch("/cd ../../..")
    assert app.gw.sent and "⚠️" in app.gw.sent[-1]
