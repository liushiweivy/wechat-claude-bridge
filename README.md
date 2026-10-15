# 微信 → Claude Code 本地桥（方案 A：wxauto 本地桥）

手机微信发消息 → PC 微信「文件传输助手」→ 本机 Python 守护进程（wxauto 轮询）
→ `claude -p` 无头执行 → 结果分段发回微信。

**零注册、零公网、零费用**，数据全程不出本机；唯一通道是登录了你本人微信的 PC 客户端。

## 工作原理

```
手机微信 ──(微信自身同步)──> PC 微信 3.9.x
                              │ UIAutomation（非注入 Hook）
                       ┌──────▼──────┐
                       │  bridge 守护 │  轮询 2.5s
                       │             │
                       │  gateway    │ 收消息 / 回声过滤 / 分段发送
                       │  app        │ 指令解析 / 调度 / 重连
                       │  runner     │ claude -p（stream-json）
                       └─────────────┘
```

- `bridge/gateway.py`：wxauto 唯一接触面（懒加载，测试可打桩）
- `bridge/core.py`：纯逻辑——指令解析、长文本分段、回声过滤、目录白名单、流事件解析
- `bridge/runner.py`：子进程线程、session_id 续接、超时与按 PID 树取消
- `bridge/app.py`：主循环（wxauto 调用只在主线程，完成事件经队列回收）
- `bridge/config.py`：配置读取与校验

## 环境要求

1. **PC 微信必须是 3.9.x**（本机实测 3.9.10.27）。wxauto 基于 UIAutomation，
   **不支持微信 4.0，切勿升级**。
2. Python 3.12（项目自带 `.venv`）。
3. 本机已安装并登录 Claude Code CLI（`claude --version` 可用）。
4. wxauto 源码已内置在 `_wxauto-src/`（该库未发布 PyPI）。

## 使用

1. PC 微信登录，保持窗口存在（可最小化）。
2. 双击 `start.bat`（首次自动建 venv、装依赖、生成 config.json）。
3. 看到「文件传输助手」收到 `🤖 Claude 桥已上线` 即成功。
4. 手机微信给「文件传输助手」发消息即可，例如：
   - `看一下 E:\1workspace\hello-trip-planner 的测试为什么挂了`
   - 长任务中发 `/status` 查进度，`/cancel` 取消

### 指令

| 指令 | 作用 |
|---|---|
| 普通文本 | 作为任务交给 Claude（默认 acceptEdits，可自动改文件） |
| `/sudo <任务>` | 本次任务用 bypassPermissions，全自动放行（谨慎） |
| `/status` | 当前状态、工作目录、会话、已用工具 |
| `/cancel` | 取消执行中的任务（按进程树终止 claude 及子进程） |
| `/new` | 丢弃多轮上下文，下次任务开新会话 |
| `/cd [子目录]` | 切换工作目录（受 allowed_dirs 白名单约束）；无参数显示当前目录 |
| `/help` | 帮助 |

多轮对话自动携带：runner 从 stream-json 捕获 `session_id`，后续任务用
`--resume` 续接；`/new` 或 `/cd` 后重置。

## 安全设计

- **单一会话信任边界**：只监听「文件传输助手」，只有你本人手机能往里发消息。
- **目录白名单**：`/cd` 只能进 `allowed_dirs` 之内，`..` 越界直接拒绝。
- **默认非全自动**：日常 acceptEdits；bypassPermissions 必须显式 `/sudo` 单条触发。
- **回声过滤**：自己发回微信的消息会被 UIA 再次读到，按「内容+15 秒时间窗」过滤，杜绝自激循环。
- **无窗口子进程**：`CREATE_NO_WINDOW`；取消只按 PID 树杀，不按镜像名（本机多 Python 项目并发）。
- 日志在 `logs/bridge.log`（UTF-8）。

## 测试

```bat
.venv\Scripts\python.exe -m pytest tests\ -q
```

48 个用例全部基于打桩（假 wxauto / 假 claude 进程），无需微信即可运行，
覆盖：指令解析、分段页码、回声窗口与消费、目录逃逸、配置校验、runner 状态流转、
静默超时看门狗、cancel/新任务代际竞态、error subtype 上报、
权限模式透传、网关回声剔除、app 类型闸门。

## 已知限制

- 微信窗口被其他 UIA 操作抢占时可能拉取失败，守护进程会 10/20/30s 退避重连。
- 单任务串行：执行中发来的任务会被拒绝并提示（避免文件并发冲突）。
- 结果按段落贪心切分，单条上限 1500 字并带 `（n/N）` 页码。
- wxauto 发送走「写剪贴板 + Ctrl+V + Enter」，桥每发一条消息都会**覆盖系统剪贴板**。
- 单任务超时 1800s（wall-clock，静默挂起同样生效）；超时按 PID 树终止。
- 微信连接断开期间（10/20/30s 退避重连）手机发的消息可能丢失，恢复后会提示重发。
