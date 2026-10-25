# 微信 → Claude Code 本地桥 设计规格（SPEC）

> 本文件是本项目唯一事实源。可直接编辑；改完对 Claude 说「按 SPEC 实现」即驱动代码对齐。
> 实现完成后对应任务会被勾选、变更记录会追加。规格与代码冲突时以本文件为准。

## 1. 目标与范围

把手机微信变成 Claude Code 的远程入口：用户在手机微信给「文件传输助手」发消息，
本机守护进程通过 wxauto（UIAutomation）轮询 PC 微信拿到消息，交给本机 Claude Code
执行，结果分段发回微信。

- **目标**：零注册、零公网、零费用，数据全程不出本机；唯一通道是登录了用户本人
  微信的 PC 客户端。
- **范围内**：消息收发、指令解析、任务执行（无头 `claude -p` 与可见窗口两条路径）、
  多轮会话续接、目录白名单、回声过滤、断线重连、探活看门狗、开机自启。
- **范围外**：多用户/群聊接入、公网 IM 机器人、微信协议层逆向（一律不碰，
  只走 UIAutomation）。

## 2. 需求

### 用户故事与验收标准

- [x] US-1 基本问答：手机发普通文本 → 桥在当前工作目录以 `claude -p` 执行 →
      最终回复发回微信。验收：真机往返成功，回复带 emoji 前缀。
- [x] US-2 长结果分段：超长结果按段落贪心切分，单条不超过 `max_msg_chars`，
      多于一段时每段带 `（n/N）` 页码，续段带 `↳ ` 前缀。
- [x] US-3 回复长度约束：AI 最终回复限制在 `max_reply_chars`（默认 200 字）内，
      采用「提示词约束 + 截断兜底」双层机制，截断优先句子边界。
- [x] US-4 多轮上下文：runner 从 stream-json 捕获 `session_id`，后续任务自动
      `--resume` 续接；`/new` 或 `/cd` 后重置。
- [x] US-5 权限分级：默认 `acceptEdits`（可自动改文件）；`/sudo <任务>` 单条以
      `bypassPermissions` 全自动放行。验收：权限模式透传有测试覆盖。
- [x] US-6 工作目录管理：`/cd [子目录]` 在 `allowed_dirs` 白名单内切换，
      `..`/绝对路径越界直接拒绝。验收：目录逃逸测试通过。
- [x] US-7 任务控制：`/status` 查状态（当前目录、会话、已用工具、本机会话台链接）；
      `/cancel` 按 PID 树终止任务；执行中再派任务被拒绝并提示（单任务串行）。
- [x] US-8 `/open` 窗口模式：在桌面打开与当前对话对应的可见 CC 窗口
      （`claude --resume` 同一会话）；无对话时开新窗口，窗口里发出首条消息后桥在
      30 分钟内自动认领该会话；绑定窗口后微信任务注入窗口执行，过程在窗口实时可见。
- [x] US-9 防自激：桥自己发回微信的消息被 UIA 再次读到时，按「内容归一化 +
      时间窗」过滤（精确匹配 300s / 前缀与合并兜底 120s），命中即消费。
- [x] US-10 通知豁免：以 🌐 开头的消息一律忽略（不回复、不派任务），供本机
      自动通知脚本使用。
- [x] US-11 断线自愈：微信 UIA 断连时 10/20/30s 退避重连；连续 3 轮重连失败
      判定进程级 COM 损坏，桥自杀，由守护 bat 约 20s 拉起全新进程。
- [x] US-12 探活看门狗：独立外部巡检（Windows 任务计划每小时拉起，跑完即退），
      读「文件传输助手」尾部消息，发现连续用户消息无桥回复且超过阈值时，发
      🔧 提醒并杀桥进程树由守护 bat 重启；桥与守护都不在时自己拉起。
- [x] US-13 开机自启：登录后守护 bat 自动拉起桥（可见 cmd 窗口形态），
      重试循环防单次失败。
- [x] US-14 微信 4.x 备用后端：`gateway_backend: "v4"` 切到 wxauto4
      （`bridge/gateway4.py`，接口与 V3 一致）。验收：gateway4 打桩测试通过。
- [ ] US-15 微信 4.x 全量迁移：tools/ 脚本对 V4 的适配缺口补齐并完成真机验证
      （现状仍锁定 3.9.x，迁移步骤见 `docs/wxauto4-migration.md`）。
- [ ] US-16 断连期间消息补偿：重连恢复后对断连窗口内手机发的消息给出
      可追加重发的机制（现状仅提示重发，可能丢失）。

## 3. 架构

