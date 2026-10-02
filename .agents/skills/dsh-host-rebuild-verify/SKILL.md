---
name: dsh-host-rebuild-verify
description: "How to make a source change in this repo actually take effect (host lib rebuild), why that disturbs a live dsh/harness process, and how to verify the SDK wire end-to-end against a real runtime with a mock LLM. Use when a change only exists in src/ and the runtime still behaves the old way, when rebuilding packages/*/lib, or when verifying SDK JSON-RPC methods."
version: 1.0.0
author: xi-dsh deployment notes
license: MIT
platforms: [linux, macos, android]
metadata:
  hermes:
    tags: [DeepSeek-Harness, Build, SDK, Wire-Protocol, Verification, Termux, Mock-LLM]
---

# 构建 + 验证：让源码改动真正生效并在线协议层验证

## 何时用这个 skill

- 改了 `packages/**/src` 但**运行时行为没变**（运行时读的是 `<pkg>/lib/index.js`，不是 `src/`）。
- 要重建 host 产物，且**有 dsh 进程正在跑**（重建会重写它的 `lib/`，见「副作用的机制」）。
- 要证明某个 SDK 方法（`session/*`、`initialize`、`shutdown`）在**真实 dsh 进程**上按契约工作，
  而且不想花 token —— 用 `scripts/wire-probe.py`（自起 mock LLM + 临时 `DSH_HOME`）。

以下正文最初是为「SDK 会话三方法」这次改动写的（现已扩到第四个 `session/rename`），
但 §4 的副作用机制、§5 的验证方法、§7 的排查表对**任何 host 侧改动**都适用；
那几个方法只是完整的实例。

这份文档给**执行者**（另一个 agent 或人）：在本仓库里把改动**编译进运行时产物**，然后跑线协议验证。
全部命令可直接复制。改动的设计已经定稿，**不要改语义**；发现设计问题就写进报告，别顺手改。

- 目的：证明这些 `session/*` 方法在**真实 dsh 进程**上按设计工作（不是只过单元测试）。
- 不做：不改协议语义（客户端镜像与中英文档要跟着一起更新，见 §9）。
- 成本：不花 token（用 mock LLM），不需要 API key。
- 预计耗时：单元测试 < 1 min；重建约 1–2 min；线协议验证 2–4 min。

---

## 1. 为什么必须重建（背景，读一次就够）

运行时解析包走 `package.json` 的 `exports` → `<pkg>/lib/index.js`（tsdown 打包产物），
**不是源码树 `src/`**。所以只改 `src/` 的话，真机看到的还是旧行为。

已经实测过（本文档的探针 `--mode old` 基线，见 §5）：改动写完但**未重建**时，真机调用回

```
-32603  unknown DeepSeek Harness SDK runtime method: session/list
```

也试过两条绕过重建的路，都无效：给 loader 加 `--patch` 覆盖行名（不生效）、把进程 cwd
设成仓库根让 tsx 走 tsconfig paths（加载器只认 `exports`）。结论：**只有重建这一条路**。

---

## 2. 前置检查（30 秒）

```bash
cd "$(git rev-parse --show-toplevel)"   # 仓库根
git status --porcelain            # 期望为空（lib/ 被 gitignore，不进这里）
node -v; pnpm -v
```

> 本 skill 首次成文时在一个专门分支（`feat/sdk-session-surface`，三笔提交）上做那三个方法；
> 那批提交早已合入 `master`，后续改动会另开分支（例如 `feat/sdk-session-rename`）。
> **别把具体分支名/提交号当硬前置**——工作树干净就能开工。

工作树不干净就先弄清是什么：`lib/` 已被 `.gitignore` 忽略，不会出现在 `git status` 里，
所以任何未跟踪文件都值得看一眼。

---

## 3. 步骤 1：单元测试（源码级，几十秒）

```bash
cd "$(git rev-parse --show-toplevel)"   # 仓库根
npx vitest run packages/sdk/server/tests/server.spec.ts
```

期望：全过（写这份时 39 条；每加一个方法都会涨）。

这步走 `src/`，**它过了不代表运行时更新了**——那是步骤 2 的事。别把这一步当验证完成。

---

## 4. 步骤 2：重建 host 产物

```bash
cd "$(git rev-parse --show-toplevel)"   # 仓库根
CI=true pnpm run build:lib:host
```

