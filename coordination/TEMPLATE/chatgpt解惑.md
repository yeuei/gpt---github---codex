# ChatGPT 当前解答

> 本文件是 ChatGPT 对当前 PR 问题的**即时有效解答**。
> 每次新的核心决策产生时完整覆盖旧内容；不要把多轮聊天历史不断追加进来。

## 基于

- PR: `#<N>`
- branch: `<branch>`
- commit / head SHA: `<sha>`
- agent汇报对应 commit: `<sha>`

## 当前问题

`<用 1–5 句话准确复述 Agent 真正需要解决的问题>`

## 结论

`<直接给出当前有效结论>`

## 依据

- 当前 `任务.md`：`<相关任务>`
- 当前技术规范：`<章节/文件>`
- 当前 Agent 证据：`<关键事实>`
- 外部研究（如有）：`<官方文档/论文/代码>`

区分项目内事实、外部事实与 ChatGPT 推理；不确定时明确写出。

## Agent 已获得的权限

明确哪些事情 Agent 可以自行决定，不需要继续等待，例如：manifest 路径 / schema、deterministic seed、row-id 编码、run 目录、logging / retry / resume 等普通工程实现。

## 立即执行

1. `<下一步>`
2. `<下一步>`
3. `<下一步>`

如果任务可以并行，要明确写出，不建立无意义串行门槛。

## 不需要做

- `<不需要重新 smoke / benchmark / audit 等>`

## 需要用户决定

`无`

如果确实需要用户，必须缩成最小决策：A / B，并给出推荐与原因。

## Blocker 处理硬规则

Agent 上报 blocker 后，ChatGPT 必须形成以下之一：

```text
A. 直接解决
B. 授权 Agent 自行决定
C. 请求用户做最小化决策
```
