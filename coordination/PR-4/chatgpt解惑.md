# ChatGPT 当前解答（ChatGPT Current Decision）

## 基于（Based On）

- 用户目标：让 Dashboard 从“只能看”升级为“可从节点发起修改请求”，最终形成 Dashboard → GPT → GitHub → Dashboard 闭环。
- PR：`#4`
- branch：`feature/dashboard-node-gpt-actions`
- stacked base：`refactor/local-git-dashboard`
- 当前规划 commit：`4929ec361c014462f57c68368c5fcf21b8d5c924`
- 当前技术真源：`docs/技术规范.md` §10

## 当前问题（Current Problem）

如何让用户在 Dashboard 中选中某个历史节点后，输入自然语言修改意见，并让已经配对的 GPT Web 对话继续修改当前开放 PR，同时不破坏 local-first、exact-SHA、审批分层和 Git 历史真实性。

## 结论（Decision）

采用一个独立的 Dashboard action 层，MVP 只实现 `request_gpt_revision`：

```text
节点右键
→ 用户输入修改意见
→ Trigger 本地 action API
→ active binding
→ 配对 GPT Web conversation
→ GPT 重新读取 GitHub 当前 HEAD
→ GPT 在当前开放 PR head 上继续修改
→ 用户显式 GitHub 刷新
→ Dashboard 发现并关联新 GPT event
```

Dashboard action 本身**不修改 Git/GitHub**，历史节点只作为上下文锚点。

默认不让 Codex CLI 充当 Dashboard → GPT Web 的 relay。现有 active binding 已经提供显式 Web conversation 路由；Codex Agent 继续在 GPT 通过 GitHub 下发后续本地工程任务时参与执行。

## 为什么这样设计（Why This Design）

1. 少一层中转，避免 Dashboard action、Codex session、Web conversation 三套状态互相漂移；
2. 复用现有 binding.v1，不制造新的隐式对话识别机制；
3. 保持 Dashboard local-first：action 发送不等于 GitHub refresh；
4. GPT 每次都重新读取 GitHub 当前 HEAD，所以用户选中的旧节点不会被错误当作“当前可编辑 commit”；
5. `Coordination-Caused-By: dashboard-action:<action_id>` 可以把用户人工意图与后续 GPT commit/event 关联起来，而不改写历史。

## Agent 已获得的权限（Agent Engineering Authority）

Agent 可以自行决定以下普通工程细节，无需再次询问 ChatGPT 或用户：

- SQLite 表名、索引、迁移代码的具体实现；
- action id 生成、幂等键、锁与 retry；
- API 内部函数/类拆分与 JSON 普通字段命名；
- 右键菜单、弹窗、按钮、快捷键和视觉样式；
- action 状态标记和因果连线的具体布局；
- 测试 fixture、mock、临时 Git repo 和日志格式。

只要保持 `docs/技术规范.md` §10 的冻结语义即可。

## 立即执行（Immediate Execution）

1. 实现 T4.2 UI，保持 `trigger/dashboard.html` 是唯一 renderer；
2. 实现 T4.3 action 持久化/API，并证明提交 action 不调用 `git fetch`、不写 Git；
3. 实现 T4.4 active binding 路由和 GPT wake message；
4. 实现 T4.5 `dashboard-action:<id>` 因果关联；
5. 实现 T4.6 自动测试并保持 PR #3 的回归测试通过；
6. 在用户本机条件满足后执行 T4.7 真实 E2E；
7. PR #3 merge 前不要把 PR #4 retarget 到 `main`；PR #3 merge 后执行 T4.8。

## 不需要做（Do Not Do）

- 不要让 action endpoint 直接提交 Git commit 或 GitHub 文件；
- 不要恢复后台自动 `git fetch`；
- 不要 force-push / amend 历史节点；
- 不要为了这一个动作重新引入第二套 Dashboard renderer；
- 不要把 Trigger 自动批准等同于 Codex shell/file approval；
- 不要依赖 PR #2 未合并代码才能开始实现；
- 不要自行扩张到自动 merge、任意 shell、重试 Agent 等新独立动作。

## 需要用户决定（User Decision Needed）

无。当前剩余内容都是已冻结目标下的工程实现；Agent 可直接推进。