> **Termux 上必须带 `CI=true`。** pnpm 跑脚本前会做一次依赖状态检查（必要时先 `pnpm install`），
> 而根 `postinstall`（`scripts/install-lefthook.mjs`）在这台机器上必挂：lefthook 没有
> `lefthook-android-arm64` 二进制，`node_modules/.bin/lefthook install --force` 报
> `Cannot find module 'lefthook-android-arm64/bin/lefthook'`。`CI=true` 让该脚本提前返回
> （它和 lefthook 自己的 postinstall 都看 CI），install 与 `pnpm run` 才能过。
> 不想改环境变量时，也可以绕过 pnpm 直接跑那两条命令（`node .../tsc -b ...` 与 `tsdown`）。

它等于两步：

1. `node --max-old-space-size=4096 ./node_modules/typescript/bin/tsc -b tsconfig.host.json`
   —— 类型检查整个 host 聚合，并把各包 JS/声明发射到 `<pkg>/lib/types/`（增量，有 tsbuildinfo）
2. `tsdown --env.DSH_BUILD_FACE host`
   —— 把 `<pkg>/lib/types/index.js` 打成 `<pkg>/lib/index.js`（`exports` 指向的就是它），并跑 typert 插件

期望：退出码 0。打包这步会扫 workspace 里所有包（本机 host+client 约 1–2 分钟）；脚本自己要 4 GB 堆，
本机内存紧时别同时跑别的重活。

### ⚠️ 副作用：机制与正确姿势（2026-09-21 实测复盘）

**机制：一次"两代并存"。** Node 的 ESM 注册表按**解析后的路径**建键：文件被重写**不会**
影响进程里已经加载的模块实例，但此后**任何新发生的 import** 都拿到新字节。于是活着的
`dsh web` 会同时持有新旧两代模块，跨代调用就可能出现服务缺失、`instanceof` 失灵、
schema/字段不一致这类问题。**潜伏而非立即爆炸**——只有"之后又被 import"的模块才会中招。

**HMR 既不会救你也不会打你。** `dsh-base` 的组成里确实挂了 `@deepseek-ai/dsh-hmr`，但它的
配置是 `root: []`（组成里原话：*Profile configuration reloads by default; module roots are
opt-in*）——模块根**不监听**，只按精确路径监听 profile 配置/补丁文件。所以重写 `lib/**`
**不会**触发任何热重载；别指望 HMR 帮你刷新，也别担心它把会话打崩。

**内容差比"你的改动"宽得多。** 上一次构建到现在之间落地的**所有**提交会一次性生效。本例：
上次构建是 Sep 20 10:38，之后有 4 个提交，其中 termux 平台修复那笔改了 4 个
**web profile 真会加载**的运行时源文件（`app-boot` 的 profile 解析、`session-persistence-jsonl`、
`attachment-local` 的 store、native flock）。也就是说：重建不只是"让新方法上线"，它把积压的
未编译改动一起激活了。**而你自己的改动可能根本不在这条链上**：`packages/sdk/*` 只被
`sdk-app`/`sdk-minimal` 挂载，`web` profile（= base + web-app + injector）**从不加载它**。

**还有资源竞争。** `tsc` 要 `--max-old-space-size=4096`，再加 353 个文件的写入，在手机上
与活着的服务抢内存与 IO——这是"界面卡"最可能的直接来源。

**正确姿势：构建 → 立刻重启**，而不是"重启 → 构建"。
```bash
# 一致性自检：进程启动时间必须晚于产物时间
pgrep -f 'bin.ts web' | head -1        # 拿到 PID
ps -o etime= -p <PID>                  # 已运行时长 D；本机 toybox 的 ps 没有 lstart
stat -c '%y' packages/sdk/server/lib/index.js    # 产物时间 T
# 现在 - D 若晚于 T → 一致；否则这个进程跑的是旧代，重启它
```

- `lib/` 被 gitignore → 不进 `git status`、不影响提交。**回滚 = 改回源码再重建**，
  别用 git 去"恢复 lib"（恢复不了）。
- 顺带：`pnpm` 会在构建前后刷 `node_modules/.modules.yaml`、`.bin/*`、`.pnpm/lock.yaml`
  （本例 17:15:14）。这些是记账文件与 shim，**包目录本身没变**（workspace 链接时间戳不变），
  对活进程无影响。真正要小心的是 `lib/**`。

### 产物自检（必须做，10 秒）

