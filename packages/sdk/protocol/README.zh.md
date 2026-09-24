---
description: "面向客户端与服务端实现者的 SDK 协议格式（wire format）说明：Harness 运行时与其 SDK 客户端之间使用的按换行分帧 JSON-RPC 传输，以及具名的请求、结果与通知类型。"
kind: "package-library"
---

# @deepseek-ai/dsh-sdk-protocol

[English](README.md) | 中文

## 概述

`dsh-sdk-protocol` 让 DeepSeek Harness 运行时与其 SDK 客户端通过按换行分帧的字节流交换 JSON-RPC 2.0 消息：一个传输类，加上协议两端共同使用的具名请求、结果与通知类型。服务端是 [`dsh-sdk-jsonrpc-server`](../server/README.zh.md) 插件；客户端是 TypeScript 的 [`dsh-sdk-client`](../client/README.zh.md) 与 [Python SDK](../../../python/README.zh.md)（后者复现这些结构但不导入它们）。当你实现或调试协议某一端时使用本包：分帧规则、方法名、载荷类型与错误语义都在这里。它是纯库——无插件、无配置、无注册。

## 目录

- [使用本包](#use-this-package)
- [理解实现](#understand-the-implementation)
- [进一步探索](#further-exploration)
- [模型体验](#model-experience)
- [已知限制与延期工作](#known-limitations-and-deferred-work)
- [开发备注](#dev-note)

-----

<a id="use-this-package"></a>
## 使用本包

当你构建或调试 SDK 协议端——服务插件、客户端库或使用该协议的自定义工具——时使用本包。它为你提供一个在调用方持有的字节流上承载 JSON-RPC 2.0 的传输，以及每个 SDK 方法与通知的类型化结构。

### 分帧与传输

在你拥有的字节流上，每个 `\n` 结尾的行承载一条 JSON-RPC 2.0 消息。同时带 `id` 与 `method` 的帧是请求，仅 `id` 是响应，仅 `method` 是通知；格式错误的行会被忽略。没有注册处理器的请求应答 `-32601`，处理器失败应答 `-32603`，错误响应会以 `JsonRpcResponseError` 拒绝挂起的请求，并保留协议中的 `code` 与可选 `data`。`start()` 挂接流监听器，`close()` 移除监听器并拒绝挂起请求，但不销毁流。

### SDK 方法

两个协议端共享同一套方法：七个客户端到服务端请求与四个服务端到客户端通知。

| 方向 | 方法 | 载荷类型 |
|---|---|---|
| client→server | `initialize` | `InitializeParams` → `InitializeResult` |
| client→server | `session/prompt` | `SessionPromptParams` → `SessionPromptResult`（持久入队回执） |
| client→server | `session/list` | `SessionListParams` → `SessionListResult`（最新在前的会话描述符） |
| client→server | `session/history` | `SessionHistoryParams` → `SessionHistoryResult`（原始日志事件） |
| client→server | `session/resume` | `SessionResumeParams` → `SessionResumeResult` |
| client→server | `session/rename` | `SessionRenameParams` → `SessionRenameResult` |
| client→server | `session/abort` | `SessionAbortParams` → `SessionAbortResult` |
| client→server | `session/archive` | `SessionArchiveParams` → `SessionArchiveResult` |
| client→server | `session/unarchive` | `SessionArchiveParams` → `SessionArchiveResult` |
| client→server | `shutdown` | 无参数 → `{}` |
| server→client | `session.event` | `SessionEventNotification`（运行时内每个会话，不过滤） |
| server→client | `session.status` | `SessionStatusNotification`（整个 agent（智能体）的 `running`/`idle` 转换） |
| server→client | `subagent.started` | `SubagentStartedNotification` |
| server→client | `subagent.finished` | `SubagentFinishedNotification`（仅进程内运行） |

`HarnessSdkRequestMap` 与 `HarnessSdkNotificationMap` 按方法名索引这些结构；包根与传输一起导出它们。

### 载荷语义

`session/list` 与 `session/history` 读取部署的 `sessionQuery` 服务（`sdk-minimal` 这类部署不挂载它，会收到带说明的错误）；`session/list` 最新在前、按精确 `cwd` 过滤、用 `limit` 截断，并给出每条记录的 `live`/`persisted`/`archived` 标记与折叠后的 `session/title`。`session/history` 以 `session.event` 同一套事件词汇返回原始日志——传 `limit` 只取最新事件，读 `truncated` 得知更早的已被丢弃。`session/resume` 通过 `agents.resume` 让已落盘的会话重新变活，之后的 `session/prompt` 便接着它的历史走而不是新建会话；它绝不新建（未知 id 直接拒绝），对「记录里的 `cwd` 与 `initialize` 的不一致」的会话会先 dispose 再拒绝（用 `realpath` 比较），而不是把它的工具跑在历史描述不到的地方；对运行时内已经活跃的会话则是幂等 no-op。续接不会把历史重放给客户端：要看历史请调 `session/history`。`session/rename` 通过部署的 `sessionTitle` 服务追加一条用户自有的 `session/title` 事件，因此被接受的标题是每个前端都会读到的持久会话状态，而非客户端本地别名；它只接受本运行时内活跃的会话（已落盘的会话必须先续接，因为服务是往活跃实例上追加），并拒绝规范化后为空白的标题。`session/abort` 以用户取消的因果中止一个会话正在跑的回合——回合以 `turn/end` 的 `aborted` 结局收束（事件流上明着可见），下一条提示词开启新回合；只有本运行时内活跃的会话才有回合可中止（绝不续接、绝不新建），对空闲会话调用是幂等 no-op（报 `aborted: false`）。`session/archive` 与 `session/unarchive` 把一个会话加进／移出部署的 `workspaceRegistry` 全局归档集——那是一个与 web 前端共享的纯可见性标记，归档的 id 在两个前端的分组界面里都会隐藏起来，而会话日志与工作区记账绝不动；两次调用都幂等，未知 id 直接拒绝（绝不新建），并且要求部署挂载 `workspaceRegistry` 服务（`dsh-sdk-app` 挂 `@deepseek-ai/dsh-workspace`；`sdk-minimal` 不挂，会收到带说明的错误）。`session/list` 的 `archived` 标记是尽力而为：没有注册表时一律读作未归档。

`SessionPromptResult.messageId` 标识已排队的用户消息；它不标识后续的助手消息、轮次结束或提示词结果。`SdkPromptContentBlock` 接受普通持久内容以及 `SdkEncodedImageBlock { type: "image", data, mimeType }`；服务器在入队前把编码图像转换为持久引用。`InitializeParams.reasoningEffort` 是所选提供方／模型路由可选的非空适配器自有标识符；省略时保留该模型的默认值。`InitializeParams.maxTokens` 是可选的正安全整数，用于限制 SDK 创建的 agent 及其进程内后代的每次对话模型输出；省略时应用所选适配器的确切模型默认值。服务器会在初始化期间解析确切路由，并在握手成功前拒绝 `session/prompt`，因此缺少适配器、模型不可用或推理强度不受支持时，不会回退到构造期默认值。`SubagentFinishedNotification.lastAssistantMessage` 携带子 agent 最后一条非空 assistant 消息；若不存在这类消息，则携带其累积的 assistant 文本；子 agent 两种输出均未产生时，该字段缺省。`serverInfo.name` 的协议值固定为 `deepseek-harness-sdk-runtime`。通知载荷依赖 `SessionEvent`（`dsh-session`）、`ContentBlock`（`dsh-llm`）与 `SubagentStopReason`（`dsh-subagent`），因此会话词汇是协议格式约定的一部分。

-----

<a id="understand-the-implementation"></a>
## 理解实现

<details>
<summary>实现细节——点击展开</summary>

本节解释协议库背后的设计；可观察行为已在[使用本包](#use-this-package)中完整说明。

### 设计理念

本包采用一种职责分离设计：两个协议端共用一个按换行分帧的传输类，并以具名类型索引协议方法。包根是唯一的导入面——源模块不支持深层导入。它是没有插件、配置或注册的纯库；服务插件与客户端负责其周围的一切行为。

### 源码地图

| 文件 | 职责 |
|---|---|
| [`src/transport.ts`](src/transport.ts) | `JsonRpcLineTransport`：行分帧、请求/响应/通知分发、错误映射、挂起请求记账 |
| [`src/types.ts`](src/types.ts) | 具名请求/结果与通知载荷类型，按方法索引 |
| [`src/index.ts`](src/index.ts) | 消费方接口：传输与具名协议类型 |
| — | 不发布运行时不变式伴生入口；这是一个由传输类和类型声明组成的纯协议库，自身没有事件流或可变数据关系；两个协议端各自负责其协议行为。 |

### 帧分发

入站行逐条解析：带 `id` 与 `method` 的帧通过请求处理器应答（或应答 `-32601`），仅 `id` 的帧结算匹配的挂起请求（错误帧以 `JsonRpcResponseError` 拒绝它），仅 `method` 的帧交给通知处理器。`start()` 挂接输入监听器；`close()` 移除它们并在不销毁流的情况下失败所有挂起请求。

</details>

-----

<a id="further-exploration"></a>
## 进一步探索

当协议约定不够用时阅读以下页面。它们从服务插件进入客户端与可运行应用。

- [JSON-RPC 服务插件](../server/README.zh.md) — 通过 stdio 服务该协议的运行时插件。
- [TypeScript SDK 客户端](../client/README.zh.md) — 驱动该协议的客户端。
- [Python SDK](../../../python/README.zh.md) — 复现这些结构的 Python 对应实现。
- [SDK 应用组合包](../../bundle/sdk-app/README.zh.md) — 启动服务器的 `dsh --profile sdk` 应用。

-----

<a id="model-experience"></a>
## 模型体验

无，因为这是面向客户端的协议库；模型可见行为归对外服务入口后方的运行时插件所有。

#### KV Cache 影响

无；此包既不组装也不发送提供方请求。

## 已知限制与延期工作

<a id="known-limitations-and-deferred-work"></a>


这些限制说明协议未覆盖或未承诺的内容。它们是当前包约束，不是与其他协议格式的对比或任务积压。

- **无协议版本协商**——握手只携带 `serverInfo.version`（`0.0.1`，客户端不校验）；处于预发布阶段，无兼容承诺。
- **无取消与会话关闭方法**——客户端放弃轮次的方式是关闭运行时进程；见 [JSON-RPC 服务插件](../server/README.zh.md)。
- **server→client 请求是未使用的能力**——传输层支持，但服务器从不发送；Python SDK 的应答接口为未来审批流程预留。

<a id="dev-note"></a>
### 开发备注

<details>
<summary>维护者的工作上下文——点击展开</summary>

本开发备注是维护者的工作上下文，明确不具权威性——已交付的行为与限制见上文各节与代码。本协议的各个结构由 Python SDK 复现（而非导入），因此在这里更改方法、载荷或协议稳定值 `serverInfo.name` 时，必须在同一次变更中更新 Python 对侧与 TypeScript 客户端。没有记录其他未解决的开放设计问题。

</details>
