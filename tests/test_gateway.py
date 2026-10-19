"""WxGateway 测试：用假 wxauto 验证拉取去回环与分段发送。"""
from types import SimpleNamespace

from bridge.gateway import Incoming, WxGateway


class FakeWx:
    def __init__(self, incoming):
        self._incoming = incoming
        self.sent = []
        self.nickname = "测试账号"

    def AddListenChat(self, who):
        self.listening = who

    def GetListenMessage(self, who):
        msgs, self._incoming = self._incoming, []
        return msgs

    def SendMsg(self, text, who=None):
        self.sent.append(text)


def _msg(content, mtype="self", sender="我"):
    return SimpleNamespace(content=content, type=mtype, sender=sender)


def _gateway(fake):
    g = WxGateway("文件传输助手", max_msg_chars=100)
    g._wx = fake
    return g


def test_pull_strips_echo_of_own_replies():
    fake = FakeWx([_msg("✅ 完成\n你好"), _msg("下一个任务")])
    g = _gateway(fake)
    g.send("✅ 完成\n你好")          # 先发过一条
    msgs = g.pull()
    assert [m.content for m in msgs] == ["下一个任务"]


def test_pull_keeps_nonempty_types_filtering_is_app_layer():
    # 网关只去空消息和回声；类型过滤（sys/time 等）在 app 层
    fake = FakeWx([_msg(""), _msg("以下为新消息", mtype="sys"), _msg("真指令")])
    msgs = _gateway(fake).pull()
    assert [m.content for m in msgs] == ["以下为新消息", "真指令"]


def test_send_long_text_is_chunked_and_marked():
    fake = FakeWx([])
    g = _gateway(fake)
    g.send("字" * 350)
    assert len(fake.sent) >= 3
    assert all(len(s) <= 110 for s in fake.sent)
    # 发出的内容立刻被回读时应识别为回声
    assert g.echo.is_echo(fake.sent[0])


def test_send_continuation_pieces_carry_prefix():
    # 探活按首字符归类消息：第 2 段起必须带 ↳，且回声登记与实际发送一致
    fake = FakeWx([])
    g = _gateway(fake)
    g.send("字" * 350)
    assert len(fake.sent) >= 2
    assert all(s.startswith("↳ ") for s in fake.sent[1:])
    for piece in fake.sent:
        assert g.echo.is_echo(piece)


def test_incoming_dataclass():
    m = Incoming("x", "self", "我")
    assert m.content == "x"