```bash
cd "$(git rev-parse --show-toplevel)"   # 仓库根
grep -c "session/list"   packages/sdk/server/lib/index.js      # 期望 >= 1
grep -c "session/resume" packages/sdk/server/lib/index.js      # 期望 >= 1
grep -c "session/rename" packages/sdk/server/lib/index.js      # 期望 >= 1
grep -c "session/list"   packages/sdk/protocol/lib/types/types.d.ts   # 期望 >= 1
grep -c "session/rename" packages/sdk/protocol/lib/types/types.d.ts   # 期望 >= 1
# 改了客户端镜像再查方法名（如 renameSession）在 packages/sdk/client/lib/index.js 里 >= 1
```

> 查 `.d.ts` 的那几条 grep 看的是声明文件，不是 `protocol/lib/index.js`：协议包里能被运行时加载的只剩
> transport（`lib/index.js` 的导出是 `JsonRpcLineTransport` / `JsonRpcResponseError`），
> 请求表 `HarnessSdkRequestMap` 是**类型**，编译后擦除，名字只留在声明文件里。
> **`protocol/lib/index.js` grep 出 0 是正常的，不是构建失败。**

任何一个是 0：说明 tsdown 那步没把新代码带进去（或只跑了 tsc）。先看
`pnpm run build:lib:host` 的完整输出有没有报错、有没有中途退出——**贴错误，别继续往下走**。

---

## 5. 步骤 3：线协议验证（这步才是证据）

探针在**本 skill 的 `scripts/wire-probe.py`**（用 `cd` 到本 skill 目录后跑）：它自己起 mock LLM、自建临时 `DSH_HOME`、
跑真 `dsh --profile sdk` 子进程、跑完自动清理（包括删掉临时复制进仓库的 mock 文件）。

```bash
python3 .agents/skills/dsh-host-rebuild-verify/scripts/wire-probe.py --mode new
```

- **通过判据：退出码 0，末尾 `== 31/31 通过（模式 new）==`，且没有 FAIL 清单。**
- 失败时加 `--keep` 保留临时目录（里面有子进程 stderr）；探针也会把 stderr 尾部带进失败信息。
- 想复现"重建前基线"：`python3 .agents/skills/dsh-host-rebuild-verify/scripts/wire-probe.py --mode old`
  （应全过；old 模式下每个 `session/*` 探针都应回 `unknown ... method`）。**这个基线已经实测通过**，说明脚本本身没问题；
  如果 `--mode old` 都不通过，先怀疑环境（dsh 启动器指向哪、checkout 是哪份），别怀疑设计。

### 探针逐步在验什么

| 阶段 | 在验什么 |
|---|---|
| A `initialize` + `session/prompt` + 等 `session.status: idle` | 基线：真运行时、真回合、mock 回复真的到达 |
| A `session/list` | 方法存在；本会话在列表里且字段齐全（`createdAt`/`live`/`persisted`）；`cwd` 过滤生效 |
| A `session/history` | 方法存在；事件非空、不截断；能在事件里找到第一轮文本；`limit` 取最新 N 条并标 `truncated`；返回 `session` 身份 |
| A `session/rename` | 方法存在；返回规范化后的标题；历史里出现 `user` 来源的 `session/title` 事件；空白标题被拒；**未 prompt 过的新 id 也能改（服务端先创建再改名）** |
| A `session/archive`/`session/unarchive` | 方法存在；归档回 `archived: true` 并让 `session/list` 带 `archived: true` 标志；重复归档幂等；未知 id 被拒（**绝不偷偷新建**）；unarchive 找回后标志翻回 `false` |
| A `session/abort` | 方法存在；空闲会话 abort 是 no-op（`aborted: false`）；不活跃会话被拒（**不 resume 不新建**）。真"中断跑着的回合"在 xi E2E 用慢 mock 验（`turn/end` reason `aborted`） |
| A 关闭 → B 新进程 `session/resume` | 跨进程续接：`resumed=true`；重复调用幂等（`resumed=false`） |
| B 追问一轮 + 读 mock 落盘的请求体 | **续接不是伪造**：第二轮模型请求里真的带着第一轮的 `MARK-ONE` |
| B `session/history` | 续接后的历史含两轮（`MARK-ONE` 与 `MARK-TWO` 都在） |
| C 在**别的目录** `initialize` 后 resume | 换目录被拒绝（错误信息含 `was created in`）——不把历史描述不到的地方当执行目录 |
| C 其余负例 | 未知 id resume 报错（**绝不偷偷新建**）；空 id / 未知 id 的 history 报错；`limit=0` 报错；C 进程没有偷偷建会话 |

### 全过时应该看到的 40 条（逐字）

