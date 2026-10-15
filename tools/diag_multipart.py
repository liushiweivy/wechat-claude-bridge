"""多分段回声真机验证：把一条长文本用小上限分多段发出，逐段验证回声全被过滤。

模拟 code-review HIGH3 场景：长结果多段发送、首段回声回报较晚。
判定：发送结束后 90s 内，EchoFilter 过滤后不得有任何残留消息。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bridge.config import load_config
from bridge.gateway import WxGateway

cfg = load_config()
gw = WxGateway(cfg.listen_chat, max_msg_chars=200, send_pause=1.2)
print("connect ...")
print("nickname:", gw.connect())
time.sleep(1)
gw.pull()  # 清积压

long_text = "多分段回声测试段落，这一段需要重复足够多次以触发切分。" * 30  # ~750 字
print(f"sending text of {len(long_text)} chars ...")
gw.send(long_text)
print("send done; polling 90s for leaked echoes ...")

leaks = []
deadline = time.time() + 90
while time.time() < deadline:
    for m in gw.pull():
        leaks.append(m)
        print(f"  LEAK! type={m.msg_type} content={m.content[:60]!r}")
    time.sleep(2.5)

if leaks:
    print(f"FAIL: {len(leaks)} 条回声漏网，会造成自激")
    sys.exit(1)
print("PASS: 全部分段回声被过滤，无自激风险")
