# gpt---github---codex

> ChatGPT ↔ GitHub ↔ Codex Agent 项目交接仓库。
>
> 当前状态：**本仓库现在服务于“本地 GitHub ↔ ChatGPT Web 触发器”项目；实现分支正在建立，尚未创建真实任务 PR。**
> Git 保存历史，当前 HEAD 只表达当前有效协议与项目事实。

## 1. 本仓库的用途

本仓库用于让 ChatGPT、GitHub、本地 Codex Agent 与用户形成稳定的项目协调闭环：

```text
用户
  ↓
ChatGPT（规划与协调）
  ↓
GitHub：任务.md
  ↓
Codex Agent（本地执行）
  ↓
代码 / 实验 / 本地运行
  ↓
GitHub：agent汇报.md
  ↓
ChatGPT：chatgpt解惑.md
  ↓
Codex Agent继续执行
```

只有真正会改变科研、产品或正式实验核心口径的问题才升级给用户决策。

## 2. 第一次进入本仓库

无论是 ChatGPT 还是 Agent，都必须先读取 GitHub 当前 HEAD，不能用旧聊天、旧本地状态或旧 handoff 冒充当前事实。

推荐阅读顺序：

1. `README.md`
2. `docs/项目总览.md`
3. `docs/技术规范.md`
4. `docs/协作协议.md`
5. `coordination/README.md`
6. 若存在开放 PR，再读取对应 `coordination/PR-<N>/`

当前尚无真实 PR，因此 **不得预建虚构的 `coordination/PR-N/` 目录**。本次实现分支创建真实 PR 后，才实例化其三文件目录。

## 3. 三文件协议

每个真实且需要交接的开放 PR 原则上对应：

```text
coordination/PR-<N>/
├── 任务.md
├── agent汇报.md
└── chatgpt解惑.md
```

三者职责必须分开：

- `任务.md`：累积性任务合同。ChatGPT 创建/追加任务；Agent 只根据真实执行修改状态。
- `agent汇报.md`：Agent 当前现实快照。每次重要 push 完整覆盖，不累计聊天历史。
- `chatgpt解惑.md`：ChatGPT 当前有效决策/解答。新的核心决策产生时完整覆盖。

状态统一使用：

```text
[ ] TODO
[~] RUNNING
[x] DONE
[!] BLOCKED
[?] WAITING_USER
[-] SUPERSEDED
```

## 4. 决策权限

### Codex Agent 默认自行决定

普通工程实现，例如：路径、manifest 普通字段、row-id、deterministic seed、JSON/JSONL、logging、retry、resume、普通 bug、worker/batching 等。

### ChatGPT 默认可以决定

任务拆分、优先级、并行关系、blocker 是否真正阻塞主线、旧实现是否已被新规范覆盖、是否需要外部研究、Agent 是否可自行冻结工程细节。

### 必须请求用户决定

会显著改变项目正式定义的问题，例如：核心数据源/比例、正式训练数据规模、模型替换、reward、benchmark、tool cap、核心 prompt 目标、算法主路线或对外核心结论。

## 5. Blocker 处理硬规则

Agent 上报 blocker 后，ChatGPT 必须在当前轮形成以下之一：

```text
A. 直接解决
B. 授权 Agent 自行决定
C. 请求用户做最小化决策
```

禁止只回复“先解决 blocker 再继续”。局部 blocker 不得无理由阻塞其它无依赖任务。

## 6. Git 与文档原则

```text
Git history = 历史
HEAD = 当前有效事实
```

不要创建 `xxx_v2.md`、`xxx_final.md`、`xxx_latest.md` 保存同一规范的历史版本。当前规范直接原位更新；被替代文件应删除，历史由 Git commit 保存。

旧 runner、旧 prompt、旧 config、旧 smoke 可以借鉴，但不得因为过去跑通过就反向定义当前规范。

## 7. 新建真实 PR 时

真实 PR 创建后，把 `coordination/TEMPLATE/` 的三个模板实例化为：

```text
coordination/PR-<真实PR号>/
```

然后由 ChatGPT 写入可关闭的 PR 总任务与子任务。一个 PR 只服务一个可独立关闭的总目标。

PR 合并后，应从 HEAD 删除对应 `coordination/PR-<N>/`；过程历史由 Git 保留。

## 8. 当前项目事实

- 交接仓库：`yeuei/gpt---github---codex`
- 当前阶段：本地触发器 V1 实现与闭环模拟
- 具体项目目标：在用户本机持续运行一个确定性触发器，通过 GitHub 将 Local Codex Agent 与固定的 ChatGPT Web 对话连接起来。
- 真实任务 PR：无
- 当前技术规范：见 `docs/技术规范.md`
- 自动事件触发：由本机 Dashboard 控制；默认启用人工审批门，见 `coordination/coordination.yaml` 与 `trigger/README.md`

本次行动的唯一首个可关闭目标、验收顺序、当前未决项和完成定义见
[`docs/本次模拟交接任务.md`](docs/本次模拟交接任务.md)。后续 ChatGPT 或 Agent
进入本仓库时，必须先读该文件，再根据真实 PR 状态继续；不得把历史提交数量或旧聊天当作任务完成证据。

本次 V1 的验收范围：

1. 本地守护程序从 GitHub commit trailer 识别事件、去重并记录时间线；
2. Dashboard 能总暂停、独立开关两个方向，并逐条人工批准；
3. `agent → ChatGPT Web` 仅通过固定 `open-browser-use` CLI 流程操作用户选择的真实 Chrome profile；
4. `ChatGPT Web → agent` 只启动用户明确配置的本地命令；
5. 用真实 GitHub 分支/PR 和 remote ChatGPT Chat 模拟至少一次闭环；任何卡住或循环均记录并用于更新协议。

## 9. 权限故障

如果 ChatGPT/Agent 发现仓库不可见、只能读不能写，或无法创建 branch / PR / commit，不得声称操作成功。应检查 GitHub App 的 repository access，权限恢复后从当前 HEAD 继续核验。
