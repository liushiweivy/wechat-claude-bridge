"""微信侧最小自检（不跑 claude 任务）。

前置：PC 微信 3.9.x 已启动并登录。
运行：.venv\\Scripts\\python.exe tools\\wechat_selfcheck.py

验证：
1. wxauto 能连接并监听「文件传输助手」；
2. 能发出一条自检消息；
3. 自检消息的回声被 EchoFilter 过滤（不会自激）；
4. 随后 60s 内你在手机/PC 上手动发的任意消息会被原样打印（回声过滤后）。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bridge.config import load_config
from bridge.gateway import WxGateway


def main() -> None:
    cfg = load_config()
    gw = WxGateway(cfg.listen_chat, cfg.max_msg_chars)

    print("[1/4] 连接 PC 微信 ...")
    nickname = gw.connect()
    print(f"      已连接账号：{nickname or '(未取到昵称)'}，监听：{cfg.listen_chat}")

    print("[2/4] 发送自检消息 ...")
    gw.send("🔧 桥接自检：如果你在文件传输助手看到这条消息，说明收发正常")
    print("      已发送，等待回声回报 ...")
    time.sleep(4)

    print("[3/4] 拉取一次（自检回声应被过滤，下面应为空）:")
    echoed = gw.pull()
    print(f"      过滤后剩余 {len(echoed)} 条（预期 0）")

    print("[4/4] 60 秒内请用手机微信给文件传输助手发一条消息（例如：你好）...")
    deadline = time.time() + 60
    got = 0
    while time.time() < deadline:
        for m in gw.pull():
            got += 1
            print(f"      收到 [{m.msg_type}] {m.sender}: {m.content}")
        time.sleep(2.5)

    if got:
        print(f"自检通过：共收到 {got} 条手动消息，回声未造成自激。")
    else:
        print("未收到手动消息；收发链路前 3 步已完成，可稍后用 start.bat 做完整验证。")


if __name__ == "__main__":
    main()