```
手机微信 ──(微信自身同步)──> PC 微信 3.9.x（文件传输助手）
                                │ UIAutomation（非注入 Hook，轮询 poll_seconds）
                         ┌──────▼──────────┐
                         │   bridge 守护    │
                         │                 │
                         │  gateway(.py)   │ wxauto 唯一接触面：收消息/回声过滤/分段发送
                         │  gateway4.py    │ 微信 4.x 备用后端（同接口）
                         │  app.py         │ 主循环：指令分发/调度/重连/自杀
                         │  core.py        │ 纯逻辑：指令解析/分段/白名单/stream-json 解析
                         │  runner.py      │ 无头路径：claude -p 子进程（stream-json）
                         │  winrunner.py   │ 窗口路径：注入提示词 + 轮询 transcript 取结果
                         │  winconsole.py  │ CONIN$ 键盘注入/屏幕回读（短寿命子进程）
                         │  config.py      │ 配置加载与校验
                         └──────┬──────────┘
                                │ spawn（CREATE_NO_WINDOW / wscript 可见窗口）
                         ┌──────▼──────────┐
                         │  claude CLI      │ -p --output-format stream-json
                         │  （窗口或后台）   │ 或交互 TUI（/open 窗口）
                         └─────────────────┘

外部看门狗：Windows 任务计划（每小时）→ tools/bridge_probe.py（短命巡检，Global
mutex 单例）→ 判定「聋」→ 杀桥进程树 → autostart-run.bat 重试循环拉起新桥
```

### 模块职责

| 模块 | 职责 | 关键约束 |
|---|---|---|
| `bridge/gateway.py` | wxauto 薄封装，唯一接触面（懒加载，测试可打桩） | 收发只在主线程 |
| `bridge/gateway4.py` | 微信 4.x 备用后端（wxauto4） | 接口与 V3 完全一致 |
| `bridge/core.py` | 纯逻辑：指令解析、分段、回声过滤、目录白名单、stream-json 解析、transcript 兜底、子进程环境清洗 | 不 import wxauto/subprocess，全部可脱机单测 |
| `bridge/runner.py` | 无头执行器：后台线程跑 `claude -p`，session_id 续接，超时与按 PID 树取消 | 每任务一个 _Task + 代际号，迟到回调按代际丢弃 |
| `bridge/winrunner.py` | 窗口执行器：注入提示词到已开 CC 窗口，轮询 transcript 取结果 | 与 runner 同并发模型（单任务串行 + 代际号） |
| `bridge/winconsole.py` | AttachConsole + WriteConsoleInputW 注入键盘、ReadConsoleOutputCharacterW 回读屏幕 | 必须在短寿命子进程里执行，不波及桥本体控制台 |
| `bridge/app.py` | 主循环：消息 → 指令分发 → 执行器 → 回微信；重连退避；连败自杀 | wxauto 调用只在主线程，完成事件经队列回收 |
| `bridge/probe.py` / `tools/bridge_probe.py` | 探活看门狗逻辑与短命巡检入口 | 独立外部进程，不与桥同进程 |
| `bridge/config.py` | config.json 加载、未知项拒绝、取值范围与白名单校验 | 启动即校验，失败即拒跑 |
| `tools/` | 运维脚本：自启安装、巡检任务安装、窗口注入 e2e、通知等 | `tools/_*` 个人运维脚本不入库（gitignore） |

## 4. 接口契约

本项目是桥接器不是 Web 服务，对外契约是微信消息协议与本地配置文件。

### 4.1 入向消息（手机 → 桥）

只监听 `listen_chat` 配置的会话（默认「文件传输助手」）。仅 `self` 与 `friend`
类型消息可驱动任务；sys/time/recall 等一律忽略。以 🌐 开头的消息一律忽略。
图片/文件等消息只有占位文本（`[图片]`、`[文件] xxx`），拿不到本体，一律忽略
（不回复、不派任务）。

| 指令 | 语义 |
|---|---|
| 普通文本 | 作为任务交给 Claude（当前工作目录，默认 acceptEdits） |
| `/sudo <任务>` | 本条任务以 bypassPermissions 执行 |
| `/status` | 桥状态：工作目录、会话、已用工具、本机会话台链接 |
| `/cancel` | 取消执行中的任务（按 PID 树终止） |
| `/new` | 丢弃多轮上下文，下次任务开新会话 |
| `/cd [子目录]` | 切换/查看工作目录（受 allowed_dirs 约束） |
| `/open`（别名 `/win`） | 打开/绑定对应对话的可见 CC 窗口；任务执行中不可用 |
| `/help` | 帮助 |
| 其他 `/xxx` | 回复「未知命令」提示 |

### 4.2 出向消息（桥 → 微信）

- **所有桥回复必须带 emoji 前缀**（探活看门狗按此前缀归类，⏳ 忙回复也算响应）；
  新增发送点必须保持该约定。
