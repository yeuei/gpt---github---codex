# Agent 当前汇报

> 本文件是**即时现实快照**，由本地 Codex Agent 维护。
> 每次重要 push 时完整覆盖旧内容；不要追加历史记录。
> 必须假设 ChatGPT 看不到本地电脑，因此需要提供足够的最小上下文，但不得写 secrets。

## 当前身份

- PR: `#<N>`
- branch: `<branch>`
- commit: `<sha>`
- 本地仓库: `<path>`

## 当前任务

- 任务 ID: `T<N>.<M>`
- 状态: `[~] / [!] / [?] / [x]`

## 本次完成

1. `<做了什么>`
2. `<做了什么>`

## 代码变化

- `<path>`：`<修改目的>`
- `<path>`：`<修改目的>`

## 本地输入 / 运行环境

- dataset / input: `<...>`
- model: `<...>`
- config / manifest: `<...>`
- runtime: `<...>`

## 实际运行

命令：

```bash
<command>
```

run directory / artifact：

```text
<path or artifact ref>
```

## 当前结果

```text
processed = ...
accepted = ...
rejected = ...
failures = ...
```

## 当前问题 / BLOCKER

`<没有则写“无”>`

如果存在 blocker，必须说明：

- 具体在哪里发生；
- 错误或冲突是什么；
- 用到了哪些文件/代码；
- 已经尝试过什么；
- 为什么 Agent 无法在不改变正式口径的情况下自行解决。

## 已尝试

1. `<...>`
2. `<...>`

## 不受影响、仍可继续的任务

- `<任务>`
- `<任务>`

局部 blocker 不得默认阻塞整个 PR。

## 需要 ChatGPT 回答

只提出**最小问题**。