```
A initialize
A session/prompt 收下消息
A 回合真的跑完（模型回复到达）
A session/list 可用
A session/list 含本会话
A 列表条目字段齐全
A session/list 按 cwd 过滤
A session/history 可用
A 历史里有事件且不截断
A 历史里能找到第一轮文本
A 历史返回会话身份
A session/history limit 生效并标 truncated
A session/rename 可用
A rename 回被接受的规范化标题
A rename 落成用户来源的 title 事件
A rename 空白标题被拒绝
A 未 prompt 过的新 id rename 也能成（先创建再改名）
A 新会话历史里有 title 事件
B session/resume 可用
B resume 报告 resumed=true
B 重复 resume 幂等（resumed=false）
B 续接后回合跑完
B 续接真的带着上一轮上下文（模型请求里有 MARK-ONE）
B 续接后的历史含两轮
C 换目录 resume 被拒绝（不把工具跑错地方）
C 未知会话 resume 报错（绝不偷偷新建）
C session/history 空 id 被拒绝
C session/history 未知 id 报错
C session/list limit=0 被拒绝
C 换目录 rename 被拒（改名也先 resume，cwd 检查拦住）
C 没有偷偷新建会话（列表里只有 A 建的）
A session/archive 可用
A archive 回 archived=true 与本会话 id
A 归档后 session/list 带 archived=true 标志
A 重复归档幂等（仍 archived=true）
A 未知 id 归档被拒（不偷偷新建）
A session/unarchive 找回（archived=false）
A 找回后 session/list 带 archived=false 标志
A session/abort 可用
A 空闲会话 abort 是 no-op（aborted=false）
A 不活跃会话 abort 被拒（不 resume 不新建）
```

---

## 6. 收尾检查

```bash
git status --porcelain        # 期望为空
ls apps/cli/xi-probe-mock.mts    # 期望 No such file
pgrep -af "xi-probe-mock|--profile sdk"                   # 期望没有残留
# 一致性自检（见 §4）：确认 dsh web 进程启动晚于产物时间，否则重启它
ps -o etime= -p "$(pgrep -f 'bin.ts web' | head -1)"
stat -c '%y' packages/sdk/server/lib/index.js
```

探针在 `finally` 里删临时 mock、杀 mock/dsh 子进程、删临时树（`--keep` 时保留）。
若被强杀留下文件，手动删。

**报告请附**：单元测试计数、重建耗时与退出码、三条 grep 的计数、探针完整输出、
以及 `git status --porcelain` 为空。

---

## 7. 失败排查表

| 症状 | 原因 | 处理 |
|---|---|---|
| 这些 `session/*` 方法仍回 `unknown ... method` | 没重建 / 只跑了 tsc / 运行时加载的是另一份副本 | 重跑 §4 并做产物自检；`cat "$(command -v dsh)"` 确认 `DSH_REPO` 指向这个 checkout |
| `session/list` 回 "requires the sessionQuery service" | 用了不挂 `sessionQuery` 的 profile（如 `sdk-minimal`） | 用 `--profile sdk`（= `dsh-base` + `dsh-sdk-app`，base 挂 `session-query-sqlite`） |
| resume 回 `was created in ... but this runtime is initialized for ...` | cwd 与日志 header 不一致 | **设计如此**。探针 A/B 用同一目录；C 上这条是**预期拒绝** |
| resume 回 `session "..." already exists` | 撞上旧行为：跑的还是旧产物 | 重跑 §4 |
| mock 起不来 / `script_exhausted` | tsx 缺失或 mock 没开 `repeatLast` | 确认 `<repo>/node_modules/tsx/dist/esm/index.mjs` 存在 |
| tsc 被 OOM kill | 内存不够 | 关掉其它重进程重试；**不要**删 `--max-old-space-size=4096` |
| 探针卡在 initialize 超时 | dsh 启动失败（依赖/权限/profile） | 加 `--keep`，看临时树里子进程 stderr 尾部 |
| 单元测试挂了 | 源码问题 | 保留输出，先别重建 |

---

## 8. 判据（什么算"过"）

1. 单元：`Tests` 全过（server.spec 现 42 条左右，每加一个方法都会涨）
2. 产物：`server/lib/index.js` 里 `session/list`/`session/resume`/`session/rename`/`session/archive`/`session/unarchive` 各 ≥1，
   且 `protocol/lib/types/types.d.ts` 里有 `'session/list'`（协议是类型，只在 .d.ts 里）
