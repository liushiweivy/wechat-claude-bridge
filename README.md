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
   微信 4.x 备用后端已就绪：`config.json` 设 `"gateway_backend": "v4"` 切到
   wxauto4（`bridge/gateway4.py`，接口与 V3 完全一致）。切换步骤、tools/
   适配缺口与回滚注意见 [docs/wxauto4-migration.md](docs/wxauto4-migration.md)。
   网上"微信 4.x 破解版"是魔改客户端，盗号/封号风险，勿用。
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
| `/open` | 在电脑桌面打开与当前对话对应的可见 CC 窗口（`claude --resume` 同一会话）；无对话时开新窗口，窗口里发出首条消息后桥自动认领该会话（30 分钟内有效）。任务执行中不可用 |
| `/help` | 帮助 |

多轮对话自动携带：runner 从 stream-json 捕获 `session_id`，后续任务用
`--resume` 续接；`/new` 或 `/cd` 后重置。

`/open` 打开的窗口与桥共用同一份 transcript：窗口开着时微信派的任务
不会实时刷新进窗口，回电脑前在窗口里 `/resume` 重进即可看到最新；
窗口里聊的内容微信侧续接时天然可见。同一对话请勿同时开多个窗口。

## 安全设计

- **单一会话信任边界**：只监听「文件传输助手」，只有你本人手机能往里发消息。
- **目录白名单**：`/cd` 只能进 `allowed_dirs` 之内，`..` 越界直接拒绝。
- **默认非全自动**：日常 acceptEdits；bypassPermissions 必须显式 `/sudo` 单条触发。
- **回声过滤**：自己发回微信的消息会被 UIA 再次读到，按「内容+15 秒时间窗」过滤，杜绝自激循环。
- **通知前缀豁免**：以 🌐 开头的消息一律忽略（不回复、不派任务），供本机自动通知脚本使用，如 claude-remote-web 的开机公网地址播报（tools/notify_public_url.py）。
- **无窗口子进程**：`CREATE_NO_WINDOW`；取消只按 PID 树杀，不按镜像名（本机多 Python 项目并发）。
- 日志在 `logs/bridge.log`（UTF-8）。
- 启动器优先直连 npm shim 旁的原生 `claude.exe`（不经 `.CMD` 跳转，杜绝
  残留 cmd.exe 攥住管道导致任务假死）；若你靠自定义 `claude.cmd` 包封装
  环境变量，需把变量放到桥进程环境，shim 会被绕过。

## 测试

```bat
.venv\Scripts\python.exe -m pytest tests\ -q
```

全部用例基于打桩（假 wxauto / 假 claude 进程），无需微信即可运行，
覆盖：指令解析、分段页码、回声窗口与消费、目录逃逸、配置校验、runner 状态流转、
静默超时看门狗、cancel/新任务代际竞态、error subtype 上报、
权限模式透传、网关回声剔除、app 类型闸门、探活判定（分类/first-seen/阈值/目标选择）。

## 探活看门狗（bridge probe）

主循环只在 `pull()` 抛异常时重连；COM/UIA 调用**挂死不返回**时桥会"看着
连着、其实聋了"（2026-10-19 晨实测：进程在、消息有来无回 10 小时）。
同进程内无解（进程级 COM 状态损坏），故探活是**独立外部进程**。

2026-10-21 起部署形态为 **Windows 任务计划每小时拉起一次的短命巡检**
（任务名 `WxBridgeHourlyProbe`，`tools/install_hourly_task.ps1` 安装）。
此前常驻 pythonw 曾无声蒸发、连同守护 bat 一起死透数小时无人拉起；系统任务
计划服务不会死，由它当"监控者的监控者"：

```
任务计划每小时 :11 调 .venv\Scripts\pythonw.exe tools\bridge_probe.py（跑完即退）
  Global mutex 单例（任务 IgnoreNew + 互斥量双保险，8 分钟硬超时兜底）
  → 读 logs/probe_state.json（上一轮 first-seen/冷却，跨进程墙钟计时）
  → 桥与守护 bat 都不在：拉回 autostart-run.bat，写空基线本轮结束（给新桥完整宽限）
  → 桥在：只读「文件传输助手」尾部 30 条（GetAllMessage，不消费桥的监听队列）
    按首字符归类（桥回复全部带 emoji 前缀，⏳ 忙回复也算响应；🌐/sys/time 忽略）
    连续 ≥2 条用户消息无桥回复：首轮先登记，同一轮内等 200s 二次确认；
    跨小时的旧块直接按 state 里的 first-seen 计龄
  → 最早一条 first-seen ≥180s → 判定聋
    桥进程在：杀进程树（守护 bat ~20s 拉起新桥）；桥和 bat 都没了：自己拉 bat
    处置前发 🔧 微信提醒（🔧 在桥前缀表内，不会自激杀循环）
  → first-seen/冷却原子写回 state，进程退出
```

- 新桥宽限：巡检发现桥进程年龄 < 200s 时丢弃 state 里的历史 first-seen
  （那些时间戳属于上一代桥），防巡检落在 🤖 上线横幅前的窄窗里把恢复中的
  桥首轮判死。任务计划 ExecutionTimeLimit 杀的是整棵进程树（venv 双层
  重定向器父子进程均会终止，2026-10-21 实测），不会留下攥着 mutex 的孤儿。
- 守护 bat 识别按 `autostart-(run|visible).bat` 基名 + 项目归属（cmdline
  含项目目录名，或进程 cwd 为本项目根）。相对路径拉起且 cwd 读不到（更高
  权限 shell）时保守等待、绝不重复拉 bat，避免双桥抢同一会话。
- `logs/probe_state.json` 含最近未回复消息的原文 key（与 probe.log 同在
  gitignored 的 `logs/`，单机单用户）；介意可删，删掉只退化为全新基线。
- 阈值环境变量：`BRIDGE_PROBE_MIN_UNANSWERED`(2)、`BRIDGE_PROBE_MIN_AGE`(180)；
  `BRIDGE_PROBE_DRY_RUN=1` 或 `--dry-run` 只记不杀；`--watch` 是人工排障用的
  常驻 15s 循环（纯内存状态，与任务实例 mutex 互斥）。
- 最坏恢复时间 ≈ 等下一轮巡检（≤1h）+ 200s 确认 + 20s 重启；微信没登录时巡检
  退出码 1，任务计划按 2 分钟间隔重试 2 次，下一小时仍会再来。
- 配套约束：桥所有回复必须带 emoji 前缀（探活按此归类），分段回复的续段
  由 gateway 自动加 `↳ `；新增 `_safe_send` 调用点时保持该约定。
- 安装/卸载（无需管理员，仅当前交互用户）：
  `powershell -ExecutionPolicy Bypass -File tools/install_hourly_task.ps1`
  （卸载加 `-Uninstall`）。

## 已知限制

- 微信窗口被其他 UIA 操作抢占时可能拉取失败，守护进程会 10/20/30s 退避重连。
- 单任务串行：执行中发来的任务会被拒绝并提示（避免文件并发冲突）。
- 结果按段落贪心切分，单条上限 1500 字并带 `（n/N）` 页码。
- wxauto 发送走「写剪贴板 + Ctrl+V + Enter」，桥每发一条消息都会**覆盖系统剪贴板**。
- 单任务超时 1800s（wall-clock，静默挂起同样生效）；超时按 PID 树终止。
- 微信连接断开期间（10/20/30s 退避重连）手机发的消息可能丢失，恢复后会提示重发。