- 长结果分段：单段 ≤ `max_msg_chars`（默认 1500），多段带 `（n/N）` 页码，
  续段由 gateway 自动加 `↳ ` 前缀；段间有发送间隔防微信限频。
- AI 最终回复 ≤ `max_reply_chars`（默认 200 字）：提示词层要求模型写短，
  截断层在超限兜底（优先句子边界，末尾加省略号）。
- 特殊前缀约定：🔧 看门狗处置提醒（在桥前缀表内，不会触发自激杀循环）；
  ⚠️ transcript 兜底恢复提示；🤖 上线横幅/帮助。

### 4.3 config.json 字段

`config.json` 为本机私有配置（gitignored），首次启动由 `config.example.json`
复制生成。未知字段直接拒绝（防拼写错误静默失效）。

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `listen_chat` | string | 文件传输助手 | 监听的会话名 |
| `workspace` | string | （工作区根） | 默认工作目录，必须位于 allowed_dirs 之内 |
| `allowed_dirs` | string[] | （工作区根） | `/cd` 可达目录白名单，运行时校验存在性 |
| `poll_seconds` | float | 2.5 | UIA 轮询间隔，<1 拒绝（过频拖慢微信窗口） |
| `default_mode` | string | acceptEdits | default/acceptEdits/plan/bypassPermissions |
| `task_timeout_seconds` | int | 1800 | 单任务 wall-clock 超时，静默挂起同样生效 |
| `max_msg_chars` | int | 1500 | 微信单条消息上限，200–2000 |
| `max_reply_chars` | int | 200 | AI 最终回复上限，100–1000 |
| `claude_bin` | string | claude | CLI 可执行名（优先直连原生 claude.exe，见 §6） |
| `extra_args` | string[] | [] | 追加给 claude 的参数 |
| `gateway_backend` | string | v3 | v3=wxauto/微信3.9；v4=wxauto4/微信4.x |

## 5. 数据模型与存储

| 存储 | 位置 | 内容 | 备注 |
|---|---|---|---|
| 本机配置 | `config.json`（项目根，gitignored） | 见 §4.3 | 模板为 `config.example.json` |
| 运行日志 | `logs/bridge.log`（gitignored） | UTF-8，RotatingFileHandler | |
| 巡检状态 | `logs/probe_state.json`（gitignored） | 未回复消息的 first-seen 时间戳与冷却；含消息原文 key（单机单用户） | 原子写回；删掉只退化为全新基线 |
| 巡检日志 | `logs/probe.log`（gitignored） | 巡检判定与处置记录 | |
| 会话 transcript | `~/.claude/projects/<slug>/<session_id>.jsonl` | Claude Code 原生落盘 | 多轮续接、/open 认领、结果兜底都靠它；slug 规则同 CC 本体 |
| wxauto 运行日志 | `@AutomationLog.txt`（gitignored） | wxauto 自身输出 | |
| 依赖源码 | `_wxauto-src/`（gitignored） | wxauto 源码（未发布 PyPI，内置） | |

无数据库、无网络存储；所有状态要么在配置文件，要么在 gitignored 的本地日志。

## 6. 关键设计决策

1. **PC 微信锁 3.9.x**（实测 3.9.12.1000 可用）：wxauto 基于 UIAutomation，
   不支持微信 4.0，切勿升级；网上「4.x 破解版」是魔改客户端，盗号/封号风险，
   禁用。4.x 备用后端（gateway4）已就绪但未全量迁移。
2. **单一会话信任边界**：只监听「文件传输助手」，只有本人手机能往里发消息，
   天然免鉴权。
3. **安全模型 = 默认非全自动 + 目录白名单**：日常 acceptEdits，bypassPermissions
   必须显式 `/sudo` 单条触发；`/cd` 只能在 allowed_dirs 之内，`..` 越界拒绝。
   这是有意的设计决策：桥能让手机远程驱动本机改文件/跑命令，白名单与显式
   sudo 是仅有的两道闸，不可拆除。
4. **回声过滤按内容而非发送者**：文件传输助手里所有消息 sender 都是自己，
   只能按「内容归一化 + 时间窗」匹配（精确 300s / 前缀与合并兜底 120s，
   归一化要剥 emoji 变体选择符、折叠空白），命中即消费，防误吞用户同文消息。
5. **无窗口子进程 + 按 PID 树杀**：子进程一律 `CREATE_NO_WINDOW`；取消只按
   PID 树终止，绝不按镜像名 taskkill（本机多 Python 项目并发，按名杀会误伤）。