3. 线协议：`--mode new` 40/40、退出码 0，且这几条必须在里面：
   - **`A 归档后 session/list 带 archived=true 标志`** 与 **`A 找回后 session/list 带 archived=false 标志`** ← 归档是 registry 共享集合而非客户端别名的硬证据
   - `A session/list 含本会话`
   - `A session/history limit 生效并标 truncated`
   - `B resume 报告 resumed=true` 与 `B 重复 resume 幂等（resumed=false）`
   - **`B 续接真的带着上一轮上下文（模型请求里有 MARK-ONE）`** ← 续接非伪造的硬证据
   - `C 换目录 resume 被拒绝（不把工具跑错地方）`
   - `A rename 落成用户来源的 title 事件` ← 改名非客户端别名的硬证据
   - `A 未 prompt 过的新 id rename 也能成（先创建再改名）` ← 新前端只有本地会话句柄时的回归

任一条不过：**保留原始输出并停下报告**，不要"修测试"、不要顺手改语义去迁就现象。

---

## 9. 本类改动的边界（已更新）

- **客户端镜像要跟着一起做**：`packages/sdk/client`（TS）与 `python/sdk`（Python）都镜像新方法，
  各自扩脚本化替身与用例；改了协议 README 就用 `verify-translation-pairing --write` 重录中英配对。
  （`session/list`/`history`/`resume`/`rename` 四方法都已镜像过，后续加方法照做。）
- xi 侧接入也已完成（`--resume [ID]` 真续接、`/switch` 接磁盘会话、并发多会话、`/rename`/`/title`）。
- **仍然不在范围内**：给上游提 PR（本仓库是 termux 部署线，不跟上游合并）。
- **不推送**任何分支之前先问：本仓库的推送会触发上游那整套 CI（见 skill
  `github-actions-cost`；本仓库公开后标准 runner 免费，但 heavy 矩阵仍会占队列）。

## 10. 语义速查（写报告时别搞错）

| 方法 | 参数 | 返回 | 关键语义 |
|---|---|---|---|
| `session/list` | `{cwd?, limit?}` | `{sessions:[{sessionId, cwd?, createdAt, title?, live, persisted}]}` | 最新在前；`cwd` 为精确匹配过滤；`limit` 需正整数 |
| `session/history` | `{sessionId, limit?}` | `{session: SessionDescriptor, events, truncated}` | `events` 与 `session.event` 通知同一套词汇；`limit` 取**最新 N 条**并置 `truncated`；未知 id 报错 |
| `session/resume` | `{sessionId}` | `{sessionId, resumed}` | **绝不新建**（未知 id 报错）；已 live 则 `resumed=false`；cwd 不匹配先 dispose 再拒绝；**不重放历史**（要历史自己调 `session/history`） |
| `session/rename` | `{sessionId, title}` | `{sessionId, title}` | 不活跃时先把它变活跃：已落盘的走 `session/resume`（cwd 校验照旧），未知 id 按 `session/prompt` 的方式惰性创建；然后通过 `sessionTitle` 服务追加 `user` 来源的 `session/title` 事件；空白标题报 `must contain visible characters` |
| `session/archive` | `{sessionId}` | `{sessionId, archived:true}` | 进 `workspaceRegistry` 全局归档集：纯可见性（分组界面隐藏），日志与工作区记账不动；幂等；会话必须存在（未知 id 报 `no such session`） |
| `session/unarchive` | `{sessionId}` | `{sessionId, archived:false}` | 移出归档集；幂等（不在集里也成功） |
| `session/abort` | `{sessionId}` | `{sessionId, aborted}` | 用户取消当前回合（`turn/end` reason `aborted`）；只对活跃会话（不 resume 不新建）；空闲会话幂等 no-op（`aborted:false`） |

八个方法都要求先 `initialize`（否则报 `SDK server is not initialized`）。
`session/archive` / `session/unarchive` 依赖 `workspaceRegistry`——`dsh-sdk-app` 用 `- insert:` 挂
`@deepseek-ai/dsh-workspace`（**顶层 `- id:` 行是 patch 语义，新挂载必须进 `- insert:` 段**），
`sdk-minimal` 不挂；`session/list` 的 `archived` 标志则 best-effort（没注册表就全是未归档）。
`session/list` / `session/history` 依赖部署挂载 `sessionQuery`——`dsh-base` 提供，`sdk-minimal` 不提供。
`session/rename` 依赖 `sessionTitle`，同样由 `dsh-base` 提供（不活跃时会先 resume 或惰性创建）。
