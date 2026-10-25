# -*- coding: utf-8 -*-
"""把图片以「图片消息」形式发到微信里自己（simple）的聊天窗，免停桥。

原理：桥只监听文件传输助手；给自己聊天窗发消息桥无感知（见 topics 经验 35）。
图片走剪贴板 CF_DIB + Ctrl+V，是内联图片而不是文件附件。

用法（必须用项目 .venv；运行期间不要动鼠标键盘）:
  .venv\\Scripts\\python.exe tools\\send_self_images.py <图1> [图2 ...] [-m 说明文字]
"""
from __future__ import annotations

import argparse
import io
import sys
import time
from pathlib import Path

import win32clipboard
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def image_to_clipboard(path: Path) -> None:
    img = Image.open(path).convert('RGB')
    buf = io.BytesIO()
    img.save(buf, 'BMP')
    data = buf.getvalue()[14:]  # 去掉 BITMAPFILEHEADER 才是 CF_DIB
    win32clipboard.OpenClipboard()
    try:
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(win32clipboard.CF_DIB, data)
    finally:
        win32clipboard.CloseClipboard()


def open_self_chat(wx, nickname: str) -> None:
    """主窗左上角头像 → 资料卡「发消息」→ 打开与自己的聊天窗。"""
    win = wx.UiaAPI
    avatar = win.ButtonControl(Name=nickname)
    if not avatar.Exists(maxSearchSeconds=3):
        raise RuntimeError('没找到主窗头像按钮（昵称不对或微信未登录）')
    avatar.Click()
    time.sleep(1.2)
    btn = win.ButtonControl(Name='发消息')
    if not btn.Exists(maxSearchSeconds=3):
        raise RuntimeError('资料卡没找到「发消息」按钮')
    btn.Click()
    time.sleep(1.5)
    cur = wx.CurrentChat()
    print(f'[info] 当前会话: {cur}')
    if cur != nickname:
        raise RuntimeError(f'没进到与自己的聊天窗（当前={cur}）')


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('files', nargs='+')
    ap.add_argument('-m', '--message', default='')
    args = ap.parse_args()

    paths = []
    for raw in args.files:
        p = Path(raw).expanduser().resolve()
        if not p.is_file():
            print(f'[error] 文件不存在: {p}')
            return 2
        paths.append(p)

    nickname = 'simple'  # 自己昵称（经验 35：搜索框搜不到自己，只能 UIA 头像路径）

    from wxauto import WeChat
    wx = WeChat()  # 附加到已运行的 PC 微信
    open_self_chat(wx, nickname)

    edit = wx.ChatBox.EditControl(searchDepth=10)
    for p in paths:
        image_to_clipboard(p)
        time.sleep(0.4)
        edit.Click()
        time.sleep(0.3)
        from wxauto.uiautomation import SendKeys
        SendKeys('{Ctrl}v')
        time.sleep(1.5)  # 图片预览上屏
        SendKeys('{Enter}')
        print(f'[ok] 已发图片: {p.name}')
        time.sleep(2.0)

    if args.message:
        from wxauto.uiautomation import SendKeys
        edit.Click()
        SendKeys(args.message, interval=0.02)
        SendKeys('{Enter}')
        print('[ok] 已发文字说明')
    return 0


if __name__ == '__main__':
    sys.exit(main())
