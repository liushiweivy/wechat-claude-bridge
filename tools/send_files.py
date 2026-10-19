# -*- coding: utf-8 -*-
"""手动经 PC 微信发送文件（可附一条文字说明）到指定会话。

用法（必须用项目 .venv，里面装了内置 wxauto）:
  .venv\\Scripts\\python.exe tools\\send_files.py <文件1> [文件2 ...] [-m 说明文字] [-w 会话名]

注意
----
1. 运行期间**不要动鼠标键盘**，wxauto 靠 UIA + 剪贴板操作微信窗口。
2. **必须先停掉桥守护再发**：桥把「文件传输助手」里所有 self 消息当任务，
   本进程发出的文件会被桥当成 "[文件] xxx" 新任务派发。
   停法：按 PID 树杀掉跑 autostart-run.bat 的 cmd.exe（会连同 python -m bridge
   与 20s 重试循环一起结束），发完再重新启动 autostart-run.bat。
3. 桥重启后 wxauto 首次拉取会把当前消息标记已用，本工具发出的文件不会被追派。
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bridge.config import load_config  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="经 PC 微信发送文件")
    parser.add_argument("files", nargs="+", help="要发送的文件绝对路径")
    parser.add_argument("-m", "--message", default="", help="随文件发送的文字说明")
    parser.add_argument("-w", "--who", default="", help="目标会话（默认配置里的监听会话=文件传输助手）")
    args = parser.parse_args()

    paths = []
    for raw in args.files:
        p = Path(raw).expanduser().resolve()
        if not p.is_file():
            print(f"[error] 文件不存在: {p}")
            return 2
        paths.append(str(p))

    cfg = load_config()
    who = args.who or cfg.listen_chat

    from wxauto import WeChat

    print("connect wechat ...")
    wx = WeChat()
    print(f"sending {len(paths)} file(s) -> {who}")
    result = wx.SendFiles(paths, who=who)
    if result is False:
        print("[error] SendFiles 返回失败")
        return 1
    time.sleep(3)                      # 等微信上传/发送动画结束
    if args.message:
        wx.SendMsg(args.message, who=who)
        time.sleep(1)
    print("done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
