"""回声诊断：对比 SendMsg 发出的文本与 GetListenMessage 读回的原始 content。

直接操作 wxauto，绕过 EchoFilter，用 repr 暴露换行/空白差异。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wxauto import WeChat

CHAT = "文件传输助手"

wx = WeChat()
wx.AddListenChat(CHAT)
time.sleep(1)
wx.GetListenMessage(who=CHAT)  # 先清空当前积压

samples = [
    "DIAG单行回声测试",
    "⏳ 已开工【常规】\nDIAG多行第二行\nDIAG第三行",
]

for i, text in enumerate(samples, 1):
    print(f"--- sample {i} sent repr: {text!r}")
    wx.SendMsg(text, who=CHAT)
    time.sleep(6)
    msgs = wx.GetListenMessage(who=CHAT)
    if isinstance(msgs, dict):
        msgs = [m for lst in msgs.values() for m in lst]
    print(f"    got {len(msgs)} messages back:")
    for m in msgs:
        print(f"    type={getattr(m, 'type', '?')!r} sender={getattr(m, 'sender', '?')!r}")
        print(f"    content repr={getattr(m, 'content', '')!r}")
        match = getattr(m, "content", "").strip() == text.strip()
        print(f"    exact-match: {match}")
    time.sleep(1)

print("done")