6. **启动器优先直连原生 claude.exe**：不经 npm shim 的 `.CMD` 跳转，杜绝残留
   cmd.exe 攥住管道写端导致任务假死；副作用是自定义 `claude.cmd` 里的环境
   变量封装会被绕过，需把变量放到桥进程环境。
7. **CONIN$ 注入（/open 窗口路径）**：用 AttachConsole + WriteConsoleInputW
   直接写目标控制台输入缓冲，不切前台、不抢焦点；必须在短寿命子进程里执行
   （FreeConsole/AttachConsole 会折腾调用者自己的控制台）。
8. **剥 CLAUDE\* 环境变量**：桥若从某个 claude 会话终端间接启动，会继承
   `CLAUDE_CODE_CHILD_SESSION=1` 等标记；带着它起的 claude 被当成子会话，
   屏幕警告且 transcript saving 关闭，认领/取结果全断。所有子进程环境统一
   剥掉 `CLAUDE*` 前缀变量。
9. **TUI 吞回车补发**：注入窗口的回车偶发被 TUI 吃掉（文字留在输入框不提交）；
   注入后若确认期内看不到执行迹象，每隔数秒补发一个裸回车（空输入上的裸回车
   是 no-op，误补发无害），最多补 3 次。
10. **transcript 兜底**：第三方网关下 CLI 偶发答案完整落盘但不发 stream-json
    的 result 事件；此时从本地 transcript jsonl 取回最终答案并加 ⚠️ 提示。
11. **/open 认领按创建时间**：新窗口会话靠找「创建时间晚于开窗口时刻的最新
    transcript」认领（Windows 上必须用 st_ctime 而非 mtime，老会话被碰一下
    mtime 就更新会被误领），且必须排除桥自己任务产生的 session_id。
12. **探活必须外部进程**：COM/UIA 调用挂死不返回时桥「看着连着、其实聋了」，
    同进程内无解（进程级 COM 状态损坏）。部署形态为 Windows 任务计划每小时
    拉起的短命巡检（此前常驻 pythonw 曾无声蒸发），任务 IgnoreNew + Global
    mutex 双保险单例，8 分钟硬超时兜底；新桥给宽限（进程年龄 <200s 丢弃历史
    first-seen），处置前发 🔧 微信提醒。
13. **连败自杀换进程**：UIA 断连有时是进程级 COM 损坏，reconnect 无效而新进程
    秒连；主循环连续 3 轮重连失败即自杀，守护 bat 约 20s 拉起全新桥。
14. **已知代价（接受不修）**：wxauto 发送走「写剪贴板 + Ctrl+V + Enter」，
    桥每发一条消息都会覆盖系统剪贴板；重连期间手机消息可能丢失，恢复后提示
    重发；窗口开着时微信派的任务不实时刷新进窗口（回窗口 `/resume` 即见最新）。

## 7. 任务清单

- [x] 基本桥接链路：wxauto 轮询 → 指令解析 → `claude -p` → 分段回微信
- [x] 指令集：/help /new /cd /sudo /cancel /status
- [x] 回复长度双层约束（提示 + 截断）与分段页码
- [x] 回声过滤（精确/前缀/合并三级 + 归一化）
- [x] 目录白名单与配置校验
- [x] runner 并发模型（代际号防迟到回调）、超时与 PID 树取消
- [x] 多轮会话续接（stream-json 捕获 session_id + --resume）
- [x] transcript 兜底恢复（result 事件缺失时取回落盘答案）
- [x] /open 窗口模式：wscript 开可见窗口、CONIN$ 注入、会话认领、winrunner 执行路径
- [x] CLAUDE\* 环境剥离、TUI 吞回车补发
- [x] 断线退避重连 + 连败自杀 + 守护 bat 拉起
- [x] 外部探活看门狗（任务计划短命巡检 + mutex 单例 + 🔧 提醒）
- [x] 开机自启（可见 cmd 窗口形态 + 重试循环）
- [x] 微信 4.x 备用后端 gateway4（接口对齐 V3）
- [x] 全量打桩测试（tests/ 下 197 个用例，无需微信即可运行）+ 真机验证
- [x] 忽略图片/文件占位消息（`[图片]`/`[文件] xxx` 不派任务）
- [ ] 微信 4.x 全量迁移与 tools/ 适配补齐（US-15）
- [ ] 断连期间消息补偿机制（US-16）

## 8. 变更记录

| 日期 | 变更 | 状态 |
|---|---|---|
| 2026-10-24 | 创建 SPEC.md（首版，对齐现有实现）与 CLAUDE.md（SPEC 驱动开发约定） | 已完成 |
| 2026-10-25 | core.parse_instruction 忽略 [图片]/[文件] 占位消息（MEDIA_PLACEHOLDER_PREFIXES）+3 测试 | 已完成 |
