# gpt---github---codex

> ChatGPT ↔ GitHub ↔ Local Agent 的交接实例与本机 Dashboard/runtime。
>
> Git 保存历史；当前 HEAD 只表达当前有效协议、代码和项目事实。

## 1. 本仓库服务什么

本实例用于运行一条可审计的协作链路：

```text
用户
  ↓
ChatGPT（规划 / GitHub 协调）
  ↓
GitHub 交接仓库
  ↓
Local Agent（本地执行）
  ↓
本地 Git / runtime / SQLite 证据
  ↓
Dashboard
```

Dashboard 是**本地观察、审批与受控人工操作界面**，不是第四套任务真源。正式任务仍由真实 PR 的
`coordination/PR-<N>/` 三文件表达，历史状态由 Git commit 保存。

## 2. 协议区

### 2.1 事实优先级

- “现在应该做什么”：用户最新指令 > 当前 `任务.md` > 当前 `chatgpt解惑.md` > 当前规范。
- “实际上做到哪里”：本地/远端真实 commit、runtime 证据 > 最新 `agent汇报.md` > 任务状态。
- 不用旧聊天、旧 renderer、旧 runner 或旧任务文件冒充当前事实。

### 2.2 PR 与三文件

只有真实且开放、需要交接的 PR 才在 HEAD 保留：

```text
coordination/PR-<真实编号>/
├── 任务.md          # 累积任务合同
├── agent汇报.md      # Agent 当前现实快照
└── chatgpt解惑.md    # ChatGPT 当前决策快照
```

PR 合并后从 HEAD 删除对应目录；历史仍可通过 Git commit 读取。一个 PR 只服务一个可独立关闭的总目标。

### 2.3 Dashboard 数据源

默认路径完全本地化：

```text
本地 Git clone / refs / commit history
        ↓
Trigger 本地扫描
        ↓
SQLite 事件与审批/投递审计
        ↓
Dashboard
```

后台轮询**不访问 GitHub 网络**。只有用户点击 **“从 GitHub 刷新”** 或显式执行
`--refresh-once` 时才运行 `git fetch`。刷新失败时，当前事件/审批/投递状态保持不变，界面显示准确错误。

### 2.4 历史任务快照

每个事件节点只允许按自身 commit SHA 读取：

```text
git show <event_sha>:coordination/PR-<N>/任务.md
```

如果该 commit 不存在对应文件，显示“历史任务快照不可用”。禁止：

- 按事件时间寻找最近任务提交；
- 用当前工作树 `任务.md` 代替历史节点；
- 根据最终完成状态回填早期节点。

### 2.5 三类状态严格分离

Dashboard 分别显示：

1. **Trigger 审批状态**：等待审批 / 人工批准 / 自动批准 / 无需审批 / 旧记录未知；
2. **投递/执行状态**：未投递 / 已填草稿 / 已发送 / 已启动 Agent / 需人工处理 / 已跳过；
3. **GitHub PR 状态**：来自显式 GitHub 刷新得到的缓存；未同步时明确显示未知。

任何一类状态都不得反推另一类。旧 SQLite 记录若无法可靠拆分，标为 `legacy_unknown`，不猜测。

### 2.6 两层审批不可混用

- Trigger 审批：是否路由一个 GitHub 协作事件；
- Codex app-server 审批：是否允许具体 shell / file 操作。

Trigger 的自动审批模式不会自动授予 Codex 命令或文件权限。

### 2.7 Binding 配对

配对遵守 `binding.v1`：`pending → claimed → active`，并允许 `expired / revoked / conflict`。
本机 `/pair` 通过 `/api/bindings/invite` 生成真实短期一次性 token，复制链接只把 token 放入 URL fragment。
ChatGPT 无法访问 localhost 时不得编造 token 或链接。

### 2.8 Dashboard 节点人工操作

Dashboard 节点上的人工 action 只表达用户意图，不直接改写 Git/GitHub。当前规划中的 `request_gpt_revision` 通过 active binding 投递到配对 GPT Web 对话；GPT 必须重新读取 GitHub 当前 HEAD 后行动。历史节点只作上下文，用户 action 不自动 fetch、不自动 merge、不授予 Codex shell/file 权限。

## 3. 当前项目事实

本节只维护当前有效事实，不保存版本历史。

- 仓库：`yeuei/gpt---github---codex`
- 默认分支：`main`
- PR #1：已合并；merge commit `1323dbde24666ed3da8911a9b90a29cf210283be`
- PR #2：真实开放、独立的配对链接工作；不是 PR #4 的实现真源
- PR #3：真实开放；`本地优先 Dashboard：按提交 SHA 恢复任务并分离状态（Local-first Dashboard: Restore Tasks by Commit SHA and Separate States）`
- PR #3 branch：`refactor/local-git-dashboard`；当前 head `d6a677d820276d35786db284768e035b36e13e56`
- PR #3 最终 GitHub Actions run `34077426736`：`success`
- PR #4：真实开放；`Dashboard 节点操作：从选中节点向 GPT 发起修改请求（Dashboard Node Actions: Request GPT Revisions from Selected Nodes）`
- PR #4 branch：`feature/dashboard-node-gpt-actions`
- PR #4 当前 stacked base：`refactor/local-git-dashboard`；PR #3 合并后应 retarget 到 `main`
- PR #4 规划 commit：`4929ec361c014462f57c68368c5fcf21b8d5c924`
- PR #4 当前任务：实现“节点右键 → 用户修改意见 → active binding → GPT → GitHub → 显式刷新 → 因果新节点”的闭环
- Dashboard runtime 真源：`trigger/`
- Dashboard 默认数据源：本地 Git clone + 本地 SQLite
- 显式网络入口：Dashboard “从 GitHub 刷新” / `python trigger/trigger.py --refresh-once`
- PR #4 尚无 Local Agent 本机实现/E2E 证据，不得把规划 commit 或 CI 冒充成功实现

## 4. 阅读路径

1. 本 README；
2. `docs/项目总览.md`；
3. `docs/技术规范.md`；
4. `docs/协作协议.md`；
5. `trigger/README.md`；
6. 当前开放 PR 对应的 `coordination/PR-<N>/`（若存在）。

## 5. 运行入口

```bash
cd trigger
cp config.example.json config.local.json
python3 trigger.py
```

打开：

```text
http://127.0.0.1:8765/
```

配对入口：

```text
http://127.0.0.1:8765/pair
```

测试与可复现命令见 `trigger/README.md`。
