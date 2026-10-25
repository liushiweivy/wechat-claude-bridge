# 微信 4.x 适配方案（wxauto4 后端，备用切换）

> 2026-10-24 落盘。目的：**3.9 不能登录的那天，桥能几分钟内切到 4.x**。
> 现在不用动任何东西——3.9 继续用，v4 代码已写好放着。

## 背景

- 桥现在依赖 wxauto（UIAutomation），**只支持微信 3.9.x**，README 明确"切勿升级"。
- 网上所谓"微信 4.1 破解 AI 操控"是魔改客户端，盗号/封号风险，**不要用**。
- 官方 4.x 客户端的正经 UI 自动化方案：
  - `cluic/wxauto4`：wxauto 原作者的 4.0 版，但**仓库已停更**（README 只剩"停止更新"）。
  - 可用 fork：`fly000001/wxauto4`（本机已克隆到 `E:\1workspace\wxauto4`，
    pyproject 齐全，版本 40.1.1，依赖 tenacity/pywin32/pyperclip/pillow/psutil/comtypes）。
  - 备选 `Hello-Mr-Crab/pywechat`（2037★，pywinauto 实现，API 完全不同，
    本机已克隆到 `E:\1workspace\pywechat` 仅作参考）。

## 已完成（放着备用）

- `bridge/gateway4.py`：与 `WxGateway` **完全同接口**的 v4 变体
  （connect/reconnect/pull/send），核心差异处理：
  - wxauto4 是**推模式**：`AddListenChat(chat, callback)`，回调参数
    `(Message, Chat)`；本模块用线程安全队列把回调适配回主循环的 `pull()` 轮询语义；
  - `AddListenChat` 会把会话**弹成独立子窗口**（wxauto4 的设计，正常现象）；
    发送优先走子窗口 `Chat.SendMsg`（其 `who` 参数无效）；
  - 返回值是 `WxResponse`（dict，`status` 键），非 success 抛 RuntimeError，
    让主循环重连逻辑接管；
  - `Reconnect` 先 `StopListening` 再重连。
- `config.json` 新键 `gateway_backend`：`"v3"`（默认，现状不动）/ `"v4"`。
  `app.py` 按它选 `WxGateway` / `WxGatewayV4`。
- 测试 `tests/test_gateway4.py`（8 个，伪造 wxauto4 模块，全程不碰微信）+
  `test_config.py` 白名单用例，全套 217 绿。

## 切换步骤（真到那天）

1. 微信升级到 4.x，登录。
2. 桥 venv 装 wxauto4：
   `.venv\Scripts\python.exe -m pip install E:\1workspace\wxauto4`
3. `config.json` 改 `"gateway_backend": "v4"`，重启桥。
4. 验证：发消息 → 桥收 → 回复 → 回声不误判 → 长文分段 ↳ 前缀正常。
5. 回滚：改回 `"v3"`（3.9 若已不能登录则不可回滚，升级前想清楚）。

## 已知缺口 / 待验证项

- **tools/ 下的运维脚本还是 v3 写法**，切换后要逐个适配：
  - `tools/bridge_probe.py:270` 用 `wx.listen[chat].GetAllMessage()`——
    wxauto4 里 `listen` 的值是 `(chat, callback)` 元组，应改 `wx.listen[chat][0]`；
  - `tools/send_files.py` 的 `SendFiles(paths, who=)`：wxauto4 同签名，直接可用，
    但 returns WxResponse 需看 status；
  - `tools/diag_echo.py` 用 `GetListenMessage` 轮询，v4 要改成回调收；
  - `tools/e2e_window_inject.py` 用 `ChatWith`+`GetAllMessage`，wxauto4 都有，基本平移；
  - `tools/send_self_images.py` 是 CF_DIB 剪贴板 + UIA 直控（不经 wxauto），
    4.x 的聊天窗口结构变了，**头像/输入框定位大概率要重调**。
- wxauto4 的消息 `sender` 字段：4.x 子窗口里"文件传输助手"消息 sender 语义
  与 3.9 是否一致（self/friend 判定靠 `attr`），待真机验证。
- pywechat 作为 wxauto4 不可用时的第二选择，移植工作量更大（API 全换）。
